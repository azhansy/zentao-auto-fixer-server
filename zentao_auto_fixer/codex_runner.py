from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


class CodexError(RuntimeError):
    pass


def run_codex_fix(
    codex_bin: str,
    worktree: Path,
    bug_id: int,
    bug_title: str,
    log_path: Optional[Path] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    return run_codex_batch_fix(
        codex_bin,
        worktree,
        [(bug_id, bug_title)],
        log_path,
        timeout_seconds=timeout_seconds,
    )


def run_codex_batch_fix(
    codex_bin: str,
    worktree: Path,
    bugs: Sequence[Tuple[int, str]],
    log_path: Optional[Path] = None,
    timeout_seconds: Optional[int] = None,
    env_overrides: Optional[Dict[str, str]] = None,
) -> str:
    prompt = _batch_prompt(bugs)
    return _run_codex_exec(
        codex_bin,
        worktree,
        prompt,
        log_path,
        timeout_seconds=timeout_seconds,
        env_overrides=env_overrides,
        log_label=_bug_log_label(bugs),
    )


def _batch_prompt(bugs: Sequence[Tuple[int, str]]) -> str:
    if len(bugs) == 1:
        bug_id, bug_title = bugs[0]
        return f"""使用 zentao-bug-fixer skill，只处理禅道 Bug #{bug_id}。

要求：
1. 当前目录就是需要修复的业务代码仓库。
2. 只读取并修复 Bug #{bug_id}，不要批量处理其他 Bug。
3. 修复后运行仓库里最相关的验证命令。
4. 修复完成后按 zentao-bug-fixer skill 要求回写禅道备注。
5. 不要提交 git commit，也不要 push；提交和推送由外层自动修复服务完成。

Bug 标题：{bug_title}
"""
    bug_lines = "\n".join(f"- #{bug_id}: {title}" for bug_id, title in bugs)
    bug_ids = ", ".join(f"#{bug_id}" for bug_id, _title in bugs)
    return f"""使用 zentao-bug-fixer skill，在同一个批次内处理以下禅道 Bugs：{bug_ids}。

Bug 列表：
{bug_lines}

要求：
1. 当前目录就是需要修复的业务代码仓库。
2. 只读取并修复上面列出的 Bug，不要处理列表外的 Bug。
3. 在一个工作区内完成全部 Bug 的代码修复；如果多个 Bug 共享根因，可以合并修改，但不要遗漏任何一个 Bug。
4. 修复后运行仓库里最相关的验证命令。
5. 对每个 Bug 写禅道备注，备注包含问题原因和解决方案。
6. 不要 resolve、close 或修改 Bug 状态；外层自动修复服务会在 commit 和 push 成功后批量标记已解决。
7. 不要提交 git commit，也不要 push；提交和推送由外层自动修复服务完成。
"""


def _run_codex_exec(
    codex_bin: str,
    worktree: Path,
    prompt: str,
    log_path: Optional[Path] = None,
    timeout_seconds: Optional[int] = None,
    env_overrides: Optional[Dict[str, str]] = None,
    log_label: str = "codex",
) -> str:
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
        env=_codex_env(env_overrides),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    assert process.stdout is not None
    with (log_path.open("a", encoding="utf-8") if log_path else _NullWriter()) as log_file:
        log_file.write("$ " + " ".join(cmd[:-1]) + " <prompt>\n")
        reader = threading.Thread(
            target=_stream_output,
            args=(process.stdout, log_file, output_tail),
            name=f"codex-output-{log_label}",
            daemon=True,
        )
        reader.start()
        timed_out = False
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            log_file.write(f"\n[codex_runner] codex exec timed out after {timeout_seconds}s; terminating process group.\n")
            log_file.flush()
            _terminate_process_group(process)
            try:
                return_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log_file.write("[codex_runner] codex exec did not exit after SIGTERM; killing process group.\n")
                log_file.flush()
                _kill_process_group(process)
                return_code = process.wait(timeout=10)
        reader.join(timeout=10)
    output = "".join(output_tail)
    if timed_out:
        raise CodexError(f"codex exec timed out after {timeout_seconds}s:\n{output}")
    if return_code != 0:
        raise CodexError(f"codex exec failed with exit {return_code}:\n{output}")
    return output


def _codex_env(env_overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return env


def _bug_log_label(bugs: Sequence[Tuple[int, str]]) -> str:
    if len(bugs) == 1:
        return str(bugs[0][0])
    return f"batch-{bugs[0][0]}-{bugs[-1][0]}"


def _stream_output(stream, log_file, output_tail: List[str]) -> None:
    for line in stream:
        log_file.write(line)
        log_file.flush()
        output_tail.append(line)
        if len(output_tail) > 200:
            del output_tail[:-200]


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except (AttributeError, OSError):
        process.terminate()


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except (AttributeError, OSError):
        process.kill()


class _NullWriter:
    def __enter__(self) -> "_NullWriter":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def write(self, _text: str) -> None:
        return None

    def flush(self) -> None:
        return None
