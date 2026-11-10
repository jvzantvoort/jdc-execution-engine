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
options:
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
    default: None
    type: str
    description: The W3C Trace Context traceparent header.
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

import os
from urllib.parse import quote

from ansible_collections.community.general.plugins.callback.opentelemetry import (
    CallbackModule as OpenTelemetryCallback,
)

ENDPOINT_VARS = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")


def awx_resource_attributes():
    """Resource attributes for an AWX job, from the env AWX sets for every job."""
    job_id, host = os.environ.get("JOB_ID"), os.environ.get("AWX_HOST")
    if not (job_id and host):
        return {}
    return {
        "awx.job.id": job_id,
        "awx.job.url": f"{host.rstrip('/')}/#/jobs/playbook/{job_id}/output",
    }


class CallbackModule(OpenTelemetryCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "jdc_opentelemetry"
    CALLBACK_NEEDS_ENABLED = False

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super().set_options(task_keys=task_keys, var_options=var_options, direct=direct)
        if self.disabled:  # switched off via enable_from_environment
            return

        mode = str(self.get_option("enabled") or "auto").lower()
        if mode == "true":
            return
        if mode == "auto" and (self.store_spans_in_file or any(os.environ.get(v) for v in ENDPOINT_VARS)):
            return

        self.disabled = True
        self._display.vvv(f"jdc_opentelemetry: tracing off (enabled={mode}, no OTLP endpoint set)")

    def v2_playbook_on_stats(self, stats):
        # The SDK builds the trace's Resource from OTEL_RESOURCE_ATTRIBUTES while
        # the trace is exported here, at the end of the run. Add the AWX
        # attributes for just that call; explicit user values come later in the
        # list and win.
        extra = awx_resource_attributes()
        if not extra:
            return super().v2_playbook_on_stats(stats)
        saved = os.environ.get("OTEL_RESOURCE_ATTRIBUTES")
        pairs = [f"{k}={quote(v, safe='')}" for k, v in extra.items()]
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(pairs + ([saved] if saved else []))
        try:
            return super().v2_playbook_on_stats(stats)
        finally:
            if saved is None:
                os.environ.pop("OTEL_RESOURCE_ATTRIBUTES", None)
            else:
                os.environ["OTEL_RESOURCE_ATTRIBUTES"] = saved
