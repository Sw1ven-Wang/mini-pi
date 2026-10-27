# Mini-Pi

A local-first, single-model coding agent harness with explicit permissions,
version-checked file changes, durable execution, and a minimal web workbench.

Mini-Pi 是一个轻量、本机单用户的 Coding Agent 工程。由同一个模型承担
Planner / Editor，Host 负责 Viewer、文件修改、Verifier、权限、恢复和审计。
当前实现版本为 **V11.1**；不引入多 Agent 或模型路由。

## 功能

- **明确的执行流程**：Plan → Evidence → Edit → Apply → Verify，支持受限 Replan。
- **安全文件修改**：显式 read/edit/patch/create/move/delete、版本校验、checkpoint 与补偿恢复。
- **统一权限**：PowerShell / Bash 分析、确定性硬拒绝、一次性与 Session 审批。
- **持久化 Runtime**：SQLite Run / Operation / Approval 状态，重启恢复与显式安全对账。
- **验收契约**：Host 注册检查、变更范围、不变量和人工验收；检查通过不等于自然语言需求全部完成。
- **本地工作台**：React + FastAPI + SSE，查看计划、Diff、审批、验证、恢复与事件。
- **运行环境**：默认 L1 本地约束；可选 digest-pinned Docker / Podman L2 隔离。
- **存储维护**：离线完整性检查、私有备份、恢复、保留清理与数据库压缩。

## 快速开始

需要 Python 3.11+、uv、Node.js 22 和 Git。Windows 验证使用 PowerShell，
Linux 使用 Bash。真实模型调用需要自行配置兼容 Provider 的 API Key，并可能产生费用。

```powershell
git clone https://github.com/Sw1ven-Wang/mini-pi.git
Set-Location mini-pi
uv sync --frozen --group dev
npm --prefix web ci
npm --prefix web run build
New-Item -ItemType Directory -Path .mini_pi -Force | Out-Null
Copy-Item web/provider-config.example.json .mini_pi/web-providers.json
```

示例配置使用 DeepSeek。根据服务端配置设置环境变量，不要把真实 Key 写进代码或提交到 Git：

```powershell
$env:DEEPSEEK_API_KEY = "your-api-key"
uv run python -m coding_agent.web `
  --allow-root "Projects=E:\Projects" `
  --provider-config ".mini_pi/web-providers.json" `
  --verify-command "uv run python -m pytest -q"
```

访问 <http://127.0.0.1:8765>。将 `E:\Projects` 换成实际项目根目录，
不要把整个磁盘、用户目录、私有状态或备份目录加入白名单。
验证命令应适用于所选工作区，并且只能由服务端配置。

Linux/macOS 下采用等价 shell 命令创建 `.mini_pi`、复制示例文件、设置环境变量，
并将 `--allow-root` 改为实际绝对路径。Linux Runtime 已有离线测试，macOS 尚未作为发布验收平台。

CLI 示例见 [demo_v7.py](demo_v7.py)，旧阶段示例保留在 `demo.py` / `demo_v6.py`。
这些示例可能修改指定的演示工作区；请先阅读环境变量与目录说明。

## 测试

```powershell
uv run python -m pytest -q
npm --prefix web run lint
npm --prefix web run test
npm --prefix web run build
```

真实前后端测试仅替换模型 Provider，不调用付费模型：

```powershell
Set-Location web
npx playwright install chromium
# Windows 的视觉回归使用 Edge：npx playwright install msedge
npm run test:e2e:integration
npm run test:e2e
```

最近一次本地完整验收记录（2026-10-15）：Windows Python **1113 passed / 11 skipped**；
Linux Runtime **219 passed / 1 skipped**；前端单测 **8 passed**；
真实 HTTP E2E **16 passed**，其中 **9 项实际重启服务**；UI / 视觉回归 **2 passed**。
这不是今日远端 CI 的通过承诺，也不代表真实模型任务质量或全部 L2 安全专项已经通过。
付费模型和 Docker / Podman 专项保持显式 opt-in。

## 安全边界

- 服务仅用于本机单用户，监听 `127.0.0.1`；不要通过反向代理或端口转发暴露公网。
- **L1 不是安全边界**。验证会运行项目代码，不能将未知或恶意仓库视为可信。
- API Key 保留在服务端；Provider 地址和验证命令不能由浏览器任意指定。
- 硬 DENY 不能通过审批覆盖；未知副作用、缺少资源停止证明的恢复不会自动重放。
- AUTO 默认关闭；L2 不可用时不静默降级。强隔离需求应独立验证容器配置。
- 运行状态和恢复材料位于工作区之外的私有目录；其中可能包含源码，不应公开上传。

详见 [安全说明](SECURITY.md) 与 [工作台及恢复维护文档](WEB_WORKBENCH.md)。

## 项目结构

```text
agent/            通用 AgentLoop、工具分发与审批协议
ai/               Provider、Model、消息与流事件
coding_agent/     编排、文件修改、权限、验证、Sandbox、Runtime 与 Web 适配
web/              React / TypeScript 工作台及浏览器测试
tests/            离线、安全、持久化与真实 HTTP 回归
```

更多说明：

- [项目架构总览](MINI_PI_PROJECT_ARCHITECTURE.md)
- [工作台、Session 恢复、存储维护与验收记录](WEB_WORKBENCH.md)
- [贡献指南](CONTRIBUTING.md)

## License

[MIT](LICENSE) © 2026 Sw1ven-Wang.
