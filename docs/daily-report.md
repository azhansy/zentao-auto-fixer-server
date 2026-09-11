# 禅道自动修复日报

AB 独立 LaunchAgent `com.dashu.zentao-auto-fixer.daily-report` 每天北京时间 09:00 执行：

```sh
/opt/homebrew/bin/python3 -m zentao_auto_fixer.daily_report
```

工作目录为自动修复服务仓库，读取现有 `.env` 和 `.auto-fixer/state.sqlite3`。
`AUTO_FIXER_REPORT_WEBHOOK_FILE` 指向仓库外、权限 0600 的飞书 webhook 文件，禁止提交真实地址。
LaunchAgent 的 `StartCalendarInterval` 为 `Hour=9, Minute=0`，AB 系统时区为 UTC+8。

统计前一天北京时间 00:00（含）至次日 00:00（不含）的处理事件，按 Bug 去重，取当天最后一条有结果语义的事件。忽略清理目录等辅助事件，不使用会被后续修改的当前状态倒推昨日结果。项目名取服务登记的项目名：目前 `Rhixio` 对应 Loopin 业务（包括其 Cable 后端），`Circllo` 对应 Circllo。

成功仅指代码交付与禅道回写完成；等待合并、等待上线、待人工处理或复验、跳过分别统计，不当作成功。失败后当天成功只计成功，跨日成功不改写此前日报。仅有轮询、无实际处理事件时不发送。

预览（不发送）：

```sh
python3 -m zentao_auto_fixer.daily_report --dry-run
python3 -m zentao_auto_fixer.daily_report --date 2026-09-08 --dry-run
python3 -m unittest tests.test_daily_report -v
```

发送成功后将飞书业务响应及统计快照保存在 `.auto-fixer/daily-reports/YYYY-MM-DD.json`；同一天重复启动不重发。文件锁防止并发发送。HTTP 成功但飞书业务码失败时退出非零、不记录成功；日志在 `.auto-fixer/logs/daily-report{,.error}.log`。

网络超时或进程在发送后、保存回执前中断时，需先确认群内是否已收到，避免盲目重发。修复网络问题后可用 `--date YYYY-MM-DD` 补发指定日报。机器休眠可能延迟定时执行，需保持 AB 在线。

2026-09-09 已通过实际 LaunchAgent 触发发送 2026-09-08 日报，飞书返回 `code=0`；未用交互 Shell 成功冒充定时执行成功。
