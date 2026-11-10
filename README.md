# jdc-execution-engine

[![build](https://github.com/jvzantvoort/jdc-execution-engine/actions/workflows/build.yml/badge.svg?branch=main)](https://github.com/jvzantvoort/jdc-execution-engine/actions/workflows/build.yml)
[![Image](https://img.shields.io/github/v/release/jvzantvoort/jdc-execution-engine?label=ghcr.io%2Fjvzantvoort%2Fjdc-execution-engine&logo=docker&logoColor=white)](https://github.com/jvzantvoort/jdc-execution-engine/pkgs/container/jdc-execution-engine)
[![License](https://img.shields.io/github/license/jvzantvoort/jdc-execution-engine)](LICENSE)

Ansible Execution Environment (EE) with **OpenTelemetry tracing built in**.
Point it at an OTLP endpoint and every playbook run produces a distributed
trace — one span per task per host — in any OpenTelemetry Collector, Jaeger,
Tempo, Elastic APM, Honeycomb, etc. Without an endpoint it behaves like a
plain EE.

Built with [ansible-builder](https://ansible.readthedocs.io/projects/builder/)
(schema v3) on top of the
[`community.general.opentelemetry`](https://docs.ansible.com/ansible/latest/collections/community/general/opentelemetry_callback.html)
callback plugin.

| Component      | Version                                                |
|----------------|--------------------------------------------------------|
| Base image     | UBI 9.8 (`registry.access.redhat.com/ubi9/ubi`, pinned by digest) |
| Python         | 3.12 (`/usr/bin/python3.12`)                           |
| ansible-core   | 2.20.11                                                |
| ansible-runner | 2.4.3                                                  |
| Collections    | ansible.posix 2.2.2, ansible.utils 6.1.1, community.crypto 3.5.1, community.general 14.0.1 |
| OpenTelemetry  | SDK 1.45.2, OTLP exporters (gRPC + HTTP/protobuf)      |

```bash
podman pull ghcr.io/jvzantvoort/jdc-execution-engine:1.2.1
```

## Quick start

```bash
make build         # build localhost/jdc-execution-engine:latest
make test          # smoke tests (spans produced; silent when unconfigured)
make e2e           # export to a real Collector over gRPC and HTTP
make lint          # yamllint + ansible-lint

make otel-up       # local OTel Collector + Jaeger (podman compose)
make demo          # run tests/test.yml, exporting to the collector
xdg-open http://localhost:16686   # Jaeger UI, service "ansible"
make otel-down
```

## Configuring telemetry

Tracing switches on as soon as an OTLP endpoint is set:

```bash
podman run --rm \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.example.com:4317 \
  -e OTEL_SERVICE_NAME=ansible-prod \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment=prod \
  ghcr.io/jvzantvoort/jdc-execution-engine:1.2.1 ansible-playbook site.yml
```

The image ships a small wrapper callback, `jdc_opentelemetry`, that loads
automatically. It needs no `callbacks_enabled` entry, so tracing also works
when your project has its own `ansible.cfg`. It is installed in ansible-core's
built-in callback directory because ansible-runner (and so AWX and
ansible-navigator) overrides `ANSIBLE_CALLBACK_PLUGINS`, which hides plugins in
the usual `/usr/share/ansible/plugins/callback`. Don't enable `community.general.opentelemetry` as well, or every
run is traced twice.

| Variable | Image default | Purpose |
|----------|---------------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | – | Collector endpoint; setting it turns tracing on |
| `ANSIBLE_OPENTELEMETRY_ENABLED` | `auto` | `auto`: trace when an endpoint is set; `true`/`false`: force on/off |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | `grpc` | `grpc` (port 4317) or `http/protobuf` (port 4318) |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5` | Export timeout (seconds) |
| `OTEL_SERVICE_NAME` | `ansible` | `service.name` of the trace |
| `OTEL_EXPORTER_OTLP_HEADERS` | – | Auth headers, e.g. `authorization=Bearer <token>` |
| `OTEL_RESOURCE_ATTRIBUTES` | – | Extra resource attributes |
| `TRACEPARENT` | – | W3C trace context: attach the run to a parent trace (e.g. a CI pipeline) |
| `ANSIBLE_OPENTELEMETRY_HIDE_TASK_ARGUMENTS` | `true` | Keep task arguments out of spans |
| `ANSIBLE_OPENTELEMETRY_DISABLE_LOGS` | `true` | Keep task results (incl. facts) out of spans |
| `ANSIBLE_OPENTELEMETRY_STORE_SPANS_IN_FILE` | – | Write spans to a JSON file instead of exporting (also turns tracing on) |

`http://` endpoints use plain text, `https://` endpoints use TLS. Never bake
credentials into the image; pass `OTEL_EXPORTER_OTLP_HEADERS` at run time
(AWX/AAP: a credential type that injects it as an env var).

If the endpoint is set but unreachable, the playbook still succeeds; the
exporter logs `Failed to export traces ...` and adds a few seconds at the end.

### Privacy

By default spans contain task names, modules, hosts, status and timing, but
**not** task arguments or task results. To include results (useful for
debugging, but they can contain facts and secrets) set
`ANSIBLE_OPENTELEMETRY_DISABLE_LOGS=false`; tasks with `no_log: true` stay
censored.

### Linking runs to a parent trace (`TRACEPARENT`)

If whatever starts Ansible is traced too, pass its
[W3C trace context](https://www.w3.org/TR/trace-context/) in `TRACEPARENT`. The
playbook's root span then becomes a child of that span, so the Ansible tasks
show up inside the caller's trace instead of as a separate trace.

From a CI pipeline, [otel-cli](https://github.com/equinix-labs/otel-cli) can
open the parent span and hand `TRACEPARENT` to the command it runs:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.example.com:4317
otel-cli exec --service ci --name "deploy ${CI_COMMIT_SHORT_SHA:-local}" -- \
  podman run --rm -e TRACEPARENT -e OTEL_EXPORTER_OTLP_ENDPOINT \
    -v "$PWD:/runner/project:Z" \
    ghcr.io/jvzantvoort/jdc-execution-engine:1.2.1 \
    ansible-playbook /runner/project/site.yml
```

`-e TRACEPARENT` with no value passes the variable through from the calling
shell. The format is `00-<32 hex trace id>-<16 hex span id>-01`. `make e2e`
checks that the root span is parented this way.

**AWX:** environment variables are set globally or through credentials, not per
launch, so AWX can't pass a different `TRACEPARENT` to each job. Each AWX job
is its own trace, tagged with its job ID (see below).

### ansible-navigator

`ansible-navigator.yml` runs playbooks in the local image and exports to a
collector on the host (`host.containers.internal:4317`):

```bash
ansible-navigator run tests/test.yml
ansible-navigator run site.yml --senv OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317
```

### AWX / Ansible Automation Platform

Add the image as an Execution Environment and set the `OTEL_*` variables via
*Settings → Job settings → Extra Environment Variables*, or via a custom
credential type for the auth header.

Every AWX job's trace carries these resource attributes, taken from the
`JOB_ID` and `AWX_HOST` variables AWX sets for each job:

| Attribute | Example |
|-----------|---------|
| `awx.job.id` | `4242` |
| `awx.job.url` | `https://awx.example.com/#/jobs/playbook/4242/output` |

Search for `awx.job.id=4242` in Jaeger (or `resource.awx.job.id = "4242"` in
Tempo) to go from a job to its trace; `awx.job.url` links back. Values you set
yourself in `OTEL_RESOURCE_ATTRIBUTES` take precedence. Outside AWX (no
`JOB_ID`/`AWX_HOST`) nothing is added.

## Verifying the image

Images pushed from CI are signed with [cosign](https://github.com/sigstore/cosign)
(keyless, GitHub OIDC) and carry a signed CycloneDX SBOM attestation:

```bash
IMG=ghcr.io/jvzantvoort/jdc-execution-engine:1.2.1
ID='https://github.com/jvzantvoort/jdc-execution-engine/.github/workflows/build.yml@refs/tags/v1.2.1'
ISSUER=https://token.actions.githubusercontent.com

cosign verify --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER" "$IMG"
cosign verify-attestation --type cyclonedx \
  --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER" "$IMG"
```

The SBOM is also attached to each GitHub release. `podman inspect` shows
`org.opencontainers.image.version`, `.revision` (commit) and `.created`.

## Reproducible builds and updates

Everything that can be pinned is pinned:

- **Base image**: tag and digest in `execution-environment.yml`
- **Python packages**: every package, including transitive ones, locked in
  `files/constraints.txt` and applied to every `pip install` in the build
- **Collections**: exact versions in `requirements.yml`

[Renovate](https://docs.renovatebot.com/) (`renovate.json`) opens weekly PRs
for the base image digest, locked Python packages, collections, the Trivy
scanner, the test Collector and GitHub Actions. To re-resolve all Python dependencies at once:

```bash
make lock   # builds without the lock, rewrites files/constraints.txt
```

System packages (dnf) are not locked: the build runs `dnf upgrade`, and the
weekly CI rebuild picks up base-image security fixes.

## Tests and linting

```bash
make lint   # yamllint + ansible-lint (production profile)
make test   # spans without args/results; AWX-style run via ansible-runner; silent without endpoint
make e2e    # real Collector: OTLP over gRPC and HTTP, TRACEPARENT parenting, AWX job attributes
```

CI runs all three; the build job only starts when lint passes. The e2e
Collector log is kept as a workflow artifact when the test fails.

## Security scanning

```bash
make scan   # Trivy; fails on CRITICAL vulnerabilities that have a fix
make sarif  # build/trivy.sarif (fixable HIGH/CRITICAL)
make sbom   # build/sbom.cdx.json (CycloneDX)
```

CI runs all three on every build. Findings show up under the repo's
*Security → Code scanning* tab, and a fixable CRITICAL blocks the push.

## Layout

```
.
├── execution-environment.yml  # EE definition (pinned base, lock, OTel defaults, labels)
├── requirements.yml           # Ansible collections (exact versions)
├── requirements.txt           # Top-level Python packages
├── bindep.txt                 # System packages
├── files/
│   ├── ansible.cfg            # /etc/ansible/ansible.cfg
│   ├── constraints.txt        # Python lock file
│   └── callback/jdc_opentelemetry.py  # auto-enabling tracing callback
├── tests/
│   ├── test.yml               # Smoke-test playbook
│   ├── ansible.cfg            # Simulated project config
│   ├── e2e.sh                 # End-to-end export test
│   ├── e2e-collector.yaml     # Collector config for the e2e test
│   └── verify_spans.py        # Asserts a trace was produced without args/results
├── otel/                      # Local Collector + Jaeger (podman/docker compose)
├── renovate.json
├── .yamllint, .ansible-lint
├── ansible-navigator.yml
├── Makefile
└── .github/workflows/build.yml  # Lint, build, test, e2e, scan, push, sign
```

## CI

GitHub Actions lints, builds, tests and scans every push and PR, then pushes
to `ghcr.io/<owner>/jdc-execution-engine` and signs the image. It runs on
`ubuntu-24.04`, and every action is pinned to a commit hash (Renovate keeps
the hashes current):

- `main` → `:latest` and `:<sha>`
- tag `v1.2.3` → `:1.2.3` and `:<sha>`, SBOM attached to the release
- weekly rebuild to pick up base-image updates

## License

MIT, see [LICENSE](LICENSE), except
`files/callback/jdc_opentelemetry.py`, which extends GPL-3.0-or-later code from
community.general and is GPL-3.0-or-later. The image itself also contains
ansible-core and collections under their own (GPL) licenses.
