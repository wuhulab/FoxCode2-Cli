"""代码库索引工具：符号索引、项目结构理解、语义搜索。

支持两种索引方式：
1. universal-ctags（首选，支持多语言）
2. Python AST 回退（原生支持，仅 Python）
"""

import ast
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator


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


@dataclass
class CodeIndex:
    workspace_dir: Path
    symbols: list[Symbol] = field(default_factory=list)
    _file_mtime: dict[str, float] = field(default_factory=dict)

    def is_stale(self) -> bool:
        """检查索引是否需要更新（基于文件修改时间）。"""
        for f in self._iter_source_files():
            mtime = f.stat().st_mtime
            key = str(f)
            if key not in self._file_mtime or self._file_mtime[key] != mtime:
                return True
        return len(self._file_mtime) != len(list(self._iter_source_files()))

    def _iter_source_files(self):
        """遍历源代码文件。"""
        for ext in (
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
        ):
            for f in self.workspace_dir.rglob(f"*{ext}"):
                rel = f.relative_to(self.workspace_dir)
                if any(p.startswith(".") for p in rel.parts if p != "."):
                    continue
                if f.name.startswith("."):
                    continue
                yield f

    def build(self) -> str:
        """构建索引，返回状态信息。"""
        self.symbols.clear()
        self._file_mtime.clear()

        # 尝试 ctags
        ctags_result = self._try_ctags()
        if ctags_result:
            return ctags_result

        # 回退到 AST
        return self._build_ast_index()

    def _try_ctags(self) -> Optional[str]:
        """尝试使用 universal-ctags 生成索引。"""
        try:
            result = subprocess.run(
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
            result = subprocess.run(
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
        except Exception as e:
            return None

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

                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 顶层函数（排除类内部的方法，因为已经在上面的循环中处理）
                    if not any(
                        isinstance(parent, ast.ClassDef)
                        for parent in ast.walk(tree)
                        if hasattr(node, "parent")
                    ):
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

    def get_context(
        self, name: str, max_lines: int = 30
    ) -> Optional[tuple[Symbol, str]]:
        """获取符号定义处的代码上下文。"""
        for sym in self.symbols:
            if sym.name == name:
                filepath = self.workspace_dir / sym.file
                if not filepath.exists():
                    return None
                try:
                    lines = filepath.read_text(encoding="utf-8").splitlines()
                except Exception:
                    return None
                start = max(0, sym.line - 3)
                end = min(len(lines), sym.line + max_lines)
                context = "\n".join(lines[start:end])
                return sym, context
        return None


# 全局索引缓存
_index_cache: dict[str, CodeIndex] = {}


def _get_index(workspace_dir: Path) -> CodeIndex:
    key = str(workspace_dir)
    if key not in _index_cache:
        _index_cache[key] = CodeIndex(workspace_dir)
    return _index_cache[key]


def register(agent):
    @agent.tool(args_validator=permission_validator("index_codebase"))
    async def index_codebase(ctx: RunContext[WorkspaceDeps]) -> str:
        """构建或更新代码库索引。首次使用或代码库变更后调用。"""
        log_tool(ctx, "index_codebase")
        index = _get_index(ctx.deps.workspace_dir)
        result = index.build()
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
            index.build()

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
            index.build()

        result = index.get_context(name, max_lines)
        if result is None:
            return f"未找到符号: '{name}'，请先使用 search_symbols 搜索"

        sym, context = result
        header = f"[{sym.kind}] {sym.name} 在 {sym.file}:{sym.line}"
        if sym.parent:
            header += f" (所属: {sym.parent})"
        return f"{header}\n```\n{context}\n```"
