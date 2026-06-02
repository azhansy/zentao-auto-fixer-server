from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional


class CodexError(RuntimeError):
    pass


def run_codex_fix(
    codex_bin: str,
    worktree: Path,
    bug_id: int,
    bug_title: str,
    log_path: Optional[Path] = None,
) -> str:
    prompt = f"""使用 zentao-bug-fixer skill，只处理禅道 Bug #{bug_id}。

要求：
1. 当前目录就是需要修复的业务代码仓库。
2. 只读取并修复 Bug #{bug_id}，不要批量处理其他 Bug。
3. 修复后运行仓库里最相关的验证命令。
4. 修复完成后按 zentao-bug-fixer skill 要求回写禅道备注。
5. 不要提交 git commit，也不要 push；提交和推送由外层自动修复服务完成。

Bug 标题：{bug_title}
"""
    cmd = [
        codex_bin,
        "exec",
        "--cd",
        str(worktree),
        "--dangerously-bypass-approvals-and-sandbox",
        prompt,
    ]
    output_tail: List[str] = []
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        cmd,
        cwd=str(worktree),
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert process.stdout is not None
    with (log_path.open("a", encoding="utf-8") if log_path else _NullWriter()) as log_file:
        log_file.write("$ " + " ".join(cmd[:-1]) + " <prompt>\n")
        for line in process.stdout:
            log_file.write(line)
            log_file.flush()
            output_tail.append(line)
            if len(output_tail) > 200:
                output_tail = output_tail[-200:]
    return_code = process.wait()
    output = "".join(output_tail)
    if return_code != 0:
        raise CodexError(f"codex exec failed with exit {return_code}:\n{output}")
    return output


class _NullWriter:
    def __enter__(self) -> "_NullWriter":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def write(self, _text: str) -> None:
        return None

    def flush(self) -> None:
        return None
