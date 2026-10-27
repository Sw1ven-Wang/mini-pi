# Mini-Pi Web Workbench

Mini-Pi Web Workbench 是单模型编排与 V11.1 持久化运行内核的本机可视化入口。页面提供受限目录选择、Provider/Model 选择、实时阶段、Session、权限审批、Mutation Diff、验证结果、恢复对账与脱敏事件流。

## 1. 准备配置

复制并修改示例 Provider 配置：

```powershell
Copy-Item web\provider-config.example.json .mini_pi\web-providers.json
$env:DEEPSEEK_API_KEY = "your-api-key"
```

配置中的 `base_url`、API Key 环境变量名和允许模型由服务端固定。浏览器只能选择已经声明的 Provider 与 Model，不能读取 API Key，也不能提交任意模型服务地址。

## 2. 构建与启动

```powershell
Set-Location web
npm install
npm run build
Set-Location ..

uv sync --all-groups
uv run python -m coding_agent.web `
  --allow-root "Projects=E:\Coding" `
  --provider-config ".mini_pi\web-providers.json" `
  --verify-command "uv run python -m pytest -q"
```

然后访问 `http://127.0.0.1:8765`。`--allow-root` 可以重复提供；页面只能浏览这些根目录的子目录。

开发模式下分别运行：

```powershell
uv run python -m coding_agent.web `
  --allow-root "Projects=E:\Coding" `
  --provider-config ".mini_pi\web-providers.json"

Set-Location web
npm run dev
```

Vite 仅监听 `127.0.0.1:5173`，并把 `/api` 代理到本机 FastAPI。

## 3. 安全边界

- FastAPI 固定监听 `127.0.0.1`，拒绝非 localhost Host 与 Origin。
- 启动时产生随机 CSRF token；写请求必须同时具有同源 Cookie 和自定义请求头。
- 目录 API 只接受 `root_id + relative_path`，每次访问都会重新检查 canonical path 和符号链接边界。
- 权限模式仍由 workspace 的 `.mini_pi/settings.json` 决定，网页不能覆盖确定性硬 DENY。
- 审批只接受 `approve_once`、`approve_session` 或 `deny`，过期与重复审批返回冲突。
- Mutation Diff 使用只读 planning 生成并有大小上限，只保留在当前进程内，不进入 Session、Trace 或 Audit。
- 验证命令是服务端启动参数，浏览器不能提交 Shell 命令。
- 验证实际加载 workspace 代码，因此也通过 Sandbox 执行：默认 L1 使用环境变量白名单，不继承 Provider Key；配置 L2 时使用容器后端，启动失败不会回退宿主执行。
- 显式读文件规则优先于默认只读允许，Viewer 搜索和仓库索引也过滤未授权内容。批量修改分别检查每个路径，移动同时检查源与目标路径。
- 首版全局只允许一个活动任务；任务结束或取消后才允许切换 workspace。

## 4. 验证

```powershell
uv run python -m pytest -q tests/web

Set-Location web
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e
npm run test:e2e:integration
```

`test:e2e` 保留浏览器级 API mock 的 UI/视觉测试。视觉基线覆盖 1440×900、1024×768 和 390×844，并检查横向溢出；截图中隐藏随 Edge/Windows 版本变化的原生 textarea resize grip，应用样式仍严格比较。

`test:e2e:integration` 先构建生产 SPA，再启动 `tests.web.live_server`，通过真实 HTTP、CSRF、SSE、PermissionEngine、MutationRuntime、SessionStore 和验证子进程完成闭环。仅模型 Provider 使用确定性的 Fake 实现，不拦截或模拟浏览器 API 请求，不消耗真实模型费用。测试工作区位于临时目录，验证还断言服务端 Provider Key 未进入测试进程。

集成测试覆盖允许一次、本 Session 允许、拒绝及 Session 恢复、取消和过期审批、刷新恢复正确 workspace/Provider/Model、硬 DENY 不可审批、L2 不可用时失败关闭，以及浏览器断网后的事件重连。测试默认使用本机 Edge，与现有 UI E2E 保持一致；后端仅监听 `127.0.0.1:8876`，测试结束后由 Playwright 关闭。

针对代码审查问题的离线回归测试：

```powershell
uv run python -m pytest -q tests/security/test_review_regressions.py tests/security/test_verification_sandbox.py tests/web/test_task_manager_e2e.py
```

## 5. 当前边界

- 不提供账户、局域网访问、多租户、终端或内嵌代码编辑器。
- completed Session 可在列表查看摘要，但不会重新执行；非 completed Web Session 可使用原 Provider/Model 配置恢复。
- V11 的运行事件持久化并有界重放。Diff Preview 仍仅保存在内存，待审批恢复时通过只读规划重新生成；不把源码 Diff 写入 Session、Audit 或 Trace。

## 6. 代码审查修复验收（2026-10-13）

已修复 Shell 执行入口/副作用参数误放行、验证绕过 Sandbox、显式 read DENY 被默认规则覆盖、路径白名单授权整个批次、排队任务取消后占用活动槽位，以及刷新页面后 workspace/Provider/Model 错配。SSE 在审批状态切换时保持连接，重连时重新读取任务快照。

- Python 全量：878 passed / 11 skipped，新增 75 项回归测试；显式 opt-in 的真实模型和容器运行时测试仍按原约定跳过。
- 前端：6 项 Vitest 通过，TypeScript、ESLint 与生产构建通过。
- 浏览器：2 项现有 UI/视觉 E2E、7 项真实前后端 E2E 通过。
- 全量测试使用工程内独立临时目录；需要允许旧 uv 子进程访问其缓存。此环境权限限制不作为产品测试失败计入。

## 7. V11：统一内核、恢复与契约

Web 和 `demo_v7.py` 通过 `coding_agent.runtime.factory.build_coding_runtime` 装配，由 `RuntimeKernel.execute` 统一控制生命周期。旧 AgentLoop、原构造函数与 JSON SessionStore 继续兼容；它们不自动获得持久化副作用恢复能力。

### 状态与存储

- 分离 Session / Run / Step / Operation；进程重启继续同一 Run，终态后的明确重试创建关联 Session 下的新 Run。
- 默认存储：`~/.mini_pi/workspaces/<workspace_id>/runtime.db` 与私有 `recovery/`。可用服务端 `--state-root` 覆盖，禁止位于执行 workspace 内。
- SQLite schema version 1、SessionSnapshot format version 2；缺失 Session 版本按旧格式读取，未知高版本失败关闭。
- SQLite 使用 WAL、FULL 同步、revision 校验和每 workspace 单写者锁。执行意图、Proposal 游标及运行事件使用事务提交；观察者仍为 best-effort，不承担关键提交。
- 恢复材料保存精确原始字节，Windows 使用当前用户私有 DACL，POSIX 使用私有目录权限。它们不是脱敏审计数据，不提供网页下载，不挂载进 L2；不自动删除未解决的恢复材料。
- 单个恢复 blob 上限 32 MiB；源文件指纹默认上限 20,000 个文件 / 128 MiB。常见缓存目录不计入源清单，但本次显式修改目标始终计入。超过限制停止，不降级为不校验执行。

### 恢复协议

- 文件修改前提交前后状态与恢复材料；执行前重新判断策略、路径、版本与 Proposal。已满足后置状态时不重复写入。
- 部分批次逆向补偿；补偿同样核对当前版本，遇到外部修改或损坏材料保留现场并进入 `needs_reconciliation`。
- 恢复过程自身中断仍可再次对账。文件系统与 SQLite 不是跨文件 ACID 事务，不宣称 exactly-once 或断电零损失。
- 服务启动不自动执行遗留任务。中断任务需要明确点击“检查并继续运行”；等待审批可以恢复展示并由用户继续决定。
- 预算和 AUTO 安全状态不随恢复重置；未完成调用的次数仍占用配额。活动时长遇到崩溃会保守计算未知间隔，包含无法排除的停机时间；人工审批等待不计入活动时长。
- Windows 本地验证启用 kill-on-close Job Object；POSIX 保留进程组超时/取消清理。L2 在调用引擎前持久化容器/网络身份与所有权标签。结果不明的验证不自动重放，即使已有检查被标记 replay_safe，仍须有执行环境及进程静止证明；首版不提供网页“强制忽略”入口。
- 对账界面只展示游标、操作种类、相对目标、状态及未清理资源身份，不展示恢复原文。

### 审批与验收

- 审批持久化绑定 Run、Proposal、workspace、前置状态、策略指纹及 Contract revision，默认有效期 24 小时。
- 一次性批准只绑定同一 Operation；消费后尚未产生效果的同一操作可在重新对账后继续，不能授权新 Proposal。Session 授权仅恢复原有精确范围且要求策略一致。
- 过期、文件变化、策略变化、取消或重复提交不能执行。硬 DENY 不产生可覆盖的网页审批。
- Contract 由 Host 安装并冻结。Planner 的 acceptance 文本不直接改变契约；必需条件必须绑定已注册 Check ID 或人工确认。
- 默认 Check ID 为 `verification_pipeline`，覆盖范围为 `registered_checks_only`：检查通过不自动证明全部自然语言需求完成。
- 支持相对路径变更范围、最大变更文件数、禁止修改目标和受保护检查源文件。默认禁止模型修改 `.git` 与 `.mini_pi` 控制平面。受保护检查源在验证前后比对，不接受验证过程削弱检查后生成的证据。
- 证据绑定 Run、Contract revision、检查定义、策略及代码指纹；空检查、旧证据、未完成操作、未消费审批和未确认人工条件都不能产生成功。
- 可用 `--acceptance-config <server-owned-json>` 加载 Host 契约；建议配置位于执行 workspace 之外。使用 `default_contract(verify_command)` 生成与服务端命令一致的定义，再添加 Requirement / Invariant，避免手写定义摘要。
- 人工确认接口为 `POST /api/tasks/{session_id}/acceptance/{requirement_id}`，要求当前 revision、同源 Cookie 和 CSRF。确认绑定当前代码版本，不是工具执行批准。

### 兼容与操作

- 现有 REST 路由及三种审批结果保持兼容；快照新增 run_id、runtime_status、revision、event_sequence、acceptance 与只读 recovery 报告。旧 status 是兼容投影，V11 页面以 runtime_status 区分中断、对账与等待验收。
- 旧 JSON 导入幂等且保留原文件；旧 completed 记录不补造契约证据，旧 active/interrupted 记录即使重复导入仍不得自动执行。可基于当前文件明确新建任务，不能把遗留操作当作已证明安全的恢复点。
- 旧 JSON 不再与 SQLite 双写争夺运行状态。回退旧程序不能继续未解决的 V11 Run；应保留整个私有状态目录，完成对账后再进行受控导出或新建任务。
- `demo_v7.py` 保留原环境变量，新增 `MINI_PI_V11_RUN_ID` 作为明确恢复入口；旧 V6 示例仍用于非持久化通用 AgentLoop 教学。
- SSE 在重启后重新 bootstrap 获取新 CSRF Cookie，再取权威快照和恢复事件流；永久关闭的 EventSource 会重新建立。预览 ID 可以变化，Proposal/审批身份不会因重启扩大授权。

### V11 验收记录（2026-10-13）

以下记录来自实际执行，不等同于真实模型质量评测：

| 验收项 | 实际结果 |
| --- | --- |
| Windows Python 全量 | 1051 passed / 11 skipped；相对 878 基线新增 173 项通过 |
| Linux Runtime 核心 | 166 passed / 0 skipped |
| Vitest | 7 passed |
| TypeScript / ESLint / 生产构建 | 全部通过，E2E TypeScript 也纳入类型检查 |
| 原有 UI / 三宽度视觉回归 | 2 passed |
| 实际 HTTP / 服务重启 E2E | 12 passed，其中 5 项真实替换服务端进程 |

- 原有 Python 非跳过基线保持通过；新增覆盖状态转换、持久化提交失败、四类文件操作硬中断、补偿中断、预算、审批、契约与迁移的回归测试。
- Linux 核心 Runtime 测试在已有 Miniconda 镜像中执行：无网络、非 root、只读代码挂载、tmpfs 测试数据；复用纯 Python 测试依赖，不下载镜像或依赖。
- 前端 Vitest、TypeScript、ESLint、生产构建与原有 2 项 UI/视觉测试通过。
- 真实 HTTP E2E 扩展为 12 项，其中 5 项直接终止并替换服务端进程，覆盖审批恢复、写入后崩溃、外部修改、人工验收和不刷新页面的重连；只替换模型 Provider。
- `.github/workflows/v11-runtime.yml` 提供 Windows/Linux 的完整回归与 Chromium 实际 HTTP E2E；本地未触发 GitHub CI，不把配置存在写成 CI 已通过。
- 原有付费模型、Docker/Podman L2 安全专项仍按 opt-in 规则跳过。Linux 核心恢复容器测试不替代这些 L2 专项。
- 移除允许根或替换目录不能通过隐藏未解决 Run 获得新的执行槽位；服务启动会要求先显式对账，不读取被移出白名单的项目内容。

最终回归命令仍为 Python 全量、前端 test/lint/build、`test:e2e` 与 `test:e2e:integration`。pytest 默认只收集 `tests/`，不会误收集浏览器临时工作区中的测试项目。

## 8. V11.1 恢复对账与存储维护

### 显式恢复闭环

- 新增 `POST /api/tasks/{session_id}/reconcile`，请求必须包含 `expected_revision` 与 `action`；沿用 localhost、Cookie、CSRF 边界，额外字段和未知动作拒绝。过期、重复或执行器仍在运行时返回 409。
- `retry_verification` 只重试验证，不重新调用 Editor 或重放文件变更。需要所有注册检查由 Host 声明 `replay_safe=true`、验证启动时的源码/策略/契约指纹仍一致，并有资源停止证明。默认仍为 false，不能因为命令叫 pytest 就假定其没有副作用。
- 对账授权保存一次性重试条件；真正开始验证前再次检查，授权后的人为修改不能绕过版本绑定。当前服务端验收配置变化也会阻止重试。
- `end_preserving_files` 在资源生命周期已确认时失效未决审批、结束 Run 并释放任务槽位，不写入或回滚工作区文件，也不伪造成功证据。未解决操作的恢复材料继续受保留保护。
- 快照中的 recovery 提供可用动作、阻止原因、相对文件目标及资源身份；不返回原始命令、环境或恢复字节。界面明确区分可重试与需离线处理，不提供 force/bypass。
- 非终态 Run 的 SSE 不再被旧 `status=failed` 投影提前关闭。替换恢复任务的传输适配器时关闭旧事件流，让浏览器续接新的事件源。

### 进程生命周期边界

- 验证进程先持久化 reservation。Windows 使用真实基础 Python 解释器以 `-I -S` 启动固定 stdin 桥；绑定 kill-on-close Job 并持久化 ownership 后，才发送实际 Shell 命令。避免 venv 启动器或 Shell 在绑定前派生未受管子进程。
- 重启后只查询绑定到 lease 身份的命名 Job。Job 不存在或活动进程为零才构成停止证明；访问错误、spawn/attach 间隙及旧版未跟踪验证均保持阻止。Job 生命周期依据 [Microsoft 官方说明](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。
- POSIX 正常完成可记录进程组停止；Host 硬崩溃后的旧进程组不能靠 PID/PGID 猜测已停止，因此仍阻止自动重试和释放工作区。未确认清理的 L2 租约同样阻止；不盲目删除容器或静默降级。
- 这些机制是生命周期控制，不把 L1 变成安全沙箱，也不承诺任意 Shell、网络或安装操作 exactly-once。

### 离线维护 CLI

先停止 Web/CLI 服务。维护与正常执行共用独占写者锁；`--state-root` 必须与服务启动时一致，路径填错不会创建一个空数据库来假装成功。备份应放在 allow-root 之外，包含原始源码，不能当成脱敏审计导出。

```powershell
$runtimeArgs = @('--workspace', 'E:\Projects\demo', '--state-root', 'E:\MiniPiState')
uv run python -m coding_agent.runtime @runtimeArgs inspect
uv run python -m coding_agent.runtime @runtimeArgs backup --destination 'E:\MiniPiBackups\before-maintenance'
uv run python -m coding_agent.runtime @runtimeArgs prune --keep-days 30 --max-blob-bytes 536870912
uv run python -m coding_agent.runtime @runtimeArgs prune --keep-days 30 --max-blob-bytes 536870912 --apply --backup-directory 'E:\MiniPiBackups\before-prune'
uv run python -m coding_agent.runtime @runtimeArgs compact --backup-directory 'E:\MiniPiBackups\before-compact'
uv run python -m coding_agent.runtime --workspace 'E:\Projects\demo' --state-root 'E:\MiniPiRestoredState' restore --source 'E:\MiniPiBackups\before-maintenance'
```

- `inspect` 检查 SQLite 完整性并报告 DB/blob 大小。`prune` 默认仅返回计划；只有 `--apply` 才清理，且必须先成功提交一份新私有备份。备份目录必须全新，不覆盖已有备份。
- 仅清理超过保留期且无不确定操作/资源的终态 Run；未解决 Run、待对账操作及其恢复材料不因配额不足被删除。共享 blob 保守追踪引用，存在未解决材料时保留全部 blob。
- `max-blob-bytes` 是本次维护的 blob 容量目标，不是运行时硬配额，也不包含备份占用。保护条件导致无法达标时报告 `quota_met=false`，应用清理的 CLI 返回退出码 2；不进行紧急强删。
- `compact` 在备份后执行 SQLite VACUUM 与 WAL checkpoint，回收已删除记录占用的页。备份/压缩可能需要额外磁盘空间；空间不足时不得继续文件执行。
- 备份使用 SQLite Backup API、私有目录、SHA-256 清单和持久化完成标记；包括尚未提交的 `.blob-*` 崩溃材料。恢复先验证格式、DB 完整性及引用文件，再写入全新目录，绝不覆盖在线数据库。
- 半恢复目录保留 `RESTORE_IN_PROGRESS`；RuntimeStore 拒绝执行，不会将其视为新空状态。保留该目录用于诊断，修复空间/权限问题后换一个全新目标重试。维护不注册为模型工具，也不提供浏览器维护 API；由操作者停止服务后离线调用。

### 兼容迁移与 CI

- SQLite 协议版本升级到 2，V11 schema 1 在持有写者锁的事务内迁移；表结构、Run/Session 数据和已有事件不重写。未知未来版本仍拒绝，旧 Session/JSON 的既有迁移规则不变。
- 版本 2 同时是旧写者栅栏：V11 旧程序不能忽略新重试条件后继续写入。旧的未跟踪验证不会因迁移自动获得重放权限。需要回退时使用升级前的完整备份，不要手工修改 `user_version`；恢复 schema 1 备份也会发布为当前协议版本。
- CI 扩展为 Windows/Linux × Python 3.11/3.13，包含完整离线回归、前端单测/lint/build、真实 HTTP/重启 E2E、布局及测试报告。视觉、HTTP 和重启夹具目录分离，避免相互清理证据。
- Windows 沿用已有 Edge 像素基线；Linux 执行三宽度无溢出检查并生成截图供复核，不声称具有尚未建立的 Linux 像素基线。
- Docker/Podman L2 为独立手动门禁，要求 digest-pinned 镜像；选择运行后，缺引擎、缺镜像、零用例或 skip 都不能算通过。真实付费模型仍 opt-in，不能混入 Fake Provider 指标。
- 当前只完成 CI 配置与本地同类验收，未推送或触发远端 Actions，也未把 Linux 核心容器测试当成 L2 安全专项结果。

### V11.1 实测验收（2026-10-15）

| 验收项 | 实际结果 |
| --- | --- |
| Windows Python 3.13 全量 | 1113 passed / 11 skipped；比 V11 新增 62 项通过，原非跳过基线保持通过 |
| Linux Python 3.13 Runtime | 219 passed / 1 skipped；仅跳过 Windows 专用启动门用例 |
| Vitest | 8 passed |
| TypeScript / ESLint / 生产构建 | 全部通过 |
| Windows UI / 三宽度像素与布局回归 | 2 passed，沿用基线且未自动更新 |
| 真实前后端 HTTP / 重启 E2E | 16 passed，其中 9 项实际终止并替换服务进程 |
| CI 配置 | YAML 解析与矩阵校验通过；远端 Actions 尚未运行 |

- 新增回归覆盖：活跃命名 Job 不得误判停止、命令不得在归属持久化前启动、20 次重复真实启动、过期/重复/跨任务/CSRF 对账、重试后再次改文件、保留修改结束任务、SSE 旧订阅切换、schema 1 迁移和旧备份恢复。
- 存储回归包含真实维护子进程、SQLite 只读连接、SQLite page limit 耗尽与事务回滚、备份写入失败、损坏/缺失文件、半恢复阻断、共享引用、未版本化租约保护、私有备份和 VACUUM。
- 新增 4 项重启 E2E 验证：同一 Run 连续两次硬崩溃后只重试验证；外部修改阻止重放但能保留并结束；未声明 replay-safe 的检查被拒绝；Windows owned Job 停止证明。原有 12 项 HTTP/E2E 均保留并通过，Provider 仍为 Fake。
- Linux Runtime 在本机已有镜像中执行，无网络、非 root、只读代码挂载及 tmpfs 测试目录；不下载依赖或镜像，不代表 Docker/Podman L2 安全专项验收。
- Windows 的 11 项 skip 仍为原有 6 项 L2 专项、4 项付费模型和 1 项 symlink 权限测试。真实模型质量、远端四格 CI 和 opt-in L2 结果不在上述通过数中。
- 最终检查未发现测试服务或心跳进程残留；没有对用户实际 Session 执行清理，维护功能仅在隔离夹具上验收。既有工作区未提交修改保留，未创建提交或推送。

最终本地回归命令：

```powershell
uv run python -m pytest -q --basetemp=.tmp_v111_release_verified -p no:cacheprovider
npm --prefix web run lint
npm --prefix web run test
npm --prefix web run test:e2e:integration
npm --prefix web run test:e2e
```

`test:e2e:integration` 包含 TypeScript 检查与生产构建。保留恢复材料时无法满足容量目标是安全结果，不应通过删除待对账 Run、修改数据库版本号或打开宽泛权限来消除该提示。
