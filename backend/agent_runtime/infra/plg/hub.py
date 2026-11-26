"""PLG observability: low-cardinality Metrics + Loki JSON logs + PG agent_spans."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import json
import logging
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple
import uuid

from agent_runtime.framework.core.contracts import SpanRecord

# Strict low-cardinality Prometheus label guard (never allow trace_id/user_id/session_id)
FORBIDDEN_METRIC_LABELS = frozenset({"trace_id", "session_id", "thread_id", "user_id", "request_id"})


class MetricsRegistry:
    """Zero-dependency Prometheus-style registry (exports Prometheus text exposition format)."""

    def __init__(self) -> None:
        self._counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = defaultdict(float)
        self._observations: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], List[float]] = defaultdict(list)

    @staticmethod
    def _validate_labels(labels: Dict[str, str]) -> Tuple[Tuple[str, str], ...]:
        bad = set(labels.keys()) & FORBIDDEN_METRIC_LABELS
        if bad:
            raise ValueError(f"High-cardinality Prometheus labels forbidden: {sorted(bad)}")
        return tuple(sorted((str(k), str(v)) for k, v in labels.items()))

    def inc(self, metric_name: str, amount: float = 1.0, **labels: str) -> None:
        key = (metric_name, self._validate_labels(labels))
        self._counters[key] += amount

    def observe(self, metric_name: str, value: float, **labels: str) -> None:
        key = (metric_name, self._validate_labels(labels))
        self._observations[key].append(value)

    def render_prometheus_text(self) -> str:
        lines: List[str] = []
        for (name, labels), val in sorted(self._counters.items()):
            lbl = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{lbl}}} {val}")
        for (name, labels), vals in sorted(self._observations.items()):
            lbl = ",".join(f'{k}="{v}"' for k, v in labels)
            avg = sum(vals) / len(vals) if vals else 0.0
            lines.append(f"{name}_avg{{{lbl}}} {avg:.4f}")
            lines.append(f"{name}_count{{{lbl}}} {len(vals)}")
        return "\n".join(lines) + ("\n" if lines else "")


class ObservabilityHub:
    """Combines Prometheus metrics, Loki structured JSON logs, and `agent_spans` tracing."""

    def __init__(self, store: Any, *, emit_stdout_logs: bool = False) -> None:
        self.store = store
        self.metrics = MetricsRegistry()
        self.loki_events: List[Dict[str, Any]] = []
        self._emit_stdout_logs = emit_stdout_logs
        self._logger = logging.getLogger("agent.loki")

    def log_event(self, event: str, trace_id: str, thread_id: str, **fields: Any) -> None:
        record = {
            "ts_ms": int(time.time() * 1000),
            "event": event,
            "trace_id": trace_id,
            "thread_id": thread_id,
            **fields,
        }
        self.loki_events.append(record)
        if self._emit_stdout_logs:
            self._logger.info(json.dumps(record, ensure_ascii=False))

    @contextmanager
    def span(
        self,
        *,
        trace_id: str,
        operation_name: str,
        service_name: str = "agent-runtime",
        parent_span_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Iterator[SpanRecord]:
        span_id = f"sp_{uuid.uuid4().hex[:10]}"
        start_ms = int(time.time() * 1000)
        t0 = time.monotonic()
        rec = SpanRecord(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            service_name=service_name,
            operation_name=operation_name,
            start_ms=start_ms,
            duration_ms=0,
            status="ok",
            attributes=dict(attributes or {}),
        )
        try:
            yield rec
        except Exception as exc:
            rec.status = "error"
            rec.attributes["error"] = type(exc).__name__
            raise
        finally:
            rec.duration_ms = max(1, int((time.monotonic() - t0) * 1000))
            self.store.save_span(rec)

    def format_waterfall(self, trace_id: str) -> str:
        """Render a compact terminal Trace waterfall (mirrors Grafana Traces panel)."""
        spans = self.store.list_spans(trace_id)
        if not spans:
            return "(no spans recorded)"
        origin = min(s.start_ms for s in spans)
        lines = [f"Trace Waterfall [{trace_id}] ({len(spans)} spans):"]
        for s in spans:
            offset = max(0, s.start_ms - origin)
            indent = "  ↳ " if s.parent_span_id else "• "
            lines.append(
                f"  {indent}{s.operation_name:<30} +{offset:>4}ms | {s.duration_ms:>4}ms | {s.status} | {s.attributes}"
            )
        return "\n".join(lines)
