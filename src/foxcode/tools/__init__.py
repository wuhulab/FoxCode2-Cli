from datetime import datetime

def log_tool(ctx, tool_name: str, *details: str):
    ctx.deps.tool_tracker.count(tool_name)
    stamp = datetime.now().strftime("[%H:%M:%S]")
    msg = f"  {stamp} {tool_name} {' '.join(details)}"
    ctx.deps.console.print(msg)
