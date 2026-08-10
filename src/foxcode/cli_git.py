"""FoxCode CLI 的 Git 相关辅助函数。"""

from __future__ import annotations

from pathlib import Path

import httpx

from .cli_ui import console
from .tools import run_subprocess


# NOTE:字符串命令入口，底层转接为参数列表形式并启用 shell 解析
async def _exec_shell(command: str, cwd: Path, timeout: int = 120) -> str:
    return await _exec_shell_args(command, cwd, timeout, shell=True)


# NOTE:底层命令执行器：优先使用参数列表（shell=False）防止注入，超时后返回错误文本
async def _exec_shell_args(
    args: list[str] | str, cwd: Path, timeout: int = 120, shell: bool = False
) -> str:
    """执行命令并返回结果文本。

    参数列表形式（shell=False）不经 shell，避免参数内容被解释为 shell 命令。
    """
    try:
        result = await run_subprocess(
            args,
            shell=shell,
            timeout=timeout,
            cwd=str(cwd),
        )
        output = ""
        if result.stdout:
            output += result.stdout.rstrip()
        if result.stderr:
            if output:
                output += "\n"
            output += f"[stderr]\n{result.stderr.rstrip()}"
        if result.returncode != 0:
            output += f"\n退出码: {result.returncode}"
        return output if output else "(命令执行成功，无输出)"
    except TimeoutError:
        return f"错误: 命令执行超时 ({timeout}秒)"
    except Exception as e:
        return f"错误: 命令执行失败 - {e}"


# NOTE:展示本轮修改文件的 git diff --stat 摘要，避免全仓库扫描
async def _show_colored_diff(workspace_dir: Path, files: list[str]):
    """展示有修改的文件的 diff 摘要。"""
    if not files:
        return
    stat = await _exec_shell_args(
        ["git", "diff", "--stat", "--"] + files, workspace_dir, timeout=10
    )
    if (
        stat
        and "退出码" not in stat
        and stat.strip()
        and stat.strip() != "(命令执行成功，无输出)"
    ):
        console.print(f"  [dim]变更摘要:\n{stat}[/dim]")


# NOTE:调用独立 API 请求为暂存区变更生成符合 Conventional Commits 规范的提交信息
async def _generate_commit_message(
    http_client: httpx.AsyncClient, config: dict, diff: str
) -> str:
    diff_stat = await _exec_shell("git diff --cached --stat", config["workspace_dir"])
    try:
        response = await http_client.post(
            f"{config['base_url']}/chat/completions",
            json={
                "model": config["model"],
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a git commit message generator. "
                            "Generate concise conventional commit messages "
                            "(e.g., feat:, fix:, chore:, refactor:, docs:, test:, style:). "
                            "Output ONLY the commit message, no explanation, no quotes."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Generate a commit message for this diff:\n\n"
                            f"## Changes\n{diff_stat}\n\n## Full diff\n{diff[:2000]}"
                        ),
                    },
                ],
                "temperature": 0.3,
                "max_tokens": 100,
            },
            headers={"Authorization": f"Bearer {config['api_key']}"},
        )
        response.raise_for_status()
        data = response.json()
        msg = data["choices"][0]["message"]["content"].strip().strip("\"'")
        return msg
    except Exception:
        return ""


# NOTE:Goal 检查点：将 goal.md/plan.md/todo.md 自动提交到 git，便于断点续作
async def _track_goal_files(workspace_dir: Path, iteration: int) -> str:
    """将 goal 持久化文件 (goal.md/plan.md/todo.md) 提交到 git 作为进度检查点。

    仅当目录是 git 仓库且这些文件有变更时才提交，不影响其他工作区文件。
    返回提交结果摘要；非 git 仓库或无变更时返回空串。
    """
    if not (workspace_dir / ".git").exists():
        return ""
    check = await _exec_shell("git rev-parse --is-inside-work-tree", workspace_dir, 10)
    if "true" not in check:
        return ""
    files = [
        f for f in ("goal.md", "plan.md", "todo.md") if (workspace_dir / f).exists()
    ]
    if not files:
        return ""
    status = await _exec_shell_args(
        ["git", "status", "--short", "--"] + files, workspace_dir, 10
    )
    if (
        not status.strip()
        or "退出码" in status
        or "没有" in status
        or "无输出" in status
    ):
        return ""
    add = await _exec_shell_args(["git", "add", "--"] + files, workspace_dir, 10)
    if "退出码" in add:
        return ""
    msg = f"goal: 第 {iteration} 轮进度 (goal.md/plan.md/todo.md)"
    result = await _exec_shell_args(
        ["git", "commit", "-m", msg, "--"] + files, workspace_dir, 10
    )
    return result
