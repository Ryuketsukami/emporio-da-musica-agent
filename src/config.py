"""Settings loader: reads .env (if present), lets environment variables override it."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RUNTIME_DIR = ROOT / "_runtime"
SESSIONS_DIR = RUNTIME_DIR / "sessions"
TICKETS_PATH = RUNTIME_DIR / "tickets.jsonl"
PROMPT_FLAGS_PATH = ROOT / "config" / "prompt_flags.toml"
# Prompt flags used when LLM_PROMPT_FLAGS is not set at all; an empty LLM_PROMPT_FLAGS turns them all off.
# All three on: across 6 SDK eval runs per setting they removed the e13/e11 misses (see evals/RESULTS.md).
DEFAULT_PROMPT_FLAGS: tuple[str, ...] = ("call_tools_directly", "quote_does_all_math", "quick_human_handoff")

TRUE_VALUES = {"1", "true", "yes"}

_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    dev: bool
    dev_model: str
    judge: bool = False
    judge_model: str = "claude-haiku-4-5"
    dev_judge_model: str = "haiku"
    prompt_flags: tuple[str, ...] = DEFAULT_PROMPT_FLAGS


def parse_env(text: str) -> dict:
    """Parse KEY=VALUE lines, skipping blanks and comment-only lines, stripping
    inline comments (whitespace followed by '#') and one pair of matching quotes."""
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, raw_value = stripped.partition("=")
        key = key.strip()
        value = _INLINE_COMMENT_RE.sub("", raw_value).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_settings(env_path: Path = ROOT / ".env", environ: Mapping[str, str] | None = None) -> Settings:
    values = parse_env(env_path.read_text(encoding="utf-8")) if env_path.is_file() else {}
    source = os.environ if environ is None else environ
    values.update({k: v for k, v in source.items() if k.startswith("LLM_")})
    return Settings(
        api_key=values.get("LLM_API_KEY", ""),
        model=values.get("LLM_MODEL") or "claude-sonnet-5",
        dev=values.get("LLM_DEV", "").strip().lower() in TRUE_VALUES,
        dev_model=values.get("LLM_DEV_MODEL") or "sonnet",
        judge=values.get("LLM_JUDGE", "").strip().lower() in TRUE_VALUES,
        judge_model=values.get("LLM_JUDGE_MODEL") or "claude-haiku-4-5",
        dev_judge_model=values.get("LLM_DEV_JUDGE_MODEL") or "haiku",
        prompt_flags=(tuple(f.strip() for f in values["LLM_PROMPT_FLAGS"].split(",") if f.strip())
                      if "LLM_PROMPT_FLAGS" in values else DEFAULT_PROMPT_FLAGS),
    )
