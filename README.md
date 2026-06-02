# Zentao Auto Fixer Server

一个配合 Codex `zentao-bug-fixer` skill 使用的本地自动化服务。服务启动后会定时轮询自部署禅道中的 Bug，把符合条件的代码类 Bug 映射到对应 Git 仓库，调用 Codex 自动修复，并把修复提交推送到配置的目标分支。

这个项目只负责“调度”：轮询、筛选、去重、同步仓库、启动 Codex、记录状态、提交和推送。真正理解禅道 Bug、修改业务代码、验证、写禅道备注的能力来自已经安装好的 `zentao-bug-fixer` skill。

## 主要解决什么问题

团队里常见的代码类 Bug 通常要经历这些重复步骤：

1. 打开禅道查看某个产品的未解决 Bug。
2. 判断是否是代码问题、是否适合自动修复。
3. 找到对应 GitLab 项目和目标分支。
4. 拉取最新代码。
5. 让 Codex 按禅道上下文修复。
6. 验证、提交、推送。
7. 给禅道写原因和解决方案，并在整批 push 成功后统一标记为已解决。

本服务把这些步骤串起来，适合自部署禅道 + 自部署 GitLab 的团队在内网机器上运行。它不依赖禅道 webhook，也不要求禅道能访问你的本机。

## 前置条件

先确认这些条件已经满足：

- 已安装并可使用 Codex CLI。
- 已安装 `zentao-bug-fixer` skill。
- `zentao-bug-fixer` skill 里的 `scripts/zentao_client.py` 能读取你的禅道 Bug。
- 运行服务的机器能访问禅道。
- 运行服务的机器能 clone/fetch/push 对应 Git 仓库。
- 如果使用 SSH 仓库地址，本机已经配置好 SSH key。
- 如果使用 HTTPS 仓库地址，本机 Git credential 已经可用。

`zentao-bug-fixer` skill 默认安装路径通常是：

```bash
~/.codex/skills/zentao-bug-fixer
```

你可以先单独验证 skill：

```bash
python3 ~/.codex/skills/zentao-bug-fixer/scripts/zentao_client.py products
python3 ~/.codex/skills/zentao-bug-fixer/scripts/zentao_client.py bugs <product_id>
python3 ~/.codex/skills/zentao-bug-fixer/scripts/zentao_client.py bug <bug_id>
```

## 工作方式

```text
ZenTao Auto Fixer Server
        |
        +-- Poller: 定时轮询禅道产品 Bug
        |       |
        |       +-- 按 projects.json 找到产品对应 Git 仓库
        |       +-- 筛选未解决的代码类 Bug
        |       +-- 写入 SQLite，避免重复处理
        |       +-- 投递到 worker 队列
        |
        +-- Worker
                |
                +-- 同步 Git 仓库目标分支
                +-- 创建临时 worktree
                +-- 调用 codex exec + zentao-bug-fixer 一次处理整批 Bug
                +-- 提交批次修复 commit
                +-- push 到目标分支
                +-- 批量标记禅道 Bug 为 resolved/fixed
                +-- 记录处理状态和事件日志
```

同一个项目、同一个 Git 仓库、同一个目标分支中已经入队的 Bug 会合成一个批次处理：同步一次仓库，Codex 一次性修复这一批 Bug，验证通过后生成一个批次 commit 并 push。不同 Git 仓库可以并行处理；同一个 Git 仓库仍然串行，避免多个批次同时改同一条目标分支。

## 不做什么

为了降低自动化风险，本服务刻意不做这些事：

- 不创建 MR。
- 不 force push。
- 不删除或关闭 Bug。
- 不自动覆盖远端分支历史。
- 不重复处理已经自动修复过、后来又被激活的 Bug。
- 不把禅道账号密码、token、GitLab token 写进代码。
- 不重新实现 `zentao-bug-fixer` 的修复逻辑。

批次处理时，服务会强制 Codex 子进程只写禅道备注，不提前 resolve。只有当整批代码 commit 并 push 成功后，服务才会逐个把这一批 Bug 标记为 `resolved/fixed`。最终关闭 `closed` 通常仍由 QA 或人工确认。

## 快速启动

复制配置模板：

```bash
cp .env.example .env
cp projects.example.json projects.json
```

编辑 `.env`，至少配置：

```bash
AUTO_FIXER_POLL_INTERVAL_SECONDS=120
AUTO_FIXER_PROJECTS_FILE=projects.json

ZENTAO_BASE_URL=https://your-zentao.example.com/zentao
ZENTAO_ACCOUNT=your-account
ZENTAO_PASSWORD=your-password
ZENTAO_API_PREFIX=/api.php/v1

ZENTAO_RESOLVE_BUG_AFTER_COMMENT=0
ZENTAO_RESOLVED_BUILD=主干
```

编辑 `projects.json`，配置禅道产品到 Git 仓库的映射：

```json
{
  "projects": [
    {
      "name": "Example Product A",
      "enabled": true,
      "zentaoProductId": 8,
      "zentaoAssignedTo": "",
      "repoUrl": "git@gitlab.example.com:group/product-a.git",
      "targetBranch": "dev",
      "onlyCodeBugs": true,
      "maxBugsPerPoll": 3
    }
  ]
}
```

启动服务：

```bash
python3 -m zentao_auto_fixer.server
```

启动后会看到类似日志：

```text
自动解决 Bug 服务已开启：地址=http://127.0.0.1:8787，将每 120 秒轮询一次。按 Ctrl+C 退出服务。
配置：workers=2 enabled_projects=1 projects_file=... data_dir=...
```

按 `Ctrl+C` 退出，退出时会打印本次运行统计：

```text
自动解决 Bug 服务已退出。本次完成处理 1 个；自动修复 1 个，其中已提交推送 1 个、无代码变更 0 个；失败 0 个、同步冲突 0 个、转人工 0 个；退出时排队 0 个、运行中 0 个。
```

## 配置说明

### `.env`

常用配置：

| 变量 | 说明 |
| --- | --- |
| `AUTO_FIXER_HOST` | HTTP 查询服务监听地址，本机使用 `127.0.0.1`。 |
| `AUTO_FIXER_PORT` | HTTP 查询服务端口。 |
| `AUTO_FIXER_DATA_DIR` | 本地数据目录，保存 SQLite、仓库缓存、worktree 和日志。 |
| `AUTO_FIXER_POLL_INTERVAL_SECONDS` | 轮询间隔，单位秒。 |
| `AUTO_FIXER_WORKERS` | 后台 worker 数量。 |
| `AUTO_FIXER_PROJECTS_FILE` | 多项目映射文件路径。 |
| `AUTO_FIXER_CODEX_BIN` | Codex CLI 路径，默认 `codex`。 |
| `AUTO_FIXER_CODEX_TIMEOUT_SECONDS` | Codex 单次执行最长时间，默认 `1800` 秒；设为 `0` 表示不限制。 |
| `AUTO_FIXER_ZENTAO_CLIENT` | `zentao-bug-fixer` skill 的禅道 helper 路径，默认可填 `auto`。 |
| `AUTO_FIXER_RETRY_FAILED` | 失败任务是否允许下一轮自动重试，默认建议 `0`。 |
| `ZENTAO_BASE_URL` | 禅道根地址。 |
| `ZENTAO_ACCOUNT` / `ZENTAO_PASSWORD` | 禅道账号密码。 |
| `ZENTAO_API_PREFIX` | 禅道 REST API 前缀，常见为 `/api.php/v1`。 |
| `ZENTAO_RESOLVE_BUG_AFTER_COMMENT` | 建议保持 `0`；批次模式下服务会在 push 成功后统一 resolve。 |
| `ZENTAO_RESOLVED_BUILD` | 自动解决 Bug 时使用的 `resolvedBuild`。 |

完整说明见 [.env.example](.env.example)。

### `projects.json`

每个项目字段：

| 字段 | 说明 |
| --- | --- |
| `name` | 本地显示名，用于日志和状态记录。 |
| `enabled` | 是否启用这个项目映射。 |
| `zentaoProductId` | 禅道产品 ID。 |
| `zentaoAssignedTo` | 只处理指定指派人；留空表示处理该产品全部符合条件的 Bug。 |
| `repoUrl` | Git 仓库地址，支持 SSH 或 HTTPS。 |
| `targetBranch` | 修复后直接提交推送到这个分支。 |
| `onlyCodeBugs` | 是否只处理代码类 Bug，建议保持 `true`。 |
| `maxBugsPerPoll` | 每轮最多入队多少个 Bug。 |

## 自动处理规则

一个 Bug 需要同时满足：

- 属于启用的 `zentaoProductId`。
- 当前未解决、未关闭。
- 如果配置了 `zentaoAssignedTo`，指派人必须匹配。
- 如果 `onlyCodeBugs=true`，Bug 类型必须是代码类问题。
- 本地 SQLite 里没有处理过这个 Bug。

如果一个 Bug 已经自动修复过，后来又被重新激活或再次出现在待修复列表，本服务不会再次自动处理，会标记为 `manual_required`，留给开发人工处理。

## 查看状态

健康检查：

```bash
curl http://127.0.0.1:8787/health
```

查看所有处理任务：

```bash
curl http://127.0.0.1:8787/runs
```

查看单个 Bug：

```bash
curl http://127.0.0.1:8787/runs/<bug_id>
```

查看单个 Bug 的处理时间线：

```bash
curl http://127.0.0.1:8787/runs/<bug_id>/events
```

查看轮询记录：

```bash
curl http://127.0.0.1:8787/polls
```

查看 Codex 输出：

```bash
tail -f .auto-fixer/logs/bug-<bug_id>-codex.log
```

常见任务状态：

| 状态 | 含义 |
| --- | --- |
| `queued` | 已入队，等待 worker 处理。 |
| `running` | 正在同步仓库或修复。 |
| `pushed` | 已提交并推送到目标分支。 |
| `no_changes` | Codex 结束但没有产生代码变更。 |
| `sync_conflict` | push 时远端分支已有新提交，服务不会强推。 |
| `manual_required` | 需要人工处理，例如自动修复后又被激活。 |
| `failed` | 处理失败，查看 `error` 和 codex log。 |

如果某个 Bug 的 `events` 长时间停在 `codex_attempt`，同时 Codex 日志没有继续更新，通常表示 `codex exec` 卡住了。可以通过 `AUTO_FIXER_CODEX_TIMEOUT_SECONDS` 设置单次最长执行时间。超时后服务会终止当前 Codex 进程及其子进程，释放同仓库队列；如果 `AUTO_FIXER_CODEX_ATTEMPTS` 大于 1，会在同一个批次 worktree 内继续下一次尝试。

## 本地数据

默认数据目录是 `.auto-fixer`：

```text
.auto-fixer/
  state.sqlite3          # 任务状态和轮询记录
  repos/                 # Git 仓库缓存
  worktrees/             # 临时 worktree
  logs/                  # Codex 执行日志
```

如果要重新测试，可以先停止服务，再备份或删除 `state.sqlite3`。删除状态库会让服务忘记哪些 Bug 已经处理过，生产环境不要随意删除。

## GitLab 使用建议

本服务不需要 GitLab API token。它直接使用 `projects.json` 中的 `repoUrl` 执行 Git 命令。

建议：

- 使用专门的服务账号或 deploy key。
- 只给必要仓库权限。
- 不允许 force push。
- 目标分支如果是保护分支，需要允许服务账号 push。
- 生产环境先从非主干分支试运行。

## 禅道使用建议

建议先在禅道里明确哪些 Bug 类型属于代码问题。服务默认只处理代码类 Bug，避免把需求、配置、环境、测试数据问题交给自动修复。

如果希望批次 push 成功后自动进入已解决状态，保持：

```bash
ZENTAO_RESOLVE_BUG_AFTER_COMMENT=0
ZENTAO_RESOLVED_BUILD=主干
```

批次模式下，Codex 子进程会被强制只写备注，不提前 resolve；外层服务在 commit 和 push 成功后调用 `zentao-bug-fixer` skill 的 resolve 能力，把这一批 Bug 标记为 `resolved/fixed`。它不会执行最终关闭。

## 本机、内网和不同局域网

本服务采用轮询模式，所以不需要禅道主动访问你的本机。只要运行服务的机器能访问禅道和 GitLab，就可以工作。

如果你只是本机查看状态，`AUTO_FIXER_HOST=127.0.0.1` 即可。如果要让同一局域网其他机器访问状态接口，可以改成：

```bash
AUTO_FIXER_HOST=0.0.0.0
```

状态接口只适合内网使用。公开到公网前需要自行增加认证、反向代理和访问控制。

## 测试

```bash
python3 -m unittest
PYTHONPYCACHEPREFIX=.pycache-compile python3 -m compileall zentao_auto_fixer tests
```

## 开源注意事项

开源或提交代码前确认：

- 不提交 `.env`。
- 不提交真实 `projects.json`。
- 不提交 `.auto-fixer`。
- 不提交禅道账号、密码、token、cookie。
- 不提交私有 GitLab / 禅道域名，示例统一使用 `example.com`。
- 不提交包含真实 Bug 内容的 Codex 日志。

建议 `.gitignore` 至少包含：

```gitignore
.env
projects.json
.auto-fixer/
.pycache-compile/
__pycache__/
```

## License

如果准备正式开源，请在仓库中补充许可证文件，例如 `MIT`、`Apache-2.0` 或团队内部指定许可证。
