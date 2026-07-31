from datetime import datetime

from ..models import ICONS


def log_tool(ctx, tool_name: str, *details: str):
    ctx.deps.tool_tracker.count(tool_name)
    icon = ICONS.get(tool_name, "")
    stamp = datetime.now().strftime("[%H:%M:%S]")
    msg = f"  {stamp}  {icon} {tool_name} {' '.join(details)}"
    ctx.deps.console.print(msg)
