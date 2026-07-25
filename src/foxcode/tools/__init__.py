from ..models import ICONS


def log_tool(ctx, tool_name: str, *details: str):
    ctx.deps.tool_tracker.count(tool_name)
    icon = ICONS.get(tool_name, "⚙️")
    msg = f"  {icon} {tool_name} {' '.join(details)}"
    ctx.deps.console.log(msg)
