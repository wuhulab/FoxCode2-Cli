"""代码库索引工具：符号索引、项目结构理解、语义搜索。

支持两种索引方式：
1. universal-ctags（首选，支持多语言）
2. Python AST 回退（原生支持，仅 Python）
"""

import ast
import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator

# NOTE:支持索引的源代码文件后缀集合（覆盖常见前后端语言）
SOURCE_EXTS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".rb",
        ".php",
    }
)


# NOTE:代码符号数据类：记录名称、类型、位置、签名与文档等信息
@dataclass
class Symbol:
    name: str
    kind: str  # class, function, method, variable, import, module
    file: str
    line: int
    end_line: int = 0
    signature: str = ""
    parent: str = ""
    docstring: str = ""


# NOTE:代码库索引核心类：维护符号列表，通过文件 mtime 判断索引是否过期
@dataclass
class CodeIndex:
    workspace_dir: Path
    symbols: list[Symbol] = field(default_factory=list)
    _file_mtime: dict[str, float] = field(default_factory=dict)
    _last_check: float = 0.0
    _source_files: list[Path] | None = field(default=None, repr=False)
    _source_files_ts: float = field(default=0.0, repr=False)
    SOURCE_LIST_TTL: float = field(default=30.0, repr=False)

    # NOTE:基于文件修改时间判断索引是否过时（带 5 秒节流防止高频扫描）
    def is_stale(self) -> bool:
        """检查索引是否需要更新（基于文件修改时间），带节流防止高频重复扫描。"""
        now = time.monotonic()
        if now - self._last_check < 5.0:
            return False
        self._last_check = now
        known = set(self._file_mtime)
        seen = set()
        for f in self._iter_source_files():
            seen.add(str(f))
            mtime = f.stat().st_mtime
            if str(f) not in self._file_mtime or self._file_mtime[str(f)] != mtime:
                return True
        return known != seen

    def _iter_source_files(self):
        """遍历源代码文件（剪枝跳过重型/隐藏目录 + 后缀过滤），带 30 秒列表缓存。"""
        now = time.monotonic()
        if (
            self._source_files is not None
            and now - self._source_files_ts < self.SOURCE_LIST_TTL
        ):
            yield from self._source_files
            return
        from . import iter_project_files

        files: list[Path] = []
        for f in iter_project_files(self.workspace_dir):
            if not f.is_file():
                continue
            if f.suffix not in SOURCE_EXTS:
                continue
            if f.name.startswith("."):
                continue
            files.append(f)
            yield f
        self._source_files = files
        self._source_files_ts = now

    # NOTE:构建代码库索引：先尝试 ctags（多语言），失败则回退到 Python AST
    async def build(self) -> str:
        """构建索引，返回状态信息。"""
        self.symbols.clear()
        self._file_mtime.clear()
        # 强制刷新源文件列表缓存，确保新创建的文件被纳入
        self._source_files = None
        self._source_files_ts = 0.0

        # 尝试 ctags
        ctags_result = await self._try_ctags()
        if ctags_result:
            return ctags_result

        # 回退到 AST
        return await asyncio.to_thread(self._build_ast_index)

    # NOTE:尝试使用 universal-ctags 生成 JSON 格式索引，覆盖多语言符号
    async def _try_ctags(self) -> Optional[str]:
        """尝试使用 universal-ctags 生成索引。"""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["ctags", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0 or "Universal Ctags" not in result.stdout:
                return None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        # 生成临时 tags 文件
        tags_file = self.workspace_dir / ".foxcode" / "tags.json"
        tags_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            # 使用 ctags 的 json 输出
            cmd = [
                "ctags",
                "--output-format=json",
                "--fields=+n+S+s+d",
                "--languages=Python,JavaScript,TypeScript,Go,Rust,Java,C,C++",
                "-R",
                "-o",
                "-",
                str(self.workspace_dir),
            ]
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if result.returncode not in (0, 1):  # ctags 有时返回 1 但仍然有输出
                return None

            lines = result.stdout.strip().splitlines()
            count = 0
            for line in lines:
                if not line.strip():
                    continue
                try:
                    tag = json.loads(line)
                except json.JSONDecodeError:
                    continue

                kind = tag.get("kind", "unknown").lower()
                kind_map = {
                    "class": "class",
                    "function": "function",
                    "method": "method",
                    "variable": "variable",
                    "member": "variable",
                    "module": "module",
                    "package": "module",
                    "imports": "import",
                }

                file_path = tag.get("path", "")
                try:
                    rel = str(Path(file_path).relative_to(self.workspace_dir))
                except ValueError:
                    rel = file_path

                symbol = Symbol(
                    name=tag.get("name", ""),
                    kind=kind_map.get(kind, kind),
                    file=rel,
                    line=int(tag.get("line", 0)),
                    signature=tag.get("signature", ""),
                    parent=tag.get("scope", ""),
                )
                self.symbols.append(symbol)
                count += 1

            # 记录 mtime
            for f in self._iter_source_files():
                self._file_mtime[str(f)] = f.stat().st_mtime

            return f"代码库索引完成 (ctags): 共 {count} 个符号"
        except Exception:
            return None

    # NOTE:ctags 不可用时回退：使用 Python AST 遍历提取类、方法与函数符号
    def _build_ast_index(self) -> str:
        """使用 Python AST 构建索引（仅 Python 项目）。"""
        count = 0
        for f in self._iter_source_files():
            if f.suffix != ".py":
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
            except Exception:
                continue

            rel = str(f.relative_to(self.workspace_dir))
            self._file_mtime[str(f)] = f.stat().st_mtime

            # 先收集类方法节点 id，避免后续重复添加为顶层函数
            class_method_ids: set[int] = set()

            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    doc = ast.get_docstring(node) or ""
                    self.symbols.append(
                        Symbol(
                            name=node.name,
                            kind="class",
                            file=rel,
                            line=node.lineno,
                            end_line=getattr(node, "end_lineno", node.lineno),
                            signature=self._class_signature(node),
                            parent="",
                            docstring=doc,
                        )
                    )
                    count += 1
                    # 类方法
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            method_doc = ast.get_docstring(item) or ""
                            self.symbols.append(
                                Symbol(
                                    name=item.name,
                                    kind="method",
                                    file=rel,
                                    line=item.lineno,
                                    end_line=getattr(item, "end_lineno", item.lineno),
                                    signature=self._func_signature(item),
                                    parent=node.name,
                                    docstring=method_doc,
                                )
                            )
                            count += 1
                            class_method_ids.add(id(item))

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 跳过已在类中处理的方法
                    if id(node) in class_method_ids:
                        continue
                    doc = ast.get_docstring(node) or ""
                    self.symbols.append(
                        Symbol(
                            name=node.name,
                            kind="function",
                            file=rel,
                            line=node.lineno,
                            end_line=getattr(node, "end_lineno", node.lineno),
                            signature=self._func_signature(node),
                            parent="",
                            docstring=doc,
                        )
                    )
                    count += 1

        return f"代码库索引完成 (AST): 共 {count} 个符号"

    @staticmethod
    def _func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        args = []
        for arg in node.args.args:
            if arg.arg == "self":
                continue
            annotation = ""
            if arg.annotation:
                try:
                    annotation = f": {ast.unparse(arg.annotation)}"
                except Exception:
                    pass
            args.append(f"{arg.arg}{annotation}")
        # *args, **kwargs
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")
        return f"({', '.join(args)})"

    @staticmethod
    def _class_signature(node: ast.ClassDef) -> str:
        bases = []
        for base in node.bases:
            try:
                bases.append(ast.unparse(base))
            except Exception:
                pass
        if bases:
            return f"({', '.join(bases)})"
        return ""

    # NOTE:基于名称/文件/父级关键字对符号列表做简单包含匹配搜索
    def search(self, query: str, kind: str = "", limit: int = 30) -> list[Symbol]:
        """搜索符号。"""
        query_lower = query.lower()
        results = []
        for sym in self.symbols:
            if kind and sym.kind != kind:
                continue
            if query_lower in sym.name.lower():
                results.append(sym)
            elif query_lower in sym.file.lower():
                results.append(sym)
            elif query_lower in sym.parent.lower():
                results.append(sym)
        return results[:limit]

    # NOTE:根据符号名定位其定义文件并提取周围代码上下文
    def get_context(
        self, name: str, max_lines: int = 30
    ) -> Optional[tuple[Symbol, str]]:
        """获取符号定义处的代码上下文。"""
        from .file_ops import _cached_read_text

        for sym in self.symbols:
            if sym.name == name:
                filepath = self.workspace_dir / sym.file
                if not filepath.exists():
                    return None
                try:
                    lines = _cached_read_text(filepath).splitlines()
                except Exception:
                    return None
                start = max(0, sym.line - 3)
                end = min(len(lines), sym.line + max_lines)
                context = "\n".join(lines[start:end])
                return sym, context
        return None


# NOTE:全局索引缓存：避免同一工作区重复构建符号索引
_index_cache: dict[str, CodeIndex] = {}


# NOTE:获取（或创建）指定工作区的代码索引缓存实例
def _get_index(workspace_dir: Path) -> CodeIndex:
    key = str(workspace_dir)
    if key not in _index_cache:
        _index_cache[key] = CodeIndex(workspace_dir)
    return _index_cache[key]


# NOTE:注册代码索引工具：构建索引、搜索符号、获取符号上下文
def register(agent):
    @agent.tool(args_validator=permission_validator("index_codebase"))
    async def index_codebase(ctx: RunContext[WorkspaceDeps]) -> str:
        """构建或更新代码库索引。首次使用或代码库变更后调用。"""
        log_tool(ctx, "index_codebase")
        index = _get_index(ctx.deps.workspace_dir)
        result = await index.build()
        ctx.deps.tool_tracker.add_chars(len(result))
        return result

    @agent.tool(args_validator=permission_validator("search_symbols"))
    async def search_symbols(
        ctx: RunContext[WorkspaceDeps],
        query: str,
        kind: str = "",
        limit: int = 30,
    ) -> str:
        """搜索代码库中的符号（类、函数、方法、变量）。

        参数:
            query: 搜索关键词（支持模糊匹配）
            kind: 可选过滤类型: class, function, method, variable, module, import
            limit: 最大返回数量
        """
        log_tool(ctx, "search_symbols", query, kind or "all")
        index = _get_index(ctx.deps.workspace_dir)
        if not index.symbols or index.is_stale():
            await index.build()

        results = index.search(query, kind, limit)
        if not results:
            return f"未找到匹配的符号: '{query}'"

        lines = [f"找到 {len(results)} 个匹配 '{query}' 的符号:"]
        for sym in results:
            parent_info = f" (in {sym.parent})" if sym.parent else ""
            sig = f" {sym.signature}" if sym.signature else ""
            lines.append(
                f"  [{sym.kind}] {sym.name}{sig}{parent_info}  → {sym.file}:{sym.line}"
            )
            if sym.docstring:
                doc = sym.docstring.split("\n")[0][:100]
                lines.append(f"      # {doc}")
        return "\n".join(lines)

    @agent.tool(args_validator=permission_validator("get_symbol_context"))
    async def get_symbol_context(
        ctx: RunContext[WorkspaceDeps],
        name: str,
        max_lines: int = 30,
    ) -> str:
        """获取指定符号的定义上下文代码。

        参数:
            name: 符号名称（精确匹配）
            max_lines: 返回的最大行数
        """
        log_tool(ctx, "get_symbol_context", name)
        index = _get_index(ctx.deps.workspace_dir)
        if not index.symbols or index.is_stale():
            await index.build()

        result = index.get_context(name, max_lines)
        if result is None:
            return f"未找到符号: '{name}'，请先使用 search_symbols 搜索"

        sym, context = result
        header = f"[{sym.kind}] {sym.name} 在 {sym.file}:{sym.line}"
        if sym.parent:
            header += f" (所属: {sym.parent})"
        return f"{header}\n```\n{context}\n```"
