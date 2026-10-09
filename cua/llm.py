"""LLM decision interface for discovery. The model is consulted *only* during discovery.

The agent loop is provider-agnostic: it hands `decide()` a system prompt, a user turn (text, plus an
optional masked screenshot) and the tool schema, and gets back exactly one tool call. The OpenAI
implementation uses Chat Completions function calling with `tool_choice="required"`.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    name: str
    args: dict
    usage: dict = field(default_factory=dict)


class DecisionModel(Protocol):
    model: str

    def decide(self, system: str, user_text: str, tools: list[dict], screenshot_png: bytes | None = None) -> ToolCall: ...


class OpenAIModel:
    def __init__(self, model: str | None = None):
        from openai import OpenAI

        self.client = OpenAI()
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1")

    def decide(self, system: str, user_text: str, tools: list[dict], screenshot_png: bytes | None = None) -> ToolCall:
        content: list[dict] | str = user_text
        if screenshot_png is not None:
            b64 = base64.b64encode(screenshot_png).decode()
            content = [{"type": "text", "text": user_text}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]
        kwargs: dict = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            tools=tools,
            tool_choice="required",
            parallel_tool_calls=False,
        )
        if self.model.startswith(("gpt-4", "gpt-3.5")):
            kwargs["temperature"] = 0  # reasoning models reject a non-default temperature
        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            raise RuntimeError(f"model returned no tool call: {msg.content!r}")
        tc = msg.tool_calls[0]
        usage = resp.usage.model_dump() if resp.usage else {}
        return ToolCall(tc.function.name, json.loads(tc.function.arguments or "{}"), usage)


class ScriptedModel:
    """Deterministic stand-in used by tests only (never by `cua discover`)."""

    model = "scripted-test-double"

    def __init__(self, script):
        self.script = script  # callable(observation_text, turn) -> ToolCall
        self.turn = 0

    def decide(self, system, user_text, tools, screenshot_png=None) -> ToolCall:
        self.turn += 1
        return self.script(user_text, self.turn)
