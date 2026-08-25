from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepoSyncResult:
    path: Path
    action: str


def run_git(
    args: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> str:
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"{' '.join(cmd)} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise GitError(f"{' '.join(cmd)} failed with exit {result.returncode}:\n{result.stdout}")
    return result.stdout.strip()


def ensure_repo_cache(
    repo_url: str,
    cache_dir: Path,
    target_branch: str,
    *,
    timeout: Optional[int] = None,
    shallow: bool = True,
) -> RepoSyncResult:
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    action = "updated"
    if cache_dir.exists() and not _is_git_worktree(cache_dir):
        shutil.rmtree(cache_dir, ignore_errors=True)
    if not cache_dir.exists():
        clone_args = ["clone", "--origin", "origin", "--single-branch", "--branch", target_branch]
        if shallow:
            clone_args.extend(["--depth", "1"])
        clone_args.extend([repo_url, str(cache_dir)])
        run_git(clone_args, timeout=timeout)
        action = "cloned"
    else:
        _ensure_origin_url(cache_dir, repo_url, timeout)

    fetch_ref = f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}"
    fetch_args = ["fetch", "origin", fetch_ref, "--prune"]
    if shallow:
        fetch_args.extend(["--depth", "1"])
    run_git(fetch_args, cwd=cache_dir, timeout=timeout)
    run_git(["checkout", "-B", target_branch, f"origin/{target_branch}"], cwd=cache_dir, timeout=timeout)
    run_git(["reset", "--hard", f"origin/{target_branch}"], cwd=cache_dir, timeout=timeout)
    return RepoSyncResult(path=cache_dir, action=action)


def create_detached_worktree(repo_cache: Path, worktree_root: Path, name: str, target_branch: str) -> Path:
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / _safe_path_name(name)
    run_git(["worktree", "prune"], cwd=repo_cache)
    _remove_existing_worktree(repo_cache, worktree_path)
    run_git(["worktree", "add", "--detach", str(worktree_path), f"origin/{target_branch}"], cwd=repo_cache)
    return worktree_path


def create_worktree(repo_cache: Path, worktree_root: Path, branch: str, target_branch: str) -> Path:
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / branch.replace("/", "-")
    run_git(["worktree", "prune"], cwd=repo_cache)
    _remove_existing_worktree(repo_cache, worktree_path)
    run_git(["worktree", "add", "-B", branch, str(worktree_path), f"origin/{target_branch}"], cwd=repo_cache)
    return worktree_path


def remote_branch_exists(repo: Path, branch: str) -> bool:
    output = run_git(["ls-remote", "--heads", "origin", branch], cwd=repo)
    return bool(output.strip())


def has_changes(repo: Path) -> bool:
    output = run_git(["status", "--porcelain"], cwd=repo)
    return bool(output.strip())


def head_commit(repo: Path) -> str:
    return run_git(["rev-parse", "HEAD"], cwd=repo).strip()


def changed_files(repo: Path) -> List[str]:
    output = run_git(["status", "--porcelain"], cwd=repo)
    return [line[3:].strip() for line in output.splitlines() if line.strip()]


def reset_hard_clean(repo: Path, commit: str) -> None:
    """Throw away everything an interrupted agent attempt left behind."""
    run_git(["reset", "--hard", commit], cwd=repo)
    run_git(["clean", "-fd"], cwd=repo)


def push_head_dry_run(repo: Path, target_branch: str) -> None:
    """Fail here rather than half-way through pushing several repositories."""
    run_git(["push", "--dry-run", "origin", f"HEAD:{target_branch}"], cwd=repo)


def commit_all(repo: Path, message: str, author_name: str, author_email: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": os.getenv("GIT_COMMITTER_NAME", author_name),
            "GIT_COMMITTER_EMAIL": os.getenv("GIT_COMMITTER_EMAIL", author_email),
        }
    )
    run_git(["add", "-A"], cwd=repo, env=env)
    run_git(["commit", "-m", message], cwd=repo, env=env)
    return run_git(["rev-parse", "HEAD"], cwd=repo)


def push_branch(repo: Path, branch: str) -> None:
    run_git(["push", "-u", "origin", branch], cwd=repo)


def push_head_to_branch(repo: Path, target_branch: str) -> None:
    run_git(["push", "origin", f"HEAD:{target_branch}"], cwd=repo)


def remove_worktree(repo_cache: Path, worktree: Path) -> None:
    _remove_existing_worktree(repo_cache, worktree)
    run_git(["worktree", "prune"], cwd=repo_cache)


def _remove_existing_worktree(repo_cache: Path, worktree: Path) -> None:
    if worktree.exists():
        try:
            run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo_cache)
            return
        except GitError:
            shutil.rmtree(worktree, ignore_errors=True)


def repo_cache_name(repo_url: str) -> str:
    return _safe_path_name(repo_url.replace("git@", "").replace("https://", "").replace("http://", ""))


def _safe_path_name(value: str) -> str:
    clean = value.replace("/", "__").replace(":", "_").replace("@", "_")
    return "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in clean)


def _is_git_worktree(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        output = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, timeout=10)
    except GitError:
        return False
    return output.strip() == "true"


def _ensure_origin_url(repo: Path, repo_url: str, timeout: Optional[int]) -> None:
    try:
        current = run_git(["remote", "get-url", "origin"], cwd=repo, timeout=timeout)
    except GitError:
        run_git(["remote", "add", "origin", repo_url], cwd=repo, timeout=timeout)
        return
    if current.strip() != repo_url:
        run_git(["remote", "set-url", "origin", repo_url], cwd=repo, timeout=timeout)
