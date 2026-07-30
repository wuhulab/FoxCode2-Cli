import subprocess
from pathlib import Path
from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool


def _detect_test_framework(workspace_dir: Path) -> str | None:
    """自动检测测试框架。"""
    if (workspace_dir / "pytest.ini").exists() or (
        workspace_dir / "pyproject.toml"
    ).exists():
        return "pytest"
    if (workspace_dir / "package.json").exists():
        return "npm"
    if (workspace_dir / "go.mod").exists():
        return "go"
    if (workspace_dir / "Cargo.toml").exists():
        return "cargo"
    if (workspace_dir / "pom.xml").exists() or (
        workspace_dir / "build.gradle"
    ).exists():
        return "maven"
    # 检查是否有测试文件
    for f in workspace_dir.rglob("*"):
        if f.is_file() and f.name.startswith("test_") and f.suffix == ".py":
            return "pytest"
        if f.is_file() and f.suffix in (".test.js", ".test.ts", ".spec.js", ".spec.ts"):
            return "npm"
    return None


def _run_command(
    cwd: Path, cmd: list[str], timeout: int = 120
) -> tuple[str, int]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            if output:
                output += "\n"
            output += f"[stderr]\n{result.stderr}"
        return output, result.returncode
    except subprocess.TimeoutExpired:
        return f"错误: 测试执行超时 ({timeout}秒)", -1
    except FileNotFoundError as e:
        return f"错误: 找不到命令 {cmd[0]}，请确保已安装", -1
    except Exception as e:
        return f"错误: 测试执行失败 - {e}", -1


def register(agent):
    @agent.tool
    async def run_tests(
        ctx: RunContext[WorkspaceDeps],
        framework: str = "",
        path: str = "",
        extra_args: str = "",
    ) -> str:
        target = path or "."
        log_tool(ctx, "run_tests", framework or "auto", target)

        workspace_dir = ctx.deps.workspace_dir
        if path:
            from .file_ops import _resolve_safe_path
            try:
                target_path = _resolve_safe_path(workspace_dir, path)
                if target_path.is_dir():
                    workspace_dir = target_path
                    target = "."
                else:
                    target = str(target_path.relative_to(ctx.deps.workspace_dir))
            except ValueError as e:
                return f"错误: {e}"

        detected = framework or _detect_test_framework(workspace_dir)
        if not detected:
            return (
                "错误: 无法自动检测测试框架，请手动指定 framework 参数\n"
                "支持的框架: pytest, npm, go, cargo, maven"
            )

        cmd: list[str] = []
        if detected == "pytest":
            cmd = ["python", "-m", "pytest", target, "-v"]
            if extra_args:
                cmd.extend(extra_args.split())
        elif detected == "npm":
            cmd = ["npm", "test"]
            if extra_args:
                cmd.extend(extra_args.split())
            if path:
                cmd = ["npx", "jest", target, "--verbose"]
                if extra_args:
                    cmd.extend(extra_args.split())
        elif detected == "go":
            cmd = ["go", "test", "-v", "./..."]
            if extra_args:
                cmd.extend(extra_args.split())
        elif detected == "cargo":
            cmd = ["cargo", "test"]
            if extra_args:
                cmd.extend(extra_args.split())
        elif detected == "maven":
            cmd = ["mvn", "test"]
            if extra_args:
                cmd.extend(extra_args.split())
        else:
            return f"错误: 不支持的测试框架: {detected}"

        output, code = _run_command(
            ctx.deps.workspace_dir, cmd, ctx.deps.shell_timeout * 4
        )
        summary = f"\n[退出码: {code}]"
        if code == 0:
            summary += " ✅ 测试通过"
        else:
            summary += " ❌ 测试失败"
        return f"框架: {detected}\n命令: {' '.join(cmd)}\n\n{output}{summary}"
