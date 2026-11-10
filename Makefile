IMAGE      ?= jdc-execution-engine
TAG        ?= latest
REGISTRY   ?= localhost
ENGINE     ?= podman
COMPOSE    ?= $(ENGINE) compose
FULL_IMAGE := $(REGISTRY)/$(IMAGE):$(TAG)

# OCI labels (org.opencontainers.image.version/revision/created)
VERSION    ?= $(patsubst v%,%,$(shell git describe --tags --always --dirty 2>/dev/null || echo dev))
REVISION   ?= $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
CREATED    ?= $(shell git log -1 --format=%cI 2>/dev/null || echo unknown)

TRIVY      ?= ghcr.io/aquasecurity/trivy:0.76.2
OTELCOL    ?= docker.io/otel/opentelemetry-collector-contrib:0.165.0
OUT        := $(CURDIR)/build
TRIVY_RUN   = mkdir -p $(OUT) && rm -f $(OUT)/image.tar && $(ENGINE) save -q -o $(OUT)/image.tar $(FULL_IMAGE) && \
	$(ENGINE) run --rm -v $(OUT):/work:Z -v trivy-cache:/root/.cache $(TRIVY) image --input /work/image.tar

BUILDER     = ansible-builder build --file execution-environment.yml --context context \
	--container-runtime $(ENGINE) --verbosity 2 \
	--build-arg IMAGE_VERSION=$(VERSION) --build-arg IMAGE_REVISION=$(REVISION) \
	--build-arg IMAGE_CREATED=$(CREATED)
RUN_TEST    = $(ENGINE) run --rm -v $(CURDIR)/tests:/runner/project:ro,Z

.PHONY: all help create build test e2e lint inspect lock scan sarif sbom push otel-up otel-down demo clean

all: build

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

create: ## Generate the build context (Containerfile) only
	ansible-builder create --file execution-environment.yml --context context

build: ## Build the EE image
	$(BUILDER) --tag $(FULL_IMAGE)

test: ## Smoke tests: playbook works, spans are produced, tracing is silent when unconfigured
	@echo "== tracing on (span file), with a project ansible.cfg that knows nothing about tracing"
	$(RUN_TEST) -e ANSIBLE_CONFIG=/runner/project/ansible.cfg \
		-e ANSIBLE_OPENTELEMETRY_STORE_SPANS_IN_FILE=/tmp/spans.json \
		$(FULL_IMAGE) sh -c 'ansible-playbook /runner/project/test.yml && \
			python3.12 /runner/project/verify_spans.py /tmp/spans.json'
	@echo "== no endpoint configured: tracing must be off and silent"
	$(RUN_TEST) $(FULL_IMAGE) sh -c 'ansible-playbook /runner/project/test.yml > /tmp/out 2>&1; rc=$$?; \
		cat /tmp/out; [ $$rc -eq 0 ] || exit $$rc; \
		if grep -qiE "opentelemetry|export|traces" /tmp/out; then echo "FAIL: tracing output without endpoint"; exit 1; fi; \
		echo "OK: tracing silent without endpoint"'

e2e: ## End-to-end: export to a real Collector over gRPC and HTTP, with TRACEPARENT
	ENGINE=$(ENGINE) tests/e2e.sh $(FULL_IMAGE) $(OTELCOL)

lint: ## yamllint + ansible-lint
	yamllint .
	ansible-lint

inspect: ## Show labels, installed collections and python packages
	$(ENGINE) image inspect $(FULL_IMAGE) --format '{{range $$k, $$v := .Labels}}{{$$k}}={{$$v}}{{"\n"}}{{end}}'
	$(ENGINE) run --rm $(FULL_IMAGE) ansible-galaxy collection list
	$(ENGINE) run --rm $(FULL_IMAGE) python3.12 -m pip list

lock: ## Re-resolve Python dependencies and rewrite files/constraints.txt
	$(BUILDER) --tag localhost/$(IMAGE):lock --build-arg PIP_CONSTRAINT=
	{ grep '^#' files/constraints.txt; \
	  $(ENGINE) run --rm localhost/$(IMAGE):lock python3.12 -m pip freeze; } > files/constraints.txt.new
	mv files/constraints.txt.new files/constraints.txt
	-$(ENGINE) rmi localhost/$(IMAGE):lock
	git diff --stat files/constraints.txt

scan: ## Vulnerability scan; fails on fixable CRITICAL findings
	$(TRIVY_RUN) --scanners vuln --severity CRITICAL --ignore-unfixed --exit-code 1

sarif: ## Vulnerability report (fixable HIGH/CRITICAL) as build/trivy.sarif
	$(TRIVY_RUN) --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --format sarif --output /work/trivy.sarif

sbom: ## CycloneDX SBOM as build/sbom.cdx.json
	$(TRIVY_RUN) --format cyclonedx --output /work/sbom.cdx.json

push: ## Push the image to $(REGISTRY); writes the digest to build/digest
	mkdir -p $(OUT)
	$(ENGINE) push --digestfile $(OUT)/digest $(FULL_IMAGE)

otel-up: ## Start a local OpenTelemetry Collector + Jaeger (UI on :16686)
	$(COMPOSE) -f otel/compose.yml up -d

otel-down: ## Stop the local OpenTelemetry stack
	$(COMPOSE) -f otel/compose.yml down

demo: ## Run tests/test.yml in the EE, exporting traces to the local collector
	$(RUN_TEST) --network jdc-otel -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
		$(FULL_IMAGE) ansible-playbook /runner/project/test.yml
	@echo "Traces: http://localhost:16686/search?service=ansible"

clean: ## Remove build context, scan output and local image
	rm -rf context $(OUT)
	-$(ENGINE) rmi $(FULL_IMAGE)
