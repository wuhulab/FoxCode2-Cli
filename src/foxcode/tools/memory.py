"""记忆工具：AI 维护 .foxcode/Memory.md（只允许通过本工具修改）。"""

import os

from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator

# NOTE:AI 可写记忆文件路径（普通文件写工具被拦截，只能通过本工具修改）
MEMORY_FILENAME = ".foxcode/Memory.md"


# NOTE:返回规范化的相对路径字符串，用于撤销记录与权限判断
def _memory_path(workspace_dir) -> str:
    """返回规范化的相对路径（用于撤销记录与权限判断）。"""
    norm = MEMORY_FILENAME.replace("\\", "/")
    if os.sep == "\\":
        norm = norm.replace("/", "\\")
    return norm


# NOTE:注册记忆更新工具：AI 维护项目知识，普通文件写工具无法绕过
def register(agent):
    @agent.tool(args_validator=permission_validator("update_memory"))
    async def update_memory(ctx: RunContext[WorkspaceDeps], content: str) -> str:
        """写入 AI 维护的项目记忆（.foxcode/Memory.md）。

        记录重要的项目知识、踩坑点、关键决策，供后续会话复用。content 为
        新的完整文件内容（AI 先读取当前内容再合并修改）。Rules.md 是用户
        规则文件，AI 只读，不可通过本工具修改。
        """
        log_tool(ctx, "update_memory")
        workspace_dir = ctx.deps.workspace_dir
        memory_path = (workspace_dir / MEMORY_FILENAME).resolve()
        ws_key = os.path.normcase(str(workspace_dir.resolve()))
        path_key = os.path.normcase(str(memory_path))
        if not path_key.startswith(ws_key):
            return "错误: 记忆文件路径越权"
        rel = _memory_path(workspace_dir)
        try:
            old = (
                memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
            )
        except Exception as e:
            return f"错误: 读取记忆文件失败 - {e}"
        try:
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text(content, encoding="utf-8")
        except Exception as e:
            return f"错误: 写入记忆文件失败 - {e}"
        ctx.deps.undo_manager.record("overwrite", rel, old_content=old)
        ctx.deps.tool_tracker.add_chars(len(content))
        return f"已更新 {MEMORY_FILENAME} ({len(content)} 字符)"
