from pydantic_ai import RunContext
from ..models import WorkspaceDeps


def register(agent):
    @agent.tool
    async def read_file(ctx: RunContext[WorkspaceDeps], filename: str) -> str:
        filepath = ctx.deps.workspace_dir / filename
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在"
        if not filepath.is_file():
            return f"错误: {filename} 不是一个文件"
        try:
            content = filepath.read_text(encoding="utf-8")
            return content
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"

    @agent.tool
    async def create_file(
        ctx: RunContext[WorkspaceDeps], filename: str, content: str
    ) -> str:
        filepath = ctx.deps.workspace_dir / filename
        if filepath.exists():
            return f"错误: 文件 {filename} 已存在"
        filepath.parent.mkdir(parents=True, exist_ok=True)
        try:
            filepath.write_text(content, encoding="utf-8")
            ctx.deps.undo_manager.record("create", filename)
            return f"已创建文件 {filename}"
        except Exception as e:
            return f"错误: 创建文件失败 - {e}"

    @agent.tool
    async def write_file(
        ctx: RunContext[WorkspaceDeps], filename: str, old_string: str, new_string: str
    ) -> str:
        filepath = ctx.deps.workspace_dir / filename
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在"
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"
        count = content.count(old_string)
        if count == 0:
            return f"错误: 在 {filename} 中未找到要替换的字符串，请确认 old_string 完全匹配"
        if count > 1:
            return f"错误: 在 {filename} 中找到 {count} 处匹配，请提供更多上下文以确保唯一匹配"
        ctx.deps.undo_manager.record("write", filename, old_content=content)
        new_content = content.replace(old_string, new_string, 1)
        filepath.write_text(new_content, encoding="utf-8")
        return f"已更新 {filename}"

    @agent.tool
    async def write_file_complete(
        ctx: RunContext[WorkspaceDeps], filename: str, content: str
    ) -> str:
        filepath = ctx.deps.workspace_dir / filename
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在，请使用 create_file 创建新文件"
        try:
            old_content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"
        ctx.deps.undo_manager.record("write", filename, old_content=old_content)
        filepath.write_text(content, encoding="utf-8")
        return f"已覆盖写入 {filename}"

    @agent.tool
    async def append_file(
        ctx: RunContext[WorkspaceDeps], filename: str, content: str
    ) -> str:
        filepath = ctx.deps.workspace_dir / filename
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在"
        try:
            old_content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"
        ctx.deps.undo_manager.record("write", filename, old_content=old_content)
        with filepath.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"已追加内容到 {filename}"

    @agent.tool
    async def delete_file(ctx: RunContext[WorkspaceDeps], filename: str) -> str:
        filepath = ctx.deps.workspace_dir / filename
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在"
        if not filepath.is_file():
            return f"错误: {filename} 不是一个文件"
        try:
            old_content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"
        filepath.unlink()
        ctx.deps.undo_manager.record("delete", filename, old_content=old_content)
        return f"已删除文件 {filename}"

    @agent.tool
    async def rename_file(
        ctx: RunContext[WorkspaceDeps], old_filename: str, new_filename: str
    ) -> str:
        old_path = ctx.deps.workspace_dir / old_filename
        new_path = ctx.deps.workspace_dir / new_filename
        if not old_path.exists():
            return f"错误: 文件 {old_filename} 不存在"
        if new_path.exists():
            return f"错误: 目标文件 {new_filename} 已存在"
        old_path.rename(new_path)
        ctx.deps.undo_manager.record("rename", new_filename, old_content=old_filename)
        return f"已重命名 {old_filename} -> {new_filename}"

    @agent.tool
    async def list_files(ctx: RunContext[WorkspaceDeps], path: str = "") -> str:
        search_path = ctx.deps.workspace_dir / path if path else ctx.deps.workspace_dir
        if not search_path.exists():
            return f"错误: 路径 {path} 不存在"
        if not search_path.is_dir():
            return f"错误: {path} 不是一个目录"
        lines = []
        try:
            for entry in sorted(search_path.rglob("*")):
                rel_path = entry.relative_to(ctx.deps.workspace_dir)
                if entry.is_dir():
                    lines.append(f"{'/' + str(rel_path)}/")
                else:
                    size = entry.stat().st_size
                    lines.append(f"{'/' + str(rel_path)} ({size} bytes)")
        except Exception as e:
            return f"错误: 列出文件失败 - {e}"
        return "\n".join(lines) if lines else f"路径 {path} 为空"
