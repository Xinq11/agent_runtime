"""Inheritable `BaseAgent` template class for vertical customer & team specialization.

Supports BOTH extension patterns seamlessly:
  1. Composition: `AgentRuntimeApp(extensions=[RagExtension(), SkillsExtension(), CustomerPlugin()])`
  2. Subclassing: `class FinanceAgent(BaseAgent): ...` overriding `system_prompt`, `build_tools()`, etc.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from agent_runtime.framework.core.contracts import AuthScope
from agent_runtime.framework.core.protocols import AgentMiddleware, AgentPlugin, AppMetadata
from agent_runtime.framework.core.workflow import DEFAULT_SYSTEM_PROMPT
from agent_runtime.framework.tools.gateway import ToolSpec
from agent_runtime.framework.tools.subagent import SubAgentProfile


class BaseAgent:
    """Base Agent blueprint that downstream teams can subclass without touching core internals."""

    name: str = "base_agent"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    def metadata(self) -> Optional[AppMetadata]:
        return None

    def default_identity(self) -> Optional[AuthScope]:
        return None

    def build_extensions(self) -> Sequence[AgentPlugin]:
        return ()

    def build_tools(self, *, store: Any) -> Sequence[ToolSpec]:
        return ()

    def build_subagents(self) -> Sequence[SubAgentProfile]:
        return ()

    def build_middlewares(self) -> Sequence[AgentMiddleware]:
        return ()

    def to_plugin(self) -> AgentPlugin:
        """Adapt a `BaseAgent` subclass cleanly into the unified `AgentPlugin` lifecycle."""
        owner = self

        class _AgentAdapterPlugin(AgentPlugin):
            name = owner.name
            system_prompt = owner.system_prompt

            def metadata(self) -> Optional[AppMetadata]:
                return owner.metadata()

            def default_identity(self) -> Optional[AuthScope]:
                return owner.default_identity()

            def register_tools(self, *, registry, store, model, gateway, obs) -> None:
                from agent_runtime.framework.tools.subagent import register_subagent_tool

                for spec in owner.build_tools(store=store):
                    registry.register(spec)
                for profile in owner.build_subagents():
                    register_subagent_tool(
                        registry=registry,
                        profile=profile,
                        model_client=model,
                        tool_gateway=gateway,
                        obs=obs,
                        tool_spec_cls=ToolSpec,
                    )

            def middlewares(self) -> Sequence[AgentMiddleware]:
                return owner.build_middlewares()

        return _AgentAdapterPlugin()

    def build_app(self, **kwargs: Any) -> Any:
        """Compile this `BaseAgent` subclass into a runnable `AgentRuntimeApp`."""
        from agent_runtime.framework.agent.app import AgentRuntimeApp

        extra_ext = list(kwargs.pop("extensions", ()))
        plugins: List[AgentPlugin] = [self.to_plugin(), *self.build_extensions(), *extra_ext]
        return AgentRuntimeApp(extensions=plugins, **kwargs)
