#!/usr/bin/env python3
"""Verify that the opentelemetry callback wrote spans for tests/test.yml.

Usage: verify_spans.py SPANS_FILE [resource.key=value ...]
"""
import json
import sys

EXPECTED = {
    "Show Python and Ansible versions",
    "Use a filter from community.general (needs jmespath)",
    "Use a filter from ansible.utils (needs netaddr)",
}


def main(path, expected_resource=()):
    with open(path, encoding="utf-8") as fh:
        spans = json.load(fh)["spans"]

    names = {span.get("name") for span in spans}
    trace_ids = {span.get("context", {}).get("trace_id") for span in spans}
    print(f"{len(spans)} spans in {len(trace_ids)} trace(s):")
    for name in sorted(n for n in names if n):
        print(f"  - {name}")

    missing = EXPECTED - names
    if missing:
        print(f"FAIL: missing spans: {sorted(missing)}", file=sys.stderr)
        return 1
    # Privacy defaults: no task arguments, no task results (facts) in spans
    leaked = [s["name"] for s in spans
              if s.get("events") or any(k.startswith("ansible.task.args") for k in s.get("attributes", {}))]
    if leaked:
        print(f"FAIL: task args/results leaked into spans: {leaked}", file=sys.stderr)
        return 1
    if len(trace_ids) != 1:
        print("FAIL: expected all spans in a single trace", file=sys.stderr)
        return 1
    for item in expected_resource:
        key, value = item.split("=", 1)
        found = {s.get("resource", {}).get("attributes", {}).get(key) for s in spans}
        if found != {value}:
            print(f"FAIL: resource {key}: expected {value!r}, got {sorted(map(str, found))}", file=sys.stderr)
            return 1
        print(f"  resource {key} = {value}")
    print("OK: OpenTelemetry spans recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/spans.json", sys.argv[2:]))
