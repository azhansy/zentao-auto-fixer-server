"""Daily event-based report; never infer yesterday's result from today's mutable status."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import urllib.request

TZ = timezone(timedelta(hours=8))
EVENT_RESULTS = {
    **dict.fromkeys(('queued', 'started', 'agent_start', 'conflict_retry_queued',
                     'requeued_after_restart', 'manual_requeued', 'ci_repair_start'), '处理中'),
    **dict.fromkeys(('failed', 'unable_to_fix', 'no_changes', 'sync_conflict',
                     'merge_request_failed', 'comment_failed', 'resolve_failed',
                     'writeback_exhausted', 'retry_exhausted'), '失败'),
    **dict.fromkeys(('resolve_done', 'manual_writeback_done', 'manual_single_lane_verified'), '成功'),
    'awaiting_merge': '待合并', 'awaiting_release': '待上线',
    'manual_failure_review': '待人工处理或复验',
    **dict.fromkeys(('skipped_stale', 'skipped_ui', 'skipped_platform',
                     'already_handled_in_zentao'), '跳过'),
}
CATEGORIES = ('成功', '失败', '处理中', '待合并', '待上线', '待人工处理或复验', '跳过')


def collect(database: Path, day: date):
    start = datetime.combine(day, time(), TZ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        rows = conn.execute('''SELECT e.bug_id, b.project_name, e.event
            FROM run_events e JOIN bug_runs b ON b.bug_id=e.bug_id
            WHERE julianday(e.created_at)>=julianday(?) AND julianday(e.created_at)<julianday(?)
            ORDER BY julianday(e.created_at), e.id''', (start.isoformat(), end.isoformat())).fetchall()
    latest = {}
    for bug_id, project, event in rows:
        if event in EVENT_RESULTS:
            latest[bug_id] = (project or '未标注项目', EVENT_RESULTS[event])
    grouped = defaultdict(Counter)
    for project, result in latest.values():
        grouped[project][result] += 1
    return dict(grouped)


def render(day: date, grouped) -> str:
    if not grouped:
        return ''
    total = Counter()
    for counts in grouped.values():
        total.update(counts)
    def line(name, counts):
        details = '，'.join(f'{key} {counts[key]}' for key in CATEGORIES if counts[key] or key in ('成功', '失败'))
        return f'{name}：处理 {sum(counts.values())} 项；{details}'
    return '\n'.join([
        f'禅道自动修复日报｜{day.isoformat()}',
        '统计时间：北京时间 00:00–24:00', line('合计', total), '',
        *(line(project, grouped[project]) for project in sorted(grouped)), '',
        '口径：按当日处理事件对 Bug 去重，取当日最后处理结果；多次重试只算一项。',
        '成功指完成代码交付及禅道回写，不代表已上线或真机验收；待合并、待上线及跳过单列。',
    ])


def send(webhook: str, text: str):
    request = urllib.request.Request(webhook, data=json.dumps({
        'msg_type': 'text', 'content': {'text': text},
    }).encode(), headers={'Content-Type': 'application/json; charset=utf-8'}, method='POST')
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get('code', result.get('StatusCode')) != 0:
        raise RuntimeError(f'Feishu rejected report: code={result.get("code", result.get("StatusCode"))}')
    return result


def deliver(database: Path, receipt_dir: Path, day: date, webhook: str):
    receipt_dir.mkdir(parents=True, exist_ok=True)
    with (receipt_dir / 'send.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = receipt_dir / f'{day.isoformat()}.json'
        if receipt.exists():
            print(f'{day}: already_sent', flush=True)
            return
        grouped = collect(database, day)
        text = render(day, grouped)
        if not text:
            print(f'{day}: no_activity; notification skipped', flush=True)
            return
        result = send(webhook, text)
        temporary = receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps({'day': str(day), 'sent_at': datetime.now(TZ).isoformat(),
            'projects': grouped, 'text': text, 'response': result}, ensure_ascii=False, indent=2))
        temporary.replace(receipt)
        print(f'{day}: sent; bugs={sum(sum(c.values()) for c in grouped.values())}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', type=date.fromisoformat)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    from .config import Settings
    settings = Settings.from_env()
    day = args.date or (datetime.now(TZ).date() - timedelta(days=1))
    if args.dry_run:
        print(render(day, collect(settings.database_path, day)) or f'{day}: no_activity')
        return
    webhook = Path(os.environ['AUTO_FIXER_REPORT_WEBHOOK_FILE']).read_text().strip()
    if not webhook.startswith('https://open.feishu.cn/open-apis/bot/v2/hook/'):
        raise ValueError('Invalid Feishu webhook host/path')
    deliver(settings.database_path, settings.data_dir / 'daily-reports', day, webhook)


if __name__ == '__main__':
    main()
