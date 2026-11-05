from __future__ import annotations

from dataclasses import dataclass
import re
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


class GitError(RuntimeError):
    pass


class RebaseConflictError(GitError):
    def __init__(self, message: str, latest: str):
        super().__init__(message)
        self.latest = latest


@dataclass(frozen=True)
class RepoSyncResult:
    path: Path
    action: str


def run_git(
    args: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    strip_output: bool = True,
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
    return result.stdout.strip() if strip_output else result.stdout


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
    _guard_direct_push(repo, target_branch)
    run_git(["push", "--dry-run", "origin", f"HEAD:{target_branch}"], cwd=repo)


def rebase_onto_latest_remote(
    repo: Path,
    target_branch: str,
    baseline: str,
    *,
    timeout: Optional[int] = None,
    shallow: bool = True,
) -> str:
    """Replay local fix commits when the target branch moved while the agent was working."""
    fetch_ref = f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}"
    fetch_args = ["fetch", "origin", fetch_ref, "--prune"]
    if shallow:
        fetch_args.extend(["--depth", "1"])
    run_git(fetch_args, cwd=repo, timeout=timeout)
    latest = run_git(["rev-parse", f"origin/{target_branch}"], cwd=repo, timeout=timeout)
    if latest == baseline:
        return baseline
    old_head = head_commit(repo)
    try:
        run_git(["rebase", "--onto", latest, baseline, old_head], cwd=repo, timeout=timeout)
    except GitError as exc:
        raise RebaseConflictError(str(exc), latest) from exc
    return latest


def merge_target_into_branch(
    repo: Path,
    source_branch: str,
    target_branch: str,
    *,
    timeout: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> str:
    """Keep the published MR source history while preparing a target merge for repair."""
    if run_git(["status", "--porcelain"], cwd=repo, timeout=timeout):
        raise GitError("MR source worktree must be clean before merging its target")
    fetch_args = ["fetch", "--no-tags"]
    if run_git(["rev-parse", "--is-shallow-repository"], cwd=repo, timeout=timeout) == "true":
        fetch_args.append("--unshallow")
    fetch_args.extend([
        "origin",
        f"+refs/heads/{source_branch}:refs/remotes/origin/{source_branch}",
        f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}",
    ])
    run_git(fetch_args, cwd=repo, timeout=timeout)
    source = run_git(["rev-parse", f"refs/remotes/origin/{source_branch}"], cwd=repo, timeout=timeout)
    if head_commit(repo) != source:
        raise GitError("MR source branch changed outside this task")
    target = run_git(["rev-parse", f"refs/remotes/origin/{target_branch}"], cwd=repo, timeout=timeout)
    ignored = set(run_git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                          cwd=repo, timeout=timeout).split("\0")) - {""}
    if ignored:
        ignore_case = run_git(["config", "--bool", "--default", "false", "--get", "core.ignorecase"],
                              cwd=repo, timeout=timeout) == "true"
        ignored = {path.casefold() if ignore_case else path for path in ignored}
        protected = ignored | {str(parent) for path in ignored for parent in Path(path).parents}
        target_paths = run_git(["diff", "--name-only", "--diff-filter=ACMRT", "-z", f"{source}...{target}"],
                               cwd=repo, timeout=timeout).split("\0")
        for path in target_paths:
            path = path.casefold() if ignore_case else path
            if path and (path in protected or any(str(parent) in ignored for parent in Path(path).parents)):
                raise GitError(f"Target merge would overwrite ignored local data: {path}")
    merge_env = os.environ.copy()
    merge_env.update(env or {})
    for field in ("NAME", "EMAIL"):
        if merge_env.get(f"GIT_AUTHOR_{field}"):
            merge_env.setdefault(f"GIT_COMMITTER_{field}", merge_env[f"GIT_AUTHOR_{field}"])
    try:
        run_git(["merge", "--no-ff", "--no-commit", "--no-overwrite-ignore", target],
                cwd=repo, env=merge_env, timeout=timeout)
    except GitError:
        if not run_git(["diff", "--name-only", "--diff-filter=U"], cwd=repo, timeout=timeout):
            raise
    return target


def continue_rebase(repo: Path, *, timeout: Optional[int] = None) -> None:
    run_git(["add", "-A"], cwd=repo, timeout=timeout)
    run_git(["-c", "core.editor=true", "rebase", "--continue"], cwd=repo, timeout=timeout)


def abort_rebase(repo: Path, *, timeout: Optional[int] = None) -> None:
    run_git(["rebase", "--abort"], cwd=repo, timeout=timeout)


def export_patch(repo: Path, baseline: str) -> str:
    """A human-applicable patch (`git am`) of everything committed since baseline."""
    return run_git(["format-patch", f"{baseline}..HEAD", "--stdout"], cwd=repo)


def commit_all(repo: Path, message: str, author_name: str, author_email: str, *, amend: bool = False) -> str:
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
    args = ["commit", "-m", message]
    if amend:
        args.append("--amend")
    run_git(args, cwd=repo, env=env)
    return run_git(["rev-parse", "HEAD"], cwd=repo)


def push_branch(repo: Path, branch: str) -> None:
    run_git(["push", "-u", "origin", branch], cwd=repo)


def push_head_to_branch(repo: Path, target_branch: str) -> None:
    _guard_direct_push(repo, target_branch)
    run_git(["push", "origin", f"HEAD:{target_branch}"], cwd=repo)


def _guard_direct_push(repo: Path, target: str) -> None:
    origin = run_git(["remote", "get-url", "origin"], cwd=repo)
    if target.removeprefix("refs/heads/") == "pre_release" and re.search(r"[:/]im/cable(?:\.git)?/?$", origin):
        raise GitError("im/cable pre_release 禁止直接 push，必须使用 feature 分支和 MR")


def push_merge_request(repo: Path, source_branch: str, target_branch: str, title: str) -> str:
    """Use GitLab's native push options; a branch push alone is not a created MR."""
    output = run_git([
        "push", "origin", f"HEAD:refs/heads/{source_branch}",
        "-o", "merge_request.create", "-o", f"merge_request.target={target_branch}",
        "-o", f"merge_request.title={title.splitlines()[0]}",
        "-o", "merge_request.remove_source_branch",
    ], cwd=repo)
    match = re.search(r"https?://[^\s<>]+/merge_requests/[0-9]+(?=\s|$)", output)
    if not match:
        raise GitError(f"修复分支 {source_branch} 已推送，但未确认 MR 创建；请检查远端分支。\n{output}")
    return match.group(0)


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
