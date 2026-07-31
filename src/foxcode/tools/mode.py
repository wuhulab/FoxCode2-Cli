from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator


def register(agent):
    @agent.tool(args_validator=permission_validator("enter_plan_mode"))
    async def enter_plan_mode(ctx: RunContext[WorkspaceDeps]) -> str:
        log_tool(ctx, "enter_plan_mode")
        ctx.deps.plan_mode = True
        if getattr(ctx.deps, "permissions", None) is not None:
            ctx.deps.permissions.plan_mode = True
        return (
            "已进入计划模式: 现在只能读取/搜索/探索，不能修改文件或执行命令。"
            "请先充分调查，然后用 ActionPlan 给出清晰的分步实施计划。"
        )

    @agent.tool(args_validator=permission_validator("exit_plan_mode"))
    async def exit_plan_mode(ctx: RunContext[WorkspaceDeps]) -> str:
        log_tool(ctx, "exit_plan_mode")
        ctx.deps.plan_mode = False
        if getattr(ctx.deps, "permissions", None) is not None:
            ctx.deps.permissions.plan_mode = False
        return "已退出计划模式: 可以正常修改文件、执行命令了。"
