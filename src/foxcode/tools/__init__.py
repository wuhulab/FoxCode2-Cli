import asyncio
import subprocess

from ..permissions import check_permission


def log_tool(ctx, tool_name: str, *details: str):
    ctx.deps.tool_tracker.count(tool_name)
    msg = f"-> {tool_name} {' '.join(details)}"
    ctx.deps.console.print(msg)


def permission_validator(tool_name: str):
    """为工具注册 args_validator，在执行前进行权限门控。

    权限拒绝时抛出 ToolFailed，模型会看到失败信息并调整行为。
    """

    async def _validate(ctx, *args, **kwargs):
        from pydantic_ai.exceptions import ToolFailed

        err = check_permission(ctx, tool_name, args, kwargs)
        if err:
            raise ToolFailed(err)

    return _validate


async def run_subprocess(
    cmd,
    *,
    timeout: int = 30,
    cwd: str | None = None,
    shell: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """在线程池中运行子进程，避免阻塞事件循环。

    subprocess.run 会释放 GIL 等待子进程，放入 asyncio.to_thread 后
    可保持事件循环响应（spinner、HTTP 请求、超时定时器等照常运行）。
    """
    return await asyncio.to_thread(
        subprocess.run,
        cmd,
        shell=shell,
        capture_output=True,
        text=text,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
    )


_CORE_TOOLS = (
    "file_ops",
    "shell",
    "search",
    "undo",
    "git",
    "grep",
    "fetch",
    "tree",
    "copy_file",
    "tests",
    "format",
    "deps",
)


def register_core_tools(agent):
    """注册核心读写工具到指定 agent（主代理与子代理共享，避免列表漂移）。"""
    import importlib

    for name in _CORE_TOOLS:
        mod = importlib.import_module(f".{name}", package=__name__)
        mod.register(agent)


def register_all_tools(agent):
    """注册全部工具（核心 + 增强），用于主代理。"""
    from . import code_index, health, lsp_bridge, mode, multi_edit, preview, review

    register_core_tools(agent)
    for mod in (mode, code_index, preview, review, health, lsp_bridge, multi_edit):
        mod.register(agent)
