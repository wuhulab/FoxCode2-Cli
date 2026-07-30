import re
import subprocess
from pathlib import Path
from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool


def _run_ripgrep(
    cwd: Path,
    pattern: str,
    path: str = "",
    file_extension: str = "",
    max_results: int = 50,
    case_sensitive: bool = False,
) -> str | None:
    """优先使用 ripgrep，否则回退到 Python 实现。"""
    args = ["rg", "--line-number", "--no-heading"]
    if not case_sensitive:
        args.append("--ignore-case")
    if max_results:
        args.extend(["--max-count", str(max_results)])
    if file_extension:
        for ext in file_extension.split(","):
            ext = ext.strip()
            if ext:
                if not ext.startswith("."):
                    ext = "." + ext
                args.extend(["--glob", f"*{ext}"])
    args.append(pattern)
    if path:
        target_path = (cwd / path).resolve()
        if not str(target_path).startswith(str(cwd.resolve())):
            return f"错误: 路径越权 {path}"
        args.append(str(target_path).replace("\\", "/"))
    else:
        args.append(str(cwd.resolve()).replace("\\", "/"))

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(cwd),
        )
        if result.returncode == 1 and not result.stdout:
            return f"未找到匹配 '{pattern}' 的结果"
        if result.returncode not in (0, 1):
            stderr = result.stderr.strip()
            if stderr:
                return f"搜索错误: {stderr}"
        output = result.stdout.rstrip()
        return output if output else f"未找到匹配 '{pattern}' 的结果"
    except FileNotFoundError:
        return None  # fallback to python
    except subprocess.TimeoutExpired:
        return "错误: 搜索超时"
    except Exception as e:
        return f"错误: 搜索失败 - {e}"


def _python_grep(
    cwd: Path,
    pattern: str,
    path: str = "",
    file_extension: str = "",
    max_results: int = 50,
    case_sensitive: bool = False,
) -> str:
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"错误: 正则表达式无效 - {e}"
    cwd = cwd.resolve()
    search_dir = (cwd / path).resolve() if path else cwd
    if not str(search_dir).startswith(str(cwd)):
        return f"错误: 路径越权 {path}"

    exts = []
    if file_extension:
        exts = [e.strip().lower() for e in file_extension.split(",") if e.strip()]
        exts = [e if e.startswith(".") else f".{e}" for e in exts]

    results = []
    count = 0
    try:
        for filepath in search_dir.rglob("*"):
            if not filepath.is_file():
                continue
            rel_dir = filepath.parent.relative_to(search_dir)
            if any(p.startswith(".") for p in rel_dir.parts if p != "."):
                continue
            if filepath.name.startswith("."):
                continue
            if exts and not any(filepath.name.lower().endswith(e) for e in exts):
                continue
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = filepath.relative_to(cwd)
                    results.append(f"{rel}:{i}:{line}")
                    count += 1
                    if count >= max_results:
                        break
            if count >= max_results:
                break
    except Exception as e:
        return f"错误: 搜索失败 - {e}"

    if results:
        return "\n".join(results)
    return f"未找到匹配 '{pattern}' 的结果"


def register(agent):
    @agent.tool
    async def search_in_files(
        ctx: RunContext[WorkspaceDeps],
        pattern: str,
        path: str = "",
        file_extension: str = "",
        max_results: int = 50,
        case_sensitive: bool = False,
    ) -> str:
        log_tool(ctx, "search_in_files", f'"{pattern}"')
        result = _run_ripgrep(
            ctx.deps.workspace_dir,
            pattern,
            path,
            file_extension,
            max_results,
            case_sensitive,
        )
        if result is None:
            result = _python_grep(
                ctx.deps.workspace_dir,
                pattern,
                path,
                file_extension,
                max_results,
                case_sensitive,
            )
        return result
