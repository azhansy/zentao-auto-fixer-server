from __future__ import annotations

import os
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent_runner import SUPPORTED_AGENTS

SUPPORTED_PLATFORMS = ("android", "ios", "mac", "windows", "web")
from .models import ProjectConfig


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_positive_int_env(name: str, default: int) -> Optional[int]:
    value = os.getenv(name)
    if value is None:
        return default
    parsed = int(value)
    if parsed <= 0:
        return None
    return parsed


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    database_path: Path
    repo_cache_dir: Path
    worktree_dir: Path
    logs_dir: Path
    projects_file: Path
    poll_interval_seconds: int
    retry_failed: bool
    worker_count: int
    max_agent_runs_per_day: int
    codex_bin: str
    claude_bin: str
    codex_attempts: int
    codex_retry_delay_seconds: int
    codex_timeout_seconds: Optional[int]
    zentao_client_script: Path
    git_timeout_seconds: int
    git_shallow_clone: bool

    git_author_name: str
    git_author_email: str

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(Path.cwd() / ".env")
        data_dir = Path(os.getenv("AUTO_FIXER_DATA_DIR", ".auto-fixer")).resolve()
        return cls(
            host=os.getenv("AUTO_FIXER_HOST", "127.0.0.1"),
            port=int(os.getenv("AUTO_FIXER_PORT", "8787")),
            data_dir=data_dir,
            database_path=data_dir / "state.sqlite3",
            repo_cache_dir=data_dir / "repos",
            worktree_dir=data_dir / "worktrees",
            logs_dir=data_dir / "logs",
            projects_file=Path(os.getenv("AUTO_FIXER_PROJECTS_FILE", "projects.json")).resolve(),
            poll_interval_seconds=int(os.getenv("AUTO_FIXER_POLL_INTERVAL_SECONDS", "300")),
            retry_failed=_bool_env("AUTO_FIXER_RETRY_FAILED", False),
            worker_count=max(1, int(os.getenv("AUTO_FIXER_WORKERS", "2"))),
            max_agent_runs_per_day=max(1, int(os.getenv("AUTO_FIXER_MAX_AGENT_RUNS_PER_DAY", "20"))),
            codex_bin=os.getenv("AUTO_FIXER_CODEX_BIN", "codex"),
            claude_bin=os.getenv("AUTO_FIXER_CLAUDE_BIN", "claude"),
            codex_attempts=max(1, int(os.getenv("AUTO_FIXER_CODEX_ATTEMPTS", "3"))),
            codex_retry_delay_seconds=max(0, int(os.getenv("AUTO_FIXER_CODEX_RETRY_DELAY_SECONDS", "15"))),
            codex_timeout_seconds=_optional_positive_int_env("AUTO_FIXER_CODEX_TIMEOUT_SECONDS", 1800),
            zentao_client_script=_zentao_client_script_from_env(),
            git_timeout_seconds=max(30, int(os.getenv("AUTO_FIXER_GIT_TIMEOUT_SECONDS", "1800"))),
            git_shallow_clone=_bool_env("AUTO_FIXER_GIT_SHALLOW_CLONE", True),
            git_author_name=os.getenv("GIT_AUTHOR_NAME", "Zentao Auto Fixer"),
            git_author_email=os.getenv("GIT_AUTHOR_EMAIL", "zentao-auto-fixer@example.com"),
        )

    def agent_bin(self, agent: str) -> str:
        return self.claude_bin if agent == "claude" else self.codex_bin

    def validate_for_worker(self) -> Optional[str]:
        missing = []
        if not os.getenv("ZENTAO_BASE_URL"):
            missing.append("ZENTAO_BASE_URL")
        if not (os.getenv("ZENTAO_TOKEN") or (os.getenv("ZENTAO_ACCOUNT") and os.getenv("ZENTAO_PASSWORD"))):
            missing.append("ZENTAO_TOKEN or ZENTAO_ACCOUNT/ZENTAO_PASSWORD")
        if not self.projects_file.is_file():
            missing.append(f"AUTO_FIXER_PROJECTS_FILE ({self.projects_file})")
        if missing:
            return "Missing required worker configuration: " + ", ".join(missing)
        return None

    def load_projects(self) -> List[ProjectConfig]:
        with self.projects_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, list):
            raise ValueError(f"{self.projects_file} must contain a projects array")
        return [_project_from_json(item, self.projects_file) for item in projects]


def _project_from_json(item: Dict[str, Any], source: Path) -> ProjectConfig:
    if not isinstance(item, dict):
        raise ValueError(f"Invalid project item in {source}: expected object")
    name = str(item.get("name") or "").strip()
    app_repo_url, app_target_branch = _repo_entry(item, "app")
    backend_repo_url, backend_target_branch = _repo_entry(item, "backend")
    product_id = item.get("zentaoProductId")
    missing = []
    if not name:
        missing.append("name")
    if product_id in (None, ""):
        missing.append("zentaoProductId")
    if not app_repo_url:
        missing.append("repoUrl")
    if not app_target_branch:
        missing.append("targetBranch")
    if bool(backend_repo_url) != bool(backend_target_branch):
        missing.append("backend.repoUrl and backend.targetBranch together")
    skip_platforms = _skip_platforms(item, name, source)
    agent = str(item.get("agent") or "codex").strip().lower()
    if agent not in SUPPORTED_AGENTS:
        raise ValueError(
            f"Project {name or '?'} in {source} has agent {item.get('agent')!r}; "
            f"supported agents are {', '.join(SUPPORTED_AGENTS)}"
        )
    if missing:
        raise ValueError(f"Project config missing {', '.join(missing)} in {source}")
    return ProjectConfig(
        name=name,
        enabled=bool(item.get("enabled", True)),
        zentao_product_id=int(product_id),
        zentao_assigned_to=str(item.get("zentaoAssignedTo") or "").strip(),
        repo_url=app_repo_url,
        target_branch=app_target_branch,
        only_code_bugs=bool(item.get("onlyCodeBugs", True)),
        max_bugs_per_poll=max(1, int(item.get("maxBugsPerPoll", 1))),
        backend_repo_url=backend_repo_url,
        backend_target_branch=backend_target_branch,
        agent=agent,
        skip_platforms=skip_platforms,
    )


def _skip_platforms(item: Dict[str, Any], name: str, source: Path) -> tuple:
    raw = item.get("skipPlatforms") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError(f"Project {name or '?'} in {source}: skipPlatforms must be a list")
    platforms = tuple(str(entry).strip().lower() for entry in raw if str(entry).strip())
    unknown = [entry for entry in platforms if entry not in SUPPORTED_PLATFORMS]
    if unknown:
        raise ValueError(
            f"Project {name or '?'} in {source} has skipPlatforms {unknown}; "
            f"supported values are {', '.join(SUPPORTED_PLATFORMS)}"
        )
    return platforms


def _repo_entry(item: Dict[str, Any], key: str) -> tuple:
    """Read a {repoUrl, targetBranch} pair; "app" falls back to the legacy top-level keys."""
    entry = item.get(key)
    if not isinstance(entry, dict):
        entry = item if key == "app" else {}
    return (
        str(entry.get("repoUrl") or "").strip(),
        str(entry.get("targetBranch") or "").strip(),
    )


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        raw_value = raw_value.strip()
        if raw_value == "":
            os.environ[key] = ""
            continue
        try:
            parsed = shlex.split(raw_value, posix=True)
        except ValueError:
            continue
        os.environ[key] = " ".join(parsed) if parsed else ""


def _zentao_client_script_from_env() -> Path:
    raw = os.getenv("AUTO_FIXER_ZENTAO_CLIENT", "").strip()
    if not raw or raw.lower() in {"xxx", "default", "auto"}:
        raw = str(Path.home() / ".codex/skills/zentao-bug-fixer/scripts/zentao_client.py")
    return Path(raw).expanduser().resolve()
