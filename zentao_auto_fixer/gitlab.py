"""GitLab MR lifecycle; only the configured host may receive the API token."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class GitLabError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitLab:
    def __init__(self, mr_url: str):
        base = os.environ.get('AUTO_FIXER_GITLAB_URL', '').rstrip('/')
        token_file = os.environ.get('AUTO_FIXER_GITLAB_TOKEN_FILE', '')
        self.token = os.environ.get('AUTO_FIXER_GITLAB_TOKEN', '')
        if token_file:
            from pathlib import Path
            self.token = Path(token_file).read_text().strip()
        parsed, expected = urlsplit(mr_url), urlsplit(base)
        if not self.token or expected.scheme != 'https' or parsed.netloc != expected.netloc or parsed.scheme != expected.scheme:
            raise GitLabError('MR API requires a token and the configured HTTPS GitLab host')
        project, separator, iid = parsed.path.partition('/-/merge_requests/')
        if not separator or not iid.isdigit():
            raise GitLabError('Invalid GitLab MR URL')
        self.base = base + '/api/v4'
        self.project = '/projects/' + quote(project.strip('/'), safe='')
        self.mr_path = self.project + '/merge_requests/' + iid

    def request(self, path: str, method='GET', data=None, raw_text=False):
        body = json.dumps(data).encode() if data is not None else None
        req = Request(self.base + path, data=body, method=method,
                      headers={'PRIVATE-TOKEN': self.token, 'Content-Type': 'application/json'})
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=30) as response:
                raw = response.read()
                if raw_text:
                    return raw.decode("utf-8", errors="replace")
                return json.loads(raw) if raw else None
        except (HTTPError, URLError, ValueError) as exc:
            # Never echo request headers or tokens into task logs.
            raise GitLabError(f'GitLab {method} {path}: {exc}') from exc

    def inspect(self, expected_sha: str, target: str):
        mr = self.request(self.mr_path)
        if mr.get('sha') != expected_sha or mr.get('target_branch') != target:
            raise GitLabError('MR head or target changed outside this task; stopping automatic delivery')
        pipeline = mr.get('head_pipeline') or {}
        if pipeline.get('sha') != expected_sha or not pipeline.get('id'):
            return mr, []
        jobs = []
        page = 1
        while True:
            chunk = self.request(self.project + f'/pipelines/{pipeline["id"]}/jobs?per_page=100&page={page}')
            jobs.extend(chunk)
            if len(chunk) < 100:
                return mr, jobs
            page += 1

    def enable_auto_merge(self, sha: str):
        project = self.request(self.project)
        if not project.get('only_allow_merge_if_pipeline_succeeds'):
            raise GitLabError('GitLab must require a successful pipeline before auto merge')
        return self.request(self.mr_path + '/merge', 'PUT',
                            {'sha': sha, 'auto_merge': True, 'should_remove_source_branch': True})

    def failed_logs(self, jobs):
        logs = []
        for job in jobs:
            if job.get('status') == 'failed':
                trace = self.request(self.project + f'/jobs/{job["id"]}/trace', raw_text=True)
                logs.append(f'{job["name"]}: {job["status"]} {job.get("web_url", "")}\n{trace[-16000:]}')
        return '\n'.join(logs).replace(self.token, '[REDACTED]')


def required_jobs_pass(jobs):
    required = set(os.getenv('AUTO_FIXER_GITLAB_REQUIRED_JOBS', 'lint,unit-test,build,integration').split(','))
    latest = {}
    for job in jobs:
        name = job.get('name')
        if name not in latest or job.get('id', 0) > latest[name].get('id', 0):
            latest[name] = job
    if not required <= latest.keys():
        return False
    return all(latest[name].get('status') == 'success' and not latest[name].get('allow_failure') for name in required)


def required_jobs_present(jobs):
    required = set(os.getenv('AUTO_FIXER_GITLAB_REQUIRED_JOBS', 'lint,unit-test,build,integration').split(','))
    blocking = {job.get('name') for job in jobs if not job.get('allow_failure')}
    return required <= blocking
