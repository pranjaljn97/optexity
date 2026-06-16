"""Iterative learning-cache loop: run an automation, capture what the agent did, compile
it into a more deterministic automation, and repeat until it stops needing the LLM.

Round 0 starts from the pure agentic ``test_automation.json``. After each run we read the
browser-use trace + the engine's per-step ``state.json`` (tokens, timing, and the
``command_fallback_count`` instrumented in ``handle_command.py``), compile the trace into a
hardened automation, and feed it back in as the next round's input. We stop when a round
triggers **zero command fallbacks** — every cached locator resolved on the fast path, so
the automation is fully deterministic — or after ``max_rounds``.

Three entrypoints (``python -m optexity.inference.cache.loop <cmd>``):
  * ``compile``  — offline: compile an existing trace.json -> cached automation (no browser/keys)
  * ``run``      — one live run of an automation via the local engine (needs a browser + creds)
  * ``loop``     — the full run->compile->run iterative loop

The measurement table it prints (tokens + wall-clock + fallbacks per round) is the
"latency / performance" evidence for the assignment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from optexity.inference.cache import compile_trace
from optexity.schema.automation import Automation

logger = logging.getLogger(__name__)

DEFAULT_SEED = "test_automation.json"
DEFAULT_CACHED = "test_automation_cached.json"


@dataclass
class RoundResult:
    round_index: int
    fallback_count: int | None
    total_tokens: int | None
    wall_clock_seconds: float | None
    raw_trace_steps: int
    emitted_nodes: int
    stats: dict = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Reading results out of a finished run's log directory
# --------------------------------------------------------------------------------------
def _latest_step_dir(logs_directory: Path) -> Path | None:
    step_dirs = sorted(
        (p for p in logs_directory.glob("step_*") if p.is_dir()),
        key=lambda p: int(p.name.split("_")[1]) if p.name.split("_")[1].lstrip("-").isdigit() else -1,
    )
    return step_dirs[-1] if step_dirs else None


def collect_trace(logs_directory: Path) -> list[dict]:
    """Merge every step's trace.json (agentic steps each write their own) in order."""
    trace: list[dict] = []
    step_dirs = sorted(
        (p for p in logs_directory.glob("step_*") if (p / "trace.json").exists()),
        key=lambda p: int(p.name.split("_")[1]) if p.name.split("_")[1].lstrip("-").isdigit() else -1,
    )
    for step_dir in step_dirs:
        with open(step_dir / "trace.json", encoding="utf-8") as f:
            trace.extend(json.load(f))
    return trace


def read_run_metrics(logs_directory: Path) -> dict:
    """Pull tokens / fallback count / wall-clock from the run's persisted state.

    ``command_fallback_count`` is cumulative on automation_state, so the latest step's
    state.json holds the run total. Wall-clock comes from the first started_at to the last
    completed_at across step states.
    """
    fallback_count = None
    total_tokens = None
    started_at = None
    completed_at = None

    state_files = sorted(
        logs_directory.glob("step_*/state.json"),
        key=lambda p: int(p.parent.name.split("_")[1]) if p.parent.name.split("_")[1].lstrip("-").isdigit() else -1,
    )
    for sf in state_files:
        try:
            with open(sf, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            continue
        if state.get("command_fallback_count") is not None:
            fallback_count = state["command_fallback_count"]
        tu = state.get("token_usage") or {}
        if tu.get("total_tokens") is not None:
            total_tokens = tu["total_tokens"]
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
            wall_clock = None

    return {
        "fallback_count": fallback_count,
        "total_tokens": total_tokens,
        "wall_clock_seconds": wall_clock,
    }


# --------------------------------------------------------------------------------------
# Compile step (offline-capable, used every round)
# --------------------------------------------------------------------------------------
def compile_run(
    logs_directory: Path,
    url: str,
    output_path: str | Path,
    *,
    base_automation: Automation | None = None,
) -> dict:
    """Compile a finished run's trace into a deterministic automation on disk."""
    trace = collect_trace(logs_directory)
    automation, stats = compile_trace(trace, url, base_automation=base_automation)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(automation.model_dump(exclude_none=True), f, indent=2)
    stats["output_path"] = str(output_path)
    return stats


# --------------------------------------------------------------------------------------
# Live runner (needs a browser + server creds; the user runs this path)
# --------------------------------------------------------------------------------------
async def run_automation_locally(automation_path: str | Path) -> Path:
    """Run a single automation JSON through the real engine and return its logs directory.

    Launches a local Chrome via ActualBrowser, builds a Task from the automation, and calls
    ``run_automation`` exactly as the worker does. Requires a working browser and the same
    env/creds the engine normally uses (OPTEXITY_API_KEY, DEPLOYMENT, GOOGLE_API_KEY).
    """
    # Imported lazily so the offline `compile` path needs no engine/settings/env.
    from optexity.inference.core.run_automation import run_automation
    from optexity.inference.infra.actual_browser import ActualBrowser
    from optexity.schema.task import Task

    with open(automation_path, encoding="utf-8") as f:
        automation = Automation.model_validate(json.load(f))

    task = Task(
        task_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        recording_id=str(uuid.uuid4()),
        endpoint_name="cache_loop",
        automation=automation,
        input_parameters={},
        secure_parameters={},
        unique_parameter_names=[],
        created_at=datetime.now(timezone.utc),
        status="queued",
        api_key="local-dev",
        company_id="local-dev",
    )

    browser = ActualBrowser(
        channel=automation.browser_channel,
        unique_child_arn="cache-loop",
        port=9222,
        headless=False,
        is_dedicated=False,
        use_proxy=False,
        proxy_session_id=None,
        os_emulation=automation.os_emulation,
        allow_cookies=automation.allow_cookies,
    )
    await browser.start()
    try:
        await run_automation(task, "cache-loop", 0, browser.cdp_url, max_tries=1)
    finally:
        await browser.stop()
    return task.logs_directory


# --------------------------------------------------------------------------------------
# The iterative loop
# --------------------------------------------------------------------------------------
async def iterate(
    seed_path: str = DEFAULT_SEED,
    cached_path: str = DEFAULT_CACHED,
    url: str | None = None,
    max_rounds: int = 4,
) -> list[RoundResult]:
    """Run the full run -> compile -> run loop until fallbacks hit 0 or max_rounds."""
    with open(seed_path, encoding="utf-8") as f:
        base_automation = Automation.model_validate(json.load(f))
    url = url or base_automation.url

    current_input = seed_path
    results: list[RoundResult] = []

    for round_index in range(max_rounds):
        logger.info(f"=== cache loop round {round_index} (input={current_input}) ===")
        logs_directory = await run_automation_locally(current_input)
        metrics = read_run_metrics(logs_directory)
        stats = compile_run(logs_directory, url, cached_path, base_automation=base_automation)

        results.append(
            RoundResult(
                round_index=round_index,
                fallback_count=metrics["fallback_count"],
                total_tokens=metrics["total_tokens"],
                wall_clock_seconds=metrics["wall_clock_seconds"],
                raw_trace_steps=stats["raw_steps"],
                emitted_nodes=stats["emitted_nodes"],
                stats=stats,
            )
        )
        _print_report(results)

        # Converged: a round that needed no LLM fallback at all.
        if round_index > 0 and metrics["fallback_count"] == 0:
            logger.info("Converged: 0 command fallbacks. Automation is fully deterministic.")
            break

        # From round 1 on, replay the hardened automation we just emitted.
        current_input = cached_path

    Path("cache_loop_report.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2)
    )
    return results


def _print_report(results: list[RoundResult]) -> None:
    print("\n  round | fallbacks | tokens | wall_clock_s | raw_steps -> nodes")
    print("  ------+-----------+--------+--------------+-------------------")
    for r in results:
        print(
            f"  {r.round_index:^5} | {str(r.fallback_count):^9} | "
            f"{str(r.total_tokens):^6} | {str(r.wall_clock_seconds):^12} | "
            f"{r.raw_trace_steps} -> {r.emitted_nodes}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Optexity learning-cache loop")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compile = sub.add_parser("compile", help="offline: compile a trace.json -> cached automation")
    p_compile.add_argument("--trace", required=True, help="path to a trace.json")
    p_compile.add_argument("--url", required=True)
    p_compile.add_argument("--out", default=DEFAULT_CACHED)

    p_run = sub.add_parser("run", help="one live run of an automation (needs browser+creds)")
    p_run.add_argument("--automation", default=DEFAULT_SEED)

    p_loop = sub.add_parser("loop", help="full iterative run->compile loop")
    p_loop.add_argument("--seed", default=DEFAULT_SEED)
    p_loop.add_argument("--cached", default=DEFAULT_CACHED)
    p_loop.add_argument("--url", default=None)
    p_loop.add_argument("--max-rounds", type=int, default=4)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.command == "compile":
        with open(args.trace, encoding="utf-8") as f:
            trace = json.load(f)
        automation, stats = compile_trace(trace, args.url)
        Path(args.out).write_text(json.dumps(automation.model_dump(exclude_none=True), indent=2))
        stats["output_path"] = args.out
        print(json.dumps(stats, indent=2))
    elif args.command == "run":
        logs = asyncio.run(run_automation_locally(args.automation))
        print(json.dumps(read_run_metrics(Path(logs)), indent=2))
    elif args.command == "loop":
        asyncio.run(iterate(args.seed, args.cached, args.url, args.max_rounds))


if __name__ == "__main__":
    main()
