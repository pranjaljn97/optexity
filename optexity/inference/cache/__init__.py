"""Learning cache: compile a browser-use agentic trace into a deterministic optexity
automation so subsequent runs skip LLM reasoning.

Pipeline:  trace.json  ->  collapse_trace  ->  build_automation  ->  <task>_cached.json

The trace is produced by the browser-use fork
(``AgentHistoryList.export_deterministic_trace``); the synth/redundancy/render stages live
here so all schema-aware logic stays on the optexity side.
"""

from __future__ import annotations

import json
from pathlib import Path

from optexity.inference.cache.redundancy import collapse_trace
from optexity.inference.cache.renderer import build_automation
from optexity.schema.automation import Automation


def compile_trace(
    trace: list[dict],
    url: str,
    *,
    parameters: dict | None = None,
    base_automation: Automation | None = None,
) -> tuple[Automation, dict]:
    """Compile an in-memory trace into a deterministic Automation.

    Returns ``(automation, stats)`` where ``stats`` summarizes the reduction (raw vs
    cached step counts, skipped steps) for logging and the iterative loop.
    """
    collapsed = collapse_trace(trace)
    automation, skipped = build_automation(
        url, collapsed, parameters=parameters, base_automation=base_automation
    )
    stats = {
        "raw_steps": len(trace),
        "cacheable_steps": sum(1 for s in trace if s.get("cacheable")),
        "collapsed_steps": len(collapsed),
        "emitted_nodes": len(automation.nodes),
        "skipped_steps": len(skipped),
    }
    return automation, stats


def compile_trace_file(
    trace_path: str | Path,
    url: str,
    output_path: str | Path,
    *,
    parameters: dict | None = None,
    base_automation: Automation | None = None,
) -> dict:
    """Compile a ``trace.json`` file to a deterministic automation JSON on disk.
    Returns the reduction stats."""
    with open(trace_path, encoding="utf-8") as f:
        trace = json.load(f)

    automation, stats = compile_trace(
        trace, url, parameters=parameters, base_automation=base_automation
    )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(automation.model_dump(exclude_none=True), f, indent=2)

    stats["output_path"] = str(output_path)
    return stats


__all__ = ["compile_trace", "compile_trace_file", "collapse_trace", "build_automation"]
