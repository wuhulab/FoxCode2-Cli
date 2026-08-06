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
            "## Project Guidelines\n"
            "The following guidelines are provided by the project and must be strictly followed:\n"
            f"{project_instructions}"
        )

    if skills_list:
        lines = [
            "\n\n---\n## Available Skills (load content on demand with list_skills / use_skill)"
        ]
        for name, desc in skills_list:
            lines.append(f"- {name}: {desc}")
        system_prompt += "\n".join(lines)

    if subagent_list:
        lines = [
            "\n\n---\n## Available Subagents (invoke with the task tool, specifying the agent argument)"
        ]
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
        retries=3,
    )

    from .tools import register_all_tools

    register_all_tools(agent)

    from . import skills, subagents

    skills.register(agent)
    subagents.register(agent)

    return agent
