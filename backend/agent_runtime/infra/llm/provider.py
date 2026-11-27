"""OpenAI-compatible streaming LLM provider (DeepSeek now; extensible to other APIs)."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
import os
import re
import time
from typing import Any, Optional, Sequence

import httpx

from agent_runtime.framework.core.config import ModelSettings
from agent_runtime.framework.core.contracts import ExecutionContext, Message, ToolCall

log = logging.getLogger("agent-runtime.llm")

_DSML_MARKER_RE = re.compile(r"<\s*[｜|]\s*DSML\s*[｜|]", re.IGNORECASE)
_DSML_PARTIAL_TAIL_RE = re.compile(
    r"<(?:\s*(?:[｜|](?:\s*(?:D(?:S(?:M(?:L(?:\s*[｜|]?)?)?)?)?)?)?)?)?$",
    re.IGNORECASE,
)


def strip_dsml_markup(text: str) -> str:
    """Strip any leaked DeepSeek internal DSML tool-call markup from assistant text."""
    if not text:
        return ""
    m = _DSML_MARKER_RE.search(text)
    if not m:
        return text
    return text[: m.start()].rstrip()


class DsmlStreamGuard:
    """Zero-latency streaming filter that halts emission if `<｜DSML｜` / `<|DSML|` appears."""

    def __init__(self) -> None:
        self._buf = ""
        self.triggered = False

    def feed(self, chunk: str) -> str:
        if self.triggered or not chunk:
            return ""
        self._buf += chunk
        m = _DSML_MARKER_RE.search(self._buf)
        if m:
            safe = self._buf[: m.start()]
            self._buf = ""
            self.triggered = True
            return safe
        tail = _DSML_PARTIAL_TAIL_RE.search(self._buf)
        if tail:
            emit = self._buf[: tail.start()]
            self._buf = self._buf[tail.start() :]
            return emit
        emit = self._buf
        self._buf = ""
        return emit

    def flush(self) -> str:
        if self.triggered or not self._buf:
            self._buf = ""
            return ""
        emit = self._buf
        self._buf = ""
        return emit


class ModelNotConfiguredError(ValueError):
    """Raised when no LLM API key is configured for the active provider."""


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


class OpenAICompatibleProvider:
    """Streaming OpenAI-compatible provider (currently DeepSeek; ready for multi-provider expansion)."""

    def __init__(
        self,
        client: Optional[httpx.AsyncClient],
        obs: Any,
        *,
        settings: Optional[ModelSettings] = None,
    ) -> None:
        self.config = settings or ModelSettings(os.getenv("DEEPSEEK_API_KEY", ""))
        self.client = client
        self.obs = obs

    @asynccontextmanager
    async def _http_client(self):
        if self.client is not None:
            yield self.client
            return
        async with httpx.AsyncClient(follow_redirects=False) as temp_client:
            yield temp_client

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
        tool_choice: Optional[str] = None,
    ) -> Message:
        cfg = self.config
        if not cfg.api_key:
            raise ModelNotConfiguredError(
                "未配置 DeepSeek API Key，请在 .env 设置 DEEPSEEK_API_KEY 或点击左下角设置填写密钥后再对话。"
            )

        start = time.monotonic()
        mode = cfg.effective
        payload: dict = {
            "model": cfg.model,
            "messages": [wire_message(m) for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tool_specs:
            payload["tools"] = [wire_spec(s) for s in tool_specs]
            if tool_choice:
                payload["tool_choice"] = tool_choice

        content, reasoning, fragments = [], [], {}
        dsml_guard = DsmlStreamGuard()
        first, finished = True, False
        async with self._http_client() as client:
            async with client.stream(
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
                            safe_piece = dsml_guard.feed(delta["content"])
                            if safe_piece:
                                content.append(safe_piece)
                                if ctx.depth == 0:
                                    await ctx.emit("token", {"text": safe_piece})
                        for part in delta.get("tool_calls") or []:
                            item = fragments.setdefault(part["index"], {"id": "", "name": "", "args": ""})
                            fn = part.get("function") or {}
                            item["id"] += part.get("id") or ""
                            item["name"] += fn.get("name") or ""
                            item["args"] += fn.get("arguments") or ""
                        if choice.get("finish_reason") in ("stop", "tool_calls"):
                            finished = True

        tail_piece = dsml_guard.flush()
        if tail_piece:
            content.append(tail_piece)
            if ctx.depth == 0:
                await ctx.emit("token", {"text": tail_piece})

        if dsml_guard.triggered:
            log.warning("Intercepted leaked DSML markup in model stream for trace=%s", ctx.trace_id)

        if not finished:
            raise ValueError("incomplete model response")
        calls = [ToolCall(c["id"], c["name"], json.loads(c["args"] or "{}")) for _, c in sorted(fragments.items())]
        final_text = "".join(content).rstrip() if dsml_guard.triggered else "".join(content)
        return Message(
            "assistant",
            final_text,
            calls,
            metadata={"reasoning_content": "".join(reasoning)} if calls and reasoning else {},
        )
