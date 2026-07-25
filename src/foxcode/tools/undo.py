from pydantic_ai import RunContext
from ..models import WorkspaceDeps


def register(agent):
    @agent.tool
    async def undo_last(ctx: RunContext[WorkspaceDeps], steps: int = 1) -> str:
        if steps < 1:
            steps = 1
        return ctx.deps.undo_manager.undo(ctx.deps.workspace_dir, steps)

    @agent.tool
    async def show_history(ctx: RunContext[WorkspaceDeps]) -> str:
        return ctx.deps.undo_manager.history_summary()
