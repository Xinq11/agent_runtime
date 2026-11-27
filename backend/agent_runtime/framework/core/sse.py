"""Bounded in-memory SSE event stream broker supporting `Last-Event-ID` reconnect replay."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import time
from typing import AsyncIterator, Deque, Dict, Optional, Set

from agent_runtime.framework.core.contracts import SSEEvent


class RunEventStream:
    """Per-run bounded event ring buffer with fan-out to live SSE subscribers."""

    def __init__(self, run_id: str, *, max_events: int = 512, subscriber_queue_size: int = 256) -> None:
        self.run_id = run_id
        self._max_events = max(16, int(max_events))
        self._subscriber_queue_size = max(16, int(subscriber_queue_size))
        self._buffer: Deque[SSEEvent] = deque(maxlen=self._max_events)
        self._subscribers: Set[asyncio.Queue] = set()
        self.seq: int = 0
        self.done: bool = False
        self.finished_at: Optional[float] = None

    def publish(self, event: SSEEvent) -> None:
        if self.done:
            return
        self.seq += 1
        event.seq = self.seq
        if not event.run_id:
            event.run_id = self.run_id
        self._buffer.append(event)
        stalled: list[asyncio.Queue] = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                stalled.append(q)
        for q in stalled:
            self._subscribers.discard(q)
            with suppress(asyncio.QueueEmpty):
                q.get_nowait()
            with suppress(asyncio.QueueFull):
                q.put_nowait(None)

    def close(self) -> None:
        if self.done:
            return
        self.done = True
        self.finished_at = time.monotonic()
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with suppress(asyncio.QueueFull):
                    q.put_nowait(None)
        self._subscribers.clear()

    def snapshot_after(self, after_seq: int = 0) -> list[SSEEvent]:
        return [ev for ev in self._buffer if ev.seq > after_seq]

    async def subscribe(
        self,
        *,
        after_seq: int = 0,
        poll_timeout: float = 0.5,
        is_disconnected=None,
    ) -> AsyncIterator[SSEEvent]:
        """Replay buffered events with `seq > after_seq`, then stream live events until close."""
        q: asyncio.Queue = asyncio.Queue(self._subscriber_queue_size)
        last_sent = max(0, int(after_seq))
        if not self.done:
            self._subscribers.add(q)
        try:
            for ev in list(self._buffer):
                if ev.seq > last_sent:
                    last_sent = ev.seq
                    yield ev
            if self.done:
                return
            while True:
                if is_disconnected is not None and await is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=poll_timeout)
                except asyncio.TimeoutError:
                    if self.done:
                        break
                    continue
                if ev is None:
                    break
                if ev.seq <= last_sent:
                    continue
                last_sent = ev.seq
                yield ev
        finally:
            self._subscribers.discard(q)


class RunEventBroker:
    """TTL + LRU registry of active and recently finished `RunEventStream` instances."""

    def __init__(
        self,
        *,
        max_streams: int = 256,
        ttl_sec: float = 300.0,
        max_events_per_run: int = 512,
    ) -> None:
        self._max_streams = max(8, int(max_streams))
        self._ttl_sec = max(1.0, float(ttl_sec))
        self._max_events_per_run = max(16, int(max_events_per_run))
        self._streams: Dict[str, RunEventStream] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            rid
            for rid, st in self._streams.items()
            if st.done and st.finished_at is not None and (now - st.finished_at) >= self._ttl_sec
        ]
        for rid in expired:
            self._streams.pop(rid, None)
        if len(self._streams) > self._max_streams:
            finished_sorted = sorted(
                ((st.finished_at or 0.0, rid) for rid, st in self._streams.items() if st.done),
                key=lambda item: item[0],
            )
            for _, rid in finished_sorted:
                if len(self._streams) <= self._max_streams:
                    break
                self._streams.pop(rid, None)

    def open(self, run_id: str) -> RunEventStream:
        self._prune()
        stream = RunEventStream(run_id, max_events=self._max_events_per_run)
        self._streams[run_id] = stream
        return stream

    def get(self, run_id: str) -> Optional[RunEventStream]:
        self._prune()
        return self._streams.get(run_id)

    def close(self, run_id: str) -> None:
        stream = self._streams.get(run_id)
        if stream is not None:
            stream.close()
        self._prune()
