from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


TERMINAL_STATUSES = {
    "pushed",
    "manual_required",
    "failed",
    "sync_conflict",
    "no_changes",
    "rejected_to_reporter",
    "writeback_failed",
    "skipped_stale",
    "skipped_platform",
}
HANDLED_STATUSES = {"running", "pushed", "no_changes", "sync_conflict", "rejected_to_reporter"}
# Any ZenTao comment written by this service or the skill carries this marker in its footer.
AI_COMMENT_MARKER = "zentao-bug-fixer"
AUTO_FIXED_STATUSES = {"pushed", "no_changes"}
REACTIVATION_WORDS = (
    "activate",
    "activated",
    "reopen",
    "reopened",
    "restart",
    "reactivate",
    "重新激活",
    "激活",
    "重新打开",
)


# Testers tag the platform in the title, e.g. 【ios】【会话窗口】...; the ZenTao `os` field is empty in practice.
_PLATFORM_WORDS = {
    "android": ("android", "安卓", "安桌"),
    "ios": ("ios", "iphone", "ipad"),
    "mac": ("mac", "osx", "macos"),
    "windows": ("windows", "win客户端", "pc端"),
    "web": ("web", "网页端", "浏览器"),
}


def platforms_of(title: str) -> Tuple[str, ...]:
    """Every platform named in the title's bracket tags; empty when the title does not say."""
    tags = re.findall(r"[【\[]([^】\]]{1,16})[】\]]", title or "")
    found = []
    for tag in tags:
        normalized = tag.strip().casefold().replace(" ", "")
        for platform, words in _PLATFORM_WORDS.items():
            if any(word in normalized for word in words) and platform not in found:
                found.append(platform)
    return tuple(found)


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    enabled: bool
    zentao_product_id: int
    zentao_assigned_to: str
    repo_url: str
    target_branch: str
    only_code_bugs: bool
    max_bugs_per_poll: int
    backend_repo_url: str = ""
    backend_target_branch: str = ""
    agent: str = "codex"
    skip_platforms: Tuple[str, ...] = ()

    @property
    def repo_key(self) -> str:
        return self.repo_url

    def skips_platforms(self, platforms: Tuple[str, ...]) -> bool:
        """Skip only when every platform the bug names is on the skip list.

        A bug tagged 【mac】【ios】 still gets fixed while only mac is skipped, and a bug
        whose title names no platform is never skipped here.
        """
        if not platforms or not self.skip_platforms:
            return False
        return all(platform in self.skip_platforms for platform in platforms)

    @property
    def has_backend_repo(self) -> bool:
        return bool(self.backend_repo_url and self.backend_target_branch)


@dataclass(frozen=True)
class BugCandidate:
    bug_id: int
    title: str
    product_id: int
    assigned_to: str
    bug_type: str
    status: str
    severity: int
    priority: int
    raw: Dict[str, Any]
    opened_by: str = ""


@dataclass(frozen=True)
class BugEvent:
    bug_id: int
    title: str
    action: str
    product_id: Optional[int]
    assigned_to: str
    raw: Dict[str, Any]

    @property
    def is_reactivation(self) -> bool:
        action = self.action.lower()
        return any(word in action for word in REACTIVATION_WORDS)


@dataclass(frozen=True)
class RunRecord:
    bug_id: int
    title: str
    status: str
    project_name: str
    target_branch: str
    repo_url: str
    product_id: Optional[int]
    commit_hash: str
    error: str
    handled_once: bool
    opened_by: str = ""
    triage_targets: str = ""
