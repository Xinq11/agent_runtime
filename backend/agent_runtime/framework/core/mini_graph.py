"""Zero-dependency fallback StateGraph executor mirroring LangGraph's interrupt/resume."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


class InMemoryCheckpointer:
    """Drop-in local checkpointer mirroring AsyncPostgresSaver thread state."""

    def __init__(self) -> None:
        self._checkpoints: Dict[str, Dict[str, Any]] = {}

    def save(self, thread_id: str, node_name: str, state: Dict[str, Any], interrupt_payload: Any) -> None:
        self._checkpoints[thread_id] = {
            "node_name": node_name,
            "state": copy.deepcopy(state),
            "interrupt_payload": copy.deepcopy(interrupt_payload),
        }

    def load(self, thread_id: str) -> Optional[Dict[str, Any]]:
        cp = self._checkpoints.get(thread_id)
        return copy.deepcopy(cp) if cp is not None else None

    def clear(self, thread_id: str) -> None:
        self._checkpoints.pop(thread_id, None)


class CompiledMiniGraph:
    """Minimal async StateGraph executor supporting edges, conditional edges, and interrupt/resume."""

    def __init__(
        self,
        nodes: Dict[str, Callable[..., Any]],
        edges: Dict[str, str],
        cond_edges: Dict[str, Tuple[Callable[[Dict[str, Any]], str], Optional[Dict[str, str]]]],
        checkpointer: Optional[InMemoryCheckpointer] = None,
        *,
        start_sentinel: str = "__start__",
        end_sentinel: str = "__end__",
        interrupt_cls: type = Exception,
        command_cls: type = object,
        resume_registry: Optional[Dict[int, List[Any]]] = None,
    ) -> None:
        self._nodes = nodes
        self._edges = edges
        self._cond_edges = cond_edges
        self._checkpointer = checkpointer or InMemoryCheckpointer()
        self._start = start_sentinel
        self._end = end_sentinel
        self._interrupt_cls = interrupt_cls
        self._command_cls = command_cls
        self._resume_registry = resume_registry if resume_registry is not None else {}

    @staticmethod
    def _task_key() -> int:
        try:
            asyncio.get_running_loop()
            return id(asyncio.current_task())
        except RuntimeError:
            return 0

    async def ainvoke(
        self,
        payload: Union[Dict[str, Any], Any],
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cfg = config or {}
        thread_id = str(cfg.get("configurable", {}).get("thread_id", "default"))
        recursion_limit = int(cfg.get("recursion_limit", 24))
        task_key = self._task_key()

        if isinstance(payload, self._command_cls) and getattr(payload, "resume", None) is not None:
            cp = self._checkpointer.load(thread_id)
            if cp is None:
                raise ValueError(f"No interrupted checkpoint found for thread_id={thread_id}")
            state = cp["state"]
            current = cp["node_name"]
            self._resume_registry[task_key] = [payload.resume]
        else:
            state = copy.deepcopy(payload) if isinstance(payload, dict) else {}
            current = self._edges.get(self._start, self._end)
            self._resume_registry.pop(task_key, None)

        steps = 0
        try:
            while current != self._end:
                steps += 1
                if steps > recursion_limit:
                    raise RuntimeError(f"recursion_limit={recursion_limit} exceeded on thread={thread_id}")
                node_fn = self._nodes[current]
                try:
                    update = node_fn(state)
                    if asyncio.iscoroutine(update):
                        update = await update
                except self._interrupt_cls as intr:
                    self._checkpointer.save(thread_id, current, state, intr.payload)
                    result = copy.deepcopy(state)
                    result["__interrupt__"] = [intr.payload]
                    return result

                if isinstance(update, dict):
                    state.update(update)

                if current in self._cond_edges:
                    router_fn, mapping = self._cond_edges[current]
                    branch = router_fn(state)
                    current = mapping[branch] if mapping else branch
                else:
                    current = self._edges.get(current, self._end)
        finally:
            self._resume_registry.pop(task_key, None)

        self._checkpointer.clear(thread_id)
        return state

    async def aget_state(self, config: Optional[Dict[str, Any]] = None) -> Any:
        cfg = config or {}
        thread_id = str(cfg.get("configurable", {}).get("thread_id", "default"))
        cp = self._checkpointer.load(thread_id)
        if cp is None:
            return SimpleNamespace(values={}, tasks=[])
        intr = SimpleNamespace(value=cp["interrupt_payload"])
        return SimpleNamespace(values=cp["state"], tasks=[SimpleNamespace(interrupts=[intr])])


class MiniStateGraph:
    """API-compatible subset of `langgraph.graph.StateGraph`."""

    def __init__(
        self,
        state_schema: Any = dict,
        *,
        start_sentinel: str = "__start__",
        end_sentinel: str = "__end__",
        interrupt_cls: type = Exception,
        command_cls: type = object,
        resume_registry: Optional[Dict[int, List[Any]]] = None,
    ) -> None:
        self._state_schema = state_schema
        self._nodes: Dict[str, Callable[..., Any]] = {}
        self._edges: Dict[str, str] = {}
        self._cond_edges: Dict[str, Tuple[Callable[[Dict[str, Any]], str], Optional[Dict[str, str]]]] = {}
        self._start = start_sentinel
        self._end = end_sentinel
        self._interrupt_cls = interrupt_cls
        self._command_cls = command_cls
        self._resume_registry = resume_registry

    def add_node(self, name: str, fn: Callable[..., Any]) -> "MiniStateGraph":
        self._nodes[name] = fn
        return self

    def add_edge(self, src: str, dst: str) -> "MiniStateGraph":
        self._edges[src] = dst
        return self

    def add_conditional_edges(
        self,
        src: str,
        router_fn: Callable[[Dict[str, Any]], str],
        mapping: Optional[Dict[str, str]] = None,
    ) -> "MiniStateGraph":
        self._cond_edges[src] = (router_fn, mapping)
        return self

    def compile(self, checkpointer: Optional[Any] = None) -> CompiledMiniGraph:
        return CompiledMiniGraph(
            nodes=dict(self._nodes),
            edges=dict(self._edges),
            cond_edges=dict(self._cond_edges),
            checkpointer=checkpointer,
            start_sentinel=self._start,
            end_sentinel=self._end,
            interrupt_cls=self._interrupt_cls,
            command_cls=self._command_cls,
            resume_registry=self._resume_registry,
        )
