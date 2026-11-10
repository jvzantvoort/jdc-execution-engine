# jdc-execution-engine

[![Image](https://img.shields.io/github/v/release/jvzantvoort/jdc-execution-engine?label=ghcr.io%2Fjvzantvoort%2Fjdc-execution-engine&logo=docker&logoColor=white)](https://github.com/jvzantvoort/jdc-execution-engine/pkgs/container/jdc-execution-engine)

Ansible Execution Environment (EE) with **OpenTelemetry tracing enabled**.
Every playbook run produces a distributed trace — one span per task per host —
exported over OTLP to any OpenTelemetry Collector, Jaeger, Tempo, Elastic APM,
Honeycomb, etc.

Built with [ansible-builder](https://ansible.readthedocs.io/projects/builder/)
(schema v3) and the
[`community.general.opentelemetry`](https://docs.ansible.com/ansible/latest/collections/community/general/opentelemetry_callback.html)
callback plugin.

| Component      | Version / source                                     |
|----------------|------------------------------------------------------|
| Base image     | `registry.access.redhat.com/ubi9/ubi:latest`         |
| Python         | 3.12 (`/usr/bin/python3.12`)                         |
| ansible-core   | `>=2.18,<2.21`                                       |
| ansible-runner | latest                                               |
| Collections    | ansible.posix, ansible.utils, community.crypto, community.general `>=9` |
| OpenTelemetry  | opentelemetry-api, -sdk, -exporter-otlp (gRPC + HTTP) |

Published image: `ghcr.io/jvzantvoort/jdc-execution-engine:latest`

## Quick start

```bash
make build         # build localhost/jdc-execution-engine:latest
make test          # run tests/test.yml and verify spans were generated

make otel-up       # local OTel Collector + Jaeger (podman compose)
make demo          # run tests/test.yml, exporting to the collector
xdg-open http://localhost:16686   # Jaeger UI, service "ansible"
make otel-down
```

## Configuring telemetry

Tracing is on by default. The callback is enabled in `/etc/ansible/ansible.cfg`
and configured through standard OpenTelemetry environment variables, so you
point it at your collector at run time:

```bash
podman run --rm \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.example.com:4317 \
  -e OTEL_SERVICE_NAME=ansible-prod \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment=prod \
  ghcr.io/jvzantvoort/jdc-execution-engine:latest ansible-playbook site.yml
```

| Variable | Image default | Purpose |
|----------|---------------|---------|
| `ANSIBLE_OPENTELEMETRY_ENABLED` | `true` | Set to `false` to switch tracing off |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | Collector endpoint |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | `grpc` | `grpc` or `http/protobuf` (use port 4318 for HTTP) |
| `OTEL_EXPORTER_OTLP_INSECURE` | `true` | Plain-text gRPC; set `false` for TLS |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5` | Export timeout (seconds) |
| `OTEL_SERVICE_NAME` | `ansible` | `service.name` of the trace |
| `OTEL_EXPORTER_OTLP_HEADERS` | – | Auth headers, e.g. `authorization=Bearer <token>` |
| `OTEL_RESOURCE_ATTRIBUTES` | – | Extra resource attributes |
| `TRACEPARENT` | – | W3C trace context: attach the run to a parent trace (e.g. a CI pipeline) |
| `ANSIBLE_OPENTELEMETRY_HIDE_TASK_ARGUMENTS` | `true` (ansible.cfg) | Keep task args (possible secrets) out of spans |
| `ANSIBLE_OPENTELEMETRY_DISABLE_LOGS` | `false` | Don't attach task results as span events |
| `ANSIBLE_OPENTELEMETRY_STORE_SPANS_IN_FILE` | – | Also write spans to a JSON file |

Never bake credentials into the image; pass `OTEL_EXPORTER_OTLP_HEADERS` at run
time (AWX/AAP: a credential type that injects it as an env var).

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

## Behaviour without a collector

If the endpoint is unreachable the playbook still succeeds; the exporter logs
`Failed to export traces ...` and adds a few seconds at the end of the run.
Set `ANSIBLE_OPENTELEMETRY_ENABLED=false` where tracing is not wanted.

## Privacy

By default the callback attaches each task's **result** to its span as an
event (that includes gathered facts). Task arguments are hidden in this image.
Use `no_log: true` on sensitive tasks, or `ANSIBLE_OPENTELEMETRY_DISABLE_LOGS=true`
to drop results from traces entirely.

## Layout

```
.
├── execution-environment.yml  # EE definition (base image, deps, OTel env defaults)
├── requirements.yml           # Ansible collections
├── requirements.txt           # Python packages (incl. OpenTelemetry SDK/exporters)
├── bindep.txt                 # System packages
├── files/ansible.cfg          # /etc/ansible/ansible.cfg (enables the callback)
├── tests/
│   ├── test.yml               # Smoke-test playbook
│   └── verify_spans.py        # Asserts the playbook produced a trace
├── otel/                      # Local Collector + Jaeger (podman/docker compose)
├── ansible-navigator.yml
├── Makefile
└── .github/workflows/build.yml  # Build, test, push to ghcr.io
```

## CI

GitHub Actions builds and smoke-tests every push and PR, and pushes to
`ghcr.io/<owner>/jdc-execution-engine`:

- `main` → `:latest` and `:<sha>`
- tag `v1.2.3` → `:1.2.3` and `:<sha>`
- weekly rebuild to pick up base-image updates
