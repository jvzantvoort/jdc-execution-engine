# SPDX-License-Identifier: GPL-3.0-or-later
# Thin wrapper around community.general.opentelemetry (GPL-3.0-or-later).

from __future__ import annotations

DOCUMENTATION = r"""
name: jdc_opentelemetry
type: notification
short_description: OpenTelemetry tracing that switches itself on when an OTLP endpoint is set
description:
  - Wraps M(community.general.opentelemetry) and loads without being listed in C(callbacks_enabled),
    so tracing also works when a project ships its own C(ansible.cfg).
  - With O(enabled=auto) (the default) tracing is active only when E(OTEL_EXPORTER_OTLP_ENDPOINT),
    E(OTEL_EXPORTER_OTLP_TRACES_ENDPOINT) or O(store_spans_in_file) is set. Otherwise the plugin
    stays silent and adds no overhead.
  - Do not also enable C(community.general.opentelemetry), or every run is traced twice.
  - Under AWX (E(JOB_ID) and E(AWX_HOST) set) the trace's resource gets C(awx.job.id) and
    C(awx.job.url), so a job can be found from its trace and the other way round.
  - The parent trace context can also come from an extra var (see O(traceparent_var)), which AWX
    can set per launch. An extra var wins over E(TRACEPARENT). Invalid values are ignored with a
    warning. An optional C(tracestate) extra var is passed along with it.
  - The extra vars C(awx_workflow_job_id), C(awx_workflow_job_name), C(awx_job_template_name),
    C(awx_job_launch_type) and C(flow_id) become the resource attributes C(awx.workflow_job.id),
    C(awx.workflow_job.name), C(awx.job_template.name), C(awx.job.launch_type) and C(flow.id).
  - While tracing is on, E(JDC_TRACEPARENT) holds the W3C traceparent of the playbook's root
    span in the controller, so tasks can pass it on with C(lookup('env', 'JDC_TRACEPARENT')).
options:
  stream_spans:
    type: bool
    default: false
    description:
      - Export each task's span as soon as the task finishes on a host, instead of exporting the
        whole trace at the end of the run. A job that hangs, is cancelled or is killed then still
        leaves the spans of the tasks that finished.
      - The playbook's root span is still exported at the end of the run, so until then the task
        spans point to a parent that has not arrived yet.
      - Spans are sent in the background in batches, every E(OTEL_BSP_SCHEDULE_DELAY) milliseconds
        (1000 unless set).
    env:
      - name: ANSIBLE_OPENTELEMETRY_STREAM_SPANS
    ini:
      - section: callback_opentelemetry
        key: stream_spans
  traceparent_var:
    type: str
    default: traceparent
    description: Name of the extra var that holds the W3C traceparent of the parent span.
    env:
      - name: ANSIBLE_OPENTELEMETRY_TRACEPARENT_VAR
    ini:
      - section: callback_opentelemetry
        key: traceparent_var
  enabled:
    type: str
    default: auto
    choices: [auto, "true", "false"]
    description:
      - V(auto) traces when an OTLP endpoint or O(store_spans_in_file) is configured.
      - V(true) always traces (exporting to the OpenTelemetry default endpoint when none is set).
      - V(false) never traces.
    env:
      - name: ANSIBLE_OPENTELEMETRY_ENABLED
    ini:
      - section: callback_opentelemetry
        key: enabled
  # The options below are passed through to community.general.opentelemetry unchanged.
  hide_task_arguments:
    default: false
    type: bool
    description: Hide the arguments for a task.
    env:
      - name: ANSIBLE_OPENTELEMETRY_HIDE_TASK_ARGUMENTS
    ini:
      - section: callback_opentelemetry
        key: hide_task_arguments
  enable_from_environment:
    type: str
    description: Only enable when the named environment variable is V(true). Prefer O(enabled).
    env:
      - name: ANSIBLE_OPENTELEMETRY_ENABLE_FROM_ENVIRONMENT
    ini:
      - section: callback_opentelemetry
        key: enable_from_environment
  otel_service_name:
    default: ansible
    type: str
    description: The service name resource attribute.
    env:
      - name: OTEL_SERVICE_NAME
    ini:
      - section: callback_opentelemetry
        key: otel_service_name
  traceparent:
    type: str
    description:
      - The W3C Trace Context traceparent header.
      - The extra var named by O(traceparent_var) takes precedence.
    env:
      - name: TRACEPARENT
  disable_logs:
    default: false
    type: bool
    description: Do not attach task results to spans as events.
    env:
      - name: ANSIBLE_OPENTELEMETRY_DISABLE_LOGS
    ini:
      - section: callback_opentelemetry
        key: disable_logs
  disable_attributes_in_logs:
    default: false
    type: bool
    description: Disable populating span attributes to the logs.
    env:
      - name: ANSIBLE_OPENTELEMETRY_DISABLE_ATTRIBUTES_IN_LOGS
    ini:
      - section: callback_opentelemetry
        key: disable_attributes_in_logs
  store_spans_in_file:
    type: str
    description: Write the spans to this JSON file instead of exporting them over OTLP.
    env:
      - name: ANSIBLE_OPENTELEMETRY_STORE_SPANS_IN_FILE
    ini:
      - section: callback_opentelemetry
        key: store_spans_in_file
  otel_exporter_otlp_traces_protocol:
    type: str
    default: grpc
    choices: [grpc, http/protobuf]
    description: Transport protocol for spans.
    env:
      - name: OTEL_EXPORTER_OTLP_TRACES_PROTOCOL
    ini:
      - section: callback_opentelemetry
        key: otel_exporter_otlp_traces_protocol
requirements:
  - community.general >= 9.0.0
  - opentelemetry-api, opentelemetry-sdk, opentelemetry-exporter-otlp (Python libraries)
"""

import contextlib
import functools
import json
import os
import re
from urllib.parse import quote

from ansible_collections.community.general.plugins.callback import opentelemetry as upstream
from ansible_collections.community.general.plugins.callback.opentelemetry import (
    CallbackModule as OpenTelemetryCallback,
    OpenTelemetrySource,
)

try:
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
except ImportError:  # upstream raises a clear error for this when the plugin loads
    RandomIdGenerator = object

ENDPOINT_VARS = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")

# Outbound trace context: the W3C traceparent of the playbook's root span
OUTBOUND_VAR = "JDC_TRACEPARENT"

# Batch delay for streamed spans, unless OTEL_BSP_SCHEDULE_DELAY is set (the SDK default is 5000)
STREAM_SCHEDULE_DELAY_MS = 1000

TRACEPARENT_RE = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")

# Extra vars (AWX sets the awx_* ones for every job) -> resource attributes
EXTRA_VAR_ATTRIBUTES = {
    "awx_workflow_job_id": "awx.workflow_job.id",
    "awx_workflow_job_name": "awx.workflow_job.name",
    "awx_job_template_name": "awx.job_template.name",
    "awx_job_launch_type": "awx.job.launch_type",
    "flow_id": "flow.id",
}


def parse_traceparent(value):
    """(trace_id, span_id, flags) as hex strings for a valid W3C version 00 traceparent, else None."""
    if not isinstance(value, str):
        return None
    match = TRACEPARENT_RE.fullmatch(value.strip().lower())
    if not match or int(match[1], 16) == 0 or int(match[2], 16) == 0:
        return None
    return match[1], match[2], match[3]


def awx_resource_attributes():
    """Resource attributes for an AWX job, from the env AWX sets for every job."""
    job_id, host = os.environ.get("JOB_ID"), os.environ.get("AWX_HOST")
    if not (job_id and host):
        return {}
    return {
        "awx.job.id": job_id,
        "awx.job.url": f"{host.rstrip('/')}/#/jobs/playbook/{job_id}/output",
    }


def extra_var_resource_attributes(extra_vars):
    """Resource attributes for the correlation extra vars that are set."""
    attributes = {}
    for var, attribute in EXTRA_VAR_ATTRIBUTES.items():
        value = extra_vars.get(var)
        if value is not None and str(value) != "":
            attributes[attribute] = str(value)
    return attributes


class PreallocatedIdGenerator(RandomIdGenerator):
    """Returns a fixed trace ID and span ID the first time each is asked for, then random IDs.

    The upstream plugin creates the playbook's root span first, so the root span gets the IDs
    that were published in JDC_TRACEPARENT when the play started. The trace ID is only asked
    for when there is no parent trace.
    """

    def __init__(self, trace_id, span_id):
        self._trace_id = trace_id
        self._span_id = span_id

    def generate_trace_id(self):
        trace_id, self._trace_id = self._trace_id, None
        return trace_id or super().generate_trace_id()

    def generate_span_id(self):
        span_id, self._span_id = self._span_id, None
        return span_id or super().generate_span_id()


class JdcOpenTelemetrySource(OpenTelemetrySource):
    """Upstream's span builder, with tracestate passed on along with traceparent."""

    tracestate = None

    def traceparent_context(self, traceparent):
        carrier = {}
        if traceparent:
            carrier["traceparent"] = traceparent
            if self.tracestate:
                carrier["tracestate"] = self.tracestate
        return upstream.TraceContextTextMapPropagator().extract(carrier=carrier)


class CallbackModule(OpenTelemetryCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "jdc_opentelemetry"
    CALLBACK_NEEDS_ENABLED = False

    def __init__(self, display=None):
        super().__init__(display=display)
        self.opentelemetry = JdcOpenTelemetrySource(display=self._display)
        self.root_ids = None  # (trace_id, span_id) of the root span, fixed at the first play
        self.extra_resource_attributes = {}
        self.stream_spans = False
        self.stream = None  # SpanStream while streaming task spans

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super().set_options(task_keys=task_keys, var_options=var_options, direct=direct)
        # self.disabled is already set when switched off via enable_from_environment
        if not self.disabled and not self._tracing_wanted():
            self.disabled = True
        if self.disabled:
            os.environ.pop(OUTBOUND_VAR, None)  # never hand out a context nobody exports
            return
        self.traceparent = self._checked_traceparent(self.traceparent, "TRACEPARENT")
        self.stream_spans = bool(self.get_option("stream_spans"))

    def _tracing_wanted(self):
        mode = str(self.get_option("enabled") or "auto").lower()
        if mode == "true":
            return True
        if mode == "auto" and (self.store_spans_in_file or any(os.environ.get(v) for v in ENDPOINT_VARS)):
            return True
        self._display.vvv(f"jdc_opentelemetry: tracing off (enabled={mode}, no OTLP endpoint set)")
        return False

    def _checked_traceparent(self, value, source):
        """The normalized traceparent, or None (with a warning) when it is set but invalid."""
        if value is None or str(value).strip() == "":
            return None
        parsed = parse_traceparent(str(value))
        if parsed is None:
            self._display.warning(f"jdc_opentelemetry: ignoring invalid traceparent in {source}: {str(value)[:80]!r}")
            return None
        return "00-{}-{}-{}".format(*parsed)

    def v2_playbook_on_play_start(self, play):
        super().v2_playbook_on_play_start(play)
        if self.root_ids is not None:  # one trace per playbook run: only the first play counts
            return
        try:
            self._start_trace(play.get_variable_manager().extra_vars or {})
        except Exception as exc:  # tracing must never break a run
            self._display.warning(f"jdc_opentelemetry: could not set up the trace context: {exc}")

    def _start_trace(self, extra_vars):
        # Inbound: extra var > TRACEPARENT env > none
        name = self.get_option("traceparent_var") or "traceparent"
        traceparent = self._checked_traceparent(extra_vars.get(name), f"extra var {name!r}")
        if traceparent:
            self.traceparent = traceparent
            tracestate = extra_vars.get("tracestate")
            self.opentelemetry.tracestate = str(tracestate) if tracestate else None
        self.extra_resource_attributes = extra_var_resource_attributes(extra_vars)

        # Outbound: fix the root span's IDs now, so tasks can pass them on
        parent = parse_traceparent(self.traceparent) if self.traceparent else None
        ids = RandomIdGenerator()
        trace_id = int(parent[0], 16) if parent else ids.generate_trace_id()
        span_id = ids.generate_span_id()
        sampled = int(parent[2], 16) & 1 if parent else 1
        self.root_ids = (trace_id, span_id)
        os.environ[OUTBOUND_VAR] = f"00-{trace_id:032x}-{span_id:016x}-{sampled:02x}"

        if self.stream_spans:
            try:
                self.stream = SpanStream(self, trace_id, span_id, sampled)
            except Exception as exc:
                self._display.warning(f"jdc_opentelemetry: not streaming spans, exporting them at the end of the run: {exc}")

    @contextlib.contextmanager
    def resource_attributes_in_env(self):
        """Add our resource attributes to OTEL_RESOURCE_ATTRIBUTES, which the SDK reads when a
        Resource is created. Explicit user values come later in the list and win."""
        extra = {**awx_resource_attributes(), **self.extra_resource_attributes}
        saved = os.environ.get("OTEL_RESOURCE_ATTRIBUTES")
        if extra:
            pairs = [f"{k}={quote(v, safe='')}" for k, v in extra.items()]
            os.environ["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(pairs + ([saved] if saved else []))
        try:
            yield
        finally:
            if saved is None:
                os.environ.pop("OTEL_RESOURCE_ATTRIBUTES", None)
            else:
                os.environ["OTEL_RESOURCE_ATTRIBUTES"] = saved

    # Streaming: export a task's span for one host as soon as its result is in,
    # then drop that host from tasks_data so upstream doesn't export it again at
    # the end. Anything that wasn't streamed is still exported by upstream,
    # including upstream's "include" pseudo-host entries for include_* tasks.

    def _stream_result(self, result):
        if self.stream is None or self.stream.failed:
            return
        try:
            self.stream.export(self.tasks_data[result._task._uuid], result._host._uuid)
        except Exception as exc:  # tracing must never break a run
            self.stream.failed = True
            self._display.warning(f"jdc_opentelemetry: streaming spans failed, exporting the rest at the end of the run: {exc}")

    def v2_runner_on_ok(self, result):
        super().v2_runner_on_ok(result)
        self._stream_result(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        super().v2_runner_on_failed(result, ignore_errors=ignore_errors)
        self._stream_result(result)

    def v2_runner_on_skipped(self, result):
        super().v2_runner_on_skipped(result)
        self._stream_result(result)

    def v2_runner_on_unreachable(self, result):
        super().v2_runner_on_unreachable(result)
        self._stream_result(result)

    def v2_playbook_on_stats(self, stats):
        # The trace is built and exported here, at the end of the run (with
        # streaming: the root span and whatever wasn't streamed). For just that
        # call, add our resource attributes, and give upstream's TracerProvider
        # an IdGenerator that hands the root span the IDs published in
        # JDC_TRACEPARENT.
        saved_provider = upstream.TracerProvider
        if self.root_ids:
            upstream.TracerProvider = functools.partial(
                saved_provider, id_generator=PreallocatedIdGenerator(*self.root_ids)
            )
        try:
            with self.resource_attributes_in_env():
                return super().v2_playbook_on_stats(stats)
        finally:
            upstream.TracerProvider = saved_provider
            if self.stream is not None:
                self.stream.close()


class SpanStream:
    """Exports task spans while the playbook runs, as children of the (not yet created) root span.

    Uses its own TracerProvider, not the global one: upstream sets the global provider at the
    end of the run, and the SDK allows that only once. Spans are built by upstream's
    update_span_data, so they look exactly like the ones exported at the end.
    """

    def __init__(self, callback, trace_id, span_id, sampled):
        self.callback = callback
        self.failed = False
        trace = upstream.trace
        with callback.resource_attributes_in_env():
            resource = upstream.Resource.create({upstream.SERVICE_NAME: callback.otel_service_name})
        self.provider = upstream.TracerProvider(resource=resource)
        self.memory = None
        if callback.store_spans_in_file:
            self.memory = upstream.InMemorySpanExporter()
            processor = upstream.SimpleSpanProcessor(self.memory)
        else:
            if callback.otel_exporter_otlp_traces_protocol == "grpc":
                exporter = upstream.GRPCOTLPSpanExporter()
            else:
                exporter = upstream.HTTPOTLPSpanExporter()
            delay = None if os.environ.get("OTEL_BSP_SCHEDULE_DELAY") else STREAM_SCHEDULE_DELAY_MS
            processor = upstream.BatchSpanProcessor(exporter, schedule_delay_millis=delay)
        self.provider.add_span_processor(processor)
        self.tracer = self.provider.get_tracer(upstream.__name__)

        # The root span inherits the parent's tracestate; so do its children
        parent = trace.get_current_span(callback.opentelemetry.traceparent_context(callback.traceparent))
        root = trace.SpanContext(
            trace_id, span_id, is_remote=False,
            trace_flags=trace.TraceFlags(sampled), trace_state=parent.get_span_context().trace_state,
        )
        self.context = trace.set_span_in_context(trace.NonRecordingSpan(root))

    def export(self, task, host_uuid):
        host_data = task.host_data.get(host_uuid)
        if host_data is None:
            return
        callback = self.callback
        with self.tracer.start_as_current_span(
            task.name, context=self.context, start_time=host_data.start or task.start, end_on_exit=False
        ) as span:
            callback.opentelemetry.update_span_data(
                task, host_data, span, callback.disable_logs, callback.disable_attributes_in_logs
            )
        del task.host_data[host_uuid]

    def close(self):
        """Flush streamed spans; with store_spans_in_file, add them to the file upstream wrote."""
        try:
            self.provider.force_flush()
            path = self.callback.store_spans_in_file
            if self.memory is not None and path and os.path.exists(path):
                streamed = [json.loads(span.to_json()) for span in self.memory.get_finished_spans()]
                with open(path, encoding="utf-8") as fh:
                    spans = json.load(fh)["spans"]
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"spans": streamed + spans}, fh, indent=4)
        except Exception as exc:
            self.callback._display.warning(f"jdc_opentelemetry: could not flush streamed spans: {exc}")
