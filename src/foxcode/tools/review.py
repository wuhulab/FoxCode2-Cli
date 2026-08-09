"""AI 代码审查工具：分析当前代码变更并给出审查意见。"""

from pathlib import Path

from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator, run_subprocess


# NOTE:获取变更文件统计概览（git diff --stat）
async def _get_git_diff(cwd: Path, staged: bool = False) -> str:
    try:
        args = ["git", "diff"]
        if staged:
            args.append("--staged")
        args.append("--stat")
        result = await run_subprocess(
            args,
            timeout=15,
            cwd=str(cwd),
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


# NOTE:获取完整 diff 内容（带行数上限截断），用于审查细节分析
async def _get_full_diff(cwd: Path, staged: bool = False, max_lines: int = 500) -> str:
    try:
        args = ["git", "diff", "--no-color"]
        if staged:
            args.append("--staged")
        result = await run_subprocess(
            args,
            timeout=30,
            cwd=str(cwd),
        )
        if result.returncode != 0:
            return ""
        lines = result.stdout.splitlines()
        if len(lines) > max_lines:
            return (
                "\n".join(lines[:max_lines])
                + f"\n... (diff 过长，仅展示前 {max_lines} 行)"
            )
        return result.stdout
    except Exception:
        return ""


# NOTE:注册代码审查工具：基于 git diff 做启发式检查（调试语句、密钥、TODO 等）
def register(agent):
    @agent.tool(args_validator=permission_validator("review_changes"))
    async def review_changes(
        ctx: RunContext[WorkspaceDeps],
        staged: bool = False,
        scope: str = "",
    ) -> str:
        """审查当前代码变更并给出审查意见。

        分析 git diff，检查潜在问题：代码风格、安全漏洞、逻辑错误、
        遗漏的测试、命名规范等。

        参数:
            staged: 是否只审查已暂存的变更（git diff --cached）
            scope: 可选，只审查特定文件/目录的变更
        """
        log_tool(ctx, "review_changes", "cached" if staged else "working")

        workspace_dir = ctx.deps.workspace_dir
        diff = await _get_full_diff(workspace_dir, staged, max_lines=600)

        if not diff.strip():
            return "未检测到代码变更。" + (
                "请先用 git add 暂存变更。" if staged else ""
            )

        stat = await _get_git_diff(workspace_dir, staged)
        lines = [
            "## 变更摘要",
            f"```\n{stat}\n```" if stat else "",
            "",
            "## 审查建议",
            "基于 diff 分析，以下是审查要点（AI 观点，仅供参考）：",
            "",
        ]

        # 简单启发式检查
        issues = []

        # 检查是否有 print/debug 语句
        if "print(" in diff or "console.log(" in diff or "debugger;" in diff:
            issues.append(
                "⚠️ 检测到可能的调试代码（print/console.log/debugger），"
                "提交前请确认是否需要移除。"
            )

        # 检查是否有 TODO/FIXME
        todo_count = diff.lower().count("todo") + diff.lower().count("fixme")
        if todo_count > 0:
            issues.append(
                f"ℹ️ 检测到 {todo_count} 处 TODO/FIXME 标记，"
                "请确认是否有计划在后续处理。"
            )

        # 检查是否有敏感信息
        sensitive_patterns = [
            ("password", "密码硬编码"),
            ("secret", "密钥硬编码"),
            ("api_key", "API 密钥"),
            ("token =", "Token 硬编码"),
        ]
        for pattern, desc in sensitive_patterns:
            if pattern in diff.lower():
                issues.append(f"🔒 检测到可能的 {desc}，请检查安全性。")
                break

        # 检查二进制文件
        if "Binary files" in diff:
            issues.append("⚠️ diff 中包含二进制文件变更，请注意文件大小。")

        # 检查大段删除
        deleted_lines = diff.count("\n-")
        added_lines = diff.count("\n+")
        if deleted_lines > 0 and added_lines == 0:
            issues.append(
                f"⚠️ 仅删除了 {deleted_lines} 行代码，确认这些删除是预期行为，没有误删。"
            )

        if issues:
            lines.extend(issues)
        else:
            lines.append("✅ 未发现明显问题，变更看起来合理。")

        lines.extend(
            [
                "",
                "---",
                "审查完成。如需更详细的分析，可使用 run_shell 运行代码检查工具（如 pylint, eslint, mypy 等）。",
            ]
        )

        return "\n".join(lines)
