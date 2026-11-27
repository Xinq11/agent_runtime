"""Zero-dependency `@tool` decorator that auto-generates JSON Schema from Python type hints.

Supports both sync and async functions with optional `ctx: ExecutionContext` injection:

    @tool(risk_level=RiskLevel.READ)
    async def search_orders(region: str, limit: int = 10, ctx: ExecutionContext = None) -> str:
        \"\"\"Query recent orders by region.\"\"\"
        ...
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, get_args, get_origin, get_type_hints

from agent_runtime.framework.core.contracts import ExecutionContext, RiskLevel
from agent_runtime.framework.tools.gateway import ToolSpec


def _python_type_to_json_schema(tp: Any) -> Dict[str, Any]:
    origin = get_origin(tp)
    if origin is Union:
        non_none = [arg for arg in get_args(tp) if arg is not type(None)]
        if len(non_none) == 1:
            return _python_type_to_json_schema(non_none[0])
        return {"type": "string"}
    if tp is str or tp is Any:
        return {"type": "string"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if origin in (list, tuple, set) or tp in (list, tuple):
        args = get_args(tp)
        item_schema = _python_type_to_json_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item_schema}
    if origin is dict or tp is dict:
        return {"type": "object"}
    return {"type": "string"}


def _build_schema_from_callable(
    fn: Callable[..., Any],
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Dict[str, Any], bool]:
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    required: List[str] = []
    allowed: List[str] = []
    properties: Dict[str, Any] = {}
    wants_ctx = False

    for param_name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        ann = hints.get(param_name, param.annotation)
        if param_name == "ctx" or ann is ExecutionContext:
            wants_ctx = True
            continue
        allowed.append(param_name)
        prop_schema = _python_type_to_json_schema(ann if ann is not inspect.Parameter.empty else str)
        if param.default is not inspect.Parameter.empty and param.default is not None:
            prop_schema["default"] = param.default
        properties[param_name] = prop_schema
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    json_schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    return tuple(required), tuple(allowed), json_schema, wants_ctx


def tool(
    fn: Optional[Callable[..., Any]] = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    risk_level: RiskLevel = RiskLevel.READ,
    timeout_sec: float = 10.0,
    bypass_truncation: bool = False,
    reconcile: Optional[Callable[..., Any]] = None,
) -> Any:
    """Turn a regular Python function into a `ToolSpec` with automatic JSON Schema."""

    def _decorate(func: Callable[..., Any]) -> ToolSpec:
        req, allowed, schema, wants_ctx = _build_schema_from_callable(func)
        tool_name = name or func.__name__
        doc = (description or inspect.getdoc(func) or tool_name).strip().splitlines()[0]

        async def _adapter(args: Dict[str, Any], ctx: ExecutionContext) -> Any:
            kwargs = dict(args)
            if wants_ctx:
                kwargs["ctx"] = ctx
            res = func(**kwargs)
            if inspect.isawaitable(res):
                return await res
            return res

        return ToolSpec(
            name=tool_name,
            description=doc,
            required_args=req,
            allowed_args=allowed,
            risk_level=risk_level,
            handler=_adapter,
            timeout_sec=timeout_sec,
            bypass_truncation=bypass_truncation,
            parameters_schema=schema,
            reconcile_handler=reconcile,
        )

    if fn is not None:
        return _decorate(fn)
    return _decorate
