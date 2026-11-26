"""Production PLG telemetry: Prometheus metrics + async Loki log shipper."""

from __future__ import annotations

import asyncio
from collections import deque
import json
import os
import time

import httpx
from prometheus_client import CollectorRegistry, Counter, Histogram

from agent_runtime.infra.plg.hub import ObservabilityHub


class Telemetry(ObservabilityHub):
    def __init__(self, store, client: httpx.AsyncClient):
        super().__init__(store)
        self.client = client
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2048)
        self.loki_events: deque = deque(maxlen=512)
        self.prom = CollectorRegistry()
        self.ttft = Histogram("agent_ttft_seconds", "Time to first model delta", ["mode"], registry=self.prom)
        self.tokens = Counter("agent_tokens", "Model tokens", ["kind", "mode"], registry=self.prom)
        self.tools = Counter("agent_tool_calls", "Tool outcomes", ["tool", "status"], registry=self.prom)
        self.dropped = Counter("agent_log_drops", "Log entries dropped", registry=self.prom)

    @staticmethod
    def _default_log_meta(event: str, fields: dict):
        if event == "http_request":
            status = int(fields.get("status_code", 200))
            lvl = "ERROR" if status >= 500 else ("WARN" if status >= 400 else "INFO")
            return lvl, "http.access", f"{fields.get('method', 'GET')} {fields.get('path', '/')} -> {status} ({fields.get('duration_ms', 0)}ms)"
        if event == "server_started":
            return "INFO", "backend.api", f"Backend ready (model={fields.get('model')}, mode={fields.get('mode')}, PG+MinIO connected)"
        if event == "turn_start":
            return "INFO", "agent.lifecycle", f"Turn started on thread={fields.get('thread_id', '')}"
        if event == "llm_done":
            calls = fields.get("tool_calls") or []
            call_str = f"tool_calls={calls}" if calls else "final_text"
            return "INFO", "agent.llm", f"LLM#{fields.get('step', 1)} ({fields.get('model', 'mock')}) completed -> {call_str}"
        if event == "tool_status":
            st = str(fields.get("status", "ok"))
            lvl = "WARN" if st == "approval_required" else ("ERROR" if st not in ("ok", "running") else "INFO")
            return lvl, "agent.tool", f"Tool {fields.get('tool', 'unknown')} finished with status={st}"
        if event == "turn_done":
            return "INFO", "agent.lifecycle", f"Turn completed (compacted={fields.get('compacted', False)})"
        if event == "turn_error":
            return "ERROR", "agent.lifecycle", f"Turn failed: {fields.get('error', 'unknown')}"
        return "INFO", "backend.api", str(fields.get("msg") or event)

    def log_event(self, event, trace_id, thread_id, **fields):
        lvl, logger_name, default_msg = self._default_log_meta(event, {"thread_id": thread_id, **fields})
        record = dict(
            ts_ms=int(time.time() * 1000),
            level=str(fields.pop("level", lvl)),
            logger=str(fields.pop("logger", logger_name)),
            msg=str(fields.pop("msg", default_msg)),
            event=event,
            trace_id=trace_id,
            thread_id=thread_id,
            **fields,
        )
        self.loki_events.append(record)
        try:
            self.queue.put_nowait((str(time.time_ns()), json.dumps(record, ensure_ascii=False)))
        except asyncio.QueueFull:
            self.dropped.inc()

    async def trace_logs(self, trace: str | None):
        if not trace:
            return []
        local = [entry for entry in self.loki_events if entry["trace_id"] == trace]
        try:
            res = await self.client.get(
                os.environ["LOKI_URL"].rstrip("/") + "/loki/api/v1/query_range",
                params={
                    "query": '{service_name="agent-runtime"} | json | trace_id=' + json.dumps(trace),
                    "limit": 50,
                    "direction": "backward",
                    "since": "168h",
                },
                timeout=2,
            )
            res.raise_for_status()
            remote = [
                json.loads(line)
                for series in res.json()["data"]["result"]
                for _, line in series["values"]
            ]
        except (httpx.HTTPError, KeyError, ValueError):
            remote = []
        merged = {json.dumps(e, sort_keys=True): e for e in [*remote, *local]}
        return sorted(merged.values(), key=lambda e: e["ts_ms"])[-50:]

    async def push(self):
        endpoint = os.environ["LOKI_URL"].rstrip("/") + "/loki/api/v1/push"
        while True:
            batch = [await self.queue.get()]
            while not self.queue.empty() and len(batch) < 100:
                batch.append(self.queue.get_nowait())
            for attempt in range(5):
                try:
                    res = await self.client.post(
                        endpoint,
                        json={
                            "streams": [
                                {
                                    "stream": {"service_name": "agent-runtime", "job": "agent-runtime"},
                                    "values": batch,
                                }
                            ]
                        },
                        timeout=3,
                    )
                    res.raise_for_status()
                    break
                except httpx.HTTPError:
                    if attempt < 4:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        self.dropped.inc(len(batch))
            for _ in batch:
                self.queue.task_done()
