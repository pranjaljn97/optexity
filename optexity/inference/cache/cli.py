"""Offline CLI for the learning cache.

Two commands (``python -m optexity.inference.cache.cli <cmd>``), neither needs a browser:

  * ``compile`` — compile a browser-use trace into a deterministic automation JSON.
  * ``metrics`` — read tokens / fallback-count / wall-clock from a finished run's logs.

The *live* iterative loop (run → serve cached → heal drift → persist → run) is realized by
the engine itself via the OPTEXITY_CACHE_SERVE / OPTEXITY_CACHE_HEAL toggles — see
``self_repair.py`` — not by this CLI.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from optexity.inference.cache import compile_trace

logger = logging.getLogger(__name__)


def _step_order(name: str) -> int:
    part = name.split("_")[1] if "_" in name else "-1"
    return int(part) if part.lstrip("-").isdigit() else -1


def collect_trace(logs_directory: Path) -> list[dict]:
    """Merge every step's ``trace.json`` (each agentic step writes its own) in step order."""
    trace: list[dict] = []
    step_dirs = sorted(
        (p for p in logs_directory.glob("step_*") if (p / "trace.json").exists()),
        key=lambda p: _step_order(p.name),
    )
    for step_dir in step_dirs:
        with open(step_dir / "trace.json", encoding="utf-8") as f:
            trace.extend(json.load(f))
    return trace


def read_run_metrics(logs_directory: Path) -> dict:
    """Pull tokens / fallback count / wall-clock from a run's persisted per-step state.json.

    ``command_fallback_count`` is cumulative, so the latest step's state holds the run total;
    wall-clock spans the first ``started_at`` to the last ``completed_at``.
    """
    fallback_count = total_tokens = started_at = completed_at = None
    state_files = sorted(
        logs_directory.glob("step_*/state.json"), key=lambda p: _step_order(p.parent.name)
    )
    for sf in state_files:
        try:
            state = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if state.get("command_fallback_count") is not None:
            fallback_count = state["command_fallback_count"]
        if (state.get("token_usage") or {}).get("total_tokens") is not None:
            total_tokens = state["token_usage"]["total_tokens"]
        if state.get("started_at") and started_at is None:
            started_at = state["started_at"]
        if state.get("completed_at"):
            completed_at = state["completed_at"]

    wall_clock = None
    if started_at and completed_at:
        try:
            wall_clock = (
                datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)
            ).total_seconds()
        except Exception:
            pass
    return {
        "fallback_count": fallback_count,
        "total_tokens": total_tokens,
        "wall_clock_seconds": wall_clock,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="optexity-cache", description="Learning-cache CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compile = sub.add_parser("compile", help="compile a trace into a deterministic automation")
    src = p_compile.add_mutually_exclusive_group(required=True)
    src.add_argument("--trace", help="path to a single trace.json")
    src.add_argument("--logs", help="a run's logs dir (merges all step_*/trace.json)")
    p_compile.add_argument("--url", required=True)
    p_compile.add_argument("--out", default="test_automation_cached.json")

    p_metrics = sub.add_parser("metrics", help="read tokens/fallbacks/wall-clock from a run's logs")
    p_metrics.add_argument("--logs", required=True)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.command == "compile":
        trace = (
            json.loads(Path(args.trace).read_text(encoding="utf-8"))
            if args.trace
            else collect_trace(Path(args.logs))
        )
        automation, stats = compile_trace(trace, args.url)
        Path(args.out).write_text(json.dumps(automation.model_dump(exclude_none=True), indent=2))
        stats["output_path"] = args.out
        print(json.dumps(stats, indent=2))
    elif args.command == "metrics":
        print(json.dumps(read_run_metrics(Path(args.logs)), indent=2))


if __name__ == "__main__":
    main()
