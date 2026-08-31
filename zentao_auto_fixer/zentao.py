from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

from .models import AI_COMMENT_MARKER, BugCandidate, ProjectConfig


class ZenTaoPollError(RuntimeError):
    pass


class ZenTaoResolveError(RuntimeError):
    pass


class ZenTaoWriteError(RuntimeError):
    pass


def bug_view_url(bug_id: int) -> str:
    base_url = os.getenv("ZENTAO_BASE_URL", "").rstrip("/")
    if not base_url:
        return ""
    return f"{base_url}/bug-view-{bug_id}.html"


def list_project_bugs(client_script: Path, project: ProjectConfig) -> List[BugCandidate]:
    try:
        return _list_project_bugs_with_helper(client_script, project)
    except ZenTaoPollError as helper_error:
        if not _curl_fallback_enabled():
            raise
        try:
            return _list_project_bugs_with_curl(project)
        except ZenTaoPollError as curl_error:
            raise ZenTaoPollError(
                f"zentao_client failed: {helper_error}; curl fallback failed: {curl_error}"
            ) from curl_error


def resolve_bug(client_script: Path, bug_id: int) -> None:
    result = subprocess.run(
        ["python3", str(client_script), "resolve", str(bug_id)],
        env=_zentao_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ZenTaoResolveError(detail or f"zentao_client resolve exited {result.returncode}")


def bug_has_ai_comment(client_script: Path, bug_id: int) -> bool:
    """True when ZenTao history already carries a comment written by this service or the skill."""
    return _has_ai_marker(_bug_detail(client_script, bug_id))


def _bug_detail(client_script: Path, bug_id: int) -> Dict[str, Any]:
    result = subprocess.run(
        ["python3", str(client_script), "bug", str(bug_id)],
        env=_zentao_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ZenTaoPollError(detail or f"zentao_client bug exited {result.returncode}")
    try:
        detail = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ZenTaoPollError(f"Invalid ZenTao bug detail JSON: {exc}") from exc
    if not isinstance(detail, dict):
        raise ZenTaoPollError(f"ZenTao bug #{bug_id} detail is not an object")
    return detail


def _has_ai_marker(detail: Dict[str, Any]) -> bool:
    actions = detail.get("actions")
    if not isinstance(actions, list):
        # This is a de-duplication gate: an unreadable history must block, never wave the bug through.
        raise ZenTaoPollError(
            f"Bug #{detail.get('id')} detail has no actions list; cannot tell if AI already handled it"
        )
    return any(
        AI_COMMENT_MARKER in str(action.get("comment") or "")
        for action in actions
        if isinstance(action, dict)
    )


def bug_is_still_actionable(
    client_script: Path,
    bug_id: int,
    *,
    ignore_ai_comment: bool = False,
) -> Tuple[bool, str]:
    """Re-read a bug right before fixing it: queued work can be hours or a restart old."""
    detail = _bug_detail(client_script, bug_id)
    status = str(detail.get("status") or "").strip().lower()
    if status != "active":
        return False, f"ZenTao status is now {status or 'unknown'!r}, not active"
    if not ignore_ai_comment and _has_ai_marker(detail):
        return False, "ZenTao already carries an AI comment for this bug"
    return True, ""


def comment_bug(client_script: Path, bug_id: int, cause: str, solution: str) -> None:
    """Post a ZenTao comment. The helper appends the AI marker footer for us."""
    result = subprocess.run(
        [
            "python3",
            str(client_script),
            "--no-resolve-bug-after-comment",
            "comment",
            str(bug_id),
            "--cause",
            cause,
            "--solution",
            solution,
        ],
        env=_zentao_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ZenTaoWriteError(detail or f"zentao_client comment exited {result.returncode}")


def assign_bug(bug_id: int, account: str) -> None:
    """Assign a bug back to somebody, leaving its status untouched (an active bug stays active)."""
    if not account:
        raise ZenTaoWriteError(f"Bug #{bug_id} has no account to assign back to")
    try:
        token = os.getenv("ZENTAO_TOKEN") or _curl_login()
        base_url = _required_env("ZENTAO_BASE_URL").rstrip("/")
    except ZenTaoPollError as exc:
        raise ZenTaoWriteError(str(exc)) from exc
    api_prefix = os.getenv("ZENTAO_API_PREFIX", "/api.php/v1").strip("/")
    url = f"{base_url}/{api_prefix}/bugs/{bug_id}"
    payload = json.dumps({"assignedTo": account}, ensure_ascii=False)
    try:
        data = _curl_json(
            [
                "-X",
                "PUT",
                "-H",
                "Accept: application/json",
                "-H",
                "Content-Type: application/json",
                "-d",
                payload,
                url,
            ],
            secret_headers=[f"Token: {token}"],
        )
    except ZenTaoPollError as exc:
        raise ZenTaoWriteError(str(exc)) from exc
    if isinstance(data, dict) and data.get("error"):
        raise ZenTaoWriteError(f"ZenTao assign failed: {data}")
    _verify_assignee(bug_id, account, token, base_url, api_prefix)


def _verify_assignee(bug_id: int, account: str, token: str, base_url: str, api_prefix: str) -> None:
    """The PUT can answer 200 without applying anything, so read the bug back."""
    try:
        detail = _curl_json(
            ["-H", "Accept: application/json", f"{base_url}/{api_prefix}/bugs/{bug_id}"],
            secret_headers=[f"Token: {token}"],
        )
    except ZenTaoPollError as exc:
        raise ZenTaoWriteError(f"Assigned bug #{bug_id} but could not read it back: {exc}") from exc
    bug = detail.get("bug") if isinstance(detail, dict) and isinstance(detail.get("bug"), dict) else detail
    current = _account_value(bug.get("assignedTo")) if isinstance(bug, dict) else ""
    if current != account:
        raise ZenTaoWriteError(
            f"ZenTao accepted the assign of bug #{bug_id} but it is still assigned to {current or 'nobody'!r}"
        )


def _list_project_bugs_with_helper(client_script: Path, project: ProjectConfig) -> List[BugCandidate]:
    cmd = [
        "python3",
        str(client_script),
        "bugs",
        str(project.zentao_product_id),
        "--all",
        "--limit",
        os.getenv("AUTO_FIXER_ZENTAO_PAGE_LIMIT", "200"),
    ]
    if not project.only_code_bugs:
        cmd.append("--include-non-code")
    result = subprocess.run(
        cmd,
        env=_zentao_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise ZenTaoPollError(result.stderr.strip() or result.stdout.strip())
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ZenTaoPollError(f"Invalid ZenTao JSON response: {exc}") from exc
    if not isinstance(data, list):
        raise ZenTaoPollError("ZenTao bugs response must be a JSON array")
    if project.only_code_bugs:
        data = [bug for bug in data if isinstance(bug, dict) and _is_code_bug(bug)]
    candidates = [_candidate_from_bug(project.zentao_product_id, bug) for bug in data if isinstance(bug, dict)]
    return _filter_and_sort_candidates(candidates, project)


def _list_project_bugs_with_curl(project: ProjectConfig) -> List[BugCandidate]:
    token = os.getenv("ZENTAO_TOKEN") or _curl_login()
    base_url = _required_env("ZENTAO_BASE_URL").rstrip("/")
    api_prefix = os.getenv("ZENTAO_API_PREFIX", "/api.php/v1").strip("/")
    query = urlencode({"limit": os.getenv("AUTO_FIXER_ZENTAO_PAGE_LIMIT", "200")})
    url = f"{base_url}/{api_prefix}/products/{project.zentao_product_id}/bugs?{query}"
    data = _curl_json(["-H", "Accept: application/json", url], secret_headers=[f"Token: {token}"])
    if isinstance(data, dict) and isinstance(data.get("bugs"), list):
        bugs = data["bugs"]
    elif isinstance(data, dict) and isinstance(data.get("data"), list):
        bugs = data["data"]
    elif isinstance(data, list):
        bugs = data
    else:
        raise ZenTaoPollError("Could not find bugs list in curl response")
    if project.only_code_bugs:
        bugs = [bug for bug in bugs if isinstance(bug, dict) and _is_code_bug(bug)]
    candidates = [_candidate_from_bug(project.zentao_product_id, bug) for bug in bugs if isinstance(bug, dict)]
    return _filter_and_sort_candidates(candidates, project)


def _filter_and_sort_candidates(
    candidates: Iterable[BugCandidate],
    project: ProjectConfig,
) -> List[BugCandidate]:
    result = list(candidates)
    if project.zentao_assigned_to:
        result = [
            bug
            for bug in result
            if bug.assigned_to == project.zentao_assigned_to
        ]
    return sorted(result, key=lambda bug: (bug.opened_at or "9999", bug.bug_id))


def _curl_login() -> str:
    account = _required_env("ZENTAO_ACCOUNT")
    password = _required_env("ZENTAO_PASSWORD")
    base_url = _required_env("ZENTAO_BASE_URL").rstrip("/")
    api_prefix = os.getenv("ZENTAO_API_PREFIX", "/api.php/v1").strip("/")
    url = f"{base_url}/{api_prefix}/tokens"
    payload = json.dumps({"account": account, "password": password}, ensure_ascii=False)
    data = _curl_json(
        ["-H", "Accept: application/json", "-H", "Content-Type: application/json", "--data", "@-", url],
        stdin_data=payload,
    )
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise ZenTaoPollError("curl login succeeded but response did not contain token")
    return str(token)


def _curl_json(args: List[str], secret_headers: Optional[List[str]] = None, stdin_data: str = "") -> Any:
    """Secret headers go through --config on stdin so they never show up in `ps`."""
    cmd = ["curl", "--silent", "--show-error", "--fail-with-body", "--max-time", "30"]
    if secret_headers:
        if stdin_data:
            raise ZenTaoPollError("curl cannot read both a config and a body from stdin")
        cmd.extend(["--config", "-"])
        stdin_data = "".join(f'header = "{header}"\n' for header in secret_headers) + stdin_data
    no_proxy = _zentao_no_proxy_host()
    if no_proxy:
        cmd.extend(["--noproxy", no_proxy])
    if _env_bool("AUTO_FIXER_ZENTAO_CURL_INSECURE", False):
        cmd.append("--insecure")
    if _env_bool("AUTO_FIXER_ZENTAO_CURL_HTTP1", True):
        cmd.append("--http1.1")
    cmd.extend(args)
    result = subprocess.run(
        cmd,
        env=_zentao_env(),
        text=True,
        input=stdin_data or None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ZenTaoPollError(f"curl exited {result.returncode}: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        preview = result.stdout[:300].replace("\n", " ")
        raise ZenTaoPollError(f"Invalid curl JSON response: {exc}: {preview}") from exc


def _candidate_from_bug(product_id: int, bug: Dict[str, Any]) -> BugCandidate:
    bug_id = _int_value(bug.get("id") or bug.get("bugID") or bug.get("bugId"), 0)
    title = str(bug.get("title") or bug.get("name") or f"ZenTao Bug {bug_id}").strip()
    return BugCandidate(
        bug_id=bug_id,
        title=title,
        product_id=product_id,
        assigned_to=_text_value(bug.get("assignedTo") or bug.get("assigned_to")),
        opened_by=_account_value(bug.get("openedBy") or bug.get("opened_by")),
        bug_type=_text_value(bug.get("type") or bug.get("bugType") or bug.get("typeName") or bug.get("typeLabel")),
        status=_text_value(bug.get("status")),
        severity=_int_value(bug.get("severity"), 99),
        priority=_int_value(bug.get("pri") or bug.get("priority"), 99),
        raw=bug,
        opened_at=_text_value(
            bug.get("openedDate")
            or bug.get("opened_date")
            or bug.get("openedAt")
            or bug.get("opened_at")
        ),
    )


def _account_value(value: Any) -> str:
    """ZenTao user fields arrive as {"account": ..., "realname": ...}; assignments need the account."""
    if isinstance(value, dict):
        return str(value.get("account") or "").strip()
    return str(value or "").strip()


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("account", "realname", "name", "title", "value", "label"):
            if value.get(key):
                return str(value[key]).strip()
        return " ".join(str(item).strip() for item in value.values() if item not in (None, ""))
    return str(value).strip()


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_code_bug(bug: Dict[str, Any]) -> bool:
    for field in ("type", "bugType", "typeName", "typeLabel", "Bug类型"):
        if _type_value_is_code_bug(bug.get(field)):
            return True
    return False


def _type_value_is_code_bug(value: Any) -> bool:
    for text in _type_text_values(value):
        normalized = text.strip().casefold().replace("_", " ").replace("-", " ")
        normalized = " ".join(normalized.split())
        compact = normalized.replace(" ", "")
        if normalized in _CODE_BUG_TYPE_VALUES or compact in _CODE_BUG_TYPE_VALUES:
            return True
    return False


def _type_text_values(value: Any) -> Iterable[str]:
    if value in (None, ""):
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _type_text_values(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _type_text_values(item)
        return
    yield str(value)


def _curl_fallback_enabled() -> bool:
    return _env_bool("AUTO_FIXER_ZENTAO_CURL_FALLBACK", True)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ZenTaoPollError(f"Missing {name}")
    return value.strip()


def _zentao_env() -> Dict[str, str]:
    env = os.environ.copy()
    host = _zentao_no_proxy_host()
    if host:
        existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
        parts = [part.strip() for part in existing.split(",") if part.strip()]
        if host not in parts:
            parts.append(host)
        env["NO_PROXY"] = ",".join(parts)
        env["no_proxy"] = env["NO_PROXY"]
    return env


def _zentao_no_proxy_host() -> str:
    base_url = os.getenv("ZENTAO_BASE_URL", "")
    host = urlparse(base_url).hostname or ""
    return host.strip()


_CODE_BUG_TYPE_VALUES = {
    "code",
    "codeerror",
    "code error",
    "codeissue",
    "code issue",
    "代码问题",
    "代码错误",
}
