# Learning cache: agentic → deterministic automations

A memory layer for Optexity automations. The first time an objective runs, browser-use
(an LLM) figures out the steps from scratch — spending tokens and latency on every run,
with no memory between runs. This module captures what the agent actually did and
**compiles it into a deterministic automation** so subsequent runs skip the LLM for the
steps that are inherently deterministic (typing a name, clicking submit, …).

## How it works

```
test_automation.json (agentic)
        │  run through the engine
        ▼
browser-use Agent ──▶ agent.history.export_deterministic_trace()  ──▶ step_*/trace.json
        │                                                                   │
        │                                  optexity.inference.cache.compile_trace
        ▼                                                                   ▼
   collapse_trace (drop noise, keep last write)  ──▶  renderer (locator + pydantic)
        │                                                                   │
        ▼                                                                   ▼
                          test_automation_cached.json  (deterministic, validated)
```

- **Capture** (browser-use fork): `AgentHistoryList.export_deterministic_trace()` walks the
  agent history and emits, per action, the DOM element it touched (xpath / attributes /
  accessible name) plus success/timing/goal. Schema-agnostic — it knows nothing about
  Optexity.
- **Redundancy** (`redundancy.py`): collapses exploration — drops perception/navigation,
  keeps only the *last* successful write per element (explore-then-correct), collapses
  repeated clicks.
- **Locator synth** (`locator_synth.py`): turns an element into a Playwright `command`,
  reusing the engine's own `LocatorExtraction._scored_candidates` precedence
  (test-id > id > name > aria-label > role+name > placeholder > text > xpath) so cached
  locators match how the engine itself prefers to locate.
- **Render** (`renderer.py`): builds real `InteractionAction` pydantic objects and runs
  `Automation.model_validate` — a successful compile *is* the proof the cache is runnable.

### Resilient-hybrid nodes

Each cached node keeps **both** a deterministic `command` and the LLM `prompt_instructions`
fallback. The engine (`handle_input.py` / `handle_command.py`) tries the command first
(fast, no tokens) and self-heals via the LLM only if a locator goes stale. So a wrong
locator is never fatal — it just triggers one fallback, which the loop then re-hardens.

### Convergence

`handle_command.py` increments `memory.automation_state.command_fallback_count` whenever a
cached command fails and falls back to the LLM. The iterative loop stops when a round
records **zero** fallbacks: every locator resolved on the fast path → fully deterministic.

## Usage

```bash
# env (engine + Gemini for the agentic capture)
cp optexity/.env.example .env && export ENV_PATH=$PWD/.env   # fill in keys

# offline: compile an existing trace.json -> cached automation (no browser/keys)
python -m optexity.inference.cache.loop compile \
    --trace /tmp/optexity/<task_id>/logs/step_0/trace.json \
    --url https://www.roboform.com/filling-test-all-fields \
    --out test_automation_cached.json

# one live run of an automation (needs a browser + creds)
python -m optexity.inference.cache.loop run --automation test_automation.json

# full iterative run -> compile -> run loop until fallbacks hit 0
python -m optexity.inference.cache.loop loop --seed test_automation.json --max-rounds 4
```

`child_process.py` also picks up a `test_automation.json` in the working directory and
overrides the server's automation, so you can iterate locally against any automation
endpoint.

## Tests

```bash
OPTEXITY_API_KEY=local-dev DEPLOYMENT=dev pytest tests/test_cache.py
```
