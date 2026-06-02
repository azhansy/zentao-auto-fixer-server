from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


TERMINAL_STATUSES = {"pushed", "manual_required", "failed", "sync_conflict", "no_changes"}
HANDLED_STATUSES = {"running", "pushed", "no_changes", "sync_conflict"}
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

    @property
    def repo_key(self) -> str:
        return self.repo_url


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
