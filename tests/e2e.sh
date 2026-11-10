#!/bin/sh
# End-to-end test: export real OTLP traces (gRPC and HTTP) to a Collector and
# check they arrive, with the run attached to a parent trace via TRACEPARENT.
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
        "$IMAGE" ansible-playbook /runner/project/test.yml >/dev/null
done
sleep 2   # let the collector's batch processor flush

mkdir -p "$(dirname "$LOG")"
$ENGINE logs $COL > "$LOG" 2>&1
for trace in 4bf92f3577b34da6a3ce929d0e0e4736 0af7651916cd43dd8448eb211c80319c; do
    n=$(grep -c "Trace ID *: $trace" "$LOG" || true)
    echo "trace $trace: $n spans"
    [ "$n" -ge 5 ] || fail=1
done
for name in "test.yml" "Show Python and Ansible versions" \
            "Use a filter from ansible.utils (needs netaddr)"; do
    grep -q "Name *: $name\$" "$LOG" || { echo "missing span: $name"; fail=1; }
done
grep -q "Parent ID *: 00f067aa0ba902b7" "$LOG" || { echo "root span not parented to TRACEPARENT"; fail=1; }

if [ $fail -ne 0 ]; then
    echo "FAIL: see $LOG"; exit 1
fi
echo "OK: traces exported over gRPC and HTTP, attached to TRACEPARENT"
