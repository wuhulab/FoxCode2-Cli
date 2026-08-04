from pydantic_ai.exceptions import ToolFailed

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
        err = check_permission(ctx, tool_name, args, kwargs)
        if err:
            raise ToolFailed(err)

    return _validate
