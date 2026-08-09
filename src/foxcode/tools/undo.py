from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool, permission_validator


# NOTE:注册撤销工具：支持撤销最近 N 步操作与查看操作历史
# NOTE:注册撤销与历史查看工具，委托给 UndoManager 处理回滚
def register(agent):
    @agent.tool(args_validator=permission_validator("undo_last"))
    async def undo_last(ctx: RunContext[WorkspaceDeps], steps: int = 1) -> str:
        log_tool(ctx, "undo_last", str(steps))
        if steps < 1:
            steps = 1
        return ctx.deps.undo_manager.undo(ctx.deps.workspace_dir, steps)

    @agent.tool(args_validator=permission_validator("show_history"))
    async def show_history(ctx: RunContext[WorkspaceDeps]) -> str:
        log_tool(ctx, "show_history")
        return ctx.deps.undo_manager.history_summary()
