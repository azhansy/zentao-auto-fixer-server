#!/usr/bin/env python3
"""Smoke check that the configured AI engine actually starts.

Runs the real command the worker builds, with a throwaway prompt, in an empty
temporary directory. Touches no repository, no ZenTao, no git. One short reply,
so it costs a fraction of a cent.

    python3 scripts/check_agent.py            # every agent used in projects.json
    python3 scripts/check_agent.py claude     # just this one

Expected output: "PONG" somewhere in the reply, then "OK".
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zentao_auto_fixer.agent_runner import SUPPORTED_AGENTS, build_agent_command  # noqa: E402
from zentao_auto_fixer.config import Settings  # noqa: E402

PROMPT = "Reply with exactly one word: PONG"


def check(settings: Settings, agent: str) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        cmd = build_agent_command(agent, settings.agent_bin(agent), Path(tmp), [], PROMPT)
        print(f"$ {' '.join(part for part in cmd if part != PROMPT)} <prompt>")
        try:
            result = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            print(f"FAIL {agent}: {settings.agent_bin(agent)!r} not found on PATH")
            return False
        except subprocess.TimeoutExpired:
            print(f"FAIL {agent}: did not answer within 180s")
            return False
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        print(f"FAIL {agent}: exit {result.returncode}\n{output[-800:]}")
        return False
    if "PONG" not in output.upper():
        print(f"FAIL {agent}: started but did not answer as asked\n{output[-800:]}")
        return False
    print(f"OK {agent} starts and answers.")
    return True


def main() -> int:
    settings = Settings.from_env()
    if len(sys.argv) > 2:
        print(__doc__)
        return 2
    if len(sys.argv) == 2:
        agents = [sys.argv[1].strip().lower()]
    else:
        agents = sorted({project.agent for project in settings.load_projects() if project.enabled})
        if not agents:
            agents = sorted({project.agent for project in settings.load_projects()})
    unknown = [agent for agent in agents if agent not in SUPPORTED_AGENTS]
    if unknown:
        print(f"Unknown agent(s): {', '.join(unknown)}; supported: {', '.join(SUPPORTED_AGENTS)}")
        return 2
    return 0 if all([check(settings, agent) for agent in agents]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
