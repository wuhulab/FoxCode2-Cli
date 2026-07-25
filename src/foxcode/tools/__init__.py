ICONS = {
    "read_file": "📖",
    "create_file": "📄",
    "write_file": "✏️",
    "write_file_complete": "📝",
    "append_file": "➕",
    "delete_file": "🗑️",
    "rename_file": "🔀",
    "list_files": "📂",
    "web_search": "🔍",
    "run_shell": "⚡",
    "run_file": "▶️",
}


def log_tool(ctx, tool_name: str, *details: str):
    ctx.deps.tool_tracker.count(tool_name)
    icon = ICONS.get(tool_name, "🔧")
    msg = f"  {icon} {tool_name} {' '.join(details)}"
    ctx.deps.console.log(msg)
