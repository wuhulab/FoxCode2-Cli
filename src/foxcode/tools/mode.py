from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator


# NOTE:注册计划模式切换工具：限制 AI 只能使用只读工具探索并出方案
def register(agent):
    @agent.tool(args_validator=permission_validator("enter_plan_mode"))
    async def enter_plan_mode(ctx: RunContext[WorkspaceDeps]) -> str:
        log_tool(ctx, "enter_plan_mode")
        ctx.deps.plan_mode = True
        if getattr(ctx.deps, "permissions", None) is not None:
            ctx.deps.permissions.plan_mode = True
        return (
            "Entered plan mode: you can only read/search/explore now, not modify files or run commands. "
            "Investigate thoroughly, then give a clear step-by-step implementation plan in the ActionPlan. "
            "Do not overthink; gather what you need and present the plan."
        )

    @agent.tool(args_validator=permission_validator("exit_plan_mode"))
    async def exit_plan_mode(ctx: RunContext[WorkspaceDeps]) -> str:
        log_tool(ctx, "exit_plan_mode")
        ctx.deps.plan_mode = False
        if getattr(ctx.deps, "permissions", None) is not None:
            ctx.deps.permissions.plan_mode = False
        return "Exited plan mode: you can modify files and run commands again."
