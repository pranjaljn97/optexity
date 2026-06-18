"""Unit tests for the learning-cache compiler (trace -> deterministic automation).

Run with:  OPTEXITY_API_KEY=local-dev DEPLOYMENT=dev pytest tests/test_cache.py
These exercise the synth/redundancy/render pipeline with no browser or LLM.
"""

import os

os.environ.setdefault("OPTEXITY_API_KEY", "local-dev")
os.environ.setdefault("DEPLOYMENT", "dev")

from optexity.inference.cache import compile_trace
from optexity.inference.cache.locator_synth import ranked_commands, synthesize_command
from optexity.inference.cache.redundancy import collapse_trace
from optexity.schema.automation import Automation


def _input(name, text, idv=None, ax=None, tag="INPUT", ok=True):
    attrs = {"name": name, "type": "text"}
    if idv:
        attrs["id"] = idv
    return {
        "step": 0,
        "action": "input",
        "params": {"text": text},
        "element": {
            "node_name": tag,
            "attributes": attrs,
            "ax_name": ax,
            "x_path": f"/html/body/form/input[@name='{name}']",
            "stable_hash": abs(hash(name)) % 99999,
        },
        "result_ok": ok,
        "error": None,
        "duration_s": 1.0,
        "goal": f"Enter {name}",
        "cacheable": ok,
    }


def _noise(action="scroll"):
    return {
        "step": 0, "action": action, "params": {}, "element": None,
        "result_ok": True, "error": None, "duration_s": 0.2, "goal": "noise",
        "cacheable": False,
    }


# --- locator synthesis: mirrors the engine's own precedence (id > name > xpath) ---
def test_id_beats_name():
    el = {"node_name": "BUTTON", "attributes": {"id": "btnSubmit", "name": "submit"},
          "ax_name": "Submit", "x_path": "/html/body/button"}
    assert synthesize_command(el)["command"] == 'locator("#btnSubmit")'


def test_name_locator_for_form_field():
    el = {"node_name": "INPUT", "attributes": {"name": "01___title"},
          "ax_name": None, "x_path": "/html/body/form/input[1]"}
    assert synthesize_command(el)["command"] == "locator(\"input[name='01___title']\")"


def test_xpath_fallback_when_nothing_stable():
    el = {"node_name": "DIV", "attributes": {}, "ax_name": None, "x_path": "/html/body/div[3]"}
    cmd = synthesize_command(el)
    assert cmd is not None and "xpath=" in cmd["command"]


def test_digit_prefixed_name_prefers_name_over_xpath():
    # Real roboform field: the engine's _looks_dynamic flags "04fullname" (>=2 digits),
    # but it's a stable hand-authored name and resolved at capture time, so the relaxed
    # cache rule should pick the name selector over a brittle positional xpath.
    el = {"node_name": "INPUT", "attributes": {"name": "04fullname"},
          "ax_name": None, "x_path": "/html/body/div[2]/form/div/div[1]/div[5]/div[2]/input"}
    assert synthesize_command(el)["command"] == "locator(\"input[name='04fullname']\")"


def test_genuine_hash_still_falls_to_xpath():
    # A digit-dense hash should NOT be re-admitted by the relaxed rule.
    el = {"node_name": "INPUT", "attributes": {"name": "x7f3a9b2c1d4"},
          "ax_name": None, "x_path": "/html/body/input"}
    assert "xpath=" in synthesize_command(el)["command"]


def test_dynamic_text_link_prefers_xpath_over_text():
    # A download link whose only signals are a random filename (href/text). A text locator
    # would go stale every visit, so the cache should fall through to the positional xpath.
    el = {"node_name": "A", "attributes": {"href": "download/tmpu521hwte.txt"},
          "ax_name": "tmpu521hwte.txt", "x_path": "/html/body/div/a[1]"}
    cmd = synthesize_command(el)
    assert "xpath=" in cmd["command"] and "tmpu521hwte" not in cmd["command"]


def test_stable_text_link_still_uses_text():
    # Non-dynamic visible text is fine to anchor on.
    el = {"node_name": "A", "attributes": {}, "ax_name": "Download", "x_path": "/html/body/a"}
    assert synthesize_command(el)["command"].startswith("get_by_text(")


def test_ranked_commands_are_sorted_best_first():
    el = {"node_name": "INPUT", "attributes": {"id": "x", "name": "y"},
          "ax_name": None, "x_path": "/p"}
    scores = [c["score"] for c in ranked_commands(el)]
    assert scores == sorted(scores, reverse=True)


# --- redundancy: explore-then-correct keeps the last write; noise is dropped ---
def test_collapse_keeps_last_write_and_drops_noise():
    trace = [
        _noise("navigate"),
        _input("01___full_name", "WRONG"),
        _noise("scroll"),
        _input("01___full_name", "myname"),
        _input("02___city", "SF"),
    ]
    collapsed = collapse_trace(trace)
    assert len(collapsed) == 2  # two distinct fields, corrections merged
    by_name = {s["element"]["attributes"]["name"]: s["params"]["text"] for s in collapsed}
    assert by_name == {"01___full_name": "myname", "02___city": "SF"}


def test_failed_steps_excluded():
    trace = [_input("a", "x", ok=False), _input("b", "y", ok=True)]
    assert len(collapse_trace(trace)) == 1


# --- end to end: produces a schema-valid Automation with resilient hybrid nodes ---
def test_compile_produces_valid_hybrid_automation():
    trace = [
        _noise("navigate"),
        _input("full_name", "myname"),
        _input("city", "SF"),
        {
            "step": 0, "action": "click", "params": {},
            "element": {"node_name": "BUTTON", "attributes": {"id": "go"}, "ax_name": "Go",
                        "x_path": "/html/body/button"},
            "result_ok": True, "error": None, "duration_s": 0.5, "goal": "Click go",
            "cacheable": True,
        },
    ]
    automation, stats = compile_trace(trace, "https://example.com/form")
    assert stats["emitted_nodes"] == 3
    # Round-trips through pydantic validation (the proof the cache is runnable).
    Automation.model_validate(automation.model_dump())

    first = automation.nodes[0].interaction_action.input_text
    assert first.input_text == "myname"
    assert first.command and first.command.startswith("locator(")
    # Resilient-hybrid policy: deterministic command AND an LLM fallback are both present,
    # and neither path is disabled.
    assert first.prompt_instructions
    assert first.skip_command is False and first.skip_prompt is False


# --- generality: a navigate + click-to-download flow compiles to a deterministic click ---
def test_download_flow_compiles_to_click_node():
    trace = [
        _noise("navigate"),
        {
            "step": 0, "action": "click", "params": {},
            "element": {"node_name": "A", "attributes": {"id": "dl", "href": "sample.pdf"},
                        "ax_name": "Download", "x_path": "/html/body/a"},
            "result_ok": True, "error": None, "duration_s": 0.6,
            "goal": "Click the download link", "cacheable": True,
        },
    ]
    automation, stats = compile_trace(trace, "https://example.com/files")
    assert stats["emitted_nodes"] == 1
    click = automation.nodes[0].interaction_action.click_element
    assert click.command == 'locator("#dl")'
    assert click.prompt_instructions == "Click the download link"
    Automation.model_validate(automation.model_dump())
