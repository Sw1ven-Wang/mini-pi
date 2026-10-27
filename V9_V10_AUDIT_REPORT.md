# Mini-Pi V9–V10 迭代审计报告

> 计划：Claude Code 式统一安全执行、语义 AUTO 与 L2 Sandbox  
> 完成日期：2026-09-01  
> 迭代前基线：470 passed / 4 skipped  
> 当前离线回归（含 Web Workbench）：803 passed / 11 skipped / 0 failed

## 1. 结论

V9 与 V10 的代码、离线测试、配置入口、演示组装、观测指标和 Benchmark 报告已落地。统一执行链现在是：

```text
ToolCall
→ capability + workspace path normalization
→ PowerShell/Bash AST → shared Shell IR
→ deterministic policy (DENY > ASK > ALLOW)
→ permission mode
→ approval or optional semantic AUTO
→ L1/L2 sandbox
→ execution
```

确定性硬 DENY 不能被配置规则、会话批准或语义分类器覆盖。AUTO 和 L2 均采用显式配置；AUTO 默认关闭，显式 L2 不可用时失败关闭，不会静默降级到 L1。

## 2. V9 交付

### 2.1 Shell IR 与解析器

- `coding_agent/safety/shell_ir.py`：共享 Shell 类型、parser status、command/redirection node 与 parse result。
- `coding_agent/safety/powershell_ast_bridge.ps1`：固定桥接程序使用 `Parser.ParseInput`，命令只从 stdin 输入且不执行。
- `coding_agent/safety/shell_parsers.py`：PowerShell persistent bridge 与 tree-sitter Bash parser；规范化 PowerShell 内置别名并展开已知 Bash wrapper。
- `coding_agent/safety/command_analysis.py`：按 command node 分类，复合命令采用最严格风险；无整串正则策略扫描。
- `coding_agent/safety/command_policy.py`：保留 V8 兼容接口，底层切换到 AST/IR。

已覆盖 compound command、pipeline、subshell、command substitution、wrapper、环境赋值、重定向、FD duplication、动态表达式和解析失败。`npm/pnpm/yarn test` 被视为项目定义的任意脚本，归入 OPAQUE/ASK，而不是伪装成参数已验证的测试。

### 2.2 权限、设置与审批

- `agent/permissions.py` 扩展决策元数据；`coding_agent/safety/permissions.py` 成为统一策略入口。
- `coding_agent/safety/settings.py` 提供 versioned settings、五种权限模式、token rule、AUTO 与 sandbox 配置。
- 设置顺序为 built-in、user、workspace、session；重复 rule ID 内容冲突时报错。
- `default` 对 mutation 询问；`accept_edits` 允许 workspace 内 create/edit/patch；`plan` 禁止 mutation/Shell；`dont_ask` 把 ASK 转为 DENY。
- 审批支持 once/session/deny；session grant 绑定 workspace 指纹、工具、capability 和精确命令哈希或规范化目标。
- `.gitignore` 只放行可提交的 `.mini_pi/settings.json`，继续忽略运行状态。
- Bash 与 PowerShell 都在 Dispatcher 前经过统一策略，并在工具内部保留硬拒绝 guard。
- Planner→Editor 的 Host-owned `WorkspaceChangeSet` 在 Mutation Runtime apply 前也映射为无内容的 capability/target 摘要并进入同一权限、AUTO 与审批链。

不可覆盖硬拒绝包括 Shell 文件删除/移动、`Invoke-Expression`、EncodedCommand、`git reset --hard`、强制 clean 和 force push。文件参数、重定向及 Shell cwd 均经过 workspace containment；已存在 symlink/junction 会在 resolve 时检查真实目标。

### 2.3 审计

新增 policy evaluated、approval requested/resolved 和 parser fallback 事件。权限审计采用字段白名单，只落盘：

- command SHA-256；
- 脱敏的规范化 operation；
- workspace 相对 target；
- risk、decision、rule ID 和 parser status。

原始命令、原始 arguments、环境变量、源码和完整工具输出不会写入权限审计。CLI 审批展示风险、脱敏操作、规范化目标和命中规则。

## 3. V10 交付

### 3.1 Semantic AUTO

- `coding_agent/safety/auto_classifier.py` 实现两阶段 reviewer。
- 只有 deterministic ASK 会调用分类器；deterministic DENY 永不调用。
- 第一阶段只接受精确 ALLOW；其它输出进入严格 JSON 复核。
- 超时、异常或损坏输出统一回到人工 ASK。
- 分类器只能把 ASK 变成一次性 allow/block，不能创建持久允许规则。
- 连续 block 3 次或累计 block 20 次后降级到 default。
- Shell 全局 allow、解释器 wildcard、package-script wildcard 在 AUTO 中被忽略。

分类请求只包含脱敏后的规范化 IR、当前要求、结构化安全约束、近期 mutation 摘要、相对路径与有界 git metadata。解释器 `-c/-e/-Command` 的代码体替换为 `<opaque-code>`；credential 参数、URL query 和宿主绝对路径不会进入请求。

`SafetyConstraints`、AUTO 模式与 block counters 已加入 Session JSON 和 compaction summary，恢复后继续生效。分类调用、allow/block/fallback、降级、延迟及估算 token 均进入 Metrics。

### 3.2 ContainerSandbox L2

- `coding_agent/sandbox/container.py`：Docker/Podman backend，engine auto probe 与显式 fail-closed。
- `coding_agent/sandbox/egress_proxy.py`：内部 HTTP(S) allowlist proxy。
- `coding_agent/sandbox/factory.py`：按 settings 构造 L1 或 L2，不做 L2→L1 静默降级。

L2 强制 digest-pinned image、非 root、只读 rootfs、只挂载 workspace、`.git` 只读、tmpfs、cap-drop、no-new-privileges、CPU/内存/PID/输出/时间限制，并且不挂载 host socket。默认网络为 none。

allowlist 模式使用 internal worker network 加 proxy egress network。代理只允许白名单域名的 HTTP/HTTPS 80/443，拒绝直接 IP、私有/保留地址、未授权域名和二次 DNS 重绑定。网络拒绝从 proxy log 汇总成结构化 sandbox violation 指标。

AUTO Shell 在语义允许后仍要求真实 L2 类型和 engine/image preflight；预检失败回到人工 ASK，不会自动在 L1 执行。

### 3.3 Benchmark

Benchmark schema/result 新增 profile、policy asks、approval interruptions、AUTO calls/allows/blocks、parser fallbacks、sandbox violations、分类延迟与估算 token。JSON/CSV/Markdown 可按 V8/V9/V10 profile 分组报告安全违规率、任务成功率、自动允许率和分类成本代理。

真实模型 run 必须使用独立 profile/批次，不能与 Fake Provider 离线指标混合解释。

## 4. 配置示例

项目设置文件位置为 `<workspace>/.mini_pi/settings.json`：

```json
{
  "version": 1,
  "permission_mode": "auto",
  "rules": [
    {
      "id": "deny-network-tool",
      "effect": "deny",
      "tool": "powershell",
      "command": ["invoke-webrequest", "*"]
    }
  ],
  "auto": {
    "enabled": true,
    "model_id": "classification-model-id",
    "timeout_seconds": 15,
    "require_l2_for_shell": true
  },
  "sandbox": {
    "level": "L2",
    "engine": "auto",
    "image": "registry.example/mini-pi@sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "network": "none",
    "cpus": 1,
    "memory_mb": 512,
    "pids_limit": 128
  }
}
```

示例 digest 只是格式占位，实际必须替换为本机已安装镜像的真实 digest。allowlist 网络还需配置非空 `allowed_domains`，且镜像需要包含所选 Shell 与 Python（proxy sidecar 使用 Python）。

## 5. 验证结果

最终命令：

```powershell
uv run python -m compileall -q ai agent coding_agent demo.py demo_v6.py demo_v7.py
uv run python -m pytest -q
git diff --check
```

pytest 结果：

- collected：814
- passed：803
- failed：0
- skipped：11

其中四个 skipped 是需要外部凭据和费用的 V7 real-model smoke tests，六个是按 Docker/Podman 参数化的 L2 opt-in tests，另一个是当前 Windows 环境无法创建目录 symlink 的条件测试。离线默认套件包含 250 个双 Shell 对抗参数样例，危险/未知语料没有 false-allow。

L2 opt-in 命令：

```powershell
$env:RUN_CONTAINER_SANDBOX_TESTS = "1"
$env:MINI_PI_L2_IMAGE = "registry/image@sha256:<64-hex-digest>"
uv run python -m pytest -q tests/security/test_v10_container_runtime.py
```

这些测试验证 workspace 写入、rootfs 与 `.git` 只读、环境 secret 隔离、network none、域名 allowlist、未授权域名、直接 IP 和 direct-DNS 旁路。当前开发机可找到 Docker CLI，但 Docker Linux daemon 未运行；Podman 未安装，且未提供 digest-pinned 测试镜像。因此六个真实引擎用例保持显式跳过，本报告不把静态/Mock 测试冒充真实容器验证。

## 6. 已知边界

- L1 是受限进程环境，不是安全边界；不应承载 AUTO 的不确定 Shell 自动执行。
- L2 网络 allowlist 仅支持 HTTP 和 HTTPS CONNECT；其它协议按设计拒绝。
- AUTO token 指标是项目现有 tokenizer-free estimator 的估算值，不是 Provider 账单 usage。
- Provider 协议当前不暴露真实计费 usage；如后续补充 usage event，应单独记录 classifier usage。
- L2 的最终强度仍依赖容器引擎、宿主配置和固定镜像内容；Docker/Podman CI 必须执行 opt-in tests。
- Session approval 使用精确命令哈希，安全范围比“规范化命令前缀”更窄；等价但格式不同的命令会重新询问。
- L3 remote sandbox、云端多租户、Multi-Agent 与模型路由不在本轮范围。

## 7. 主要测试文件

- `tests/security/test_v9_shell_ir.py`
- `tests/security/test_v9_permission_modes.py`
- `tests/security/test_v10_auto_mode.py`
- `tests/security/test_v10_container_sandbox.py`
- `tests/security/test_v10_container_runtime.py`
- `tests/observability/test_audit.py`
- `tests/observability/test_metrics.py`
- `tests/observability/test_runtime_events.py`
- `tests/test_benchmark.py`
