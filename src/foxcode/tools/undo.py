from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool, permission_validator


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
