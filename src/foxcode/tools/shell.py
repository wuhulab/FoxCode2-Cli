import subprocess
import sys
from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool


def register(agent):
    @agent.tool
    async def run_shell(ctx: RunContext[WorkspaceDeps], command: str) -> str:
        cmd_preview = command[:60] + "..." if len(command) > 60 else command
        log_tool(ctx, "run_shell", cmd_preview)
        filepath = ctx.deps.workspace_dir
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=ctx.deps.shell_timeout,
                cwd=str(filepath),
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += "\n"
                output += f"[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n退出码: {result.returncode}"
            return output if output else "(命令执行成功，无输出)"
        except subprocess.TimeoutExpired:
            return f"错误: 命令执行超时 ({ctx.deps.shell_timeout}秒)"
        except Exception as e:
            return f"错误: 命令执行失败 - {e}"

    @agent.tool
    async def run_file(ctx: RunContext[WorkspaceDeps], filename: str) -> str:
        log_tool(ctx, "run_file", filename)
        filepath = ctx.deps.workspace_dir / filename
        if not filepath.exists():
            return f"错误: 文件 {filename} 不存在"
        ext = filepath.suffix.lower()
        interpreter_map = {
            ".py": [sys.executable],
            ".js": ["node"],
            ".ts": ["npx", "ts-node"],
            ".go": ["go", "run"],
            ".rs": ["rustc"],
            ".sh": ["bash"],
            ".bat": ["cmd", "/c"],
            ".ps1": ["powershell", "-File"],
        }
        interpreter = interpreter_map.get(ext)
        if interpreter is None:
            return f"错误: 不支持的文件类型 {ext}，请使用 run_shell 来执行"
        try:
            cmd = [*interpreter, str(filepath)]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=ctx.deps.shell_timeout,
                cwd=str(ctx.deps.workspace_dir),
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += "\n"
                output += f"[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n退出码: {result.returncode}"
            return output if output else "(执行成功，无输出)"
        except subprocess.TimeoutExpired:
            return f"错误: 执行超时 ({ctx.deps.shell_timeout}秒)"
        except FileNotFoundError:
            return f"错误: 找不到解释器来运行 {ext} 文件，请确保已安装相应的运行环境"
        except Exception as e:
            return f"错误: 执行失败 - {e}"
