#!/usr/bin/env python3
"""One-off check that the ZenTao assign API works with the configured account.

Assigns a bug to whoever it is already assigned to, so nothing actually changes.
This is the only write path the auto fixer uses that the test suite cannot cover.

Pick an old closed bug as the target: the check still leaves one "assigned" line in
that bug's ZenTao history, and nobody is watching a closed bug.

    python3 scripts/check_assign.py <closed_bug_id>
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zentao_auto_fixer.config import Settings  # noqa: E402
from zentao_auto_fixer.zentao import assign_bug  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    bug_id = int(sys.argv[1])
    settings = Settings.from_env()

    detail = subprocess.run(
        ["python3", str(settings.zentao_client_script), "bug", str(bug_id)],
        capture_output=True,
        text=True,
        check=True,
    )
    current = json.loads(detail.stdout).get("assignedTo") or {}
    account = current.get("account") if isinstance(current, dict) else str(current)
    if not account:
        print(f"Bug #{bug_id} has no current assignee, pick another bug.")
        return 1

    status = json.loads(detail.stdout).get("status")
    if status == "active":
        print(f"Bug #{bug_id} is still active - prefer a closed bug so the extra history line bothers nobody.")
    print(f"Re-assigning bug #{bug_id} to its current assignee {account!r} (no-op)...")
    assign_bug(bug_id, account)
    print("OK - the assign API works, so handing bugs back to their reporter will work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
