"""OpenAI-compatible streaming LLM provider (DeepSeek, OpenAI, vLLM, Ollama, etc.)."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from typing import Any, Optional, Sequence

import httpx

from agent_runtime.framework.core.config import ModelSettings
from agent_runtime.framework.core.contracts import ExecutionContext, Message, ToolCall
from agent_runtime.infra.llm.mock import MockScriptedLLM, ScriptRouter


def wire_message(m: Message) -> dict:
    value: dict = {"role": m.role, "content": m.content}
    if m.tool_calls:
        value["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)},
            }
            for c in m.tool_calls
        ]
        if m.metadata.get("reasoning_content"):
            value["reasoning_content"] = m.metadata["reasoning_content"]
    if m.role == "tool":
        value["tool_call_id"] = m.tool_call_id
    return value


def wire_spec(spec: Any) -> dict:
    """Serialize ToolSpec using its explicit JSON Schema (falling back to allowed_args)."""
    schema = getattr(spec, "parameters_schema", None)
    if not schema:
        fallback_types = {"amount": "number", "offset": "integer", "limit": "integer", "verbose": "boolean"}
        schema = {
            "type": "object",
            "properties": {arg: {"type": fallback_types.get(arg, "string")} for arg in spec.allowed_args},
            "required": list(spec.required_args),
            "additionalProperties": False,
        }
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": schema,
        },
    }


class _MockMetricsBridge:
    """Forward mock model token counts into Prometheus counters when telemetry is active."""

    def __init__(self, obs: Any) -> None:
        self.obs = obs

    def inc(self, name: str, amount: float, *, kind: str, **labels: str) -> None:
        tokens = getattr(self.obs, "tokens", None)
        if tokens is not None:
            tokens.labels(kind, "mock").inc(amount)


class OpenAICompatibleProvider:
    """Vendor-agnostic streaming OpenAI-compatible provider with optional mock fallback."""

    def __init__(
        self,
        client: Optional[httpx.AsyncClient],
        obs: Any,
        *,
        settings: Optional[ModelSettings] = None,
    ) -> None:
        self.mock_router: Optional[ScriptRouter] = None
        self.config = settings or ModelSettings(os.getenv("DEEPSEEK_API_KEY", ""))
        self.client = client
        self.obs = obs

    def set_default_mock_router(self, router: ScriptRouter) -> None:
        if self.mock_router is None:
            self.mock_router = router

    def _observe_ttft(self, mode: str, elapsed_sec: float) -> None:
        ttft = getattr(self.obs, "ttft", None)
        if ttft is not None:
            ttft.labels(mode).observe(elapsed_sec)

    def _inc_tokens(self, kind: str, mode: str, count: int) -> None:
        tokens = getattr(self.obs, "tokens", None)
        if tokens is not None and count:
            tokens.labels(kind, mode).inc(count)

    async def generate(
        self,
        *,
        messages: Sequence[Message],
        tool_specs: Sequence[Any],
        ctx: ExecutionContext,
    ) -> Message:
        cfg = self.config
        start = time.monotonic()
        mode = cfg.effective
        if mode == "mock" or self.client is None:
            index = max((i for i, m in enumerate(messages) if m.role == "user"), default=0)
            mock = MockScriptedLLM(obs=SimpleNamespace(metrics=_MockMetricsBridge(self.obs)), router=self.mock_router)
            result = await mock.generate(messages=messages[index:], tool_specs=tool_specs, ctx=ctx)
            self._observe_ttft("mock", time.monotonic() - start)
            if result.content and ctx.depth == 0:
                await ctx.emit("token", {"text": result.content})
            return result

        payload: dict = {
            "model": cfg.model,
            "messages": [wire_message(m) for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tool_specs:
            payload["tools"] = [wire_spec(s) for s in tool_specs]

        content, reasoning, fragments = [], [], {}
        first, finished = True, False
        async with self.client.stream(
            "POST",
            cfg.base_url + "/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer " + cfg.api_key},
            timeout=90,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                packet = json.loads(raw)
                if packet.get("error"):
                    raise ValueError("model reported a streaming error")
                usage = packet.get("usage")
                if usage:
                    counts = {
                        "in": usage.get("prompt_tokens", 0),
                        "out": usage.get("completion_tokens", 0),
                        "cache_read": usage.get(
                            "prompt_cache_hit_tokens",
                            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                        ),
                    }
                    for kind, count in counts.items():
                        self._inc_tokens(kind, mode, int(count or 0))
                for choice in packet.get("choices", []):
                    delta = choice.get("delta") or {}
                    if first and any(delta.get(k) for k in ("content", "reasoning_content", "tool_calls")):
                        self._observe_ttft(mode, time.monotonic() - start)
                        first = False
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                    if delta.get("content"):
                        content.append(delta["content"])
                        if ctx.depth == 0:
                            await ctx.emit("token", {"text": delta["content"]})
                    for part in delta.get("tool_calls") or []:
                        item = fragments.setdefault(part["index"], {"id": "", "name": "", "args": ""})
                        fn = part.get("function") or {}
                        item["id"] += part.get("id") or ""
                        item["name"] += fn.get("name") or ""
                        item["args"] += fn.get("arguments") or ""
                    if choice.get("finish_reason") in ("stop", "tool_calls"):
                        finished = True

        if not finished:
            raise ValueError("incomplete model response")
        calls = [ToolCall(c["id"], c["name"], json.loads(c["args"] or "{}")) for _, c in sorted(fragments.items())]
        return Message(
            "assistant",
            "".join(content),
            calls,
            metadata={"reasoning_content": "".join(reasoning)} if calls and reasoning else {},
        )
