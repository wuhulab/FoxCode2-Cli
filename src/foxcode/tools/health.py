"""项目健康检查工具：检查项目依赖、测试、配置等是否完整。"""

import subprocess
from pathlib import Path

from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator


CHECKS = {
    ".py": {
        "package_files": ["requirements.txt", "pyproject.toml"],
        "test_patterns": ["pytest.ini", "setup.cfg", "pyproject.toml"],
        "linter_files": [".flake8", "pyproject.toml", ".pylintrc"],
        "format_files": ["pyproject.toml", ".black", ".isort.cfg"],
    },
    ".js": {
        "package_files": ["package.json"],
        "test_patterns": ["jest.config.js", "vitest.config.ts", "package.json"],
        "linter_files": [".eslintrc.js", ".eslintrc.json", ".eslintrc"],
        "format_files": [".prettierrc", ".prettierrc.json", "package.json"],
    },
    ".ts": {
        "package_files": ["package.json"],
        "test_patterns": ["jest.config.js", "vitest.config.ts", "package.json"],
        "linter_files": [".eslintrc.js", ".eslintrc.json", ".eslintrc"],
        "format_files": [".prettierrc", ".prettierrc.json", "package.json"],
    },
    ".go": {
        "package_files": ["go.mod"],
        "test_patterns": ["*_test.go"],
        "linter_files": [".golangci.yml", ".golangci.yaml"],
        "format_files": [],
    },
    ".rs": {
        "package_files": ["Cargo.toml"],
        "test_patterns": [],
        "linter_files": [],
        "format_files": ["rustfmt.toml"],
    },
}


def _detect_langs(workspace_dir: Path) -> list[str]:
    """检测项目中使用的主要编程语言。"""
    counts: dict[str, int] = {}
    for f in workspace_dir.rglob("*"):
        if f.is_file() and not any(
            p.startswith(".")
            for p in f.relative_to(workspace_dir).parts[:-1]
            if p != "."
        ):
            ext = f.suffix.lower()
            if ext in CHECKS:
                counts[ext] = counts.get(ext, 0) + 1
    return sorted(counts.keys(), key=lambda k: counts[k], reverse=True)


def _has_file(workspace_dir: Path, filenames: list[str]) -> tuple[bool, str]:
    for name in filenames:
        path = workspace_dir / name
        if path.exists():
            return True, name
    return False, ""


def _has_pattern(workspace_dir: Path, patterns: list[str]) -> tuple[bool, str]:
    for pattern in patterns:
        if pattern.startswith("*"):
            matches = list(workspace_dir.rglob(pattern))
            if matches:
                return True, str(matches[0].relative_to(workspace_dir))
        else:
            path = workspace_dir / pattern
            if path.exists():
                return True, pattern
    return False, ""


def _check_git(workspace_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            cwd=str(workspace_dir),
        )
        if result.returncode != 0:
            return "⚠️ 未检测到 Git 仓库"
        changes = result.stdout.strip().splitlines()
        if changes:
            return f"⚠️ 有 {len(changes)} 个未提交的变更"
        return "✅ Git 状态正常"
    except Exception as e:
        return f"⚠️ Git 检查失败: {e}"


def _check_tests(workspace_dir: Path, langs: list[str]) -> str:
    has_test_files = False
    for ext in langs:
        if ext in CHECKS:
            found, _ = _has_pattern(workspace_dir, CHECKS[ext]["test_patterns"])
            if found:
                has_test_files = True
                break
            # 手动查找测试文件
            for f in workspace_dir.rglob("*test*"):
                if f.is_file():
                    has_test_files = True
                    break
    return "✅ 检测到测试相关配置/文件" if has_test_files else "⚠️ 未检测到测试配置"


def _check_readme(workspace_dir: Path) -> str:
    for name in ["README.md", "README.rst", "README", "readme.md"]:
        if (workspace_dir / name).exists():
            return f"✅ 找到 {name}"
    return "⚠️ 未找到 README 文件"


def _check_license(workspace_dir: Path) -> str:
    for name in ["LICENSE", "LICENSE.md", "COPYING", "license"]:
        if (workspace_dir / name).exists():
            return f"✅ 找到 {name}"
    return "⚠️ 未找到 LICENSE 文件"


def register(agent):
    @agent.tool(args_validator=permission_validator("project_health"))
    async def project_health(ctx: RunContext[WorkspaceDeps]) -> str:
        """检查项目健康状况。分析依赖管理、测试覆盖、Git 状态、文档完整性等。"""
        log_tool(ctx, "project_health")
        workspace_dir = ctx.deps.workspace_dir

        langs = _detect_langs(workspace_dir)
        main_lang = langs[0] if langs else ""

        lines = ["## 项目健康检查报告\n"]

        # 1. 语言检测
        if langs:
            lines.append(f"**检测到的主要语言**: {', '.join(langs[:3])}")
        else:
            lines.append("**未检测到已知编程语言**")
        lines.append("")

        # 2. 依赖管理
        if main_lang and main_lang in CHECKS:
            has_pkg, pkg_file = _has_file(
                workspace_dir, CHECKS[main_lang]["package_files"]
            )
            if has_pkg:
                lines.append(f"✅ 依赖管理: 找到 {pkg_file}")
            else:
                lines.append(f"⚠️ 依赖管理: 未找到 {main_lang} 项目的包管理文件")
        else:
            lines.append("- 依赖管理: 不确定")
        lines.append("")

        # 3. Git 状态
        lines.append(_check_git(workspace_dir))
        lines.append("")

        # 4. 测试
        lines.append(_check_tests(workspace_dir, langs))
        lines.append("")

        # 5. 代码规范工具
        if main_lang and main_lang in CHECKS:
            has_linter, linter = _has_file(
                workspace_dir, CHECKS[main_lang]["linter_files"]
            )
            has_formatter, formatter = _has_file(
                workspace_dir, CHECKS[main_lang]["format_files"]
            )
            if has_linter:
                lines.append(f"✅ 代码检查器 (Linter): {linter}")
            else:
                lines.append("⚠️ 未找到代码检查器配置")
            if has_formatter:
                lines.append(f"✅ 代码格式化工具: {formatter}")
            else:
                lines.append("⚠️ 未找到代码格式化工具配置")
        lines.append("")

        # 6. 文档
        lines.append(_check_readme(workspace_dir))
        lines.append(_check_license(workspace_dir))
        lines.append("")

        # 7. .gitignore
        if (workspace_dir / ".gitignore").exists():
            lines.append("✅ 找到 .gitignore")
        else:
            lines.append("⚠️ 未找到 .gitignore")

        # 8. .env.example
        if (workspace_dir / ".env.example").exists():
            lines.append("✅ 找到 .env.example")
        else:
            lines.append("⚠️ 未找到 .env.example")

        lines.append("")
        lines.append("---")
        lines.append(
            "💡 建议: 确保依赖文件完整、有测试覆盖、README 清晰、配置好 lint/format 工具。"
        )

        return "\n".join(lines)
