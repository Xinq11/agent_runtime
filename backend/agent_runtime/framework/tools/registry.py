"""Tool specification and immutable-protected startup-frozen ToolRegistry."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, List, Optional, Tuple

from agent_runtime.framework.core.contracts import AuthScope, RiskLevel

IMMUTABLE_BUILTIN_NAMES = frozenset({"read", "write", "read_tool_output"})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required_args: Tuple[str, ...] = ()
    allowed_args: Tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.READ
    handler: Optional[Callable] = None
    timeout_sec: float = 10.0
    is_subagent: bool = False
    bypass_truncation: bool = False
    parameters_schema: Optional[dict] = None

    def __post_init__(self):
        if not self.name or not callable(self.handler):
            raise ValueError("tool name and callable handler are required")
        if not self.allowed_args:
            object.__setattr__(self, "allowed_args", tuple(self.required_args))
        if not set(self.required_args).issubset(self.allowed_args):
            raise ValueError("required_args must be a subset of allowed_args")
        object.__setattr__(self, "risk_level", RiskLevel(self.risk_level))
        if not math.isfinite(self.timeout_sec) or self.timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive and finite")

    def permitted(self, scope: AuthScope) -> bool:
        tool_ok = scope.allowed_tools is None or self.name in scope.allowed_tools
        return tool_ok and (self.risk_level == RiskLevel.READ or scope.allow_write)


class ToolRegistry:
    """Registry with reserved immutable built-in names and post-bootstrap freeze."""

    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}
        self._builtins: set[str] = set()
        self._frozen = False

    def register_builtin(self, spec: ToolSpec) -> None:
        if self._frozen:
            raise RuntimeError("ToolRegistry is frozen")
        if spec.name in self._specs:
            raise ValueError(f"duplicate builtin tool: {spec.name}")
        self._specs[spec.name] = spec
        self._builtins.add(spec.name)

    def register(self, spec: ToolSpec) -> None:
        if self._frozen:
            raise RuntimeError(f"ToolRegistry is frozen; cannot register '{spec.name}' after startup")
        if spec.name in IMMUTABLE_BUILTIN_NAMES or spec.name in self._builtins:
            raise ValueError(f"Cannot override immutable built-in tool: {spec.name}")
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._specs[spec.name] = spec

    def freeze(self) -> None:
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def list_specs(self) -> List[ToolSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def visible_specs(self, auth_scope: AuthScope, include_subagents: bool = True) -> List[ToolSpec]:
        return [
            s
            for s in self.list_specs()
            if s.permitted(auth_scope) and (include_subagents or not s.is_subagent)
        ]
