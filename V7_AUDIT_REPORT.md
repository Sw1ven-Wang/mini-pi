# Mini-Pi V7 审计报告

审计日期：2026-09-02

实验目标：在不引入多模型路由、Multi-Agent、向量检索或生产级 Sandbox 的前提下，以同一 Provider / Model 实现 Planner → Viewer → Editor → Verifier → Replan 的分阶段运行时。

## A. 修改文件

| Path | Reason |
|---|---|
| `coding_agent/agents/__init__.py` | 导出 V7 phase contracts 和角色 |
| `coding_agent/agents/models.py` | Plan、Evidence、EditorRequest、VerificationDecision 数据契约 |
| `coding_agent/agents/planner.py` | 无工具、隔离上下文的 Planner |
| `coding_agent/agents/viewer.py` | 确定性只读 Evidence Runtime |
| `coding_agent/agents/editor.py` | 隔离 Editor 与 version-bound Change IR 生成 |
| `coding_agent/agents/verifier.py` | VerificationPipeline 到 VerificationDecision 的确定性映射 |
| `coding_agent/repository_map.py` | Python AST 最小 Repository Map |
| `coding_agent/replanning.py` | 显式 replan triggers、revision 和次数上限 |
| `coding_agent/orchestration.py` | 独立 V7 状态机和运行指标 |
| `coding_agent/experiments.py` | Baseline/V7 指标比较与表格渲染 |
| `coding_agent/session.py` | 持久化 current_plan、plan_revision、replan reason |
| `coding_agent/compaction.py` | 压缩摘要保留完整 PlanState |
| `coding_agent/errors.py` | Planning、Editing、Replan limit 领域错误 |
| `coding_agent/search/glob.py` | 导航时忽略 `.mini_pi` runtime state |
| `demo_v7.py` | 显式 workspace 的真实模型 V7 演示入口 |
| `pyproject.toml` | 注册 opt-in `real_model` 测试标记 |
| `tests/fixtures/v7_task_set.json` | 12 类建议实验任务清单 |
| `tests/test_v7_*.py` | Stop Rules A–N 单元、集成、E2E、真实模型门控和对照实验 |

说明：工作树中原有 V3–V6 未提交改动均被保留；本报告只归纳本轮 V7 改动。

## B. Planner

- 输入：goal、constraints、session summary、Repository Map 摘要、recent unresolved diagnostics。
- 输出：最多 10 步且 revision 合法的 `TaskPlan`。
- 工具权限：空列表；拒绝任何 tool call。
- Planner 不接收完整聊天、源码全文或 mutation 工具。
- 初始 revision 固定由 Host 归一化为 1。

## C. Viewer

- 根据 `TaskPlan.target_files` 和 `evidence_queries` 执行确定性的 glob/search/read。
- 每个 `EvidenceSnippet` 同时携带 workspace-relative path、行号、内容和读取时的 `FileVersion`。
- 读取同步写入 `ObservationStore`，但 observation 仍只是 last-known cache；mutation 保留 disk final recheck。
- 硬限制：最多 20 文件、50 snippets、默认 100,000 chars（绝对上限 200,000）。
- 无匹配、缺失文件、读取失败及截断均进入 `unresolved_questions`；Viewer 没有 mutation 能力。

## D. Editor

- 输入仅为 `ChangeIntent + EvidenceBundle + 当前 diagnostics`，其中 evidence 已包含相关 observed versions。
- 不包含完整聊天、旧 shell 大日志、无关文件或 stale 长上下文。
- 与 Planner 共享同一个 Provider 和 Model 实例，但使用独立 Context。
- Editor 不直接落盘、不调用工具，只输出 `WorkspaceChangeSet`；Host 根据 operation kind 交给 Existing Mutation Runtime。
- Host 注入 authoritative version，拒绝越出 intent/evidence scope 的路径，以及未出现在 version-bound evidence 中的 `old_text`。

## E. Verifier

- 复用现有 `VerificationPipeline`、`Diagnostic` 和 `VerificationSummary`。
- Pipeline 失败或出现相对 baseline 的新增 ERROR 时判定 FAIL。
- FAIL 时保守地把当前 acceptance criteria 放入 `failed_criteria` 并设置 `should_replan=true`；全部检查通过且无新增 ERROR 才 PASS。

## F. Replan

触发条件已覆盖：

- `STALE_VERSION`
- `NO_MATCH`
- `AMBIGUOUS_MATCH`
- 目标文件不存在
- Viewer evidence 不充分
- Verification 新增 ERROR
- acceptance criteria 未满足
- 关键假设失效 / Editor 输出不可安全应用

Host 在调用新 Planner 前把本次 reason 放入最小 PlannerContext。候选计划的模型 revision 被忽略，Host 严格设置为上一版 `+1`。默认最多 3 次；达到上限后在再次调用模型之前抛出 `ReplanLimitExceededError`。

## G. Session

- `SessionState` 新增 `current_plan`、`plan_revision`、`latest_replan_reason`，并验证 plan/revision 一致性。
- `SessionStore` 原子 JSON 持久化会自然保存这些纯数据字段。
- `CodingSession.resume()` 恢复当前计划和 revision，不从聊天文本猜测计划。
- Compaction summary 显式保留 objective、constraints、acceptance criteria、completed/pending/blocked steps、revision 和 latest replan reason。

## H. Context Experiment

同一个 Fake Provider 实例、同一个 Model 实例、字节一致的 fixture、相同任务：

- V6 Baseline editor context：6,637 estimated tokens
- V7 Editor context：3,220 estimated tokens
- 减少比例：51.48%

Token 数由项目 `ApproxTokenEstimator` 统一估算，不冒充 Provider billing usage。

## I. Reliability Experiment

| Metric | V6 Baseline | V7 |
|---|---:|---:|
| Task Success | true | true |
| Model Calls | 8 | 4 |
| Input Tokens (estimated) | 15,359 | 4,042 |
| Output Tokens (estimated) | 583 | 570 |
| Editor Input Tokens (estimated) | 6,637 | 3,220 |
| Tool Calls | 14 | 17 |
| Read Calls | 8 | 10 |
| Search Calls | 1 | 3 |
| Unique Files Read | 8 | 5 |
| Mutation Attempts | 3 | 2 |
| NO_MATCH | 1 | 0 |
| AMBIGUOUS_MATCH | 0 | 0 |
| STALE_VERSION | 0 | 0 |
| Verification Attempts | 2 | 2 |
| Replan Count | 0 | 1 |
| Total Duration (single run) | 987 ms | 918 ms |

结论限于单次 deterministic FakeModel 核心任务：V7 减少模型调用、总输入上下文、Editor 上下文、无关文件和失败 mutation；显式 evidence refresh/replan 使总 tool/read/search 调用增加。时间只作本机观测，不作为性能结论。12 类任务清单已准备，但本表不伪装成 12-task aggregate。

## J. E2E Trace

核心 FakeModel E2E 使用真实临时仓库、Viewer、WorkspaceMutationRuntime 和 pytest VerificationPipeline：

```text
Plan revision 1
→ Evidence (definition / calls / tests / wrapper)
→ Edit (故意遗漏 secondary caller / compatibility alias)
→ Mutation Runtime apply
→ pytest FAIL
→ diagnostics
→ Replan revision 2
→ fresh Evidence
→ second Edit
→ Mutation Runtime apply
→ pytest PASS
```

最终确认内部 caller 使用 `compute_total`，兼容入口仍工作，且 `public_api.py` 字节级未修改。

## K. Full Regression

最终命令：

```powershell
uv run -m pytest -q
```

最终结果：397 collected、393 passed、0 failed、4 skipped，耗时 18.96s。

4 个 skip 均来自 `tests/test_v7_real_model.py`。当前进程没有 `DEEPSEEK_API_KEY`，也没有显式设置 `RUN_REAL_MODEL_SMOKE=1`，所以 Stop Rule M 尚未获得真实网络执行证据。运行方式：

```powershell
$env:DEEPSEEK_API_KEY = "..."
$env:RUN_REAL_MODEL_SMOKE = "1"
uv run -m pytest -q tests/test_v7_real_model.py
```

## L. Known Limitations

- Planner 仍可能产生错误计划。
- Viewer 第一版是 deterministic literal retrieval。
- Repository Map 仅提供 Python AST 顶层 class/function/imports。
- Verifier 主要依赖 host-defined deterministic checks。
- 当前实验没有 Multi-Agent。
- 当前实验没有 Model Routing / ModelProfile / EditModeRouter。
- 当前实验没有 semantic/vector retrieval。
- 当前实验没有 production sandbox。
- Token 数据是统一估算值，不是 Provider usage；单次 timing 不代表 benchmark 分布。
- Stop Rule M 因本机缺少真实模型凭据尚未 green；FakeModel 测试没有被当成真实模型结果。

## Stop Rule 状态

| Rule | 状态 | Evidence |
|---|---|---|
| A Plan Models | green | `test_v7_plan_models.py` |
| B Planner | green | `test_v7_planner.py` |
| C EvidenceBundle | green | `test_v7_evidence_models.py` |
| D Viewer | green | `test_v7_viewer.py` |
| E RepositoryMap | green | `test_v7_repository_map.py` |
| F EditorRequest | green | `test_v7_editor_request.py` |
| G Editor | green | `test_v7_editor.py` |
| H Verifier | green | `test_v7_verifier.py` |
| I Replan | green | `test_v7_replanning.py` |
| J Session PlanState | green | `test_v7_session_plan_state.py` |
| K Orchestrator | green | `test_v7_orchestration.py` |
| L FakeModel E2E | green | `test_v7_e2e.py` |
| M Real Model Smoke | blocked externally | 4 tests collected; credentials/opt-in absent |
| N Baseline vs V7 | green | `test_v7_experiment.py` and this report |
