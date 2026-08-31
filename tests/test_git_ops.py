import subprocess
import tempfile
import unittest
from pathlib import Path

from zentao_auto_fixer.git_ops import (
    RebaseConflictError,
    continue_rebase,
    export_patch,
    head_commit,
    push_head_dry_run,
    push_head_to_branch,
    rebase_onto_latest_remote,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _clone_pair(root: Path) -> tuple[Path, Path, Path]:
    remote = root / "remote.git"
    seed = root / "seed"
    worker = root / "worker"
    _git(root, "init", "--bare", "-q", str(remote))
    _git(root, "clone", "-q", str(remote), str(seed))
    _git(seed, "config", "user.email", "seed@example.com")
    _git(seed, "config", "user.name", "seed")
    _git(seed, "checkout", "-q", "-b", "dev")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "push", "-q", "-u", "origin", "dev")
    _git(root, "clone", "-q", "--branch", "dev", str(remote), str(worker))
    _git(worker, "config", "user.email", "worker@example.com")
    _git(worker, "config", "user.name", "worker")
    return remote, seed, worker


class ExportPatchTests(unittest.TestCase):
    def test_export_patch_is_applicable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q")
            _git(repo, "config", "user.email", "a@example.com")
            _git(repo, "config", "user.name", "a")
            (repo / "file.txt").write_text("before\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "baseline")
            baseline = head_commit(repo)

            (repo / "file.txt").write_text("after\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "fix")

            patch = export_patch(repo, baseline)
            self.assertIn("Subject: [PATCH] fix", patch)
            self.assertIn("-before", patch)
            self.assertIn("+after", patch)


class RebaseLatestTests(unittest.TestCase):
    def test_remote_advance_is_rebased_and_pushable(self):
        with tempfile.TemporaryDirectory() as tmp:
            _remote, seed, worker = _clone_pair(Path(tmp))
            baseline = head_commit(worker)
            (worker / "fix.txt").write_text("fix\n")
            _git(worker, "add", "-A")
            _git(worker, "commit", "-q", "-m", "fix")

            (seed / "remote.txt").write_text("remote\n")
            _git(seed, "add", "-A")
            _git(seed, "commit", "-q", "-m", "remote advance")
            latest = head_commit(seed)
            _git(seed, "push", "-q", "origin", "dev")

            self.assertEqual(
                rebase_onto_latest_remote(worker, "dev", baseline, shallow=True),
                latest,
            )
            self.assertEqual(_git_output(worker, "rev-parse", "HEAD^"), latest)
            self.assertEqual((worker / "fix.txt").read_text(), "fix\n")
            self.assertEqual((worker / "remote.txt").read_text(), "remote\n")
            push_head_dry_run(worker, "dev")
            push_head_to_branch(worker, "dev")

    def test_content_conflict_can_be_resolved_and_rebase_continued(self):
        with tempfile.TemporaryDirectory() as tmp:
            _remote, seed, worker = _clone_pair(Path(tmp))
            baseline = head_commit(worker)
            (worker / "base.txt").write_text("worker fix\n")
            _git(worker, "add", "-A")
            _git(worker, "commit", "-q", "-m", "fix")

            (seed / "base.txt").write_text("remote change\n")
            _git(seed, "add", "-A")
            _git(seed, "commit", "-q", "-m", "remote advance")
            latest = head_commit(seed)
            _git(seed, "push", "-q", "origin", "dev")

            with self.assertRaises(RebaseConflictError) as raised:
                rebase_onto_latest_remote(worker, "dev", baseline, shallow=True)
            self.assertEqual(raised.exception.latest, latest)
            self.assertIn("<<<<<<<", (worker / "base.txt").read_text())

            (worker / "base.txt").write_text("remote change + worker fix\n")
            continue_rebase(worker)
            self.assertEqual(_git_output(worker, "rev-parse", "HEAD^"), latest)
            self.assertEqual((worker / "base.txt").read_text(), "remote change + worker fix\n")


if __name__ == "__main__":
    unittest.main()
