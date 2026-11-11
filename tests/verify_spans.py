#!/usr/bin/env python3
"""Verify that the opentelemetry callback wrote spans for tests/test.yml.

Usage: verify_spans.py SPANS_FILE [resource.key=value ...] [options]

  --parent TRACEPARENT   the root span is a child of this traceparent
  --no-parent            the root span has no parent
  --absent KEY           resource attribute KEY is not set (repeatable)
  --outbound FILE        FILE holds JDC_TRACEPARENT: same trace, span ID of the root span
"""
import argparse
import json
import re
import sys

EXPECTED = {
    "Show Python and Ansible versions",
    "Use a filter from community.general (needs jmespath)",
    "Use a filter from ansible.utils (needs netaddr)",
    "Record the outbound trace context (JDC_TRACEPARENT)",
}
TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


def hexid(value):
    """'0xabc…' (span JSON) -> 'abc…'"""
    return (value or "").removeprefix("0x")


def check(spans, args):
    names = {span.get("name") for span in spans}
    trace_ids = {hexid(span["context"]["trace_id"]) for span in spans}
    print(f"{len(spans)} spans in {len(trace_ids)} trace(s):")
    for name in sorted(n for n in names if n):
        print(f"  - {name}")

    missing = EXPECTED - names
    if missing:
        return fail(f"missing spans: {sorted(missing)}")
    # test.yml runs on one host, so a repeated name means a span was exported twice
    if len(names) != len(spans):
        return fail("duplicate spans")
    # Privacy defaults: no task arguments, no task results (facts) in spans
    leaked = [s["name"] for s in spans
              if s.get("events") or any(k.startswith("ansible.task.args") for k in s.get("attributes", {}))]
    if leaked:
        return fail(f"task args/results leaked into spans: {leaked}")
    if len(trace_ids) != 1:
        return fail("expected all spans in a single trace")
    trace_id = trace_ids.pop()

    span_ids = {hexid(s["context"]["span_id"]) for s in spans}
    roots = [s for s in spans if hexid(s.get("parent_id")) not in span_ids]
    if len(roots) != 1:
        return fail(f"expected one root span, got {[s['name'] for s in roots]}")
    root = roots[0]
    root_span_id, root_parent = hexid(root["context"]["span_id"]), hexid(root.get("parent_id"))
    print(f"  root span {root['name']!r}: trace {trace_id}, span {root_span_id}, parent {root_parent or '-'}")

    if args.parent:
        want_trace, want_span = TRACEPARENT.fullmatch(args.parent).groups()
        if (trace_id, root_parent) != (want_trace, want_span):
            return fail(f"root span not parented to {args.parent}")
    if args.no_parent and root_parent:
        return fail(f"root span has a parent ({root_parent}), expected none")

    if args.outbound:
        with open(args.outbound, encoding="utf-8") as fh:
            outbound = fh.read().strip()
        match = TRACEPARENT.fullmatch(outbound)
        if not match:
            return fail(f"JDC_TRACEPARENT is not a valid traceparent: {outbound!r}")
        if match.groups() != (trace_id, root_span_id):
            return fail(f"JDC_TRACEPARENT {outbound} does not point at the root span")
        print(f"  JDC_TRACEPARENT = {outbound}")

    resources = [s.get("resource", {}).get("attributes", {}) for s in spans]
    for item in args.resource:
        key, value = item.split("=", 1)
        found = {r.get(key) for r in resources}
        if found != {value}:
            return fail(f"resource {key}: expected {value!r}, got {sorted(map(str, found))}")
        print(f"  resource {key} = {value}")
    for key in args.absent:
        if any(key in r for r in resources):
            return fail(f"resource {key} is set, expected it to be absent")
        print(f"  resource {key} absent")
    print("OK: OpenTelemetry spans recorded")
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spans_file", nargs="?", default="/tmp/spans.json")
    parser.add_argument("resource", nargs="*", help="expected resource attribute, key=value")
    parser.add_argument("--parent")
    parser.add_argument("--no-parent", action="store_true")
    parser.add_argument("--absent", action="append", default=[])
    parser.add_argument("--outbound")
    args = parser.parse_args(argv)
    with open(args.spans_file, encoding="utf-8") as fh:
        spans = json.load(fh)["spans"]
    return check(spans, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
