from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode, urlparse

from .models import BugCandidate, ProjectConfig


class ZenTaoPollError(RuntimeError):
    pass


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


def _list_project_bugs_with_helper(client_script: Path, project: ProjectConfig) -> List[BugCandidate]:
    cmd = ["python3", str(client_script), "bugs", str(project.zentao_product_id), "--all"]
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
    data = _curl_json(["-H", "Accept: application/json", "-H", f"Token: {token}", url])
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
    return sorted(result, key=lambda bug: (bug.priority or 99, bug.severity or 99, bug.bug_id))


def _curl_login() -> str:
    account = _required_env("ZENTAO_ACCOUNT")
    password = _required_env("ZENTAO_PASSWORD")
    base_url = _required_env("ZENTAO_BASE_URL").rstrip("/")
    api_prefix = os.getenv("ZENTAO_API_PREFIX", "/api.php/v1").strip("/")
    url = f"{base_url}/{api_prefix}/tokens"
    payload = json.dumps({"account": account, "password": password}, ensure_ascii=False)
    data = _curl_json(["-H", "Accept: application/json", "-H", "Content-Type: application/json", "-d", payload, url])
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise ZenTaoPollError("curl login succeeded but response did not contain token")
    return str(token)


def _curl_json(args: List[str]) -> Any:
    cmd = ["curl", "--silent", "--show-error", "--fail-with-body", "--max-time", "30"]
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
        bug_type=_text_value(bug.get("type") or bug.get("bugType") or bug.get("typeName") or bug.get("typeLabel")),
        status=_text_value(bug.get("status")),
        severity=_int_value(bug.get("severity"), 99),
        priority=_int_value(bug.get("pri") or bug.get("priority"), 99),
        raw=bug,
    )


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
