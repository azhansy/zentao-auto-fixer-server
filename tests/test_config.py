import json
import os
import tempfile
import unittest
from pathlib import Path

from zentao_auto_fixer.config import Settings


class ConfigTests(unittest.TestCase):
    def test_load_projects_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "A",
                                "zentaoProductId": 8,
                                "repoUrl": "git@example.com:a.git",
                                "targetBranch": "develop",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            old = os.environ.get("AUTO_FIXER_PROJECTS_FILE")
            os.environ["AUTO_FIXER_PROJECTS_FILE"] = str(path)
            try:
                settings = Settings.from_env()
                projects = settings.load_projects()
            finally:
                if old is None:
                    os.environ.pop("AUTO_FIXER_PROJECTS_FILE", None)
                else:
                    os.environ["AUTO_FIXER_PROJECTS_FILE"] = old
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0].zentao_product_id, 8)
            self.assertEqual(projects[0].target_branch, "develop")

    def test_load_projects_with_app_and_backend_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "A",
                                "zentaoProductId": 8,
                                "app": {"repoUrl": "git@example.com:app.git", "targetBranch": "dev"},
                                "backend": {"repoUrl": "git@example.com:api.git", "targetBranch": "main"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            project = _load_single_project(path)
            self.assertEqual(project.repo_url, "git@example.com:app.git")
            self.assertEqual(project.target_branch, "dev")
            self.assertEqual(project.backend_repo_url, "git@example.com:api.git")
            self.assertEqual(project.backend_target_branch, "main")
            self.assertTrue(project.has_backend_repo)

    def test_legacy_project_has_no_backend_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "A",
                                "zentaoProductId": 8,
                                "repoUrl": "git@example.com:a.git",
                                "targetBranch": "develop",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(_load_single_project(path).has_backend_repo)

    def test_half_configured_backend_repo_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "A",
                                "zentaoProductId": 8,
                                "repoUrl": "git@example.com:a.git",
                                "targetBranch": "develop",
                                "backend": {"repoUrl": "git@example.com:api.git"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _load_single_project(path)

    def test_agent_defaults_to_codex_and_can_be_switched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "A",
                                "zentaoProductId": 8,
                                "repoUrl": "git@example.com:a.git",
                                "targetBranch": "dev",
                            },
                            {
                                "name": "B",
                                "zentaoProductId": 9,
                                "agent": "Claude",
                                "repoUrl": "git@example.com:b.git",
                                "targetBranch": "dev",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            projects = _load_projects(path)
            self.assertEqual(projects[0].agent, "codex")
            self.assertEqual(projects[1].agent, "claude")

    def test_unknown_agent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "A",
                                "zentaoProductId": 8,
                                "agent": "gemini",
                                "repoUrl": "git@example.com:a.git",
                                "targetBranch": "dev",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _load_projects(path)

    def test_agent_bin_picks_the_matching_binary(self):
        settings = Settings.from_env()
        self.assertEqual(settings.agent_bin("claude"), settings.claude_bin)
        self.assertEqual(settings.agent_bin("codex"), settings.codex_bin)

    def test_placeholder_zentao_client_uses_default(self):
        old = os.environ.get("AUTO_FIXER_ZENTAO_CLIENT")
        os.environ["AUTO_FIXER_ZENTAO_CLIENT"] = "xxx"
        try:
            settings = Settings.from_env()
        finally:
            if old is None:
                os.environ.pop("AUTO_FIXER_ZENTAO_CLIENT", None)
            else:
                os.environ["AUTO_FIXER_ZENTAO_CLIENT"] = old
        self.assertTrue(str(settings.zentao_client_script).endswith("zentao_client.py"))
        # ~/.codex/skills is often a symlink into the shared skill source, and resolve() follows it.
        self.assertIn("skills/zentao-bug-fixer", str(settings.zentao_client_script))

    def test_codex_timeout_can_be_configured_or_disabled(self):
        old = os.environ.get("AUTO_FIXER_CODEX_TIMEOUT_SECONDS")
        try:
            os.environ["AUTO_FIXER_CODEX_TIMEOUT_SECONDS"] = "90"
            self.assertEqual(Settings.from_env().codex_timeout_seconds, 90)
            os.environ["AUTO_FIXER_CODEX_TIMEOUT_SECONDS"] = "0"
            self.assertIsNone(Settings.from_env().codex_timeout_seconds)
        finally:
            if old is None:
                os.environ.pop("AUTO_FIXER_CODEX_TIMEOUT_SECONDS", None)
            else:
                os.environ["AUTO_FIXER_CODEX_TIMEOUT_SECONDS"] = old


def _load_single_project(path: Path):
    return _load_projects(path)[0]


def _load_projects(path: Path):
    old = os.environ.get("AUTO_FIXER_PROJECTS_FILE")
    os.environ["AUTO_FIXER_PROJECTS_FILE"] = str(path)
    try:
        projects = Settings.from_env().load_projects()
    finally:
        if old is None:
            os.environ.pop("AUTO_FIXER_PROJECTS_FILE", None)
        else:
            os.environ["AUTO_FIXER_PROJECTS_FILE"] = old
    return projects


if __name__ == "__main__":
    unittest.main()
