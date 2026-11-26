"""Thin LangGraph compatibility selector (real `langgraph` in prod, `MiniStateGraph` offline)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent_runtime.framework.core.mini_graph import (
    CompiledMiniGraph,
    InMemoryCheckpointer,
    MiniStateGraph,
)

try:
    from langgraph.checkpoint.memory import InMemorySaver as LG_InMemorySaver  # type: ignore
    from langgraph.errors import GraphInterrupt as LG_GraphInterrupt  # type: ignore
    from langgraph.graph import END as LG_END  # type: ignore
    from langgraph.graph import START as LG_START  # type: ignore
    from langgraph.graph import StateGraph as LG_StateGraph  # type: ignore
    from langgraph.types import Command as LG_Command  # type: ignore
    from langgraph.types import interrupt as lg_interrupt  # type: ignore

    HAS_LANGGRAPH = True
except Exception:
    HAS_LANGGRAPH = False
    LG_InMemorySaver = None
    LG_GraphInterrupt = None
    LG_START = "__start__"
    LG_END = "__end__"
    LG_StateGraph = None
    LG_Command = None
    lg_interrupt = None

START = LG_START
END = LG_END


@dataclass
class Command:
    """Compatible with `langgraph.types.Command(resume=...)`."""

    resume: Any = None
    goto: Optional[str] = None
    update: Optional[Dict[str, Any]] = None


class GraphInterrupt(Exception):
    """Raised internally by `interrupt(payload)` to pause execution at a checkpoint."""

    def __init__(self, payload: Any) -> None:
        super().__init__("Graph execution interrupted for human review")
        self.payload = payload


GRAPH_INTERRUPT_TYPES = (
    (LG_GraphInterrupt, GraphInterrupt) if LG_GraphInterrupt is not None else (GraphInterrupt,)
)

_ACTIVE_RESUME_VALUES: Dict[int, List[Any]] = {}


def make_command(
    *, resume: Any = None, goto: Optional[str] = None, update: Optional[Dict[str, Any]] = None
) -> Any:
    """Return real `langgraph.types.Command` when installed, else local `Command`."""
    if HAS_LANGGRAPH and LG_Command is not None:
        kwargs: Dict[str, Any] = {}
        if resume is not None:
            kwargs["resume"] = resume
        if goto is not None:
            kwargs["goto"] = goto
        if update is not None:
            kwargs["update"] = update
        return LG_Command(**kwargs)
    return Command(resume=resume, goto=goto, update=update)


def interrupt(payload: Any) -> Any:
    """Pause the graph and surface `payload`, returning the resume value on replay."""
    if HAS_LANGGRAPH and lg_interrupt is not None:
        try:
            return lg_interrupt(payload)
        except RuntimeError:
            pass

    try:
        asyncio.get_running_loop()
        task_key = id(asyncio.current_task())
    except RuntimeError:
        task_key = 0
    queue = _ACTIVE_RESUME_VALUES.get(task_key)
    if queue:
        return queue.pop(0)
    raise GraphInterrupt(payload)


def default_checkpointer(*, prefer_real_langgraph: bool = True) -> Any:
    """Return real `langgraph` `InMemorySaver` when real LangGraph is active, else `InMemoryCheckpointer`."""
    if prefer_real_langgraph and HAS_LANGGRAPH and LG_InMemorySaver is not None:
        return LG_InMemorySaver()
    return InMemoryCheckpointer()


def build_state_graph(state_schema: Any = dict, *, prefer_real_langgraph: bool = True) -> Any:
    """Return real `langgraph.graph.StateGraph` when installed, else `MiniStateGraph`."""
    if prefer_real_langgraph and HAS_LANGGRAPH and LG_StateGraph is not None:
        return LG_StateGraph(state_schema)
    return MiniStateGraph(
        state_schema,
        start_sentinel=START,
        end_sentinel=END,
        interrupt_cls=GraphInterrupt,
        command_cls=Command,
        resume_registry=_ACTIVE_RESUME_VALUES,
    )


__all__ = [
    "Command",
    "CompiledMiniGraph",
    "END",
    "GRAPH_INTERRUPT_TYPES",
    "GraphInterrupt",
    "HAS_LANGGRAPH",
    "InMemoryCheckpointer",
    "MiniStateGraph",
    "START",
    "build_state_graph",
    "default_checkpointer",
    "interrupt",
    "make_command",
]
