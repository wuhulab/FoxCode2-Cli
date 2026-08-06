from pathlib import Path
from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool, permission_validator, run_subprocess


async def _run_git(cwd: Path, *args: str, timeout: int = 30) -> str:
    try:
        result = await run_subprocess(
            ["git", *args],
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
        if result.returncode != 0 and not output:
            return f"git 退出码: {result.returncode}"
        return output if output else "(命令执行成功，无输出)"
    except TimeoutError:
        return f"错误: git 命令执行超时 ({timeout}秒)"
    except FileNotFoundError:
        return "错误: 未找到 git，请确保已安装 Git"
    except Exception as e:
        return f"错误: git 命令执行失败 - {e}"


def register(agent):
    @agent.tool(args_validator=permission_validator("git_status"))
    async def git_status(ctx: RunContext[WorkspaceDeps]) -> str:
        log_tool(ctx, "git_status")
        return await _run_git(ctx.deps.workspace_dir, "status", "--short", "--branch")

    @agent.tool(args_validator=permission_validator("git_diff"))
    async def git_diff(
        ctx: RunContext[WorkspaceDeps], filename: str = "", staged: bool = False
    ) -> str:
        target = filename or "所有变更文件"
        log_tool(ctx, "git_diff", target)
        args = ["diff"]
        if staged:
            args.append("--staged")
        if filename:
            args.append(filename)
        return await _run_git(ctx.deps.workspace_dir, *args)

    @agent.tool(args_validator=permission_validator("git_log"))
    async def git_log(
        ctx: RunContext[WorkspaceDeps], n: int = 10, filename: str = ""
    ) -> str:
        target = filename or "全部"
        log_tool(ctx, "git_log", f"{n}条", target)
        args = ["log", f"--max-count={n}", "--oneline", "--decorate"]
        if filename:
            args.append(filename)
        return await _run_git(ctx.deps.workspace_dir, *args)

    @agent.tool(args_validator=permission_validator("git_add"))
    async def git_add(ctx: RunContext[WorkspaceDeps], filename: str = "") -> str:
        target = filename or "."
        log_tool(ctx, "git_add", target)
        args = ["add"]
        if filename:
            args.append(filename)
        else:
            args.append(".")
        return await _run_git(ctx.deps.workspace_dir, *args)

    @agent.tool(args_validator=permission_validator("git_commit"))
    async def git_commit(ctx: RunContext[WorkspaceDeps], message: str = "") -> str:
        log_tool(
            ctx, "git_commit", message[:40] + "..." if len(message) > 40 else message
        )
        if not message:
            return "错误: 提交信息不能为空"
        args = ["commit", "-m", message]
        return await _run_git(ctx.deps.workspace_dir, *args)

    @agent.tool(args_validator=permission_validator("git_branch"))
    async def git_branch(ctx: RunContext[WorkspaceDeps]) -> str:
        log_tool(ctx, "git_branch")
        return await _run_git(ctx.deps.workspace_dir, "branch", "-a")

    @agent.tool(args_validator=permission_validator("git_checkout"))
    async def git_checkout(
        ctx: RunContext[WorkspaceDeps], branch: str = "", create: bool = False
    ) -> str:
        if not branch:
            return "错误: 分支名不能为空"
        log_tool(ctx, "git_checkout", branch)
        args = ["checkout"]
        if create:
            args.append("-b")
        args.append(branch)
        return await _run_git(ctx.deps.workspace_dir, *args)
