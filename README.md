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
| `TRACEPARENT` | – | W3C trace context: attach the run to a parent trace (e.g. a CI pipeline). The `traceparent` extra var takes precedence |
| `ANSIBLE_OPENTELEMETRY_TRACEPARENT_VAR` | `traceparent` | Name of the extra var that carries the parent trace context |
| `ANSIBLE_OPENTELEMETRY_HIDE_TASK_ARGUMENTS` | `true` | Keep task arguments out of spans |
| `ANSIBLE_OPENTELEMETRY_DISABLE_LOGS` | `true` | Keep task results (incl. facts) out of spans |
| `ANSIBLE_OPENTELEMETRY_STORE_SPANS_IN_FILE` | – | Write spans to a JSON file instead of exporting (also turns tracing on) |
| `ANSIBLE_OPENTELEMETRY_STREAM_SPANS` | `false` | Export each task's span as soon as it finishes, so hung or killed jobs still leave spans (see [Streaming](#streaming-task-spans)) |
| `JDC_TRACEPARENT` | *set by the callback* | Trace context of the playbook's root span, for tasks to pass on (see [below](#passing-the-trace-on-jdc_traceparent)); unset when tracing is off |

These extra vars are read at the start of the run (with `-e`, from
ansible-runner's `env/extravars`, or from an AWX launch):

| Extra var | Effect |
|-----------|--------|
| `traceparent` | Parent trace context, W3C format; wins over the `TRACEPARENT` env var. Invalid values are ignored with a warning |
| `tracestate` | W3C `tracestate`, passed on together with `traceparent` |
| `flow_id` | Your own correlation ID, added as resource attribute `flow.id` |
| `awx_workflow_job_id`, `awx_workflow_job_name`, `awx_job_template_name`, `awx_job_launch_type` | Added as resource attributes (AWX sets these itself, see [AWX](#awx--ansible-automation-platform)) |

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

The same context can be passed as an extra var instead, which is what you need
for AWX:

```bash
ansible-playbook site.yml -e traceparent="$TRACEPARENT" -e flow_id=CHG0042
```

An extra var `traceparent` wins over the `TRACEPARENT` env var. A value that
isn't a valid W3C traceparent (`00-<32 hex>-<16 hex>-<2 hex>`, IDs not all
zero) is ignored with a warning; the run never fails because of it. Use
`ANSIBLE_OPENTELEMETRY_TRACEPARENT_VAR` if the name `traceparent` is taken in
your playbooks.

### Linking AWX jobs to the caller's trace

AWX sets environment variables globally or through credentials, not per
launch, so it can't hand each job a different `TRACEPARENT`. Extra vars *are*
per launch, so the caller passes its trace context as the `traceparent` extra
var. AWX discards extra vars a template doesn't accept, so on the job template
or workflow job template either:

- enable **Prompt on launch** for *Variables*, or
- add a survey with a text field `traceparent` (not required), and `flow_id`
  if you use it.

Workflow extra vars reach every node of the workflow, so every job in it joins
the caller's trace.

Launch with `--monitor` (CLI) or `wait: true` (`awx.awx` modules). The caller
then waits for the job, so its span covers the whole job: a job that is
queued for long, hangs or fails shows up as a long or failed caller span,
even when Ansible itself never exported anything (see
[Limitations](#limitations)). Without it the caller's span ends a few
milliseconds after the launch and tells you nothing.

**Jenkins** (the [OpenTelemetry plugin](https://plugins.jenkins.io/opentelemetry/)
sets `TRACEPARENT` in `sh` steps to the current step's span):

```groovy
stage('Deploy') {
  steps {
    withCredentials([string(credentialsId: 'awx-token', variable: 'CONTROLLER_OAUTH_TOKEN')]) {
      sh '''
        export CONTROLLER_HOST=https://awx.example.com
        awx job_templates launch 42 --monitor \
          --extra_vars "{traceparent: $TRACEPARENT, flow_id: $BUILD_TAG}"
      '''
    }
  }
}
```

Use `awx workflow_job_templates launch <id> --monitor ...` for a workflow.

**Rundeck** has no built-in tracing; wrap the step in
[otel-cli](https://github.com/equinix-labs/otel-cli), which opens a span and
puts its `TRACEPARENT` in the environment of the command it runs:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.example.com:4317
otel-cli exec --service rundeck --name "deploy @job.name@" -- \
  sh -c 'awx job_templates launch 42 --monitor \
    --extra_vars "{traceparent: $TRACEPARENT, flow_id: rundeck-@job.execid@}"'
```

The single quotes matter: `$TRACEPARENT` must be expanded by the shell that
otel-cli starts, not by the step's own shell.

### Passing the trace on (`JDC_TRACEPARENT`)

While tracing is on, the callback sets `JDC_TRACEPARENT` in the controller's
environment to the trace context of the playbook's root span, before the first
task runs. Tasks pass it to whatever they call next, so that system's spans
join the same trace:

```yaml
- name: Call an API within this trace
  ansible.builtin.uri:
    url: https://api.example.com/deployments
    method: POST
    headers:
      traceparent: "{{ lookup('env', 'JDC_TRACEPARENT') }}"
    body_format: json
    body: {app: web}

- name: Run the next job template within this trace
  awx.awx.job_launch:            # needs the awx.awx collection (not in this image)
    name: deploy-db
    extra_vars:
      traceparent: "{{ lookup('env', 'JDC_TRACEPARENT') }}"
      flow_id: "{{ flow_id | default('') }}"
    wait: true                   # see --monitor above
```

When tracing is off `JDC_TRACEPARENT` is unset (the callback even clears an
inherited one), the lookup returns an empty string, and an empty `traceparent`
is ignored by this image and by standard OpenTelemetry propagators, so the same
playbook works with and without tracing.

Downstream spans become children of the playbook's root span, not of the task
that made the call: task spans only get their IDs when the trace is built at
the end of the run.

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

AWX also passes a set of `awx_*` extra vars to every job. These become
resource attributes too, when present:

| Attribute | From extra var | Example |
|-----------|----------------|---------|
| `awx.workflow_job.id` | `awx_workflow_job_id` | `4240` (only for jobs inside a workflow) |
| `awx.workflow_job.name` | `awx_workflow_job_name` | `Release web` |
| `awx.job_template.name` | `awx_job_template_name` | `deploy-web` |
| `awx.job.launch_type` | `awx_job_launch_type` | `manual`, `workflow`, `scheduled`, ... |
| `flow.id` | `flow_id` (yours, not AWX's) | `CHG0042` |

Search for `awx.job.id=4242` in Jaeger (or `resource.awx.job.id = "4242"` in
Tempo) to go from a job to its trace; `awx.job.url` links back.
`awx.workflow_job.id` finds every job of one workflow run, and `flow.id` finds
everything that belongs to one business flow, across systems and traces.
Values you set yourself in `OTEL_RESOURCE_ATTRIBUTES` take precedence. Outside
AWX (no `JOB_ID`/`AWX_HOST`, no `awx_*` extra vars) nothing is added.

### Limitations

- **Spans are exported at the end of the run** unless
  [streaming](#streaming-task-spans) is on. The upstream callback builds the
  whole trace in `v2_playbook_on_stats`, so a job that hangs, is cancelled or
  is killed exports no spans at all. Launching with `--monitor` /
  `wait: true` keeps the caller's span open for the job's duration, so the
  stall is still visible from the caller's side.
- **A workflow has no span of its own.** Each workflow node is a separate job
  and becomes a separate child of the caller's span, so nodes appear as
  siblings, not nested in a workflow span. Group them with
  `awx.workflow_job.id`.
- **Downstream systems must accept `traceparent`** (W3C Trace Context) to join
  the trace. For systems that don't, pass `flow_id` and log it there, then
  correlate on `flow.id`.
- One trace per playbook run: the context is fixed when the first play starts.

### Streaming task spans

With `ANSIBLE_OPENTELEMETRY_STREAM_SPANS=true` each task's span (per host) is
exported as soon as the task finishes on that host, in the background, so a
job that hangs or is killed still shows how far it got: the last span tells
you which task finished last, and the gap after it is where the job stalled.

```bash
podman run --rm -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.example.com:4317 \
  -e ANSIBLE_OPENTELEMETRY_STREAM_SPANS=true \
  ghcr.io/jvzantvoort/jdc-execution-engine:1.2.1 ansible-playbook site.yml
```

For AWX, set it next to the other `OTEL_*` variables. Things to know:

- The playbook's **root span is still exported at the end** (a span can only
  be exported once it has ended). Until then, and forever for a killed job,
  the task spans point to a parent that isn't there. They still show up in
  the right trace; trace viewers flag the missing root span. The caller's
  span (see `--monitor`) still covers the whole job.
- Spans go out in batches every `OTEL_BSP_SCHEDULE_DELAY` milliseconds (1000
  unless you set it). A job killed with SIGKILL loses at most the last batch.
- The trace looks the same as without streaming: same spans, IDs, parents and
  attributes. `include_*` tasks' extra "include" spans and anything that
  couldn't be streamed are exported at the end as before.
- With an unreachable endpoint the run still succeeds, but the end of the run
  waits for the export timeout (`OTEL_EXPORTER_OTLP_TIMEOUT`) once more.
- If streaming fails for any reason, the callback logs a warning and exports
  the remaining spans at the end of the run.

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
make test   # spans without args/results; AWX-style runs via ansible-runner (extra-var traceparent,
            # precedence, invalid values, JDC_TRACEPARENT, workflow attributes); silent without endpoint
make e2e    # real Collector: OTLP over gRPC and HTTP, TRACEPARENT and extra-var parenting, AWX attributes,
            # streaming (same spans; a hung job's spans arrive before it is killed)
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
│   ├── stream.yml             # A job that hangs (streaming e2e test)
│   ├── ansible.cfg            # Simulated project config
│   ├── e2e.sh                 # End-to-end export test
│   ├── propagation.sh         # Trace context in (extra vars) and out (JDC_TRACEPARENT)
│   ├── e2e-collector.yaml     # Collector config for the e2e test
│   └── verify_spans.py        # Asserts a trace was produced without args/results, checks parenting
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
