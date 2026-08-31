# Zentao Auto Fixer Server

一个配合 `zentao-bug-fixer` skill 使用的本地自动化服务。服务启动后会定时轮询自部署禅道中的 Bug，把状态为「激活」的代码类 Bug 交给 AI 引擎（Codex 或 Claude Code，按项目配置）先分诊再修复，并把修复提交推送到配置的目标分支。

这个项目只负责“调度”：轮询、筛选、去重、同步仓库、启动 AI 引擎、记录状态、提交、推送和回写禅道。真正理解禅道 Bug、判断该改哪个仓库、修改业务代码和验证的能力来自 AI 引擎和已经安装好的 `zentao-bug-fixer` skill。

## 主要解决什么问题

团队里常见的代码类 Bug 通常要经历这些重复步骤：

1. 打开禅道查看某个产品的未解决 Bug。
2. 判断是否是代码问题、是否适合自动修复。
3. 找到对应 GitLab 项目和目标分支。
4. 拉取最新代码。
5. 让 AI 按禅道上下文分诊并修复。
6. 验证、提交、推送。
7. 给禅道写原因和解决方案，并在整批 push 成功后统一标记为已解决。

本服务把这些步骤串起来，适合自部署禅道 + 自部署 GitLab 的团队在内网机器上运行。它不依赖禅道 webhook，也不要求禅道能访问你的本机。

## 前置条件

先确认这些条件已经满足：

- 已安装并可使用 Codex CLI 或 Claude Code CLI（按 `projects.json` 里的 `agent` 选择）。
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
                +-- 调用 codex / claude + zentao-bug-fixer 分诊并处理整批 Bug
                +-- 提交批次修复 commit
                +-- push 到目标分支
                +-- 批量标记禅道 Bug 为 resolved/fixed
                +-- 记录处理状态和事件日志
```

同一个项目、同一个 Git 仓库、同一个目标分支中已经入队的 Bug 会合成一个批次处理：同步一次仓库（配了后端仓库就同步两个），AI 一次性分诊并修复这一批 Bug，验证通过后每个仓库各生成一个批次 commit 并 push。不同 Git 仓库可以并行处理；同一个 Git 仓库仍然串行，避免多个批次同时改同一条目标分支。

## 不做什么

为了降低自动化风险，本服务刻意不做这些事：

- 不创建 MR。
- 不 force push。
- 不删除或关闭 Bug。
- 不自动覆盖远端分支历史。
- 不重复处理已经自动修复过、后来又被激活的 Bug。
- 不把禅道账号密码、token、GitLab token 写进代码。
- 不重新实现 `zentao-bug-fixer` 的修复逻辑。

批次处理时，AI 子进程完全不碰禅道，也不做 git 提交。只有当整批代码 commit 并 push 成功后，服务才会逐个写禅道备注并标记为 `resolved/fixed`。最终关闭 `closed` 通常仍由 QA 或人工确认。

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
      "agent": "claude",
      "fallbackAgent": "codex",
      "skipPlatforms": ["android", "mac"],
      "processUiBugs": false,
      "allowFullXcodeBuild": false,
      "app": {
        "repoUrl": "git@gitlab.example.com:group/product-a-app.git",
        "targetBranch": "dev"
      },
      "backend": {
        "repoUrl": "git@gitlab.example.com:group/product-a-api.git",
        "targetBranch": "pre-release"
      },
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
| `AUTO_FIXER_MAX_AGENT_RUNS_PER_DAY` | 每天最多启动多少次 AI，默认 `20`。一批 Bug 通常算一次；Claude 额度耗尽后启动 Codex 后备会再算一次。到顶后不再启动后备，防止连环烧钱。 |
| `AUTO_FIXER_PROJECTS_FILE` | 多项目映射文件路径。 |
| `AUTO_FIXER_CODEX_BIN` | Codex CLI 路径，默认 `codex`。 |
| `AUTO_FIXER_CLAUDE_BIN` | Claude Code CLI 路径，默认 `claude`；`agent=claude` 的项目会用它。 |
| `AUTO_FIXER_CODEX_TIMEOUT_SECONDS` | AI 引擎单次执行最长时间，默认 `1800` 秒；设为 `0` 表示不限制。两种引擎共用。 |
| `AUTO_FIXER_ZENTAO_CLIENT` | `zentao-bug-fixer` skill 的禅道 helper 路径，默认可填 `auto`。 |
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
| `skipPlatforms` | 暂时不处理的端，可填 `android`、`ios`、`mac`、`windows`、`web`。按 Bug 标题里的方括号标记（如 `【android】`）判断，只有标题标注的端**全部**在这个列表里才跳过，不入队也不调 AI。留空表示都处理。 |
| `processUiBugs` | 是否处理标题带完整 `【UI】` / `[UI]` 标签的 Bug，大小写不敏感。默认 `false`，跳过且不调用 AI；只有明确改成 JSON 布尔值 `true` 才处理。普通正文里的 `UI` 或 `【UI设计】` 不算该标签。 |
| `allowFullXcodeBuild` | 是否允许 AI 运行 `xcodebuild build/archive` 等完整构建。默认 `false`，只跑与改动直接相关的测试用例；设为 `true` 才允许完整构建。 |
| `agent` | 用哪个 AI 引擎修这个项目，可填 `codex` 或 `claude`，不填默认 `codex`。可执行文件路径分别由 `AUTO_FIXER_CODEX_BIN` 和 `AUTO_FIXER_CLAUDE_BIN` 指定。 |
| `fallbackAgent` | 可选后备引擎。当前仅在主引擎明确报告额度耗尽时切换；普通报错、超时或鉴权失败不会切换。每次后备启动也计入每日 AI 启动上限。 |
| `app.repoUrl` / `app.targetBranch` | App 客户端仓库和目标分支。同一个仓库同时覆盖 Android 和 iOS。 |
| `backend.repoUrl` / `backend.targetBranch` | 后端仓库和目标分支，可留空。留空时 AI 判定为后端问题的 Bug 会被打回给提 Bug 的人。 |
| `repoUrl` / `targetBranch`（旧写法） | 顶层写法仍然兼容，等价于 `app`。 |
| `onlyCodeBugs` | 是否只处理代码类 Bug，建议保持 `true`。 |
| `maxBugsPerPoll` | 每轮最多入队多少个 Bug。 |

## 自动处理规则

一个 Bug 需要同时满足：

- 属于启用的 `zentaoProductId`。
- 禅道状态是「激活」（`active`）。
- 如果配置了 `zentaoAssignedTo`，指派人必须匹配。
- 如果 `onlyCodeBugs=true`，Bug 类型必须是代码类问题。
- 本地 SQLite 里没有成功或明确无法修复的终态；技术失败允许一次自动重试。
- 标题标注的端不在 `skipPlatforms` 里（标题没写端的照常处理）。
- 标题不带完整 `【UI】` / `[UI]` 标签；如需处理此类 Bug，项目必须显式配置 `processUiBugs=true`。
- 禅道备注里没有 AI 处理过的标记（备注尾注含 `zentao-bug-fixer`）。换机器或清空本地数据库后，这条标记仍然能挡住重复修复。

### 分诊和修复

入队后每一批 Bug 交给项目配置的 AI 引擎（`agent`，Codex 或 Claude Code）做一次「先分诊、再修复」：

1. AI 读禅道详情，判断问题出在哪、复现在 Android 还是 iOS、要改 App 还是后端。
2. 默认从 App 入手；确认是后端问题就改后端；两边都要改就一起改。
3. 判断不出是什么问题的 Bug 标记为 `rejected`，服务只在本地记为 `unable_to_fix`；不写禅道、不改状态、不重新指派。

**一条 Bug 只能描述一个端**：标题里同时标了多个端（例如 `【mac】【ios】`）的 Bug 不会被修，只在本地静默记录。这一步在调用 AI 之前完成，不消耗 AI 额度。

禅道备注固定以「AI 理解的问题」和「复现步骤」开头，再写原因和解决方案，方便测试同事一眼核对 AI 理解的和自己报的是不是同一个问题：

```text
问题原因：
【AI 理解的问题】
iOS 端群聊消息重复响铃

【复现步骤】
1. 打开会话窗口
2. 两台设备登录同一账号
3. 安卓端已读后 iOS 再次响铃

【原因分析】
推送回调没有按会话去重

解决方案：
在推送回调里按 sessionId 去重
改动仓库：app
复现端：ios
提交：app:abc1234
```
4. AI 不做 git 提交、不写禅道。只有实际产生代码、相关测试通过且 commit/push 成功，服务才写成功备注并解决 Bug；其他结果只记本地。App 和后端各自提交推送到各自的目标分支。

### 只修一次

禅道已经存在 AI 成功备注或 Bug 已解决时不再处理；明确无法修复的本地终态也不再处理。超时、结果缺失、同步冲突等技术失败在新 Bug 之后自动重试一次，仍失败则静默记为 `retry_exhausted`。

## 查看状态

服务保持仅监听 `127.0.0.1`。部署机配置内网反向代理后，可访问 `http://<部署机内网地址>:8787/`；未配置代理时使用 SSH 隧道：

```bash
ssh -L 8787:127.0.0.1:8787 <user>@<host>
```

保持 SSH 会话运行，然后浏览器访问 `http://127.0.0.1:8787/`。看板每 5 秒刷新，展示成功、失败、进行中、排队任务；点击 Bug 可查看 SQLite 中记录的处理事件流水，页面底部展示最近轮询流水。

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

查看 AI 引擎输出：

```bash
tail -f .auto-fixer/logs/batch-<first>-<last>-agent.log
cat .auto-fixer/logs/batch-<first>-<last>-triage.json   # AI 的分诊结论
```

常见任务状态：

| 状态 | 含义 |
| --- | --- |
| `queued` | 已入队，等待 worker 处理。 |
| `running` | 正在同步仓库或修复。 |
| `pushed` | 代码、测试、提交、推送和禅道回写均已完成。 |
| `unable_to_fix` | 没有修复到代码，只在本地静默记录。 |
| `sync_conflict` | push 时远端分支已有新提交，等待一次自动重试。 |
| `retry_exhausted` | 技术失败已重试一次，停止自动处理且不写禅道。 |
| `writeback_failed` | 代码已推送，成功备注或解决操作等待一次独立重试。 |
| `handled_in_zentao` | 禅道已经有 AI 完成标记，不再重复处理。 |
| `failed` | 处理失败，查看 `error` 和 agent log。 |

如果某个 Bug 的 `events` 长时间停在 `agent_attempt`，同时 AI 日志没有继续更新，通常表示引擎进程卡住了。可以通过 `AUTO_FIXER_CODEX_TIMEOUT_SECONDS` 设置单次最长执行时间。超时后服务会终止当前 AI 进程及其子进程，释放同仓库队列；如果 `AUTO_FIXER_CODEX_ATTEMPTS` 大于 1，会在同一个批次 worktree 内继续下一次尝试。

## 本地数据

默认数据目录是 `.auto-fixer`：

```text
.auto-fixer/
  state.sqlite3          # 任务状态和轮询记录
  repos/                 # Git 仓库缓存
  worktrees/             # 临时 worktree
  logs/                  # AI 引擎执行日志和分诊结论
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

批次模式下，AI 子进程完全不碰禅道；外层服务在 commit 和 push 成功后调用 `zentao-bug-fixer` skill 写备注并把这一批 Bug 标记为 `resolved/fixed`，分诊失败的 Bug 则写备注并指派回提 Bug 的人。它不会执行最终关闭。

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
- 不提交包含真实 Bug 内容的 AI 引擎日志。

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
