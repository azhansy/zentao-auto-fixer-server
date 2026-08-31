# 禅道 Bug 自动修复服务设计

## 目标

启动一个常驻服务，定时轮询自部署禅道里的目标产品 Bug。每个符合条件的 Bug 第一次被发现时，服务根据产品到 Git 仓库的映射，自动同步对应 GitLab 项目的目标分支，调用现有 `zentao-bug-fixer` skill 完成修复，然后直接提交并推送到该产品配置的目标分支。

如果同一个 Bug 已经被自动修复过，后续又被激活、重新打开或再次出现在待处理列表里，服务不再自动处理，转为人工处理。

示例自部署服务地址：

- GitLab: `https://gitlab.example.com/`
- 禅道: `https://zentao.example.com/zentao/`

## 不做的事

- 不依赖禅道主动访问本机 webhook。
- 不创建 MR。
- 不强行推送，不覆盖远端分支历史。
- 不重复自动处理已修复过又被激活的 Bug。
- 不把 GitLab token、禅道账号密码、产品 ID 写入代码。
- 不重新实现 `zentao-bug-fixer` 的修复能力，只负责调度它。

## 第一版架构

```text
Auto Fixer Server
      |
      +-- HTTP: 健康检查、任务状态查询
      +-- Poller: 定时请求禅道 API
      |       |
      |       +-- 读取 projects.json 项目映射
      |       +-- 按产品拉取 Bug 列表
      |       +-- 判断是否符合自动修复条件
      |       +-- SQLite 去重和状态记录
      |       +-- 投递后台队列
      |
      +-- Worker
              |
              +-- 按 Git 项目加锁
              +-- fetch/reset 同步目标分支
              +-- codex exec 调用 zentao-bug-fixer skill 一次处理同仓库批次
              +-- git diff 检查
              +-- 批次 commit
              +-- 普通 push 到目标分支
              +-- 批量标记禅道 Bug 为 resolved/fixed
              +-- 记录结果
```

## 轮询流程

服务启动后同时启动 HTTP server、poller、worker。

1. poller 每隔 `AUTO_FIXER_POLL_INTERVAL_SECONDS` 秒运行一次。
2. poller 读取 `AUTO_FIXER_PROJECTS_FILE` 指向的项目映射文件。
3. 对每个启用的项目映射，调用禅道 API 读取对应产品 Bug 列表。
4. poller 只保留符合自动修复条件的 Bug。
5. 对每个符合条件的 Bug，先查 SQLite 本地状态。
6. 如果本地没有处理记录，写入 `queued` 并投递 worker。
7. 成功或明确无法修复的终态不再投递；技术失败在新 Bug 之后自动重试一次。
8. 代码已推送但禅道回写失败时只重试回写，不再次调用 AI。
9. worker 批量处理队列里的 Bug。同一个 Git 项目同一时间只允许一个批次执行；同项目、同仓库、同分支中已经 queued 的 Bug 会合成一个批次。

## 多项目同步策略

不同禅道产品对应不同 Git 项目时，不建议继续用一组 `TARGET_REPO_URL` 环境变量。建议使用一个 JSON 映射文件：

```json
{
  "projects": [
    {
      "name": "Example Mobile",
      "enabled": true,
      "zentaoProductId": 8,
      "zentaoAssignedTo": "dev-account",
      "repoUrl": "git@gitlab.example.com:mobile/example-mobile.git",
      "targetBranch": "develop",
      "onlyCodeBugs": true,
      "maxBugsPerPoll": 3
    },
    {
      "name": "Example Server",
      "enabled": true,
      "zentaoProductId": 12,
      "zentaoAssignedTo": "server-dev",
      "repoUrl": "git@gitlab.example.com:backend/example-server.git",
      "targetBranch": "main",
      "onlyCodeBugs": true,
      "maxBugsPerPoll": 2
    }
  ]
}
```

同步原则：

- 每个禅道产品必须明确绑定一个 Git 仓库和目标分支。
- 同一个 Git 仓库可以绑定多个禅道产品，但执行时必须共享同一把仓库锁。
- 每轮 poller 按项目顺序扫描，最多入队 `maxBugsPerPoll` 个 Bug，避免一次拉太多任务。
- 队列里的任务可以跨不同 Git 项目并行，但同一个 Git 项目必须串行。
- 直接提交目标分支时，每处理一个 Bug 前都要重新 `fetch` 并同步到 `origin/<targetBranch>`。
- push 只允许普通 push；如果远端分支在修复期间有新提交导致 push 被拒绝，当前 Bug 标记为 `sync_conflict` 或 `failed`，下一轮或人工处理。

## 批量处理策略

“批量”指服务每轮可以发现并入队多个 Bug，worker 会把同项目、同仓库、同分支中已经 queued 的 Bug 合成一个批次：同步一次仓库、Codex 一次性修复、验证一次、生成一个批次 commit、push 成功后再统一标记这些 Bug 为已解决。

推荐顺序：

1. 按项目映射顺序扫描产品。
2. 每个产品先筛选可自动修复 Bug。
3. 每个产品先处理从未执行的 Bug，再处理技术重试；组内按创建时间从旧到新。
4. 每个产品最多入队 `maxBugsPerPoll` 个。
5. worker 按仓库和分支认领 queued Bug，生成一个批次。
6. Codex 在同一个 worktree 内读取并修复批次里的全部 Bug。
7. 批次通过验证后生成一个 commit。
8. push 成功后，服务逐个调用禅道 resolve，把这一批 Bug 标记为 `resolved/fixed`。

批次 commit 的回滚粒度会变大。如果希望降低风险，应调小 `maxBugsPerPoll` 或拆分产品/分支配置。

## 自动修复条件

第一版建议用保守规则，避免自动处理需求、配置、环境类问题。

Bug 必须满足：

- 属于配置的禅道产品或项目。
- 状态是未关闭、未解决或重新激活后仍待处理。
- Bug 类型是代码问题，例如禅道里的 `codeerror` 或中文 `代码问题`。
- 指派人匹配配置的自动修复账号，或配置允许处理全部指派人。
- 本地状态库里没有成功或正在进行的自动处理记录。

Bug 不满足以下任意情况时跳过：

- 类型不是代码问题。
- 已关闭。
- 已经自动修复过。
- 本地状态是 `running`、`pushed`、`unable_to_fix`、`retry_exhausted`。
- Bug 描述缺少复现步骤，后续可作为可配置策略。

## 状态规则

服务用 SQLite 保存状态，不只依赖禅道当前状态。

核心规则：

1. `bug_id` 第一次被轮询发现且符合条件，创建一条 `queued` 任务。
2. 后台 worker 处理任务，只有代码改动、相关测试、commit、push 都成功后才写禅道备注并进入 `pushed`。
3. 无代码改动或 AI 明确无法定位时进入 `unable_to_fix`，不写禅道、不重新指派。
4. `failed`、`sync_conflict`、重新激活的 `skipped_stale` 自动重试一次，仍失败进入 `retry_exhausted`。
5. `writeback_failed` 只重试成功备注和解决操作，不重新运行 AI。

## 重新激活判断

轮询模式不像 webhook 那样天然知道“某次事件是重新激活”。因此重新激活用本地状态和禅道当前状态共同推断：

```text
禅道已有 AI 成功备注或本地状态为 pushed
=> 不再处理

本地状态为技术失败且禅道仍 active、没有 AI 完成备注
=> 在新 Bug 之后重试一次
```

明确无法修复的结果只保存在本地；技术失败不会冒充“无法修复”。

## 目标分支提交

不创建 MR，也不创建长期修复分支。每个批次处理时使用本地临时 worktree，从远端目标分支最新提交开始：

```text
origin/<targetBranch>
      |
      v
本地临时 worktree
      |
      v
codex 修复批次内全部 Bug
      |
      v
commit: fix: zentao batch #123 #124 ...
      |
      v
git push origin HEAD:<targetBranch>
      |
      v
resolve #123 #124 ...
```

push 失败时不做 force push。典型失败处理：

- 如果是认证失败，任务 `failed`。
- 如果是远端分支更新导致非 fast-forward，任务 `sync_conflict`。
- 如果是代码冲突，不强推，任务进入 `sync_conflict` 并最多自动重试一次。

## 配置

最小必要配置：

```bash
AUTO_FIXER_POLL_INTERVAL_SECONDS=300
AUTO_FIXER_PROJECTS_FILE=projects.json

ZENTAO_BASE_URL="https://zentao.example.com/zentao"
ZENTAO_ACCOUNT="your-account"
ZENTAO_PASSWORD="your-password"
```

如果 GitLab clone/push 使用 HTTPS，可以把项目映射里的 `repoUrl` 配成 HTTPS URL；如果使用 SSH，需要保证运行服务的机器已经配置好 deploy key。

## 禅道 API 读取

第一版直接复用 `zentao-bug-fixer` skill 里的 helper 能力读取 Bug：

```bash
python3 ~/.codex/skills/zentao-bug-fixer/scripts/zentao_client.py bugs <product_id>
python3 ~/.codex/skills/zentao-bug-fixer/scripts/zentao_client.py bug <bug_id>
```

服务层只做筛选、去重、入队和状态管理。Bug 详情读取、修复、备注回写仍由 `zentao-bug-fixer` skill 承担。

## Codex 调用方式

worker 在目标 Git worktree 下执行：

```bash
codex exec \
  --cd <worktree> \
  --ask-for-approval never \
  --sandbox danger-full-access \
  "使用 zentao-bug-fixer skill，在同一个批次内处理禅道 Bugs #123 #124 ..."
```

`zentao-bug-fixer` 需要的禅道环境变量由服务进程继承传递。

## 直接推送的风险控制

直接提交目标分支比 MR 风险更高，所以第一版必须保守：

- 每个批次一个 commit。
- 每个仓库同一时间只跑一个 Bug。
- 每个批次开始前同步远端目标分支。
- 禁用 force push。
- push 失败不自动覆盖远端。
- 技术失败最多自动重试一次，每次 AI 启动都受持久化日限额约束。
- 建议目标分支启用 GitLab 保护策略，只允许服务账号推送到指定分支。
- 建议服务账号只给必要仓库权限。

## 运行

```bash
cp .env.example .env
# 填写 .env 后：
set -a
. ./.env
set +a

python3 -m zentao_auto_fixer.server
```

健康检查：

```bash
curl http://127.0.0.1:8787/health
```

查询任务：

```bash
curl http://127.0.0.1:8787/runs/6025
```

## 后续增强

- 支持产品到仓库的映射表。
- 支持多个 worker，但同一个 `bug_id` 和同一个 Git 项目必须互斥。
- 支持给禅道写“转人工处理”备注。
- 支持持久化队列和重启恢复 `running` 任务。
- 支持 `failed` 任务手动重试接口。
- 支持多个产品、多个仓库、多个目标分支映射。
