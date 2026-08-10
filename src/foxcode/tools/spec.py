"""Spec 工具：生成与读取技术规格说明文档。

支持将 AI 分析生成的技术规格说明持久化到 .foxcode/SPEC.md，
作为后续编码的参考蓝图和验收依据。
"""

from pathlib import Path

from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator
from .file_ops import _resolve_safe_path


# NOTE:默认 Spec 文件路径（相对于工作区根目录）
DEFAULT_SPEC_PATH = ".foxcode/SPEC.md"


# NOTE:建议的规格说明章节模板（供 AI 参考，也可用于文档）
SPEC_TEMPLATE = """## 1. 需求概述
- 背景与目标
- 用户场景
- 范围边界（包含 / 不包含）

## 2. 技术方案
- 技术栈选型及理由
- 架构设计（分层 / 模块关系）
- 关键算法或数据流

## 3. API 设计
- 接口列表（方法、路径、参数、返回值）
- 错误码设计
- 认证与权限

## 4. 数据模型
- 核心实体及字段
- 数据库表结构 / Schema
- 状态机或关系图

## 5. 实现步骤
- 阶段拆分与优先级
- 每个阶段的核心改动点
- 风险点与回滚策略

## 6. 测试计划
- 单元测试覆盖点
- 集成测试场景
- 性能 / 安全测试指标

## 7. 验收标准
- 功能验收 checklist
- 非功能性指标（性能、兼容性、可访问性）
- 交付物清单
"""


# NOTE:核心生成逻辑（模块级，便于测试）
def _do_generate_spec(
    workspace_dir: Path,
    title: str,
    content: str,
    path: str = "",
) -> str:
    target_path = path.strip() if path else DEFAULT_SPEC_PATH
    filepath = _resolve_safe_path(workspace_dir, target_path)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    header = ""
    if title and not content.lstrip().startswith("#"):
        header = f"# {title}\n\n"

    full_content = header + content
    filepath.write_text(full_content, encoding="utf-8")
    rel = filepath.relative_to(workspace_dir)
    return f"规格说明已保存: {rel} ({len(full_content)} 字符)"


# NOTE:核心读取逻辑（模块级，便于测试）
def _do_read_spec(workspace_dir: Path, path: str = "") -> str:
    target_path = path.strip() if path else DEFAULT_SPEC_PATH
    filepath = _resolve_safe_path(workspace_dir, target_path)
    if not filepath.is_file():
        rel = filepath.relative_to(workspace_dir)
        return f"错误: 规格说明文件不存在: {rel}"
    content = filepath.read_text(encoding="utf-8")
    rel = filepath.relative_to(workspace_dir)
    return f"--- {rel} ---\n{content}"


# NOTE:注册 Spec 生成与读取工具


def register(agent):
    @agent.tool(args_validator=permission_validator("generate_spec"))
    async def generate_spec(
        ctx: RunContext[WorkspaceDeps],
        title: str,
        content: str,
        path: str = "",
    ) -> str:
        """生成技术规格说明文档并保存到工作区。

        **你必须调用此工具来保存规格说明；仅在 ActionPlan.explanation 中描述不会写入文件。**

        默认保存到 .foxcode/SPEC.md（可通过 path 参数覆盖）。
        若文件已存在则会覆盖。

        规格说明应包含以下章节（请在 content 中按此结构组织）：
        1. 需求概述 — 背景、目标、用户场景、范围边界
        2. 技术方案 — 技术栈、架构设计、关键数据流
        3. API 设计 — 接口列表、错误码、认证
        4. 数据模型 — 实体字段、数据库 schema
        5. 实现步骤 — 阶段拆分、风险与回滚
        6. 测试计划 — 单元/集成/性能测试覆盖
        7. 验收标准 — 功能 checklist、非功能性指标、交付物

        参数说明:
        - title: 规格说明标题（会作为文件顶部 # 级标题）
        - content: Markdown 格式的完整规格正文
        - path: 可选的自定义保存路径（默认为 .foxcode/SPEC.md）
        """
        log_tool(ctx, "generate_spec", title)
        try:
            return _do_generate_spec(ctx.deps.workspace_dir, title, content, path)
        except ValueError as e:
            return f"错误: 路径非法 - {e}"
        except Exception as e:
            return f"错误: 写入文件失败 - {e}"

    @agent.tool(args_validator=permission_validator("read_spec"))
    async def read_spec(
        ctx: RunContext[WorkspaceDeps],
        path: str = "",
    ) -> str:
        """读取工作区中的技术规格说明文档。

        默认读取 .foxcode/SPEC.md（可通过 path 参数覆盖）。
        在编码前先读取已有 spec，确保实现与规格一致。
        """
        log_tool(ctx, "read_spec", path or DEFAULT_SPEC_PATH)
        try:
            return _do_read_spec(ctx.deps.workspace_dir, path)
        except ValueError as e:
            return f"错误: 路径非法 - {e}"
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"
