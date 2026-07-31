from pathlib import Path
from typing import Any

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from .models import ActionPlan, WorkspaceDeps
from .permissions import is_write_tool


async def _prepare_main_tools(
    ctx: RunContext[WorkspaceDeps], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """计划模式下隐藏写/执行类工具，模型只能探索并给出方案。"""
    if ctx.deps.plan_mode:
        return [td for td in tool_defs if not is_write_tool(td.name)]
    return tool_defs


def create_agent(
    config: dict,
    http_client: httpx.AsyncClient | None = None,
    project_instructions: str = "",
    mcp_toolsets: list | None = None,
    skills_list: list[tuple[str, str]] | None = None,
    subagent_list: list[tuple[str, str]] | None = None,
) -> Agent[WorkspaceDeps, ActionPlan]:
    model = OpenAIChatModel(
        config["model"],
        provider=OpenAIProvider(
            base_url=config["base_url"],
            api_key=config["api_key"],
            http_client=http_client,
        ),
    )

    model_settings = ModelSettings(temperature=config["temperature"])

    prompt_file = Path(__file__).parent / "system_prompt.md"
    system_prompt = prompt_file.read_text(encoding="utf-8").strip()

    if project_instructions:
        system_prompt += (
            "\n\n---\n"
            "## 项目指南\n"
            "以下是项目提供的指南，请严格遵守：\n"
            f"{project_instructions}"
        )

    if skills_list:
        lines = ["\n\n---\n## 可用 Skills（用 list_skills / use_skill 按需获取内容）"]
        for name, desc in skills_list:
            lines.append(f"- {name}: {desc}")
        system_prompt += "\n".join(lines)

    if subagent_list:
        lines = ["\n\n---\n## 可用子代理（用 task 工具指定 agent 参数调用）"]
        for name, desc in subagent_list:
            lines.append(f"- {name}: {desc}")
        system_prompt += "\n".join(lines)

    mcp_toolsets = [t.prefixed(t.id) for t in (mcp_toolsets or [])] or None

    agent: Agent[WorkspaceDeps, ActionPlan] = Agent(
        model,
        deps_type=WorkspaceDeps,
        output_type=ActionPlan,
        system_prompt=system_prompt,
        model_settings=model_settings,
        toolsets=mcp_toolsets,
        capabilities=[PrepareTools(_prepare_main_tools)],
    )

    from .tools import (
        copy_file,
        deps,
        fetch,
        file_ops,
        format as fmt,
        git,
        grep,
        search,
        shell,
        tests,
        tree,
        undo,
    )
    from .tools import mode

    for mod in (
        file_ops,
        shell,
        search,
        undo,
        git,
        grep,
        fetch,
        tree,
        copy_file,
        tests,
        fmt,
        deps,
        mode,
    ):
        mod.register(agent)

    from . import skills, subagents

    skills.register(agent)
    subagents.register(agent)

    return agent
