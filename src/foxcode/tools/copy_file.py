from pathlib import Path
from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool
from .file_ops import _resolve_safe_path


def register(agent):
    @agent.tool
    async def copy_file(
        ctx: RunContext[WorkspaceDeps],
        source: str,
        destination: str,
    ) -> str:
        log_tool(ctx, "copy_file", f"{source} -> {destination}")
        try:
            src = _resolve_safe_path(ctx.deps.workspace_dir, source)
            dst = _resolve_safe_path(ctx.deps.workspace_dir, destination)
        except ValueError as e:
            return f"错误: {e}"
        if not src.exists():
            return f"错误: 源文件 {source} 不存在"
        if dst.exists():
            return f"错误: 目标文件 {destination} 已存在"
        try:
            import shutil
            if src.is_dir():
                shutil.copytree(src, dst)
                return f"已复制目录 {source} -> {destination}"
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                return f"已复制文件 {source} -> {destination}"
        except Exception as e:
            return f"错误: 复制失败 - {e}"
