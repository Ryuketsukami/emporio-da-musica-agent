"""Rebuilds outcome rates per version group from session JSONL logs (spec §17, D17).

Rates are recomputed from the raw events every run, never accumulated, so a rubric change
or a backfilled label is reflected the next time this runs. `--label` fills in missing
judge labels first; the summary itself only reads what is already on disk.
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src.config import RUNTIME_DIR, SESSIONS_DIR, load_settings
from src.eventlog import read_events
from src.labels import RESOLUTIONS, RUBRIC_VERSION, label_session, latest_labels
from src.llm import judge_model_name, make_judge_backend

GROUP_FIELDS = ("commit", "prompt_sha", "tools_sha", "data_sha", "model")


def load_sessions(dirs: list[Path]) -> list[dict]:
    """Each `*.jsonl` file in `dirs` becomes `{"path", "header", "labels"}`; files
    without a header event are skipped."""
    sessions = []
    for directory in dirs:
        for path in sorted(directory.glob("*.jsonl")):
            events = read_events(path)
            header = next((e for e in events if e.get("kind") == "header"), None)
            if header is None:
                continue
            sessions.append({"path": path, "header": header, "labels": latest_labels(events), "spend": spend(events)})
    return sessions


def spend(events: list[dict]) -> dict:
    """What Duda's model calls cost in one session (the judge's cost is on its label). `cost_usd` is None
    when a call had no price or the log predates cost tracking."""
    answers = [e for e in events if e.get("kind") in ("reply", "fallback", "error")]
    costs = [e.get("cost_usd") for e in answers]
    known = bool(costs) and all(isinstance(c, (int, float)) for c in costs)
    return {"cost_usd": sum(costs) if known else None,
            "messages": sum(1 for e in events if e.get("kind") == "customer")}


def _version_key(header: dict) -> tuple:
    app = header.get("app") or {}
    commit = (app.get("commit") or "unknown")[:7]
    if app.get("dirty"):
        commit += "-dirty"
    return commit, header.get("prompt_sha"), header.get("tools_sha"), header.get("data_sha"), header.get("model")


def _has_current_judge_label(labels: dict | None) -> bool:
    """A judge label made with the current rubric, without an error."""
    if labels is None or labels.get("rubric_version") != RUBRIC_VERSION:
        return False
    judge = labels.get("judge")
    return isinstance(judge, dict) and "error" not in judge


def _rate(count: int, total: int) -> float | None:
    return None if total == 0 else round(count / total, 3)


def summarize(sessions: list[dict], include_all: bool = False) -> dict:
    kept = [s for s in sessions if include_all or s["header"].get("source") == "chat"]

    groups: dict[tuple, list[dict]] = {}
    for s in kept:
        groups.setdefault(_version_key(s["header"]), []).append(s)

    result_groups = []
    for key in sorted(groups, key=lambda k: tuple("" if v is None else v for v in k)):
        group = groups[key]
        labelled = [s for s in group if s["labels"] is not None]
        judged = [s for s in labelled if _has_current_judge_label(s["labels"])]

        resolution = {
            name: _rate(sum(1 for s in judged if s["labels"]["judge"]["resolution"] == name), len(judged))
            for name in RESOLUTIONS
        }
        frustrated = sum(1 for s in judged if s["labels"]["judge"]["frustration"] >= 1)
        fallbacks = sum(1 for s in labelled if s["labels"]["signals"]["fallbacks"] >= 1)
        guard_hits = sum(1 for s in labelled if s["labels"]["signals"]["guard_hits"] >= 1)
        tech_errors = sum(1 for s in labelled if s["labels"]["signals"]["tech_errors"] >= 1)
        handoff_tickets = sum(s["labels"]["signals"]["tickets"]["human"] for s in labelled)
        priced = [s["spend"] for s in group if s["spend"]["cost_usd"] is not None and s["spend"]["messages"]]
        priced_cost = sum(p["cost_usd"] for p in priced)
        priced_messages = sum(p["messages"] for p in priced)

        result_groups.append({
            "version": dict(zip(GROUP_FIELDS, key)),
            "sessions": len(group),
            "labelled": len(labelled),
            "judged": len(judged),
            "resolution": resolution,
            "frustrated_rate": _rate(frustrated, len(judged)),
            "fallback_rate": _rate(fallbacks, len(labelled)),
            "guard_hit_rate": _rate(guard_hits, len(labelled)),
            "tech_error_rate": _rate(tech_errors, len(labelled)),
            "handoff_tickets": handoff_tickets,
            "priced_sessions": len(priced),
            "cost_per_session": round(priced_cost / len(priced), 6) if priced else None,
            "cost_per_message": round(priced_cost / priced_messages, 6) if priced else None,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filter": "all" if include_all else "chat",
        "groups": result_groups,
    }


def _print_table(summary: dict) -> None:
    for group in summary["groups"]:
        version = "/".join(str(group["version"][field]) for field in GROUP_FIELDS)
        print(
            f"{version}  sessions={group['sessions']} labelled={group['labelled']} judged={group['judged']} "
            f"resolved={group['resolution']['resolved']} frustrated_rate={group['frustrated_rate']} "
            f"fallback_rate={group['fallback_rate']} guard_hit_rate={group['guard_hit_rate']} "
            f"tech_error_rate={group['tech_error_rate']} handoff_tickets={group['handoff_tickets']} "
            f"cost_per_session={group['cost_per_session']} cost_per_message={group['cost_per_message']}"
        )


def _label_missing(directory: Path, judge_backend) -> None:
    settings = load_settings()
    judge = judge_backend if judge_backend is not None else make_judge_backend(settings)
    judge_model = judge_model_name(settings)
    for path in sorted(directory.glob("*.jsonl")):
        events = read_events(path)
        if not any(e.get("kind") == "end" for e in events):  # still running, or crashed
            continue
        if _has_current_judge_label(latest_labels(events)):
            continue
        label_session(path, judge, judge_model)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.report")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--label", action="store_true")
    parser.add_argument("--dir", type=Path, default=SESSIONS_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, judge_backend=None) -> int:
    args = _parse_args(argv)

    if args.label:
        _label_missing(args.dir, judge_backend)

    summary = summarize(load_sessions([args.dir]), include_all=args.all)
    _print_table(summary)

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = RUNTIME_DIR / "summary.json.tmp"
    final_path = RUNTIME_DIR / "summary.json"
    tmp_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, final_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
