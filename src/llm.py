"""Model backends behind one call() interface.

SdkBackend talks to the Anthropic API directly and is the production path.
CliBackend shells out to the locally installed `claude -p` CLI and is a dev
stand-in for boxes with no API key: it has no native tool-use protocol, so
tool calls are emulated by asking the model to reply with a JSON envelope
listing them, and its usage numbers come from the CLI's own accounting, not
the SDK's — they are not comparable to a production run's.
"""

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Mapping
from uuid import uuid4

from src.config import ROOT, Settings

CLI_RETRY_NOTICE = "\n\nYour previous reply was not valid JSON. Reply with the JSON object only."

TOOLS_INSTRUCTION = (
    'Reply with ONE JSON object and nothing else, no code fence: '
    '{"text": string or null, "tool_calls": [{"name": string, "input": object}]}. '
    "To call tools, put them in tool_calls and set text to null; you will get "
    "their results in the next message. To answer the customer, set text and "
    "leave tool_calls empty."
)


class LLMError(Exception):
    """Raised when a backend fails to produce a usable Turn."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class Turn:
    text: str | None
    tool_calls: list[ToolCall]
    usage: Usage
    content: list[dict]
    model: str | None = None


def _content_from_tool_calls(tool_calls: list[ToolCall]) -> list[dict]:
    return [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in tool_calls]


class SdkBackend:
    def __init__(self, api_key: str, model: str, max_tokens: int = 1024, client=None):
        self.model = model
        self.max_tokens = max_tokens
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        self.client = client

    @property
    def label(self) -> str:
        return f"sdk:{self.model}"

    def call(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                messages=messages,
            )
        except Exception as exc:
            raise LLMError(str(exc)) from exc

        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        content: list[dict] = []
        for block in resp.content:
            if block.type == "text":
                texts.append(block.text)
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))
                content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})

        usage = Usage(
            input=getattr(resp.usage, "input_tokens", 0) or 0,
            output=getattr(resp.usage, "output_tokens", 0) or 0,
            cache_read=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        )
        return Turn(text="\n".join(texts) if texts else None, tool_calls=tool_calls, usage=usage, content=content,
                    model=getattr(resp, "model", None))


def render_conversation(messages: list[dict]) -> str:
    """Render an Anthropic-format message history as plain text for the CLI prompt."""
    tool_names: dict[str, str] = {}
    lines: list[str] = []
    for message in messages:
        content = message["content"]
        role = message["role"]
        if isinstance(content, str):
            if role == "user" and content.startswith("[verificação automática]"):
                lines.append(f"SISTEMA: {content}")
            elif role == "user":
                lines.append(f"CLIENTE: {content}")
            else:
                lines.append(f"VOCÊ: {content}")
            continue
        for block in content:
            if block["type"] == "text":
                lines.append(f"VOCÊ: {block['text']}" if role == "assistant" else f"CLIENTE: {block['text']}")
            elif block["type"] == "tool_use":
                tool_names[block["id"]] = block["name"]
                lines.append(f"VOCÊ CHAMOU {block['name']} {json.dumps(block['input'], ensure_ascii=False)}")
            elif block["type"] == "tool_result":
                name = tool_names.get(block["tool_use_id"], block["tool_use_id"])
                lines.append(f"RESULTADO {name}: {block['content']}")
    return "\n\n".join(lines)


def extract_json(text: str) -> dict:
    """Parse text as JSON; if that fails, parse the slice from the first '{' to the last '}'."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise
        return json.loads(text[start:end + 1])


def cli_env(base: Mapping[str, str], home: str | None = None) -> dict[str, str]:
    """The environment the claude CLI gets: no API key (forces subscription login,
    never API billing), no inherited effort override, and thinking off. `home`
    replaces HOME and USERPROFILE, so the CLI cannot read the account owner's
    profile (it puts their e-mail into the model's context)."""
    env = dict(base)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_EFFORT", None)
    env["MAX_THINKING_TOKENS"] = "0"
    if home is not None:
        env["HOME"] = home
        env["USERPROFILE"] = home
    return env


class CliBackend:
    def __init__(self, model: str, runner=subprocess.run, timeout: int = 240):
        self.model = model
        self.runner = runner
        self.timeout = timeout
        self._empty_home: str | None = None

    @property
    def label(self) -> str:
        return f"cli:{self.model}"

    def _home(self) -> str | None:
        """An empty temp home, once per instance, but only with CLAUDE_CODE_OAUTH_TOKEN:
        without the token the CLI's login lives in the real home and must stay reachable."""
        if "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ:
            return None
        if self._empty_home is None:
            self._empty_home = tempfile.mkdtemp(prefix="duda-cli-home-")
        return self._empty_home

    def _build_prompt(self, messages: list[dict], tools: list[dict]) -> str:
        tool_lines = "\n".join(
            json.dumps({"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}, ensure_ascii=False)
            for t in tools
        )
        return (
            render_conversation(messages)
            + "\n\n# Ferramentas disponíveis\n"
            + tool_lines
            + ("\n" if tool_lines else "")
            + TOOLS_INSTRUCTION
        )

    def _run(self, system: str, prompt: str) -> subprocess.CompletedProcess:
        argv = [
            "claude", "-p",
            "--model", self.model,
            "--output-format", "json",
            "--system-prompt", system,
            "--tools", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            prompt,
        ]
        proc = self.runner(argv, capture_output=True, text=True, timeout=self.timeout, cwd=str(ROOT), env=cli_env(os.environ, self._home()))
        if proc.returncode != 0:
            raise LLMError(f"claude cli exited {proc.returncode}: {proc.stderr[-500:]}")
        return proc

    def call(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        prompt = self._build_prompt(messages, tools)
        proc = self._run(system, prompt)
        envelope = json.loads(proc.stdout)
        try:
            data = extract_json(envelope.get("result", ""))
        except (ValueError, json.JSONDecodeError):
            proc = self._run(system, prompt + CLI_RETRY_NOTICE)
            envelope = json.loads(proc.stdout)
            try:
                data = extract_json(envelope.get("result", ""))
            except (ValueError, json.JSONDecodeError) as exc:
                raise LLMError(f"claude cli returned non-JSON reply: {envelope.get('result', '')!r}") from exc

        usage_raw = envelope.get("usage") or {}
        usage = Usage(
            input=int(usage_raw.get("input_tokens") or 0),
            output=int(usage_raw.get("output_tokens") or 0),
        )
        tool_calls = [
            ToolCall(id=f"cli_{uuid4().hex[:12]}", name=tc["name"], input=tc["input"])
            for tc in data.get("tool_calls") or []
        ]
        if not tools and "text" not in data and "tool_calls" not in data:
            text = envelope.get("result", "")  # a tool-less caller (the judge) asked for its own JSON shape
        else:
            text = data.get("text")
        content = ([{"type": "text", "text": text}] if text else []) + _content_from_tool_calls(tool_calls)
        return Turn(text=text, tool_calls=tool_calls, usage=usage, content=content)


def make_backend(settings: Settings) -> SdkBackend | CliBackend:
    if settings.dev:
        return CliBackend(settings.dev_model)
    if not settings.api_key:
        raise LLMError("LLM_API_KEY is empty; set it in .env or use LLM_DEV=true")
    return SdkBackend(settings.api_key, settings.model)


def make_judge_backend(settings: Settings) -> SdkBackend | CliBackend:
    if settings.dev:
        return CliBackend(settings.dev_judge_model)
    if not settings.api_key:
        raise LLMError("LLM_API_KEY is empty; set it in .env or use LLM_DEV=true")
    return SdkBackend(settings.api_key, settings.judge_model, max_tokens=512)


def judge_model_name(settings: Settings) -> str:
    """The judge model name recorded on a `labels` event, matching whichever backend
    `make_judge_backend` would build."""
    return settings.dev_judge_model if settings.dev else settings.judge_model
