from pathlib import Path
from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool, permission_validator


# NOTE:递归构建 ASCII 目录树，支持深度限制、排除模式、隐藏文件开关
def _build_tree(
    directory: Path,
    workspace_dir: Path,
    prefix: str = "",
    max_depth: int = 5,
    current_depth: int = 0,
    exclude_patterns: tuple[str, ...] = (
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".ruff_cache",
        ".pytest_cache",
        ".mypy_cache",
        ".egg-info",
        ".foxcode",
    ),
    show_hidden: bool = False,
) -> list[str]:
    if current_depth >= max_depth:
        return [f"{prefix}... (已达最大深度 {max_depth})"]

    lines: list[str] = []
    try:
        entries = sorted(
            directory.iterdir(),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
    except PermissionError:
        return [f"{prefix}[权限不足]"]
    except OSError:
        return [f"{prefix}[读取失败]"]

    visible_entries = []
    for entry in entries:
        if entry.name in exclude_patterns:
            continue
        if not show_hidden and entry.name.startswith("."):
            continue
        visible_entries.append(entry)

    for i, entry in enumerate(visible_entries):
        is_last = i == len(visible_entries) - 1
        connector = "└── " if is_last else "├── "
        child_prefix = "    " if is_last else "│   "

        if entry.is_dir():
            lines.append(f"{prefix}{connector}{entry.name}/")
            lines.extend(
                _build_tree(
                    entry,
                    workspace_dir,
                    prefix + child_prefix,
                    max_depth,
                    current_depth + 1,
                    exclude_patterns,
                    show_hidden,
                )
            )
        else:
            size = ""
            try:
                b = entry.stat().st_size
                if b < 1024:
                    size = f" ({b}B)"
                elif b < 1024 * 1024:
                    size = f" ({b / 1024:.1f}KB)"
                else:
                    size = f" ({b / (1024 * 1024):.1f}MB)"
            except OSError:
                pass
            lines.append(f"{prefix}{connector}{entry.name}{size}")

    return lines


# NOTE:注册目录树展示工具：生成类 tree 命令的层级结构可视化
def register(agent):
    @agent.tool(args_validator=permission_validator("tree"))
    async def tree(
        ctx: RunContext[WorkspaceDeps],
        path: str = "",
        max_depth: int = 5,
        show_hidden: bool = False,
    ) -> str:
        log_tool(ctx, "tree", path or ".", f"depth={max_depth}")
        search_path = ctx.deps.workspace_dir
        if path:
            try:
                from .file_ops import _resolve_safe_path

                search_path = _resolve_safe_path(ctx.deps.workspace_dir, path)
            except ValueError as e:
                return f"错误: {e}"
        if not search_path.exists():
            return f"错误: 路径 {path} 不存在"
        if not search_path.is_dir():
            return f"错误: {path} 不是一个目录"

        rel = search_path.relative_to(ctx.deps.workspace_dir)
        header = f"{rel or '.'}/"
        lines = _build_tree(
            search_path,
            ctx.deps.workspace_dir,
            max_depth=max_depth,
            show_hidden=show_hidden,
        )
        if not lines:
            return f"{header}\n(空目录)"
        return f"{header}\n" + "\n".join(lines)
