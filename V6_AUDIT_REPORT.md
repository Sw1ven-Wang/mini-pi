# Mini-Pi V6 Audit Report

## A. V6 modification scope

- `agent/hooks.py`: generic tool-result and AgentLoop observer protocols.
- `agent/tool_dispatcher.py`: optional result observer; observer failures are logged without rewriting a successful tool outcome.
- `agent/agent_loop.py`: one optional observer argument and notifications after Assistant/ToolResult context updates.
- `coding_agent/observations.py`: observed file model/store, path normalization, tool-result ingestion and Host version resolution.
- `coding_agent/tools/edit.py`, `delete.py`, `move.py`, `patch.py`: Host-resolved versions while retaining the lower-level expected-version safety primitives and legacy direct-call compatibility.
- `coding_agent/context_budget.py`: conservative estimator, reserve-aware budget and usage model.
- `coding_agent/compaction.py`: deterministic summary and atomic tool-call/result grouping.
- `coding_agent/context_manager.py`: budget trigger, staged retention reduction and hard-limit rejection.
- `coding_agent/session.py`: pure session models, persistence hook, CodingSession lifecycle, resume and interrupted-session reconciliation.
- `coding_agent/session_store.py`: safe-ID, atomic JSON save/load and persistence-boundary secret filtering.
- `coding_agent/errors.py`: typed observation, session, budget, compaction and reconciliation errors.
- `demo_v6.py`: create/resume example using local JSON sessions.
- `.gitignore`, `pyproject.toml`, `uv.lock`: ignore local sessions and make the configured asyncio pytest plugin part of the actual dev environment.
- `tests/test_observations.py`, `test_dispatcher_observer.py`, `test_v6_version_resolution.py`, `test_session_store.py`, `test_session_runtime.py`, `test_context_budget.py`, `test_compaction.py`, `test_v6_e2e.py`: V6 unit, regression and E2E coverage.

Pre-existing V1–V5 workspace changes were preserved; no reset or rollback of user work was performed.

## B. Observation architecture

```text
AgentToolResult.details
→ ToolDispatcher result observer
→ CodingObservationObserver
→ ObservationStore (last-known cache)
→ VersionResolver
→ Edit/Delete/Move/Patch expected version
→ existing disk final recheck
```

Paths are normalized to workspace-relative POSIX form. Existing observations require a version; missing observations cannot carry one. The store is never refreshed implicitly from disk on resume, so an external edit correctly makes its saved version stale.

## C. Session data

`SessionState` persists the safe session ID, timestamps, status, user goal, cumulative turn count, compaction summary/count, observations, JSON-only metadata and interrupted flag. `SessionSnapshot` contains that state plus the typed working `Context`. Pydantic JSON roundtrip restores User, Assistant/tool-call and ToolResult message subclasses.

`SessionStore` saves `session.json` through a same-directory temporary file, flush, `fsync` and `os.replace`. It does not use workspace mutation/checkpoint machinery.

## D. Secrets and runtime objects

Provider clients, API-key constructor arguments, Workspace, locks, queues and processes are not fields of either persisted model. Metadata accepts JSON data only and removes keys such as `api_key`, authorization, token, password and secret. The final persistence boundary repeats recursive filtering and redacts common `sk-...` and Bearer-token text in Context/tool arguments.

Arbitrary opaque secret strings that have no recognizable key or token pattern cannot be distinguished reliably from normal user text; callers should still avoid placing secrets in prompts or tool output.

## E. Context budget

- `max_tokens`: injected model context limit.
- `reserve_tokens`: space held back for the next response/tool traffic.
- `compact_threshold_ratio`: default 75% of the reserve-adjusted input limit.
- estimator: deterministic approximation (ASCII about four characters/token; non-ASCII one character/token).

The manager compacts before provider overflow. If the mandatory goal, summary and latest complete group still exceed the hard input limit, it raises `context_budget_exceeded` instead of calling the provider.

## F. Compaction

Assistant messages containing tool calls and all matching ToolResult messages form one atomic group. Orphan, duplicate or mismatched groups raise `compaction_invariant`. Compaction always retains the current user goal and latest complete group, plus as many recent complete groups as the budget permits.

The summary deterministically preserves goal, session/turn/interrupted state, completed/current/unresolved/verification metadata, next actions and observed/missing file status. File hashes remain in `ObservationStore` and are not copied into the model prompt.

The persisted snapshot contains the current working Context. Full event-log replay is not implemented in V6.

## G. Resume behavior

- Normal resume restores typed Context, cumulative session state and observations, while reinjecting fresh runtime objects.
- An external file change leaves the old observation intact; the next mutation returns `stale_version`, and only a new read updates the observation.
- An orphan/pending tool group marks the session interrupted. Resume truncates to the last stable message boundary, injects a reconciliation warning, never replays the pending call, and requires a new read/search/glob/PowerShell inspection result before completion.

## H. AgentLoop audit

The core loop was not rewritten. Its provider call, serial tool dispatch, append order, termination, max-turn handling and return value are unchanged when `observer=None`. V6 adds one optional observer parameter and calls it after AssistantMessage and ToolResultMessage appends.

## I. Test results

Baseline before V6 work:

```text
uv run -m pytest -q
239 passed, 0 failed, 0 skipped
```

Final result:

```text
uv run -m pytest -q
320 passed, 0 failed, 0 skipped
```

Additional checks:

```text
python -m compileall -q agent ai coding_agent tests demo_v6.py
git diff --check
```

Both completed without code/whitespace errors (Git only reports the repository's configured LF-to-CRLF conversion notices).

## J. E2E traces

```text
read a.py
→ context crosses threshold
→ compact whole groups
→ edit a.py
→ read/edit b.py
→ final answer
→ both files verified changed
```

```text
read H1
→ atomic session save
→ reconstruct runtime and resume
→ external disk write H2
→ edit with saved H1 rejected as stale
→ read H2
→ edit with Host-resolved H2
→ final file and session status verified
```

## K. Known limitations

- Token estimation is approximate and intentionally conservative.
- Summaries are deterministic runtime summaries, not semantic LLM summaries.
- ObservationStore does not watch the filesystem proactively.
- Sessions are local JSON; there is no cloud sync or event-sourced replay.
- Interrupted side-effect calls are never replayed automatically.
- Raw checkpoint/recovery state remains process-local and is not made restart-durable by V6.
- Shell command policy remains a soft prompt/permission boundary; a full shell guard is not implemented.
- Arbitrary secret-looking prose without recognizable keys/patterns cannot be perfectly detected.
- Planner/editor separation and explicit plan state remain V7 work.
