# Mini-Pi V8 Audit Report

Date: 2026-09-02  
Scope: Production hardening, observability, command policy, L1 sandbox, replay, fault injection, security, and offline benchmark.

## A. Modified files

| Path | Reason |
|---|---|
| `coding_agent/observability/` | Event model, ordered best-effort EventBus, trace/span context, redaction, metrics, artifact store, runtime adapters, and JSONL audit sink. |
| `coding_agent/safety/command_analysis.py` | Conservative whole-command PowerShell risk classification. |
| `coding_agent/safety/command_policy.py` | Host-owned allow/ask/deny policy and safer-tool suggestions. |
| `coding_agent/sandbox/` | Explicit L0–L3 model and constrained local L1 process sandbox. |
| `coding_agent/task_budget.py` | Shared turn/model/tool/replan/duration budget and timeout cancellation. |
| `coding_agent/replay/` | Redacted trace records, atomic persistence, and temporary-workspace tool-level replay. |
| `coding_agent/testing/fault_injection.py` | Production Noop and deterministic Scripted fault injectors. |
| `coding_agent/benchmark/` | Benchmark schemas, isolated runner, security checks, and JSON/CSV/Markdown reporting. |
| `agent/hooks.py`, `agent/agent_loop.py`, `agent/tool_dispatcher.py` | Generic lifecycle/budget hooks, read-only parallel batches, cancellation and shared budget propagation. |
| `coding_agent/orchestration.py`, `coding_agent/session.py`, `coding_agent/agents/viewer.py` | Task trace integration, safe phase events, recorder hooks, session events, and cross-phase budget propagation. |
| `coding_agent/tools/powershell.py`, `coding_agent/process/runner.py` | Pre-execution command guard, L1 sandbox use, explicit child environment, bounded output, timeout and process-tree cancellation. |
| `coding_agent/filesystem/atomic_write.py`, `coding_agent/filesystem/workspace.py`, `coding_agent/mutation/runtime.py`, `coding_agent/session_store.py`, `coding_agent/verification/pipeline.py` | Fault points, device-path rejection, cancellation-safe recovery, and persistence/verification injection. |
| `tests/observability/`, `tests/security/`, `tests/e2e/`, `tests/test_task_budget.py`, `tests/test_trace_*.py`, `tests/test_fault_injection.py`, `tests/test_benchmark.py` | V8 unit, security, recovery, replay, benchmark, and production-path acceptance coverage. |
| `tests/fixtures/repos/basic/`, `tests/fixtures/benchmark_tasks.json` | Repeatable offline fixture and 13-task suite. |

## B. Observability

- Events: structured task, plan, model, tool, mutation, rollback, verification, context, session, and sandbox events. Event payloads are redacted before every sink.
- Trace/span: one root trace per task, child spans for model/tool/mutation/recovery work, stable parent relationships, and UUID identifiers.
- Metrics: correctness, model/token/tool activity, reads/searches, mutation failures, stale/no-match/ambiguous errors, verification, replan, compaction, permission, rollback, timeout, policy-block, and sandbox counters.
- Artifacts: SHA-256 content-addressed, deduplicated and atomically replaced; callers cannot choose storage paths.
- Audit: security-relevant approvals/denials, rules, mutations, rollbacks and sandbox violations are saved as redacted JSONL under a configured `.mini_pi/audit/` root.
- Sink failures are counted/logged and cannot overwrite the domain result.

## C. Secret handling

- Known secret keys and token patterns are recursively replaced with `[REDACTED]` before event, trace, and text-artifact persistence.
- Artifact metadata contains only ID, kind, SHA-256, size and store-relative path.
- LocalSandbox constructs an allowlisted child environment and drops names containing key/token/secret/password/authorization/credential markers.
- The production E2E sets `DEEPSEEK_API_KEY=SECRET_E2E_VALUE`; neither the real child PowerShell environment output, serialized trace, nor artifact metadata contains it.

## D. Command policy

| Risk | Default decision |
|---|---|
| `READ_ONLY` | allow |
| `BUILD_TEST` | allow (configurable to ask) |
| `WRITE` | ask |
| `NETWORK` | ask |
| `INSTALL` | ask |
| `DESTRUCTIVE` | deny |
| `UNKNOWN` | ask |

The classifier scans the complete command and applies highest-risk precedence. `Remove-Item`, `Move-Item`, `git reset --hard`, `git clean -fd`, and opaque execution are denied before a process starts. Outer approval cannot override a destructive denial. Suggestions point to DeleteTool/MoveTool where applicable.

CommandPolicy is a policy guard, not a sandbox.

## E. Sandbox

Implemented level: **L1 LocalSandbox**.

It fixes cwd to the Workspace, provides an allowlisted secret-filtered environment, bounds output and timeout, and terminates process trees. `ResourcePolicy` explicitly reports `strong_isolation=false` and `network_isolated=false`.

L2 container and L3 remote isolation are not implemented. L1 does not provide an OS security boundary.

## F. Parallelism

ToolDispatcher runs a batch concurrently only when every resolved tool is read-only. Read/read and read/search batches are parallel and retain input result order. Any write/edit/create/delete/move/execute capability makes the whole batch serial. Cancellation propagates through `asyncio.gather` and cancels all active reads.

## G. Budget and cancellation

`BudgetTracker` propagates through Orchestrator, Planner, Viewer, Editor, Verifier, AgentLoop and ToolDispatcher. It limits turns, model calls, tool calls, replans and wall duration. Exhaustion raises structured `BUDGET_EXHAUSTED`; it never produces a successful outcome.

Cancellation tests cover model wait, parallel reads, permission wait, process execution and mutation. A mutation cancellation is caught inside the mutation critical path, reverse recovery runs, and only then is cancellation re-raised. Task/model failure events and failed session state are retained on cancellation.

## H. Replay

Trace replay is deterministic and tool-level only; it never invokes a model. The source fixture is resolved and hashed, copied into a new `TemporaryDirectory`, wrapped in `Workspace`, and replayed through real file versions and `WorkspaceMutationRuntime`. Tests prove the source hash set is unchanged, the final state and verification result are reproduced, and a stale recorded version stops replay.

## I. Fault injection

| Point | Expected behavior | Result |
|---|---|---|
| `before_atomic_replace` | Original file remains intact; temp file removed | passed |
| `after_first_file_write` | Reverse recovery restores original state | passed |
| `before_second_file_write` | Earlier operations recover in reverse order | passed |
| `checkpoint_create_failure` | Mutation stops before uncheckpointed write | covered by hook/runtime regression |
| `rollback_failure` | Structured `WorkspaceRecoveryError` contains failures | passed |
| `session_save_failure` | No partial `session.json` is published | passed |
| `process_timeout` | Process error path is deterministic and bounded | covered by injection and process timeout tests |
| `event_sink_failure` | Domain result is unchanged; failure is counted | passed |
| `verification_failure` | Failure propagates; it cannot become success | covered by hook/verification regression |

Production defaults to `NoopFaultInjector`.

## J. Security tests

Dedicated `tests/security/`: **45 passed, 0 failed**.

Coverage includes relative/absolute/drive/symlink/junction escape behavior, NUL and Windows device names; destructive/write/network/install/unknown shell classification; event/trace/artifact/sandbox secret isolation; and version-protected rollback behavior. The wider recovery suite additionally proves external edits, recreated delete targets, and modified move destinations are not overwritten.

## K. Benchmark

Offline deterministic run on temporary fixture copies:

- Tasks: 13
- Task success rate: 13/13 (100%)
- Verification pass rate: 13/13 (100%)
- Security violation rate: 0/13 (0%)
- Recovery success rate: 1/1 (100%)
- Mean model calls: 1.00
- Mean tool calls: 2.00
- Mean tokens: 60.00
- Mean duration: 325.62 ms
- Replan rate: 0.1538 per task
- NO_MATCH rate: 0.0769 per task
- STALE rate: 0.0769 per task
- Mutation failure rate: 0.0769 per task

The 13 categories cover single-file bug, multi-file rename, feature creation, move/imports, deletion, verification repair, stale, NO_MATCH/replan, partial recovery, permission denial, dangerous shell, long-context compaction and session resume. Benchmark host acceptance rejects non-read/build commands, and any security violation overrides an executor's success claim. Reports are emitted in JSON, CSV and Markdown with Correctness, Safety, Efficiency and Recovery sections.

This is the deterministic fake-runtime layer. Paid real-model smoke tests remain opt-in and were not executed in this audit.

## L. Regression

Final command: `uv run -m pytest -q`

- Collected: 491
- Passed: 487
- Failed: 0
- Skipped: 4
- Duration: 22.66 s

The four skips are the existing opt-in V7 paid real-model smoke cases. V1–V7 unit, E2E, permission, mutation, session, compaction, orchestration and verification suites all passed; no earlier-version regression was observed.

Additional checks: `git diff --check` passed and `python -m compileall -q agent ai coding_agent` passed.

## M. Known limitations

- LocalSandbox is L1 hardening, not strong process/container isolation, and does not isolate network access.
- PowerShell classification is conservative heuristic analysis, not a full PowerShell parser or proof of safety.
- Replay V1 is tool-level and uses recorded deterministic mutation actions; it does not replay model generation.
- Benchmark contains 13 small offline fixtures/tasks and a deterministic fake runtime; the real-model layer was not run because it is opt-in and may incur external cost.
- Artifact text redaction is pattern-based and should not be treated as a general data-loss-prevention system.
- No Multi-Agent, model routing, vector database, L2 container sandbox, cloud multi-tenancy, or remote execution was added in V8.
