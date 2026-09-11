"""Complete Cable fixes after verified CI and merge into pre_release."""
from urllib.parse import urlsplit

from .gitlab import GitLabError


FEATURE_CHECKS = {'sql-static-gate', 'hk-seed-environment-gate'}


def is_cable_release(item):
    return (urlsplit(item['url']).path.startswith('/im/cable/-/merge_requests/')
            and item['target'] == 'pre_release')


def check_cable_release(client, item, payload, data_dir, bug_id, repair_failed=None):
    mr, jobs = client.inspect(item['sha'], 'pre_release')
    if mr.get('state') not in {'opened', 'merged'}:
        raise GitLabError('Cable 修复 MR 已关闭且未合并')
    pipeline = mr.get('head_pipeline') or {}
    if (mr['state'] == 'opened' and pipeline.get('sha') == item['sha']
            and pipeline.get('status') == 'failed' and repair_failed is not None
            and FEATURE_CHECKS <= {job.get('name') for job in jobs if not job.get('allow_failure')}):
        repair_failed(client.failed_logs(jobs))
        return False, 'CI 失败已进入原 MR 续修流程，等待重新验证'
    if not client.verified_pipeline(pipeline, item['sha'], FEATURE_CHECKS):
        return False, '等待修复 MR 的 SQL 静态检查、环境检查及其他阻断检查通过'
    # These checks do not replace the targeted tests performed during the repair.
    verdict = payload.get('verdict', {})
    if (verdict.get('verification_passed') is not True or verdict.get('verification_method') != 'test'
            or not verdict.get('verification_command')):
        return False, '缺少本次修复的针对性测试记录，不能自动合并'
    if mr['state'] == 'opened':
        if not mr.get('merge_when_pipeline_succeeds') and not mr.get('auto_merge_enabled'):
            client.enable_auto_merge(item['sha'])
        return False, '等待修复 MR 合入 pre_release'

    merge_sha = mr.get('merge_commit_sha') or mr.get('squash_commit_sha')
    if not merge_sha:
        raise GitLabError('Cable 修复 MR 缺少实际合并提交')
    return True, '修复测试及必需 CI 已通过，MR 已合入 pre_release，修复交付完成'
