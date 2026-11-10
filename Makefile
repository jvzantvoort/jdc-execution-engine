IMAGE      ?= jdc-execution-engine
TAG        ?= latest
REGISTRY   ?= localhost
ENGINE     ?= podman
COMPOSE    ?= $(ENGINE) compose
FULL_IMAGE := $(REGISTRY)/$(IMAGE):$(TAG)

.PHONY: all help create build test inspect push otel-up otel-down demo clean

all: build

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

create: ## Generate the build context (Containerfile) only
	ansible-builder create --file execution-environment.yml --context context

build: ## Build the EE image
	ansible-builder build --file execution-environment.yml --context context \
		--container-runtime $(ENGINE) --tag $(FULL_IMAGE) --verbosity 2

test: ## Smoke test: run tests/test.yml and verify OpenTelemetry spans were produced
	$(ENGINE) run --rm -v $(CURDIR)/tests:/runner/project:ro,Z \
		-e ANSIBLE_OPENTELEMETRY_STORE_SPANS_IN_FILE=/tmp/spans.json \
		$(FULL_IMAGE) sh -c 'ansible-playbook /runner/project/test.yml && \
			python3.12 /runner/project/verify_spans.py /tmp/spans.json'

inspect: ## Show installed collections and python packages
	$(ENGINE) run --rm $(FULL_IMAGE) ansible-galaxy collection list
	$(ENGINE) run --rm $(FULL_IMAGE) python3.12 -m pip list

push: ## Push the image to $(REGISTRY)
	$(ENGINE) push $(FULL_IMAGE)

otel-up: ## Start a local OpenTelemetry Collector + Jaeger (UI on :16686)
	$(COMPOSE) -f otel/compose.yml up -d

otel-down: ## Stop the local OpenTelemetry stack
	$(COMPOSE) -f otel/compose.yml down

demo: ## Run tests/test.yml in the EE, exporting traces to the local collector
	$(ENGINE) run --rm --network jdc-otel -v $(CURDIR)/tests:/runner/project:ro,Z \
		-e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
		$(FULL_IMAGE) ansible-playbook /runner/project/test.yml
	@echo "Traces: http://localhost:16686/search?service=ansible"

clean: ## Remove build context and local image
	rm -rf context
	-$(ENGINE) rmi $(FULL_IMAGE)
