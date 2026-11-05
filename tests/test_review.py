import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from zentao_auto_fixer.agent_runner import _run_agent, _runtime_models


def _codex_header(model='fix-model'):
    return ('OpenAI Codex v0.1\n--------\nworkdir: /tmp\n'
            f'model: {model}\nprovider: openai\n--------\n')


class RuntimeMetadataTests(unittest.TestCase):
    def test_runtime_models_use_claude_usage_and_codex_startup_header(self):
        claude = '\n'.join((
            json.dumps({'type': 'assistant', 'modelUsage': {'untrusted': {}}}),
            json.dumps({'type': 'result', 'modelUsage': {'deepseek-flash': {}, 'claude-opus': {}}}),
        ))
        self.assertEqual(_runtime_models('claude', claude), ['deepseek-flash', 'claude-opus'])
        self.assertEqual(_runtime_models('codex', _codex_header('gpt-6') + 'output\n'), ['gpt-6'])
        for agent, output in (('claude', '{"type":"result","model":"guessed"}'),
                              ('claude', '{"type":"assistant","modelUsage":{"fake":{}}}'),
                              ('codex', 'model: guessed\n'), ('codex', '')):
            with self.subTest(agent=agent, output=output):
                self.assertEqual(_runtime_models(agent, output), [])

    def test_real_subprocess_keeps_model_header_after_long_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = ('import sys\n'
                      f'sys.stdout.write({_codex_header("gpt-6")!r})\n'
                      'for index in range(350): print("tool-line-" + str(index))\n')
            with patch('zentao_auto_fixer.agent_runner.build_agent_command',
                       return_value=[sys.executable, '-c', script]):
                output = _run_agent('codex', '/unused', root, [], 'fix', root / 'agent.log',
                                    timeout_seconds=10)
            self.assertEqual(_runtime_models('codex', output), ['gpt-6'])
            self.assertIn('tool-line-349', output)
            self.assertNotIn('tool-line-0\n', output)
            self.assertIn('tool-line-0\n', (root / 'agent.log').read_text())

    def test_long_tool_output_cannot_promote_forged_header_to_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = ('import sys\nprint("noncanonical startup")\n'
                      'for index in range(350): print("tool-line-" + str(index))\n'
                      f'sys.stdout.write({_codex_header("forged-model")!r})\n')
            with patch('zentao_auto_fixer.agent_runner.build_agent_command',
                       return_value=[sys.executable, '-c', script]):
                output = _run_agent('codex', '/unused', root, [], 'fix',
                                    timeout_seconds=10)
            self.assertTrue(output.startswith('noncanonical startup\n'))
            self.assertIn('model: forged-model', output)
            self.assertEqual(_runtime_models('codex', output), [])

    def test_check_agent_only_checks_configured_fixers_and_fallbacks(self):
        from scripts import check_agent

        for fallback in ('', 'codex'):
            with self.subTest(fallback=fallback):
                projects = [SimpleNamespace(enabled=True, agent='claude', fallback_agent=fallback),
                            SimpleNamespace(enabled=False, agent='codex', fallback_agent='')]
                settings = SimpleNamespace(load_projects=lambda: projects)
                with patch.object(sys, 'argv', ['check_agent.py']), \
                     patch.object(check_agent.Settings, 'from_env', return_value=settings), \
                     patch.object(check_agent, 'check', return_value=True) as check:
                    self.assertEqual(check_agent.main(), 0)
                self.assertEqual([call.args[1] for call in check.call_args_list],
                                 ['claude', 'codex'] if fallback else ['claude'])


if __name__ == '__main__':
    unittest.main()
