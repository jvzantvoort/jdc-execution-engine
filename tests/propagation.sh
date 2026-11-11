#!/bin/sh
# Trace context propagation through ansible-runner, the way AWX runs a job:
# inbound traceparent from extra vars, AWX/flow correlation attributes and the
# outbound JDC_TRACEPARENT. Runs inside the image (see `make test`).
set -eu
P=/runner/project
SPANS=/tmp/spans.json
OUT=/tmp/jdc_traceparent
PARENT=00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
ENV_PARENT=00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01
WORKFLOW_ATTRS="--absent awx.workflow_job.id --absent awx.workflow_job.name
  --absent awx.job_template.name --absent awx.job.launch_type --absent flow.id"
export ANSIBLE_OPENTELEMETRY_STORE_SPANS_IN_FILE=$SPANS

# run NAME EXTRAVARS_YAML [VAR=value ...]: ansible-runner job; output in /tmp/pd-NAME/out
run() {
    name=$1 extravars=$2
    shift 2
    echo "== $name"
    pd=/tmp/pd-$name
    rm -rf "$pd" $SPANS $OUT
    mkdir -p "$pd/project" "$pd/env"
    cp $P/test.yml "$pd/project/"
    printf '%s\n' "$extravars" > "$pd/env/extravars"
    if ! env "$@" ansible-runner run "$pd" -p test.yml > "$pd/out" 2>&1; then
        cat "$pd/out"; echo "FAIL: $name: playbook failed"; exit 1
    fi
}
verify() { python3.12 $P/verify_spans.py $SPANS "$@"; }

run extravars "{traceparent: '$PARENT', tracestate: 'vendor=abc', flow_id: order-1234, awx_workflow_job_id: 77,
  awx_workflow_job_name: Deploy all, awx_job_template_name: deploy-web, awx_job_launch_type: workflow}" \
    JOB_ID=4242 AWX_HOST=https://awx.example.com
verify awx.job.id=4242 awx.workflow_job.id=77 "awx.workflow_job.name=Deploy all" \
    awx.job_template.name=deploy-web awx.job.launch_type=workflow flow.id=order-1234 \
    --parent $PARENT --outbound $OUT
grep -q "key=vendor, value=abc" $SPANS || { echo "FAIL: tracestate extra var not passed on"; exit 1; }

run precedence "{traceparent: '$PARENT'}" TRACEPARENT=$ENV_PARENT
verify --parent $PARENT --outbound $OUT $WORKFLOW_ATTRS

run invalid "{traceparent: '00-00000000000000000000000000000000-00f067aa0ba902b7-01'}"
grep -q "ignoring invalid traceparent" /tmp/pd-invalid/out || { cat /tmp/pd-invalid/out; echo "FAIL: no warning"; exit 1; }
verify --no-parent --outbound $OUT $WORKFLOW_ATTRS

run invalid-falls-back "{traceparent: not-a-traceparent}" TRACEPARENT=$ENV_PARENT
verify --parent $ENV_PARENT --outbound $OUT

run custom-var "{parent_ctx: '$PARENT', traceparent: '$ENV_PARENT'}" ANSIBLE_OPENTELEMETRY_TRACEPARENT_VAR=parent_ctx
verify --parent $PARENT --outbound $OUT

echo "OK: trace context propagation"
