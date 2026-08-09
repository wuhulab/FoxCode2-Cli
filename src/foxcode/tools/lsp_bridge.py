"""LSP 桥接工具：基于 jedi 提供 Python 代码的静态分析能力。

提供以下工具：
- go_to_definition: 跳转到符号定义
- find_references: 查找符号引用
- get_type_info: 获取表达式类型信息
- get_docstring: 获取符号文档字符串

对其他语言，未来可扩展为调用真正的 LSP 进程。
"""

from pathlib import Path

from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator


# NOTE:将相对路径解析为安全路径，并校验必须是 .py 文件
def _resolve_filepath(workspace_dir: Path, filename: str) -> Path | None:
    from .file_ops import _resolve_safe_path

    try:
        p = _resolve_safe_path(workspace_dir, filename)
        if p.is_file() and p.suffix == ".py":
            return p
    except ValueError:
        pass
    return None


# NOTE:将(行,列)转换为源码中的绝对字符偏移（供 jedi 定位使用）
def _rowcol_to_pos(source: str, row: int, col: int) -> int:
    """将行号/列号转换为源码中的字符位置（0-index）。"""
    lines = source.splitlines()
    pos = sum(len(line) + 1 for line in lines[: row - 1])  # +1 for newline
    pos += max(0, col - 1)
    return min(pos, len(source))


# NOTE:延迟检测 jedi 库是否安装，未安装时返回友好提示
def _jedi_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("jedi") is not None


# NOTE:格式化 jedi 返回的定义列表为易读文本（含签名、类型、文档摘要）
def _format_definition(defs: list) -> str:
    """格式化 jedi 定义结果。"""
    if not defs:
        return "未找到定义"
    lines = []
    seen = set()
    for d in defs:
        file_info = f"{d.module_path}:{d.line}" if d.module_path else "builtin"
        sig = d.get_signatures()
        sig_str = ""
        if sig:
            params = ", ".join(p.name for p in sig[0].params)
            sig_str = f"({params})"
        name = d.full_name or d.name
        key = f"{name}:{file_info}"
        if key in seen:
            continue
        seen.add(key)
        doc = d.docstring(raw=True) or ""
        if doc:
            doc = doc.split("\n")[0][:120]
        type_str = f" [{d.type}]" if d.type else ""
        lines.append(f"  {name}{sig_str}{type_str}  → {file_info}")
        if doc:
            lines.append(f"      # {doc}")
    return "\n".join(lines)


# NOTE:格式化 jedi 引用列表为项目相对路径的列表文本（最多 30 条）
def _format_references(refs: list) -> str:
    if not refs:
        return "未找到引用"
    lines = []
    for r in refs:
        if r.module_path:
            try:
                rel = Path(r.module_path).relative_to(Path(".").resolve())
            except ValueError:
                rel = Path(r.module_path)
            file_info = f"{rel}:{r.line}:{r.column}"
        else:
            file_info = "builtin"
        lines.append(f"  {file_info}")
    return f"找到 {len(refs)} 处引用:\n" + "\n".join(lines[:30])


# NOTE:注册基于 jedi 的 LSP 桥接工具：跳转定义、查找引用、类型推断、文档查询
def register(agent):
    @agent.tool(args_validator=permission_validator("go_to_definition"))
    async def go_to_definition(
        ctx: RunContext[WorkspaceDeps],
        filename: str,
        row: int,
        col: int = 0,
    ) -> str:
        """跳转到光标所在位置的符号定义。

        参数:
            filename: Python 文件路径（相对于工作区）
            row: 行号（1-indexed）
            col: 列号（1-indexed，可选）
        """
        log_tool(ctx, "go_to_definition", f"{filename}:{row}:{col}")

        if not _jedi_available():
            return "错误: jedi 未安装。请先运行 pip install jedi 以启用代码分析功能。"

        import jedi

        filepath = _resolve_filepath(ctx.deps.workspace_dir, filename)
        if filepath is None:
            return f"错误: {filename} 不是有效的 Python 文件"

        try:
            source = filepath.read_text(encoding="utf-8").replace("\r\n", "\n")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"

        pos = _rowcol_to_pos(source, row, col or 1)
        script = jedi.Script(source, path=str(filepath))
        try:
            defs = script.goto(pos, follow_builtin_imports=True)
        except Exception as e:
            return f"分析失败: {e}"

        return f"{filename}:{row}:{col} 的定义:\n" + _format_definition(defs)

    @agent.tool(args_validator=permission_validator("find_references"))
    async def find_references(
        ctx: RunContext[WorkspaceDeps],
        filename: str,
        row: int,
        col: int = 0,
    ) -> str:
        """查找光标所在位置的符号在项目中的所有引用。

        参数:
            filename: Python 文件路径（相对于工作区）
            row: 行号（1-indexed）
            col: 列号（1-indexed，可选）
        """
        log_tool(ctx, "find_references", f"{filename}:{row}:{col}")

        if not _jedi_available():
            return "错误: jedi 未安装。请先运行 pip install jedi 以启用代码分析功能。"

        import jedi

        filepath = _resolve_filepath(ctx.deps.workspace_dir, filename)
        if filepath is None:
            return f"错误: {filename} 不是有效的 Python 文件"

        try:
            source = filepath.read_text(encoding="utf-8").replace("\r\n", "\n")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"

        pos = _rowcol_to_pos(source, row, col or 1)
        script = jedi.Script(source, path=str(filepath))
        try:
            refs = script.get_references(pos)
        except Exception as e:
            return f"分析失败: {e}"

        return _format_references(refs)

    @agent.tool(args_validator=permission_validator("get_type_info"))
    async def get_type_info(
        ctx: RunContext[WorkspaceDeps],
        filename: str,
        row: int,
        col: int = 0,
    ) -> str:
        """获取光标所在位置表达式的类型信息。

        参数:
            filename: Python 文件路径（相对于工作区）
            row: 行号（1-indexed）
            col: 列号（1-indexed，可选）
        """
        log_tool(ctx, "get_type_info", f"{filename}:{row}:{col}")

        if not _jedi_available():
            return "错误: jedi 未安装。请先运行 pip install jedi 以启用代码分析功能。"

        import jedi

        filepath = _resolve_filepath(ctx.deps.workspace_dir, filename)
        if filepath is None:
            return f"错误: {filename} 不是有效的 Python 文件"

        try:
            source = filepath.read_text(encoding="utf-8").replace("\r\n", "\n")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"

        pos = _rowcol_to_pos(source, row, col or 1)
        script = jedi.Script(source, path=str(filepath))
        sigs = script.get_signatures(pos)
        infers = script.infer(pos)

        lines = []

        if sigs:
            lines.append("**函数签名:**")
            for s in sigs:
                params = ", ".join(p.name for p in s.params)
                ret = s.return_type or ""
                lines.append(f"  {s.name}({params}) -> {ret}")

        if infers:
            lines.append("**推断类型:**")
            for i in infers:
                type_str = i.type or "?"
                full = i.full_name or i.name or "?"
                desc = i.description or ""
                lines.append(f"  {full} [{type_str}]")
                if desc:
                    lines.append(f"      {desc[:200]}")

        if not lines:
            return "未获取到类型信息"
        return f"{filename}:{row}:{col} 的类型:\n" + "\n".join(lines)

    @agent.tool(args_validator=permission_validator("get_docstring"))
    async def get_docstring(
        ctx: RunContext[WorkspaceDeps],
        filename: str,
        row: int,
        col: int = 0,
    ) -> str:
        """获取光标所在位置符号的文档字符串。

        参数:
            filename: Python 文件路径（相对于工作区）
            row: 行号（1-indexed）
            col: 列号（1-indexed，可选）
        """
        log_tool(ctx, "get_docstring", f"{filename}:{row}:{col}")

        if not _jedi_available():
            return "错误: jedi 未安装。请先运行 pip install jedi 以启用代码分析功能。"

        import jedi

        filepath = _resolve_filepath(ctx.deps.workspace_dir, filename)
        if filepath is None:
            return f"错误: {filename} 不是有效的 Python 文件"

        try:
            source = filepath.read_text(encoding="utf-8").replace("\r\n", "\n")
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"

        pos = _rowcol_to_pos(source, row, col or 1)
        script = jedi.Script(source, path=str(filepath))
        defs = script.goto(pos, follow_builtin_imports=True)
        if not defs:
            return "未找到该符号的文档"

        d = defs[0]
        doc = d.docstring(raw=False) or d.docstring(raw=True) or ""
        name = d.full_name or d.name
        if not doc:
            return f"{name}: 没有文档字符串"
        return f"**{name}**\n```\n{doc}\n```"
