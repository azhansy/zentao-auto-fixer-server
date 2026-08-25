from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


SUPPORTED_AGENTS = ("codex", "claude")


class AgentError(RuntimeError):
    pass



class TriageResultError(RuntimeError):
    pass


def run_agent_batch_fix(
    agent: str,
    agent_bin: str,
    zentao_client_script: Path,
    app_worktree: Path,
    backend_worktree: Optional[Path],
    bugs: Sequence[Tuple[int, str]],
    result_path: Path,
    log_path: Optional[Path] = None,
    timeout_seconds: Optional[int] = None,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Dict[int, Dict[str, Any]]:
    """Triage and fix a batch of bugs, returning the agent's per-bug verdict keyed by bug id."""
    result_path.parent.mkdir(parents=True, exist_ok=True)
    if result_path.exists():
        result_path.unlink()
    prompt = _batch_prompt(bugs, zentao_client_script, app_worktree, backend_worktree, result_path)
    _run_agent(
        agent,
        agent_bin,
        app_worktree,
        [path for path in (backend_worktree, result_path.parent) if path is not None],
        prompt,
        log_path,
        timeout_seconds=timeout_seconds,
        env_overrides=env_overrides,
        log_label=_bug_log_label(bugs),
    )
    return read_triage_result(result_path, [bug_id for bug_id, _title in bugs])


def _batch_prompt(
    bugs: Sequence[Tuple[int, str]],
    zentao_client_script: Path,
    app_worktree: Path,
    backend_worktree: Optional[Path],
    result_path: Path,
) -> str:
    bug_lines = "\n".join(f"- #{bug_id}: {title}" for bug_id, title in bugs)
    bug_ids = ", ".join(f"#{bug_id}" for bug_id, _title in bugs)
    repo_lines = [f"- app（App 客户端仓库，同时覆盖 Android 和 iOS）：{app_worktree}"]
    if backend_worktree is not None:
        repo_lines.append(f"- backend（后端服务仓库）：{backend_worktree}")
    else:
        repo_lines.append("- backend：本项目没有配置后端仓库，判定为后端问题时只能标记为 rejected。")
    repos = "\n".join(repo_lines)
    return f"""在同一个批次内先分诊、再修复以下禅道 Bugs：{bug_ids}。

Bug 列表：
{bug_lines}

可用代码仓库：
{repos}

要求：
1. 逐个 Bug 先做分诊。读禅道详情只允许用这一条只读命令，不要用别的方式访问禅道：
   python3 {zentao_client_script} bug <bugID>
2. 默认先从 app 入手；确认是后端问题就改 backend；app 和 backend 都要改就一起改。
3. 如果读完详情仍然判断不出是什么问题（描述不足、无法定位到代码），不要猜着改，把这个 Bug 标记为 rejected。
   标记 rejected 之前，必须把你为它做过的试探性改动全部还原，不能留在工作区里。
4. 只修改上面列出的仓库路径里的文件，不要处理列表外的 Bug。
5. 修完运行仓库里最相关的验证命令。验证产生的临时文件、日志、笔记请自行删除，不要留在工作区。
6. 禁止执行 git commit、git push、git reset、git checkout 等任何改变仓库状态的 git 命令。
7. 禁止写禅道备注、禁止指派、禁止 resolve 或改 Bug 状态。提交、推送和禅道回写全部由外层自动修复服务完成，
   你多写一条备注会破坏服务的去重判断。
8. 这是无人值守的批处理，没有人能回答你的问题：不要请求确认、不要输出"等待确认"后停下、不要调用任何需要人工
   拍板的工具（例如 dashu-ask），直接把该做的做完。
9. 最后把分诊和修复结果写成 JSON 文件到：{result_path}
   格式（每个 Bug 一条，decision 只能是 fixed 或 rejected）：
   {{
     "bugs": [
       {{"id": 1234, "decision": "fixed", "targets": ["app"], "platform": "ios",
         "understanding": "用一两句话复述你理解的这个 Bug 是什么现象",
         "steps": ["1. 打开某页面", "2. 点击某按钮", "3. 出现什么错误现象"],
         "cause": "问题原因，一到三句话", "solution": "改了什么，一到三句话"}},
       {{"id": 1235, "decision": "rejected",
         "understanding": "你理解到的部分，看不懂就写你的猜测",
         "steps": ["从描述里能还原出来的操作步骤，还原不出就留空数组"],
         "reason": "为什么无法定位", "missing": "需要提 Bug 的人补充哪些信息"}}
     ]
   }}
   targets 只能填 "app" 和/或 "backend"；platform 填 android、ios、both 或 unknown。
   understanding 和 steps 每个 Bug 都必须写（包括 rejected 的）：它们会原样贴进禅道备注，
   让测试同事一眼核对 AI 理解的和他报的是不是同一个问题。steps 写你实际用来定位/验证的
   操作路径，不要照抄禅道原文，也不要写代码层面的调用链。
   列表里的每个 Bug 都必须在这个文件里有且只有一条记录。
"""


def read_triage_result(result_path: Path, expected_bug_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    if not result_path.is_file():
        raise TriageResultError(f"the agent did not write the triage result file {result_path}")
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TriageResultError(f"Invalid triage result JSON in {result_path}: {exc}") from exc
    entries = data.get("bugs") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise TriageResultError(f"Triage result {result_path} must contain a bugs array")

    verdicts: Dict[int, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            bug_id = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        if bug_id in verdicts:
            raise TriageResultError(f"Bug #{bug_id} appears more than once in the triage result")
        decision = str(entry.get("decision") or "").strip().lower()
        if decision not in {"fixed", "rejected"}:
            raise TriageResultError(f"Bug #{bug_id} has an unknown decision {entry.get('decision')!r}")
        verdicts[bug_id] = {
            "decision": decision,
            "targets": _normalized_targets(entry.get("targets")),
            "platform": str(entry.get("platform") or "unknown").strip().lower(),
            "understanding": str(entry.get("understanding") or "").strip(),
            "steps": _normalized_steps(entry.get("steps")),
            "cause": str(entry.get("cause") or "").strip(),
            "solution": str(entry.get("solution") or "").strip(),
            "reason": str(entry.get("reason") or "").strip(),
            "missing": str(entry.get("missing") or "").strip(),
        }
    missing = [bug_id for bug_id in expected_bug_ids if bug_id not in verdicts]
    if missing:
        raise TriageResultError(
            "Triage result is missing verdicts for " + ", ".join(f"#{bug_id}" for bug_id in missing)
        )
    return verdicts


def _normalized_steps(value: Any) -> List[str]:
    """Reproduction steps as a clean list, whether the agent sent a list or one newline-joined string."""
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    steps = [" ".join(str(item).split()) for item in value]
    return [step for step in steps if step]


def _normalized_targets(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item for item in ("app", "backend") if item in {str(entry).strip().lower() for entry in value}]


def build_agent_command(
    agent: str,
    agent_bin: str,
    worktree: Path,
    extra_dirs: Sequence[Path],
    prompt: str,
) -> List[str]:
    """Non-interactive invocation for the configured coding agent, with the prompt as one argument."""
    if agent == "claude":
        # The prompt is a positional argument and --add-dir is variadic, so the prompt must come first
        # or --add-dir would swallow it.
        cmd = [agent_bin, "-p", prompt, "--dangerously-skip-permissions"]
        for extra in extra_dirs:
            cmd.extend(["--add-dir", str(extra)])
        return cmd
    if agent == "codex":
        cmd = [agent_bin, "exec", "--cd", str(worktree)]
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
        cmd.append(prompt)
        return cmd
    raise AgentError(f"Unknown agent {agent!r}; supported agents are {', '.join(SUPPORTED_AGENTS)}")


def _run_agent(
    agent: str,
    agent_bin: str,
    worktree: Path,
    extra_dirs: Sequence[Path],
    prompt: str,
    log_path: Optional[Path] = None,
    timeout_seconds: Optional[int] = None,
    env_overrides: Optional[Dict[str, str]] = None,
    log_label: str = "agent",
) -> str:
    cmd = build_agent_command(agent, agent_bin, worktree, extra_dirs, prompt)
    output_tail: List[str] = []
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        cmd,
        cwd=str(worktree),
        env=_agent_env(env_overrides),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    assert process.stdout is not None
    with (log_path.open("a", encoding="utf-8") if log_path else _NullWriter()) as log_file:
        log_file.write("$ " + " ".join(part for part in cmd if part != prompt) + " <prompt>\n")
        reader = threading.Thread(
            target=_stream_output,
            args=(process.stdout, log_file, output_tail),
            name=f"agent-output-{log_label}",
            daemon=True,
        )
        reader.start()
        timed_out = False
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            log_file.write(f"\n[agent_runner] {agent} timed out after {timeout_seconds}s; terminating process group.\n")
            log_file.flush()
            _terminate_process_group(process)
            try:
                return_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log_file.write(f"[agent_runner] {agent} did not exit after SIGTERM; killing process group.\n")
                log_file.flush()
                _kill_process_group(process)
                return_code = process.wait(timeout=10)
        reader.join(timeout=10)
    output = "".join(output_tail)
    if timed_out:
        raise AgentError(f"{agent} timed out after {timeout_seconds}s:\n{output}")
    if return_code != 0:
        raise AgentError(f"{agent} failed with exit {return_code}:\n{output}")
    return output


def _agent_env(env_overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
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
