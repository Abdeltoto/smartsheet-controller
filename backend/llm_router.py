import json
from typing import Any, AsyncIterator

import openai
import anthropic


class LLMRouter:
    def __init__(self, provider: str, model: str, api_key: str):
        self.provider = provider.lower()
        self.model = model.strip() if model else ""
        if not self.model:
            self.model = "gpt-4o" if self.provider == "openai" else "claude-sonnet-4-20250514"
        if self.provider == "openai":
            self.openai_client = openai.AsyncOpenAI(api_key=api_key)
        elif self.provider == "anthropic":
            self.anthropic_client = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, system: str = "") -> dict:
        if self.provider == "openai":
            return await self._openai_chat(messages, tools, system)
        else:
            return await self._anthropic_chat(messages, tools, system)

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None, system: str = "") -> AsyncIterator[dict]:
        if self.provider == "openai":
            async for chunk in self._openai_chat_stream(messages, tools, system):
                yield chunk
        else:
            async for chunk in self._anthropic_chat_stream(messages, tools, system):
                yield chunk

    async def _openai_chat(self, messages: list[dict], tools: list[dict] | None, system: str) -> dict:
        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})

        for msg in messages:
            if msg["role"] == "tool_result":
                api_messages.append({
                    "role": "tool",
                    "tool_call_id": msg["tool_call_id"],
                    "content": msg["content"],
                })
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                api_messages.append(msg)
            else:
                api_messages.append({"role": msg["role"], "content": msg["content"]})

        kwargs: dict[str, Any] = {"model": self.model, "messages": api_messages}
        if tools:
            kwargs["tools"] = [_to_openai_tool(t) for t in tools]

        response = await self.openai_client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        if choice.finish_reason == "tool_calls":
            tool_calls = []
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
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

    async def _anthropic_chat(self, messages: list[dict], tools: list[dict] | None, system: str) -> dict:
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
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["arguments"],
                    })
                api_messages.append({"role": "assistant", "content": content_blocks})
            else:
                api_messages.append({"role": msg["role"], "content": msg["content"]})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [_to_anthropic_tool(t) for t in tools]

        response = await self.anthropic_client.messages.create(**kwargs)

        tool_calls = []
        text_parts = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input})
            elif block.type == "text":
                text_parts.append(block.text)

        if tool_calls:
            raw_msg = {
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
                "tool_calls": tool_calls,
            }
            return {"type": "tool_calls", "tool_calls": tool_calls, "raw_message": raw_msg}
        return {"type": "text", "content": "\n".join(text_parts)}


    async def _openai_chat_stream(self, messages: list[dict], tools: list[dict] | None, system: str) -> AsyncIterator[dict]:
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

        kwargs: dict[str, Any] = {"model": self.model, "messages": api_messages, "stream": True}
        if tools:
            kwargs["tools"] = [_to_openai_tool(t) for t in tools]

        stream = await self.openai_client.chat.completions.create(**kwargs)

        text_content = ""
        tool_calls_data: dict[int, dict] = {}
        finish_reason = None

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            finish_reason = chunk.choices[0].finish_reason if chunk.choices else finish_reason
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
                tool_calls.append({
                    "id": tc["id"],
                    "name": tc["name"],
                    "arguments": json.loads(tc["arguments"]),
                })
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

    async def _anthropic_chat_stream(self, messages: list[dict], tools: list[dict] | None, system: str) -> AsyncIterator[dict]:
        api_messages = []
        for msg in messages:
            if msg["role"] == "tool_result":
                api_messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": msg["content"]}]})
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                content_blocks = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    content_blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]})
                api_messages.append({"role": "assistant", "content": content_blocks})
            else:
                api_messages.append({"role": msg["role"], "content": msg["content"]})

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
                        current_tool["arguments"] = json.loads(current_tool["arguments"]) if current_tool["arguments"] else {}
                        tool_calls.append(current_tool)
                        current_tool = None

            if tool_calls:
                raw_msg = {
                    "role": "assistant",
                    "content": text_content or None,
                    "tool_calls": tool_calls,
                }
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
