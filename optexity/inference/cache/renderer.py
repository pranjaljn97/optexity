"""Render collapsed deterministic steps into a valid optexity ``Automation``.

This is the target side of the compiler. It maps each normalized browser-use step to an
optexity ``InteractionAction`` carrying a deterministic Playwright ``command`` *and* the
LLM ``prompt_instructions`` fallback (resilient-hybrid policy: the engine tries the
command first and self-heals via the LLM if a locator goes stale — see
``handle_input.py`` / ``handle_command.py``). Everything is built through the real pydantic
models and run through ``Automation.model_validate``, so a successful render is itself the
proof that the cache is schema-valid.
"""

from __future__ import annotations

from optexity.inference.cache.locator_synth import element_fingerprint, synthesize_command
from optexity.schema.actions.interaction_action import (
    ClickElementAction,
    ElementFingerprint,
    InputTextAction,
    InteractionAction,
    SelectOptionAction,
    UploadFileAction,
)
from optexity.schema.automation import ActionNode, Automation, Parameters


def _default_prompt(step: dict, verb: str) -> str:
    """LLM-fallback instruction: prefer the agent's own per-step goal, else synthesize a
    minimal description from the action and value."""
    goal = (step.get("goal") or "").strip()
    if goal:
        return goal
    value = step.get("params", {}).get("text")
    return f"{verb} {value!r}" if value else verb


def _interaction_for_step(step: dict) -> InteractionAction | None:
    """Map one collapsed step to an InteractionAction, or None if no stable locator can
    be synthesized for it (the caller records the skip)."""
    synth = synthesize_command(step["element"])
    if synth is None:
        return None
    command = synth["command"]
    family = step["family"]
    params = step.get("params") or {}
    # Element identity for self-repair verification (inert unless the feature is enabled).
    fingerprint = ElementFingerprint(**element_fingerprint(step["element"]))

    if family == "input":
        return InteractionAction(
            input_text=InputTextAction(
                command=command,
                fingerprint=fingerprint,
                input_text=params.get("text"),
                prompt_instructions=_default_prompt(step, "Enter text in the field"),
            )
        )
    if family == "click":
        return InteractionAction(
            click_element=ClickElementAction(
                command=command,
                fingerprint=fingerprint,
                prompt_instructions=_default_prompt(step, "Click the element"),
            )
        )
    if family == "select":
        return InteractionAction(
            select_option=SelectOptionAction(
                command=command,
                fingerprint=fingerprint,
                input_text=params.get("text"),
                prompt_instructions=_default_prompt(step, "Select the option"),
            )
        )
    if family == "upload":
        return InteractionAction(
            upload_file=UploadFileAction(
                command=command,
                fingerprint=fingerprint,
                input_text=params.get("path"),
                prompt_instructions=_default_prompt(step, "Upload the file"),
            )
        )
    return None


def compile_steps_to_nodes(steps: list[dict]) -> tuple[list[ActionNode], list[dict]]:
    """Turn collapsed steps into ActionNodes. Returns ``(nodes, skipped)`` where
    ``skipped`` records any step we couldn't synthesize a locator for (kept for logging)."""
    nodes: list[ActionNode] = []
    skipped: list[dict] = []
    for step in steps:
        interaction = _interaction_for_step(step)
        if interaction is None:
            skipped.append(step)
            continue
        nodes.append(ActionNode(type="action_node", interaction_action=interaction))
    return nodes, skipped


def build_automation(
    url: str,
    steps: list[dict],
    *,
    parameters: dict | None = None,
    base_automation: Automation | None = None,
) -> tuple[Automation, list[dict]]:
    """Build a validated deterministic ``Automation`` from collapsed steps.

    ``base_automation`` (the original agentic automation, if available) seeds parameters
    and top-level settings so we don't lose configuration when re-emitting. Returns
    ``(automation, skipped_steps)``. Raises pydantic ``ValidationError`` if the result is
    not schema-valid — which is the point: it guarantees the cache is runnable.
    """
    nodes, skipped = compile_steps_to_nodes(steps)

    if parameters is not None:
        params = Parameters.model_validate(parameters)
    elif base_automation is not None:
        params = base_automation.parameters
    else:
        params = Parameters(input_parameters={}, generated_parameters={})

    automation = Automation(
        url=url,
        parameters=params,
        nodes=nodes,
        automation_description="Deterministic cache compiled from a browser-use agentic run.",
    )
    # Round-trip through validation to prove the emitted JSON is loadable by the engine.
    Automation.model_validate(automation.model_dump())
    return automation, skipped
