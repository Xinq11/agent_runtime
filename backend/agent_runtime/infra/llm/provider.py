"""OpenAI-compatible streaming LLM provider with safe transient retries and cache telemetry."""

from __future__ import annotations

import asyncio
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

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_SEC = 10.0

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
    """Streaming OpenAI-compatible provider (DeepSeek / OpenAI / Qwen / self-hosted gateways)."""

    def __init__(
        self,
        client: Optional[httpx.AsyncClient],
        obs: Any,
        *,
        settings: Optional[ModelSettings] = None,
        max_retries: Optional[int] = None,
        retry_base_delay_sec: float = 0.5,
        timeout_sec: Optional[float] = None,
    ) -> None:
        self.config = settings or ModelSettings(os.getenv("DEEPSEEK_API_KEY", ""))
        self.client = client
        self.obs = obs
        self.max_retries = (
            max(0, int(max_retries))
            if max_retries is not None
            else max(0, int(os.getenv("AGENT_LLM_MAX_RETRIES", "2")))
        )
        self.retry_base_delay_sec = max(0.0, float(retry_base_delay_sec))
        self.timeout_sec = (
            max(1.0, float(timeout_sec))
            if timeout_sec is not None
            else max(1.0, float(os.getenv("AGENT_LLM_TIMEOUT_SEC", "90")))
        )

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

    def _observe_cache_ratio(self, mode: str, prompt_tokens: int, cache_read: int) -> None:
        if prompt_tokens <= 0:
            return
        ratio = max(0.0, min(1.0, float(cache_read) / float(prompt_tokens)))
        metric = getattr(self.obs, "prompt_cache_hit_ratio", None)
        if metric is not None:
            metric.labels(mode).observe(ratio)

    def _record_retry(self, mode: str, reason: str, attempt: int, ctx: ExecutionContext) -> None:
        metric = getattr(self.obs, "llm_retries", None)
        if metric is not None:
            metric.labels(mode, reason).inc()
        log_fn = getattr(self.obs, "log_event", None)
        if log_fn is not None:
            log_fn("llm_retry", ctx.trace_id, ctx.thread_id, mode=mode, reason=reason, attempt=attempt)

    def _retry_delay(self, attempt: int, response: Optional[httpx.Response] = None) -> float:
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    return max(0.0, min(_MAX_RETRY_AFTER_SEC, float(raw)))
                except ValueError:
                    pass
        return min(_MAX_RETRY_AFTER_SEC, self.retry_base_delay_sec * (2 ** attempt))

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
                "未配置模型 API Key，请在 .env 设置 DEEPSEEK_API_KEY 或点击左下角设置填写密钥后再对话。"
            )

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

        for attempt in range(self.max_retries + 1):
            if ctx.check_cancelled():
                raise asyncio.CancelledError("Run cancelled before LLM request")
            start = time.monotonic()
            content, reasoning, fragments = [], [], {}
            dsml_guard = DsmlStreamGuard()
            first, finished, emitted_any = True, False, False
            try:
                async with self._http_client() as client:
                    async with client.stream(
                        "POST",
                        cfg.base_url + "/chat/completions",
                        json=payload,
                        headers={"Authorization": "Bearer " + cfg.api_key},
                        timeout=self.timeout_sec,
                    ) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if ctx.check_cancelled():
                                raise asyncio.CancelledError("Run cancelled during LLM stream")
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
                                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                                completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                                cache_read = int(
                                    usage.get(
                                        "prompt_cache_hit_tokens",
                                        (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                                    )
                                    or 0
                                )
                                for kind, count in (
                                    ("in", prompt_tokens),
                                    ("out", completion_tokens),
                                    ("cache_read", cache_read),
                                ):
                                    self._inc_tokens(kind, mode, count)
                                self._observe_cache_ratio(mode, prompt_tokens, cache_read)
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
                                        emitted_any = True
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
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                if (
                    not emitted_any
                    and status_code in _RETRYABLE_STATUS_CODES
                    and attempt < self.max_retries
                ):
                    reason = f"http_{status_code}"
                    self._record_retry(mode, reason, attempt + 1, ctx)
                    delay = self._retry_delay(attempt, exc.response)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if not emitted_any and attempt < self.max_retries:
                    reason = "timeout" if isinstance(exc, httpx.TimeoutException) else "network"
                    self._record_retry(mode, reason, attempt + 1, ctx)
                    delay = self._retry_delay(attempt)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                raise

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

        raise RuntimeError("unreachable retry loop exit")
