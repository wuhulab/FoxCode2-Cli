from pathlib import Path
from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool, permission_validator, run_subprocess


# NOTE:通过检测配置文件自动推断项目使用的包管理器
def _detect_package_manager(workspace_dir: Path) -> str | None:
    if (workspace_dir / "package.json").exists():
        return "npm"
    if (workspace_dir / "requirements.txt").exists() or (
        workspace_dir / "pyproject.toml"
    ).exists():
        return "pip"
    if (workspace_dir / "Cargo.toml").exists():
        return "cargo"
    if (workspace_dir / "go.mod").exists():
        return "go"
    if (workspace_dir / "pom.xml").exists():
        return "maven"
    if (workspace_dir / "build.gradle").exists():
        return "gradle"
    return None


# NOTE:运行包管理器命令的通用包装，返回输出与退出码
async def _run_cmd(cwd: Path, cmd: list[str], timeout: int = 120) -> tuple[str, int]:
    try:
        result = await run_subprocess(
            cmd,
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
    except TimeoutError:
        return f"错误: 命令执行超时 ({timeout}秒)", -1
    except FileNotFoundError:
        return f"错误: 找不到命令 {cmd[0]}，请确保已安装", -1
    except Exception as e:
        return f"错误: 命令执行失败 - {e}", -1


# NOTE:注册依赖安装工具：自动检测包管理器并适配 pip/npm/cargo/go/maven/gradle
def register(agent):
    @agent.tool(args_validator=permission_validator("install_deps"))
    async def install_deps(
        ctx: RunContext[WorkspaceDeps],
        packages: str = "",
        manager: str = "",
        dev: bool = False,
    ) -> str:
        log_tool(ctx, "install_deps", packages or "(auto)", manager or "auto")
        workspace_dir = ctx.deps.workspace_dir
        detected = manager or _detect_package_manager(workspace_dir)

        if not detected:
            return (
                "错误: 无法自动检测包管理器，请手动指定 manager 参数\n"
                "支持的包管理器: pip, npm, cargo, go, maven, gradle"
            )

        cmd: list[str] = []
        if detected == "pip":
            if packages:
                cmd = ["python", "-m", "pip", "install", *packages.split()]
                if dev:
                    cmd.append("-e")
            else:
                if (workspace_dir / "requirements.txt").exists():
                    cmd = [
                        "python",
                        "-m",
                        "pip",
                        "install",
                        "-r",
                        "requirements.txt",
                    ]
                else:
                    cmd = ["python", "-m", "pip", "install", "-e", "."]
        elif detected == "npm":
            if packages:
                cmd = ["npm", "install", *packages.split()]
                if dev:
                    cmd.append("--save-dev")
            else:
                cmd = ["npm", "install"]
        elif detected == "cargo":
            if packages:
                cmd = ["cargo", "add", *packages.split()]
            else:
                cmd = ["cargo", "build"]
        elif detected == "go":
            if packages:
                cmd = ["go", "get", *packages.split()]
            else:
                cmd = ["go", "mod", "tidy"]
        elif detected == "maven":
            cmd = ["mvn", "dependency:resolve"]
        elif detected == "gradle":
            cmd = ["gradle", "build", "--dry-run"]
        else:
            return f"错误: 不支持的包管理器: {detected}"

        output, code = await _run_cmd(workspace_dir, cmd, ctx.deps.shell_timeout * 3)
        summary = f"\n[退出码: {code}]"
        if code == 0:
            summary += " ✅ 依赖安装成功"
        else:
            summary += " ❌ 安装失败"
        return f"包管理器: {detected}\n命令: {' '.join(cmd)}\n\n{output}{summary}"
