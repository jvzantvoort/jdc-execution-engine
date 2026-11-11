#!/bin/sh
# End-to-end test: export real OTLP traces (gRPC and HTTP) to a Collector and
# check they arrive, with the run attached to a parent trace via TRACEPARENT
# and, through ansible-runner, via the traceparent extra var.
# Usage: tests/e2e.sh <image> <collector-image>    (ENGINE=podman|docker)
set -eu
IMAGE=$1
OTELCOL=$2
ENGINE=${ENGINE:-podman}
DIR=$(cd "$(dirname "$0")" && pwd)
NET=jdc-e2e
COL=jdc-e2e-collector
LOG="$DIR/../build/e2e.log"

cleanup() {
    $ENGINE rm -f $COL >/dev/null 2>&1 || true
    $ENGINE network rm $NET >/dev/null 2>&1 || true
}
cleanup
trap cleanup EXIT

$ENGINE network create $NET >/dev/null
$ENGINE run -d --name $COL --network $NET \
    -v "$DIR/e2e-collector.yaml:/etc/otelcol/config.yaml:ro,Z" \
    "$OTELCOL" --config=/etc/otelcol/config.yaml >/dev/null

i=0
until $ENGINE logs $COL 2>&1 | grep -q 'Everything is ready'; do
    i=$((i + 1)); [ $i -le 30 ] || { $ENGINE logs $COL; echo "FAIL: collector not ready"; exit 1; }
    sleep 1
done

# protocol  port  trace id (parent span id is fixed)
fail=0
for run in "grpc 4317 4bf92f3577b34da6a3ce929d0e0e4736" \
           "http/protobuf 4318 0af7651916cd43dd8448eb211c80319c"; do
    set -- $run
    echo "== $1 → $COL:$2 (TRACEPARENT trace $3)"
    $ENGINE run --rm --network $NET -v "$DIR:/runner/project:ro,Z" \
        -e OTEL_EXPORTER_OTLP_ENDPOINT="http://$COL:$2" \
        -e OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="$1" \
        -e TRACEPARENT="00-$3-00f067aa0ba902b7-01" \
        -e JOB_ID=4242 -e AWX_HOST=https://awx.example.com \
        "$IMAGE" ansible-playbook /runner/project/test.yml >/dev/null
done
# AWX-style: ansible-runner, parent trace context and correlation IDs from extra vars
RUNNER_TRACE=5b8aa5a2d2c872e8321cf37308d69df2
RUNNER_PARENT=051581bf3cb55c13
echo "== grpc via ansible-runner → $COL:4317 (extra var traceparent trace $RUNNER_TRACE)"
$ENGINE run --rm --network $NET -v "$DIR:/runner/project:ro,Z" \
    -e OTEL_EXPORTER_OTLP_ENDPOINT="http://$COL:4317" \
    -e JOB_ID=4243 -e AWX_HOST=https://awx.example.com \
    -e EXTRAVARS="{traceparent: 00-$RUNNER_TRACE-$RUNNER_PARENT-01, flow_id: e2e-flow, awx_workflow_job_id: 77}" \
    "$IMAGE" sh -c 'mkdir -p /tmp/pd/project /tmp/pd/env && cp /runner/project/test.yml /tmp/pd/project/ &&
        printf "%s\n" "$EXTRAVARS" > /tmp/pd/env/extravars && ansible-runner run /tmp/pd -p test.yml' >/dev/null
sleep 2   # let the collector's batch processor flush

mkdir -p "$(dirname "$LOG")"
$ENGINE logs $COL > "$LOG" 2>&1
for trace in 4bf92f3577b34da6a3ce929d0e0e4736 0af7651916cd43dd8448eb211c80319c $RUNNER_TRACE; do
    n=$(grep -c "Trace ID *: $trace" "$LOG" || true)
    echo "trace $trace: $n spans"
    [ "$n" -ge 5 ] || fail=1
done
for name in "test.yml" "Show Python and Ansible versions" \
            "Use a filter from ansible.utils (needs netaddr)"; do
    grep -q "Name *: $name\$" "$LOG" || { echo "missing span: $name"; fail=1; }
done
grep -q "awx.job.id: Str(4242)" "$LOG" || { echo "missing resource attribute awx.job.id"; fail=1; }
grep -q "Parent ID *: 00f067aa0ba902b7" "$LOG" || { echo "root span not parented to TRACEPARENT"; fail=1; }
grep -q "Parent ID *: $RUNNER_PARENT" "$LOG" || { echo "root span not parented to the traceparent extra var"; fail=1; }
grep -q "flow.id: Str(e2e-flow)" "$LOG" || { echo "missing resource attribute flow.id"; fail=1; }
grep -q "awx.workflow_job.id: Str(77)" "$LOG" || { echo "missing resource attribute awx.workflow_job.id"; fail=1; }

if [ $fail -ne 0 ]; then
    echo "FAIL: see $LOG"; exit 1
fi
echo "OK: traces exported over gRPC and HTTP, attached to TRACEPARENT and the traceparent extra var, tagged with the AWX job"
