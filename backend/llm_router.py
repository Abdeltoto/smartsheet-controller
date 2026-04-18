import json
import logging
from typing import Any, AsyncIterator

import openai
import anthropic

log = logging.getLogger(__name__)


def _safe_parse_args(raw: str | None) -> dict:
    """Parse tool-call arguments tolerantly.
    Returns {"__parse_error__": "..."} on JSON failure so the agent can recover."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("Tool call arguments JSON parse failed: %s | raw=%r", e, raw[:200])
        return {"__parse_error__": str(e), "__raw__": raw[:500]}

PROVIDERS = {
    "openai": {
        "base_url": None,
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "o4-mini"],
    },
    "anthropic": {
        "base_url": None,
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
        "models": ["claude-sonnet-4-20250514", "claude-haiku-3-20250618", "claude-opus-4-20250514"],
    },
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GOOGLE_API_KEY",
        "default_model": "gemini-2.0-flash",
        "models": ["gemini-2.0-flash", "gemini-2.5-pro-preview-05-06", "gemini-2.5-flash-preview-04-17"],
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it"],
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "env_key": "MISTRAL_API_KEY",
        "default_model": "mistral-large-latest",
        "models": ["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest", "codestral-latest"],
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4-20250514",
        "models": ["anthropic/claude-sonnet-4-20250514", "openai/gpt-4o", "google/gemini-2.0-flash-001", "meta-llama/llama-3.3-70b-instruct"],
    },
}


def get_provider_info() -> dict:
    return {
        name: {"default_model": p["default_model"], "models": p["models"], "env_key": p["env_key"]}
        for name, p in PROVIDERS.items()
    }


class LLMRouter:
    def __init__(self, provider: str, model: str, api_key: str):
        self.provider = provider.lower()
        self.model = model.strip() if model else ""
        self._api_key = api_key
        # Per-router cumulative token usage (lifetime of the session)
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "calls": 0,
            "by_model": {},  # model -> {input, output, calls}
            "last_call": None,  # {input, output, model, ts}
        }

        info = PROVIDERS.get(self.provider)
        if not info:
            raise ValueError(f"Unsupported provider: {provider}. Supported: {', '.join(PROVIDERS.keys())}")

        if not self.model:
            self.model = info["default_model"]

        if self.provider == "anthropic":
            self.anthropic_client = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            kwargs: dict[str, Any] = {"api_key": api_key}
            if info["base_url"]:
                kwargs["base_url"] = info["base_url"]
            self.openai_client = openai.AsyncOpenAI(**kwargs)

    def _record_usage(self, input_tokens: int, output_tokens: int) -> None:
        import time as _t
        self.usage["input_tokens"] += input_tokens
        self.usage["output_tokens"] += output_tokens
        self.usage["calls"] += 1
        bm = self.usage["by_model"].setdefault(self.model, {"input": 0, "output": 0, "calls": 0})
        bm["input"] += input_tokens
        bm["output"] += output_tokens
        bm["calls"] += 1
        self.usage["last_call"] = {
            "model": self.model,
            "provider": self.provider,
            "input": input_tokens,
            "output": output_tokens,
            "ts": _t.time(),
        }
        log.info("LLM tokens [%s/%s]: in=%d out=%d (cum in=%d out=%d)",
                 self.provider, self.model, input_tokens, output_tokens,
                 self.usage["input_tokens"], self.usage["output_tokens"])

    def switch_model(self, model: str):
        self.model = model.strip()

    def _is_openai_compat(self) -> bool:
        return self.provider != "anthropic"

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, system: str = "") -> dict:
        if self._is_openai_compat():
            return await self._openai_chat(messages, tools, system)
        return await self._anthropic_chat(messages, tools, system)

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None, system: str = "") -> AsyncIterator[dict]:
        if self._is_openai_compat():
            async for chunk in self._openai_chat_stream(messages, tools, system):
                yield chunk
        else:
            async for chunk in self._anthropic_chat_stream(messages, tools, system):
                yield chunk

    # ──────────── OpenAI-compatible (OpenAI, Google, Groq, Mistral, DeepSeek, OpenRouter) ────────────

    def _build_openai_messages(self, messages: list[dict], system: str) -> list[dict]:
        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        for msg in messages:
            if msg["role"] == "tool_result":
                api_messages.append({"role": "tool", "tool_call_id": msg["tool_call_id"], "content": msg["content"]})
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                api_messages.append(msg)
            else:
                api_messages.append({"role": msg["role"], "content": msg["content"]})
        return api_messages

    async def _openai_chat(self, messages: list[dict], tools: list[dict] | None, system: str) -> dict:
        api_messages = self._build_openai_messages(messages, system)
        kwargs: dict[str, Any] = {"model": self.model, "messages": api_messages}
        if tools:
            kwargs["tools"] = [_to_openai_tool(t) for t in tools]

        response = await self.openai_client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        if usage:
            self._record_usage(getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)

        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            tool_calls = []
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": _safe_parse_args(tc.function.arguments),
                })
            return {
                "type": "tool_calls",
                "tool_calls": tool_calls,
                "raw_message": {
                    "role": "assistant",
                    "content": choice.message.content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in choice.message.tool_calls
                    ],
                },
            }
        return {"type": "text", "content": choice.message.content or ""}

    async def _openai_chat_stream(self, messages: list[dict], tools: list[dict] | None, system: str) -> AsyncIterator[dict]:
        api_messages = self._build_openai_messages(messages, system)
        kwargs: dict[str, Any] = {"model": self.model, "messages": api_messages, "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            kwargs["tools"] = [_to_openai_tool(t) for t in tools]

        try:
            stream = await self.openai_client.chat.completions.create(**kwargs)
        except (openai.BadRequestError, openai.UnprocessableEntityError, TypeError) as e:
            # Provider doesn't accept stream_options — retry without it
            log.debug("Provider rejected stream_options, retrying without: %s", e)
            kwargs.pop("stream_options", None)
            stream = await self.openai_client.chat.completions.create(**kwargs)

        text_content = ""
        tool_calls_data: dict[int, dict] = {}

        async for chunk in stream:
            # Final chunk with usage stats has no choices but has usage attribute
            if getattr(chunk, "usage", None):
                u = chunk.usage
                self._record_usage(getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            if delta.content:
                text_content += delta.content
                yield {"type": "stream_delta", "content": delta.content}

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_data:
                        tool_calls_data[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        tool_calls_data[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_data[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_data[idx]["arguments"] += tc_delta.function.arguments

        if tool_calls_data:
            tool_calls = []
            for idx in sorted(tool_calls_data.keys()):
                tc = tool_calls_data[idx]
                tool_calls.append({"id": tc["id"], "name": tc["name"], "arguments": _safe_parse_args(tc["arguments"])})
            raw_msg = {
                "role": "assistant",
                "content": text_content or None,
                "tool_calls": [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tool_calls_data[idx]["arguments"]}}
                    for idx, tc in enumerate(tool_calls)
                ],
            }
            yield {"type": "tool_calls", "tool_calls": tool_calls, "raw_message": raw_msg}
        else:
            yield {"type": "stream_end", "content": text_content}

    # ──────────── Anthropic ────────────

    def _build_anthropic_messages(self, messages: list[dict]) -> list[dict]:
        api_messages = []
        for msg in messages:
            if msg["role"] == "tool_result":
                api_messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": msg["content"]}],
                })
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                content_blocks = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    content_blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]})
                api_messages.append({"role": "assistant", "content": content_blocks})
            else:
                api_messages.append({"role": msg["role"], "content": msg["content"]})
        return api_messages

    async def _anthropic_chat(self, messages: list[dict], tools: list[dict] | None, system: str) -> dict:
        api_messages = self._build_anthropic_messages(messages)
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": 8192, "messages": api_messages}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [_to_anthropic_tool(t) for t in tools]

        response = await self.anthropic_client.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        if usage:
            self._record_usage(getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
        tool_calls = []
        text_parts = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input})
            elif block.type == "text":
                text_parts.append(block.text)

        if tool_calls:
            raw_msg = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None, "tool_calls": tool_calls}
            return {"type": "tool_calls", "tool_calls": tool_calls, "raw_message": raw_msg}
        return {"type": "text", "content": "\n".join(text_parts)}

    async def _anthropic_chat_stream(self, messages: list[dict], tools: list[dict] | None, system: str) -> AsyncIterator[dict]:
        api_messages = self._build_anthropic_messages(messages)
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": 8192, "messages": api_messages}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [_to_anthropic_tool(t) for t in tools]

        async with self.anthropic_client.messages.stream(**kwargs) as stream:
            text_content = ""
            tool_calls = []
            current_tool: dict | None = None

            async for event in stream:
                if event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        current_tool = {"id": event.content_block.id, "name": event.content_block.name, "arguments": ""}
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        text_content += event.delta.text
                        yield {"type": "stream_delta", "content": event.delta.text}
                    elif event.delta.type == "input_json_delta" and current_tool:
                        current_tool["arguments"] += event.delta.partial_json
                elif event.type == "content_block_stop":
                    if current_tool:
                        current_tool["arguments"] = _safe_parse_args(current_tool["arguments"])
                        tool_calls.append(current_tool)
                        current_tool = None

            # Capture final usage from the stream
            try:
                final_msg = await stream.get_final_message()
                u = getattr(final_msg, "usage", None)
                if u:
                    self._record_usage(getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)
            except Exception as e:
                log.debug("Anthropic usage capture failed: %s", e)

            if tool_calls:
                raw_msg = {"role": "assistant", "content": text_content or None, "tool_calls": tool_calls}
                yield {"type": "tool_calls", "tool_calls": tool_calls, "raw_message": raw_msg}
            else:
                yield {"type": "stream_end", "content": text_content}


def _to_openai_tool(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        },
    }


def _to_anthropic_tool(tool: dict) -> dict:
    return {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": tool["parameters"],
    }
