import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_git_ops import _clone_pair, _git, _git_output
from zentao_auto_fixer.git_ops import GitError, head_commit, merge_target_into_branch


SOURCE = "feature/zentao-9240"


class MergeTargetIntoBranchTests(unittest.TestCase):
    def _branches(self, root, *, conflict=False):
        remote, seed, worker = _clone_pair(root)
        _git(worker, "checkout", "-qb", SOURCE)
        (worker / "base.txt" if conflict else worker / "fix.txt").write_text("fix\n")
        _git(worker, "add", "-A")
        _git(worker, "commit", "-qm", "fix")
        _git(worker, "push", "-q", "origin", f"HEAD:{SOURCE}")
        source = head_commit(worker)
        (seed / "base.txt" if conflict else seed / "target.txt").write_text("target\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "target update")
        _git(seed, "push", "-q", "origin", "dev")
        return remote, seed, worker, source, head_commit(seed)

    def _commit_and_push(self, remote, worker, source, target):
        _git(worker, "add", "-A")
        _git(worker, "commit", "-qm", "merge target and repair")
        _git(worker, "push", "-q", "origin", f"HEAD:{SOURCE}")
        merged = head_commit(worker)
        self.assertEqual(_git_output(remote, "rev-parse", SOURCE), merged)
        self.assertEqual(_git_output(remote, "rev-parse", "dev"), target)
        _git(remote, "merge-base", "--is-ancestor", source, merged)
        _git(remote, "merge-base", "--is-ancestor", target, merged)
        self.assertEqual(_git_output(worker, "status", "--porcelain"), "")

    def test_target_advance_merges_without_rewriting_published_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, _, worker, source, target = self._branches(Path(tmp))
            self.assertEqual(merge_target_into_branch(worker, SOURCE, "dev"), target)
            self.assertEqual(head_commit(worker), source)
            self.assertEqual((worker / "fix.txt").read_text(), "fix\n")
            self.assertEqual((worker / "target.txt").read_text(), "target\n")
            self._commit_and_push(remote, worker, source, target)

    def test_real_conflicts_remain_available_for_the_repair_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, _, worker, source, target = self._branches(Path(tmp), conflict=True)
            self.assertEqual(merge_target_into_branch(worker, SOURCE, "dev"), target)
            self.assertEqual(head_commit(worker), source)
            self.assertEqual(_git_output(worker, "diff", "--name-only", "--diff-filter=U"), "base.txt")
            self.assertIn("<<<<<<<", (worker / "base.txt").read_text())
            (worker / "base.txt").write_text("target + fix\n")
            self._commit_and_push(remote, worker, source, target)

    def test_existing_author_environment_supplies_the_merge_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, worker, _, target = self._branches(Path(tmp))
            _git(worker, "config", "--unset", "user.name")
            _git(worker, "config", "--unset", "user.email")
            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                                         "GIT_AUTHOR_NAME": "Service", "GIT_AUTHOR_EMAIL": "service@example.com"}):
                os.environ.pop("GIT_COMMITTER_NAME", None)
                os.environ.pop("GIT_COMMITTER_EMAIL", None)
                self.assertEqual(merge_target_into_branch(worker, SOURCE, "dev"), target)

    def test_caller_can_supply_the_service_identity_without_git_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, worker, _, target = self._branches(Path(tmp))
            _git(worker, "config", "--unset", "user.name")
            _git(worker, "config", "--unset", "user.email")
            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}):
                for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
                    os.environ.pop(name, None)
                self.assertEqual(merge_target_into_branch(
                    worker, SOURCE, "dev",
                    env={"GIT_AUTHOR_NAME": "Service", "GIT_AUTHOR_EMAIL": "service@example.com"},
                ), target)

    def test_shallow_single_branch_clone_fetches_both_histories(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                remote, _, _, source, target = self._branches(root, conflict=conflict)
                worker = root / "shallow"
                _git(root, "clone", "-q", "--depth", "1", "--single-branch", "--branch", SOURCE,
                     remote.as_uri(), str(worker))
                _git(worker, "config", "user.name", "worker")
                _git(worker, "config", "user.email", "worker@example.com")
                self.assertEqual(_git_output(worker, "rev-parse", "--is-shallow-repository"), "true")
                self.assertEqual(merge_target_into_branch(worker, SOURCE, "dev"), target)
                self.assertEqual(_git_output(worker, "rev-parse", "--is-shallow-repository"), "false")
                self.assertEqual(head_commit(worker), source)
                if conflict:
                    self.assertIn("<<<<<<<", (worker / "base.txt").read_text())
                    (worker / "base.txt").write_text("target + fix\n")
                self._commit_and_push(remote, worker, source, target)

    def test_dirty_worktree_is_rejected_without_losing_local_changes(self):
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as tmp:
                _, _, worker, source, _ = self._branches(Path(tmp))
                (worker / "base.txt").write_text("local work\n")
                if staged:
                    _git(worker, "add", "base.txt")
                with self.assertRaisesRegex(GitError, "must be clean"):
                    merge_target_into_branch(worker, SOURCE, "dev")
                self.assertEqual(head_commit(worker), source)
                self.assertEqual((worker / "base.txt").read_text(), "local work\n")
                self.assertFalse((worker / "target.txt").exists())

    def test_external_source_update_is_rejected_without_merging(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, seed, worker, source, _ = self._branches(Path(tmp))
            _git(seed, "fetch", "-q", "origin", SOURCE)
            _git(seed, "checkout", "-q", "-b", SOURCE, "FETCH_HEAD")
            (seed / "someone-else.txt").write_text("external change\n")
            _git(seed, "add", "-A")
            _git(seed, "commit", "-qm", "external change")
            _git(seed, "push", "-q", "origin", SOURCE)
            with self.assertRaisesRegex(GitError, "changed outside this task"):
                merge_target_into_branch(worker, SOURCE, "dev")
            self.assertEqual(head_commit(worker), source)
            self.assertEqual(_git_output(worker, "status", "--porcelain"), "")

    def test_fetch_failure_is_not_misreported_as_a_merge_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, worker, source, _ = self._branches(root)
            _git(worker, "remote", "set-url", "origin", str(root / "missing.git"))
            with self.assertRaises(GitError):
                merge_target_into_branch(worker, SOURCE, "dev")
            self.assertEqual(head_commit(worker), source)
            self.assertEqual(_git_output(worker, "status", "--porcelain"), "")

    def test_unrelated_history_is_not_misreported_as_a_merge_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, seed, worker, source, _ = self._branches(Path(tmp))
            _git(seed, "checkout", "-q", "--orphan", "unrelated")
            _git(seed, "rm", "-qrf", ".")
            (seed / "unrelated.txt").write_text("unrelated history\n")
            _git(seed, "add", "-A")
            _git(seed, "commit", "-qm", "unrelated root")
            _git(seed, "push", "-q", "origin", "unrelated")
            with self.assertRaisesRegex(GitError, "unrelated histories"):
                merge_target_into_branch(worker, SOURCE, "unrelated")
            self.assertEqual(head_commit(worker), source)
            self.assertEqual(_git_output(worker, "status", "--porcelain"), "")

    def test_ignored_local_file_is_not_overwritten_by_the_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, worker, source, _ = self._branches(Path(tmp))
            exclude = worker / ".git/info/exclude"
            exclude.write_text(exclude.read_text() + "\ntarget.txt\n")
            (worker / "target.txt").write_text("ignored local data\n")
            self.assertEqual(_git_output(worker, "status", "--porcelain"), "")
            with self.assertRaises(GitError):
                merge_target_into_branch(worker, SOURCE, "dev")
            self.assertEqual(head_commit(worker), source)
            self.assertEqual((worker / "target.txt").read_text(), "ignored local data\n")

    def test_ignored_file_and_directory_collisions_are_rejected(self):
        for target_name, local_name in (("assets/target.txt", "assets"), ("assets", "assets/local.secret")):
            with self.subTest(target=target_name, local=local_name), tempfile.TemporaryDirectory() as tmp:
                _, seed, worker, source, _ = self._branches(Path(tmp))
                destination = seed / target_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("target data\n")
                _git(seed, "add", "-A")
                _git(seed, "commit", "-qm", "target path update")
                _git(seed, "push", "-q", "origin", "dev")
                local = worker / local_name
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_text("ignored local data\n")
                exclude = worker / ".git/info/exclude"
                exclude.write_text(exclude.read_text() + "\n" + local_name + "\n")
                self.assertEqual(_git_output(worker, "status", "--porcelain"), "")
                with self.assertRaisesRegex(GitError, "overwrite ignored local data"):
                    merge_target_into_branch(worker, SOURCE, "dev")
                self.assertEqual(head_commit(worker), source)
                self.assertEqual(local.read_text(), "ignored local data\n")

    def test_unrelated_ignored_cache_does_not_block_the_target_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, worker, _, target = self._branches(Path(tmp))
            local = worker / "local.secret"
            local.write_text("ignored local data\n")
            exclude = worker / ".git/info/exclude"
            exclude.write_text(exclude.read_text() + "\nlocal.secret\n")
            self.assertEqual(merge_target_into_branch(worker, SOURCE, "dev"), target)
            self.assertEqual(local.read_text(), "ignored local data\n")
            self.assertEqual((worker / "target.txt").read_text(), "target\n")

    def test_ignored_collision_respects_git_case_insensitivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, worker, source, _ = self._branches(Path(tmp))
            _git(worker, "config", "core.ignorecase", "true")
            local = worker / "TARGET.txt"
            local.write_text("ignored local data\n")
            exclude = worker / ".git/info/exclude"
            exclude.write_text(exclude.read_text() + "\nTARGET.txt\n")
            with self.assertRaisesRegex(GitError, "overwrite ignored local data"):
                merge_target_into_branch(worker, SOURCE, "dev")
            self.assertEqual(head_commit(worker), source)
            self.assertEqual(local.read_text(), "ignored local data\n")


if __name__ == "__main__":
    unittest.main()
