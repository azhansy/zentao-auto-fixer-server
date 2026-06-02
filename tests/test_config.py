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
        self.assertIn(".codex/skills/zentao-bug-fixer", str(settings.zentao_client_script))


if __name__ == "__main__":
    unittest.main()
