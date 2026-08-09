from pathlib import Path

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from .models import ActionPlan, WorkspaceDeps
from .permissions import is_write_tool


# NOTE:计划模式下动态隐藏写/执行类工具，限制模型只能只读探索
async def _prepare_main_tools(
    ctx: RunContext[WorkspaceDeps], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """计划模式下隐藏写/执行类工具，模型只能探索并给出方案。"""
    if ctx.deps.plan_mode:
        return [td for td in tool_defs if not is_write_tool(td.name)]
    return tool_defs


# NOTE:构建主 Agent，整合系统提示、项目指南、用户规则、记忆、技能与子代理列表
def create_agent(
    config: dict,
    http_client: httpx.AsyncClient | None = None,
    project_instructions: str = "",
    mcp_toolsets: list | None = None,
    skills_list: list[tuple[str, str]] | None = None,
    subagent_list: list[tuple[str, str]] | None = None,
    rules: str = "",
    memory: str = "",
) -> Agent[WorkspaceDeps, ActionPlan]:
    # NOTE:初始化 OpenAI 兼容模型（支持自定义 base_url 与密钥）
    model = OpenAIChatModel(
        config["model"],
        provider=OpenAIProvider(
            base_url=config["base_url"],
            api_key=config["api_key"],
            http_client=http_client,
        ),
    )

    # NOTE:从配置读取温度参数控制生成随机性
    model_settings = ModelSettings(temperature=config["temperature"])

    # NOTE:加载内置系统提示，作为 AI 行为基线约束
    prompt_file = Path(__file__).parent / "system_prompt.md"
    system_prompt = prompt_file.read_text(encoding="utf-8").strip()

    # NOTE:追加项目级自定义指南（来自 .foxcode/instructions.md）
    if project_instructions:
        system_prompt += (
            "\n\n---\n"
            "## Project Guidelines\n"
            "The following guidelines are provided by the project and must be strictly followed:\n"
            f"{project_instructions}"
        )

    # NOTE:注入用户规则（.foxcode/Rules.md），声明最高优先级且 AI 只读
    if rules:
        system_prompt += (
            "\n\n---\n"
            "## User Rules (read-only)\n"
            "The following rules are set by the user in .foxcode/Rules.md. They are the highest priority "
            "constraints and must be strictly obeyed. This file is read-only for you: never edit, delete, "
            "rename, or copy onto it.\n"
            f"{rules}"
        )

    # NOTE:注入 AI 维护的项目记忆（.foxcode/Memory.md），用于避免已知陷阱
    if memory:
        system_prompt += (
            "\n\n---\n"
            "## Project Memory (AI-maintained)\n"
            "The following notes live in .foxcode/Memory.md. They record important project knowledge and "
            "pitfalls learned by AI. Apply them to avoid known traps. When you learn something valuable "
            "(important decisions, tricky pitfalls, gotchas), persist it by calling the update_memory tool "
            "with the full new content of Memory.md. Keep it concise and well-organized.\n"
            f"{memory}"
        )

    # NOTE:追加可用 Skills 列表，供 AI 按需加载使用
    if skills_list:
        lines = [
            "\n\n---\n## Available Skills (load content on demand with list_skills / use_skill)"
        ]
        for name, desc in skills_list:
            lines.append(f"- {name}: {desc}")
        system_prompt += "\n".join(lines)

    # NOTE:追加可用子代理列表，供 AI 委派只读探索任务
    if subagent_list:
        lines = [
            "\n\n---\n## Available Subagents (invoke with the task tool, specifying the agent argument)"
        ]
        for name, desc in subagent_list:
            lines.append(f"- {name}: {desc}")
        system_prompt += "\n".join(lines)

    # NOTE:为 MCP 工具集添加前缀命名空间，防止与本地工具冲突
    mcp_toolsets = [t.prefixed(t.id) for t in (mcp_toolsets or [])] or None

    # NOTE:创建主 Agent，绑定结构化输出类型 ActionPlan
    agent: Agent[WorkspaceDeps, ActionPlan] = Agent(
        model,
        deps_type=WorkspaceDeps,
        output_type=ActionPlan,
        system_prompt=system_prompt,
        model_settings=model_settings,
        toolsets=mcp_toolsets,
        capabilities=[PrepareTools(_prepare_main_tools)],
        retries=3,
    )

    # NOTE:注册全部本地工具到 Agent（核心 + 增强工具）
    from .tools import register_all_tools

    register_all_tools(agent)

    # NOTE:注册 Skills 和子代理功能（视为特殊工具集）
    from . import skills, subagents

    skills.register(agent)
    subagents.register(agent)

    return agent
