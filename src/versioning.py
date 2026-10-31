"""App/data version fingerprints for the session header event (spec §17, D15)."""

import hashlib
import json
import subprocess
from pathlib import Path

from src.config import DATA_DIR, ROOT

SOURCES = ("chat", "script", "example", "eval")


def app_version(run=subprocess.run) -> dict:
    """Current git commit and whether the worktree has uncommitted changes.

    Any failure (non-zero exit, exception) collapses to an "unknown" result rather
    than raising, so a header can always be written even without git available.
    """
    try:
        rev = run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        status = run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    except Exception:
        return {"commit": "unknown", "dirty": None}
    if rev.returncode != 0 or status.returncode != 0:
        return {"commit": "unknown", "dirty": None}
    return {"commit": rev.stdout.strip(), "dirty": bool(status.stdout.strip())}


def sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def data_sha(data_dir: Path = DATA_DIR) -> str:
    """SHA-256 over every file's (relative path, bytes) under `data_dir`, sorted by path. Each
    part is prefixed with its length, so moving bytes between a name and its content changes the hash."""
    hasher = hashlib.sha256()
    paths = sorted(p for p in data_dir.rglob("*") if p.is_file())
    for path in paths:
        for part in (path.relative_to(data_dir).as_posix().encode("utf-8"), path.read_bytes()):
            hasher.update(f"{len(part)}:".encode("ascii"))
            hasher.update(part)
    return hasher.hexdigest()[:12]


def build_header(session_id: str, source: str, backend_label: str, system: str, tools: list[dict],
                  now_override: str | None, run=subprocess.run, data_dir: Path = DATA_DIR) -> dict:
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source!r}")
    backend, _, model = backend_label.partition(":")
    return {
        "schema": 1,
        "session": session_id,
        "source": source,
        "app": app_version(run),
        "backend": backend,
        "model": model,
        "prompt_sha": sha12(system),
        "tools_sha": sha12(json.dumps(tools, sort_keys=True, ensure_ascii=False)),
        "data_sha": data_sha(data_dir),
        "now_override": now_override,
    }
