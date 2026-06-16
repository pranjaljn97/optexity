"""Unit tests for the self-repairing cache (fingerprint verify + heal-on-fallback).

Run: OPTEXITY_API_KEY=local-dev DEPLOYMENT=dev pytest tests/test_self_repair.py
No browser/LLM needed — these exercise the pure logic and persistence.
"""

import json
import os

os.environ.setdefault("OPTEXITY_API_KEY", "local-dev")
os.environ.setdefault("DEPLOYMENT", "dev")

from optexity.inference.cache import compile_trace
from optexity.inference.cache.self_repair import (
    CacheConfig,
    fingerprint_matches,
    make_fingerprint,
    persist_healed_automation,
    record_heal,
)
from optexity.schema.actions.interaction_action import ElementFingerprint, InputTextAction
from optexity.schema.automation import Automation
from optexity.schema.memory import Memory


# --- toggle defaults OFF (opt-in) ---
def test_config_defaults_off(monkeypatch):
    monkeypatch.delenv("OPTEXITY_CACHE_VERIFY", raising=False)
    monkeypatch.delenv("OPTEXITY_CACHE_HEAL", raising=False)
    cfg = CacheConfig()
    assert cfg.verify_fingerprint is False and cfg.heal_on_fallback is False


def test_config_env_opt_in(monkeypatch):
    monkeypatch.setenv("OPTEXITY_CACHE_VERIFY", "1")
    monkeypatch.setenv("OPTEXITY_CACHE_HEAL", "1")
    cfg = CacheConfig()
    assert cfg.verify_fingerprint is True and cfg.heal_on_fallback is True


# --- strict fingerprint matching ---
def _fp(**attrs):
    tag = attrs.pop("tag", "input")
    return ElementFingerprint(**make_fingerprint(tag, attrs, None))


def test_same_element_matches():
    fp = _fp(name="04fullname", id="x")
    live = {"tag": "input", "attributes": {"name": "04fullname", "id": "x"}}
    assert fingerprint_matches(fp, live) is True


def test_diverged_name_is_mismatch():
    fp = _fp(name="04fullname")
    live = {"tag": "input", "attributes": {"name": "13adr_city"}}
    assert fingerprint_matches(fp, live) is False


def test_diverged_tag_is_mismatch():
    fp = _fp(name="x", tag="input")
    live = {"tag": "select", "attributes": {"name": "x"}}
    assert fingerprint_matches(fp, live) is False


def test_attrs_absent_at_capture_are_ignored():
    fp = _fp(name="x")  # only name captured
    live = {"tag": "input", "attributes": {"name": "x", "placeholder": "anything new"}}
    assert fingerprint_matches(fp, live) is True


def test_unreadable_live_does_not_falsely_match():
    # A captured identity attr missing entirely on the live element ⇒ mismatch.
    fp = _fp(name="x")
    live = {"tag": "input", "attributes": {}}
    assert fingerprint_matches(fp, live) is False


# --- renderer now emits fingerprints ---
def test_compiled_nodes_carry_fingerprint():
    trace = [{
        "step": 0, "action": "input", "params": {"text": "myname"},
        "element": {"node_name": "INPUT", "attributes": {"name": "04fullname"},
                    "ax_name": None, "x_path": "/f/input"},
        "result_ok": True, "error": None, "duration_s": 1.0, "goal": "fill", "cacheable": True,
    }]
    automation, _ = compile_trace(trace, "https://example.com")
    fp = automation.nodes[0].interaction_action.input_text.fingerprint
    assert fp is not None and fp.attributes.get("name") == "04fullname"


# --- heal-on-fallback rewrites the node + records the heal ---
def _memory():
    m = Memory(unique_child_arn="t")
    m.automation_state.step_index = 3
    return m


def test_record_heal_rewrites_node_and_logs():
    action = InputTextAction(command='locator("xpath=/wrong/input")',
                             fingerprint=ElementFingerprint(tag="input", attributes={"name": "old"}))
    mem = _memory()
    heal = {"command": 'locator("input[name=\'13adr_city\']")',
            "fingerprint": make_fingerprint("input", {"name": "13adr_city"}, None)}
    record_heal(action, heal, mem)
    assert action.command == 'locator("input[name=\'13adr_city\']")'
    assert action.fingerprint.attributes["name"] == "13adr_city"
    assert len(mem.heals) == 1 and mem.heals[0]["step_index"] == 3


def test_record_heal_noop_when_command_unchanged():
    action = InputTextAction(command='locator("input[name=\'x\']")')
    mem = _memory()
    record_heal(action, {"command": 'locator("input[name=\'x\']")'}, mem)
    assert mem.heals == []


def test_record_heal_noop_without_heal_info():
    action = InputTextAction(command="c")
    mem = _memory()
    record_heal(action, None, mem)
    assert mem.heals == []


# --- persistence only when a heal happened ---
def _task(tmp_path):
    from datetime import datetime, timezone
    import uuid
    auto = Automation(
        url="https://example.com",
        parameters={"input_parameters": {}, "generated_parameters": {}},
        nodes=[{"type": "action_node", "interaction_action": {
            "input_text": {"command": 'locator("input[name=\'x\']")', "input_text": "v"}}}],
    )
    from optexity.schema.task import Task
    return Task(task_id=str(uuid.uuid4()), user_id="u", recording_id="r",
                endpoint_name="e", automation=auto, input_parameters={}, secure_parameters={},
                unique_parameter_names=[], created_at=datetime.now(timezone.utc),
                status="queued", api_key="k", company_id="c")


# --- parent-compatibility shim: serialize_axtree tolerates either browser-use signature ---
def test_serialize_axtree_tolerates_both_signatures():
    from optexity.inference.infra.utils import serialize_axtree

    class UpstreamAPI:  # parent browser-use: no remove_empty_nodes kwarg
        def llm_representation(self, include_attributes=None):
            return "axtree"

    class KwargAPI:  # a version that accepts the kwarg
        def llm_representation(self, include_attributes=None, remove_empty_nodes=False):
            return f"axtree:{remove_empty_nodes}"

    assert serialize_axtree(UpstreamAPI(), remove_empty_nodes=True) == "axtree"
    assert serialize_axtree(KwargAPI(), remove_empty_nodes=True) == "axtree:True"


def test_persist_only_when_healed(tmp_path):
    task = _task(tmp_path)
    mem = _memory()
    out = str(tmp_path / "cached.json")
    # no heals -> nothing written
    assert persist_healed_automation(task, mem, out) is None
    assert not os.path.exists(out)
    # with a heal -> writes a schema-valid automation
    mem.heals.append({"step_index": 0, "new_command": "c"})
    assert persist_healed_automation(task, mem, out) == out
    Automation.model_validate(json.load(open(out)))


def test_cache_is_keyed_per_endpoint_no_overwrite(tmp_path, monkeypatch):
    """100 endpoints each get their own cache entry — runs do not overwrite each other."""
    import optexity.inference.cache.self_repair as sr
    monkeypatch.setenv("OPTEXITY_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("OPTEXITY_CACHE_HEAL_PATH", raising=False)
    sr.cache_config = sr.CacheConfig()  # re-read env

    paths = set()
    for i in range(100):
        task = _task(tmp_path)
        task.endpoint_name = f"endpoint_{i}"
        task.automation.url = f"https://example.com/{i}"
        mem = _memory()
        mem.heals.append({"step_index": 0, "new_command": f"c{i}"})
        out = sr.persist_healed_automation(task, mem)
        paths.add(out)

    assert len(paths) == 100  # one distinct file per endpoint, none overwritten
    # each entry round-trips and is the right endpoint's automation
    loaded = sr.load_cached_automation("endpoint_42")
    assert loaded is not None and loaded["url"].endswith("/42")


def test_serve_path_reuses_cache(tmp_path, monkeypatch):
    """With OPTEXITY_CACHE_SERVE on, a run for an endpoint with a cache entry swaps in the
    cached automation (the reuse the write exists for)."""
    import optexity.inference.cache.self_repair as sr
    from optexity.inference import child_process

    monkeypatch.setenv("OPTEXITY_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("OPTEXITY_CACHE_HEAL_PATH", raising=False)
    monkeypatch.delenv("OPTEXITY_TEST_AUTOMATION", raising=False)
    monkeypatch.setenv("OPTEXITY_CACHE_SERVE", "1")
    sr.cache_config = sr.CacheConfig()

    cached = Automation(
        url="https://cached.example.com",
        parameters={"input_parameters": {}, "generated_parameters": {}},
        nodes=[{"type": "action_node", "interaction_action": {
            "input_text": {"command": 'locator("input[name=\'x\']")', "input_text": "v"}}}],
    )
    sr.persist_automation("endpoint_7", cached)

    task = _task(tmp_path)
    task.endpoint_name = "endpoint_7"
    assert task.automation.url == "https://example.com"  # server automation before
    child_process._apply_local_automation_override(task)
    assert task.automation.url == "https://cached.example.com"  # served from cache after

    # no cache entry for a different endpoint -> server automation untouched
    task2 = _task(tmp_path)
    task2.endpoint_name = "endpoint_unknown"
    child_process._apply_local_automation_override(task2)
    assert task2.automation.url == "https://example.com"
