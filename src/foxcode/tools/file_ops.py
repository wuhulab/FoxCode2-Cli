import os
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field
from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator
from .security import check_content_security, format_security_warnings

# NOTE:缓存已解析的工作区路径（同一 workspace 的多次文件操作无需重复 resolve）
_workspace_norm_cache: dict[str, str] = {}

# NOTE:受保护文件清单：AI 不可通过普通文件工具直接修改（Rules.md 只读、Memory.md 需专用工具）
PROTECTED_WRITE = {
    ".foxcode/rules.md",
    ".foxcode/memory.md",
}


# NOTE:检查文件路径是否落入受保护清单，返回拦截原因；None 表示允许写入
def check_protected_write(filename: str) -> str | None:
    """检查 filename 是否落入受保护文件，返回拒绝原因（None 表示可写）。

    - .foxcode/Rules.md: 用户规则，AI 只读，禁止任何修改
    - .foxcode/Memory.md: AI 记忆，只能通过 update_memory 工具修改
    """
    norm = filename.replace("\\", "/").strip().lower()
    while norm.startswith("./"):
        norm = norm[2:]
    if norm == ".foxcode/rules.md":
        return (
            ".foxcode/Rules.md 是用户规则文件，AI 只读，禁止修改；"
            "如需调整规则请告知用户"
        )
    if norm == ".foxcode/memory.md":
        return ".foxcode/Memory.md 只能通过 update_memory 工具修改"
    return None


# NOTE:路径安全解析：将用户传入的相对路径限制在工作区内，防止目录遍历攻击
def _resolve_safe_path(workspace_dir: Path, filename: str) -> Path:
    resolved = (workspace_dir / filename).resolve()
    ws_key = str(workspace_dir)
    workspace_norm = _workspace_norm_cache.get(ws_key)
    if workspace_norm is None:
        workspace_norm = os.path.normcase(str(workspace_dir.resolve()))
        _workspace_norm_cache[ws_key] = workspace_norm
    resolved_norm = os.path.normcase(str(resolved))
    if resolved_norm == workspace_norm:
        return resolved
    if not resolved_norm.startswith(workspace_norm):
        raise ValueError(f"路径越权: {filename} 不在工作区内")
    if not workspace_norm.endswith(os.sep):
        if resolved_norm[len(workspace_norm)] != os.sep:
            raise ValueError(f"路径越权: {filename} 不在工作区内")
    return resolved


# NOTE:读取文件指定行范围，返回 (起始行, 结束行, 总行数, 选中内容)；end_line=0 表示读到末尾
def _read_file_range(
    filepath: Path, start_line: int, end_line: int
) -> tuple[int, int, int, str]:
    """读取文件的指定行范围，返回 (起始行, 结束行, 总行数, 选中内容)。

    end_line 为 0 或超出总行数时读取到文件末尾。
    start_line 超出总行数时抛 ValueError。
    """
    total = 0
    selected: list[str] = []
    with filepath.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, 1):
            total = idx
            if idx >= start_line and (end_line == 0 or idx <= end_line):
                selected.append(line)
    if start_line < 1:
        start_line = 1
    if end_line == 0 or end_line > total:
        end_line = total
    if start_line > total:
        raise ValueError(f"起始行 {start_line} 超出文件总行数 {total}")
    return start_line, end_line, total, "".join(selected)


# NOTE:安全扫描：检测写入内容中是否包含密钥泄露等敏感信息，若发现则打印警告
def _warn_security(ctx: RunContext[WorkspaceDeps], content: str):
    findings = check_content_security(content)
    warning = format_security_warnings(findings)
    if warning:
        ctx.deps.console.print(warning)


# NOTE:注册全部文件操作工具：读、写范围读、创建、编辑、覆盖、追加、删除、重命名、列出
def register(agent):
    # NOTE:读取完整文件内容，自动累加字符量供 token 估算
    @agent.tool(args_validator=permission_validator("read_file"))
    async def read_file(ctx: RunContext[WorkspaceDeps], filename: str) -> str:
        log_tool(ctx, "read_file", filename)
        try:
            filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
        except ValueError as e:
            return f"错误: {e}"
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在"
        if not filepath.is_file():
            return f"错误: {filename} 不是一个文件"
        try:
            content = filepath.read_text(encoding="utf-8")
            ctx.deps.tool_tracker.add_chars(len(content))
            return content
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"

    # NOTE:读取文件指定行范围，支持多别名入参（兼容大小写与驼峰）
    @agent.tool(args_validator=permission_validator("read_file_range"))
    async def read_file_range(
        ctx: RunContext[WorkspaceDeps],
        filename: str,
        start_line: Annotated[
            int,
            Field(validation_alias=AliasChoices("start_line", "startLine", "start")),
        ] = 1,
        end_line: Annotated[
            int,
            Field(validation_alias=AliasChoices("end_line", "endLine", "end")),
        ] = 0,
    ) -> str:
        log_tool(ctx, "read_file_range", filename, f"{start_line}-{end_line or 'end'}")
        try:
            filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
        except ValueError as e:
            return f"错误: {e}"
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在"
        if not filepath.is_file():
            return f"错误: {filename} 不是一个文件"
        try:
            start_line, end_line, total, output = _read_file_range(
                filepath, start_line, end_line
            )
        except ValueError as e:
            return f"错误: {e}"
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"
        ctx.deps.tool_tracker.add_chars(len(output))
        header = f"[文件 {filename} 第 {start_line}-{end_line} 行 / 共 {total} 行]\n"
        return header + output

    # NOTE:创建新文件（若已存在则报错），写入前先做安全扫描与受保护文件检查
    @agent.tool(args_validator=permission_validator("create_file"))
    async def create_file(
        ctx: RunContext[WorkspaceDeps], filename: str, content: str
    ) -> str:
        log_tool(ctx, "create_file", filename)
        _warn_security(ctx, content)
        protected = check_protected_write(filename)
        if protected:
            return f"错误: {protected}"
        try:
            filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
        except ValueError as e:
            return f"错误: {e}"
        if filepath.exists():
            return f"错误: 文件 {filename} 已存在"
        filepath.parent.mkdir(parents=True, exist_ok=True)
        try:
            filepath.write_text(content, encoding="utf-8")
            ctx.deps.tool_tracker.add_chars(len(content))
            ctx.deps.undo_manager.record("create", filename)
            return f"已创建文件 {filename}"
        except Exception as e:
            return f"错误: 创建文件失败 - {e}"

    # NOTE:基于字符串替换编辑文件（要求唯一匹配），支持多别名入参兼容模型输出差异
    @agent.tool(args_validator=permission_validator("write_file"))
    async def write_file(
        ctx: RunContext[WorkspaceDeps],
        filename: str,
        old_string: Annotated[
            str,
            Field(
                validation_alias=AliasChoices("old_string", "oldString", "oldcontent")
            ),
        ],
        new_string: Annotated[
            str,
            Field(
                validation_alias=AliasChoices("new_string", "newString", "newcontent")
            ),
        ],
    ) -> str:
        log_tool(ctx, "write_file", filename)
        _warn_security(ctx, new_string)
        protected = check_protected_write(filename)
        if protected:
            return f"错误: {protected}"
        try:
            filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
        except ValueError as e:
            return f"错误: {e}"
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
        new_content = content.replace(old_string, new_string, 1)
        try:
            filepath.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return f"错误: 写入文件失败 - {e}"
        ctx.deps.undo_manager.record("write", filename, old_content=content)
        ctx.deps.tool_tracker.add_chars(len(new_content))
        return f"已更新 {filename}"

    # NOTE:完整覆盖写入已有文件（非创建），适合 AI 重写整个文件内容
    @agent.tool(args_validator=permission_validator("write_file_complete"))
    async def write_file_complete(
        ctx: RunContext[WorkspaceDeps], filename: str, content: str
    ) -> str:
        log_tool(ctx, "write_file_complete", filename)
        _warn_security(ctx, content)
        protected = check_protected_write(filename)
        if protected:
            return f"错误: {protected}"
        try:
            filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
        except ValueError as e:
            return f"错误: {e}"
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在，请使用 create_file 创建新文件"
        try:
            old_content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"
        try:
            filepath.write_text(content, encoding="utf-8")
        except Exception as e:
            return f"错误: 写入文件失败 - {e}"
        ctx.deps.undo_manager.record("overwrite", filename, old_content=old_content)
        ctx.deps.tool_tracker.add_chars(len(content))
        return f"已覆盖写入 {filename}"

    # NOTE:在已有文件末尾追加内容，保留旧内容用于撤销
    @agent.tool(args_validator=permission_validator("append_file"))
    async def append_file(
        ctx: RunContext[WorkspaceDeps], filename: str, content: str
    ) -> str:
        log_tool(ctx, "append_file", filename)
        _warn_security(ctx, content)
        protected = check_protected_write(filename)
        if protected:
            return f"错误: {protected}"
        try:
            filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
        except ValueError as e:
            return f"错误: {e}"
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在"
        try:
            old_content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"
        try:
            with filepath.open("a", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return f"错误: 追加文件失败 - {e}"
        ctx.deps.undo_manager.record("append", filename, old_content=old_content)
        ctx.deps.tool_tracker.add_chars(len(content))
        return f"已追加内容到 {filename}"

    # NOTE:删除文件（先读取内容以备撤销），受保护文件禁止删除
    @agent.tool(args_validator=permission_validator("delete_file"))
    async def delete_file(ctx: RunContext[WorkspaceDeps], filename: str) -> str:
        log_tool(ctx, "delete_file", filename)
        protected = check_protected_write(filename)
        if protected:
            return f"错误: {protected}"
        try:
            filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
        except ValueError as e:
            return f"错误: {e}"
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在"
        if not filepath.is_file():
            return f"错误: {filename} 不是一个文件"
        try:
            old_content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"
        try:
            filepath.unlink()
        except Exception as e:
            return f"错误: 删除文件失败 - {e}"
        ctx.deps.undo_manager.record("delete", filename, old_content=old_content)
        return f"已删除文件 {filename}"

    # NOTE:重命名文件（需检查双方路径均不越权且不受保护），记录旧名用于撤销
    @agent.tool(args_validator=permission_validator("rename_file"))
    async def rename_file(
        ctx: RunContext[WorkspaceDeps],
        old_filename: Annotated[
            str,
            Field(
                validation_alias=AliasChoices("old_filename", "oldFilename", "old_path")
            ),
        ],
        new_filename: Annotated[
            str,
            Field(
                validation_alias=AliasChoices("new_filename", "newFilename", "new_path")
            ),
        ],
    ) -> str:
        log_tool(ctx, "rename_file", f"{old_filename} -> {new_filename}")
        protected = check_protected_write(old_filename)
        if protected:
            return f"错误: {protected}"
        protected = check_protected_write(new_filename)
        if protected:
            return f"错误: {protected}"
        try:
            old_path = _resolve_safe_path(ctx.deps.workspace_dir, old_filename)
            new_path = _resolve_safe_path(ctx.deps.workspace_dir, new_filename)
        except ValueError as e:
            return f"错误: {e}"
        if not old_path.exists():
            return f"错误: 文件 {old_filename} 不存在"
        if new_path.exists():
            return f"错误: 目标文件 {new_filename} 已存在"
        try:
            old_path.rename(new_path)
        except Exception as e:
            return f"错误: 重命名文件失败 - {e}"
        ctx.deps.undo_manager.record("rename", new_filename, old_content=old_filename)
        return f"已重命名 {old_filename} -> {new_filename}"

    # NOTE:列出目录内容（剪枝跳过重型目录），条目上限 500 防止输出过大
    @agent.tool(args_validator=permission_validator("list_files"))
    async def list_files(ctx: RunContext[WorkspaceDeps], path: str = "") -> str:
        log_tool(ctx, "list_files", path or ".")
        try:
            search_path = (
                _resolve_safe_path(ctx.deps.workspace_dir, path)
                if path
                else ctx.deps.workspace_dir
            )
        except ValueError as e:
            return f"错误: {e}"
        if not search_path.exists():
            return f"错误: 路径 {path} 不存在"
        if not search_path.is_dir():
            return f"错误: {path} 不是一个目录"
        lines = []
        try:
            from . import iter_project_entries

            for entry in iter_project_entries(search_path):
                rel_path = entry.relative_to(ctx.deps.workspace_dir)
                if entry.is_dir():
                    lines.append(f"{rel_path}/")
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{rel_path} ({size} bytes)")
                if len(lines) >= 500:
                    lines.append("... (条目过多，仅展示前 500 项)")
                    break
        except Exception as e:
            return f"错误: 列出文件失败 - {e}"
        return "\n".join(lines) if lines else f"路径 {path} 为空"
