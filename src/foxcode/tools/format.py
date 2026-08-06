from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool, permission_validator, run_subprocess


FORMATTERS = {
    ".py": (["python", "-m", "black"], ["python", "-m", "isort", "--profile", "black"]),
    ".js": (["npx", "prettier", "--write"],),
    ".ts": (["npx", "prettier", "--write"],),
    ".jsx": (["npx", "prettier", "--write"],),
    ".tsx": (["npx", "prettier", "--write"],),
    ".json": (["npx", "prettier", "--write"],),
    ".md": (["npx", "prettier", "--write"],),
    ".yaml": (["npx", "prettier", "--write"],),
    ".yml": (["npx", "prettier", "--write"],),
    ".rs": (["cargo", "fmt"],),
    ".go": (["gofmt", "-w"],),
}


def register(agent):
    @agent.tool(args_validator=permission_validator("format_code"))
    async def format_code(
        ctx: RunContext[WorkspaceDeps],
        filename: str = "",
        directory: str = "",
    ) -> str:
        target = filename or directory or "."
        log_tool(ctx, "format_code", target)

        workspace_dir = ctx.deps.workspace_dir
        if filename:
            from .file_ops import _resolve_safe_path

            try:
                filepath = _resolve_safe_path(workspace_dir, filename)
            except ValueError as e:
                return f"错误: {e}"
            if not filepath.exists():
                return f"错误: 文件 {filename} 不存在"
            ext = filepath.suffix.lower()
            formatters = FORMATTERS.get(ext)
            if not formatters:
                return f"错误: 不支持的文件类型 {ext}，支持的类型: {', '.join(FORMATTERS.keys())}"
            results = []
            for cmd_base in formatters:
                cmd = [*cmd_base, str(filepath)]
                try:
                    result = await run_subprocess(
                        cmd,
                        timeout=ctx.deps.shell_timeout,
                        cwd=str(workspace_dir),
                    )
                    if result.returncode == 0:
                        results.append(f"✅ {' '.join(cmd[:3])}... 成功")
                    else:
                        err = result.stderr.strip()[:200]
                        results.append(f"❌ {' '.join(cmd[:3])}... 失败: {err}")
                except FileNotFoundError:
                    results.append(f"⚠️ {' '.join(cmd[:3])}... 命令未找到")
                except Exception as e:
                    results.append(f"❌ {' '.join(cmd[:3])}... 错误: {e}")
            return "\n".join(results)

        elif directory:
            from .file_ops import _resolve_safe_path

            try:
                dirpath = _resolve_safe_path(workspace_dir, directory)
            except ValueError as e:
                return f"错误: {e}"
            if not dirpath.is_dir():
                return f"错误: {directory} 不是一个目录"
            # 尝试对整个目录格式化（black 支持目录，prettier 需要通配符）
            results = []
            for cmd_base in (["python", "-m", "black"], ["npx", "prettier", "--write"]):
                cmd = [*cmd_base, str(dirpath)]
                try:
                    result = await run_subprocess(
                        cmd,
                        timeout=ctx.deps.shell_timeout * 2,
                        cwd=str(workspace_dir),
                    )
                    if result.returncode == 0:
                        results.append(f"✅ {' '.join(cmd_base)} {directory} 成功")
                        if result.stdout.strip():
                            results.append(result.stdout.strip())
                    else:
                        err = result.stderr.strip()[:300]
                        if err:
                            results.append(f"⚠️ {' '.join(cmd_base)}: {err}")
                except FileNotFoundError:
                    pass
                except Exception as e:
                    results.append(f"❌ {' '.join(cmd_base)}: {e}")
            if not results:
                return "未找到可用的格式化工具，请确保已安装 black 或 prettier"
            return "\n".join(results)
        else:
            return "错误: 请指定 filename 或 directory 参数"
