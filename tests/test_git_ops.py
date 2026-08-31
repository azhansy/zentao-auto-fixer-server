import subprocess
import tempfile
import unittest
from pathlib import Path

from zentao_auto_fixer.git_ops import export_patch, head_commit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


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


if __name__ == "__main__":
    unittest.main()
