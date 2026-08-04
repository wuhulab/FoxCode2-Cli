"""Subagents：定义隔离上下文的子代理，供 task 工具调用。"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from .models import ToolTracker, WorkspaceDeps
from .permissions import PermissionManager, is_write_tool
from .tools import permission_validator


@dataclass
class SubAgentDef:
    name: str
    description: str
    system_prompt: str
    path: Path
    model: str | None = None
    tools: list[str] | None = None


@dataclass
class SubAgentManager:
    agents_dir: Path
    defs: dict[str, SubAgentDef] = field(default_factory=dict)
    _agent_cache: dict[str, Agent] = field(default_factory=dict)

    def load(self):
        self.defs = {}
        if not self.agents_dir.is_dir():
            return
        for f in sorted(self.agents_dir.iterdir()):
            if f.suffix.lower() not in (".md", ".txt") or not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            front, body = _split_frontmatter(text)
            meta = {}
            if front:
                try:
                    meta = yaml.safe_load(front) or {}
                except Exception:
                    meta = {}
            name = str(meta.get("name") or f.stem).strip().lower()
            desc = str(meta.get("description") or "").strip()
            tools = meta.get("tools")
            if isinstance(tools, list):
                tools = [str(t) for t in tools]
            else:
                tools = None
            model = str(meta.get("model")) if meta.get("model") else None
            self.defs[name] = SubAgentDef(
                name=name,
                description=desc,
                system_prompt=body.strip() or f"你是名为 {name} 的专用子代理。",
                path=f,
                model=model,
                tools=tools,
            )

    def list(self) -> list[SubAgentDef]:
        return sorted(self.defs.values(), key=lambda d: d.name)

    def get(self, name: str) -> SubAgentDef | None:
        return self.defs.get(name.strip().lower())


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    import re

    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        return m.group(1), text[m.end() :]
    return None, text


def _subagent_prepare(
    ctx: RunContext[WorkspaceDeps], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """子代理只暴露只读工具。"""
    return [td for td in tool_defs if not is_write_tool(td.name)]


def create_subagent_agent(
    config: dict,
    http_client,
    system_prompt: str,
    model_name: str | None = None,
    tools: list[str] | None = None,
) -> Agent[WorkspaceDeps, str]:
    """创建只读子代理 Agent。"""
    model = OpenAIChatModel(
        model_name or config["model"],
        provider=OpenAIProvider(
            base_url=config["base_url"],
            api_key=config["api_key"],
            http_client=http_client,
        ),
    )
    model_settings = ModelSettings(temperature=config["temperature"])

    if tools is not None:
        allowed = set(tools)

        def _prepare(ctx, tool_defs):
            return [td for td in tool_defs if td.name in allowed]

        capabilities = [PrepareTools(_prepare)]
    else:
        capabilities = [PrepareTools(_subagent_prepare)]

    child: Agent[WorkspaceDeps, str] = Agent(
        model,
        deps_type=WorkspaceDeps,
        output_type=str,
        system_prompt=system_prompt,
        model_settings=model_settings,
        capabilities=capabilities,
        retries=3,
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
    ):
        mod.register(child)

    return child


def _child_deps(ctx: RunContext[WorkspaceDeps]) -> WorkspaceDeps:
    perms = PermissionManager(
        console=ctx.deps.console,
        workspace_dir=ctx.deps.workspace_dir,
        tool_tracker=None,
    )
    perms.subagent_mode = True
    perms.mode = "plan"
    return WorkspaceDeps(
        workspace_dir=ctx.deps.workspace_dir,
        http_client=ctx.deps.http_client,
        undo_manager=ctx.deps.undo_manager,
        console=ctx.deps.console,
        tool_tracker=ToolTracker(),
        shell_timeout=ctx.deps.shell_timeout,
        project_instructions="",
        permissions=perms,
        plan_mode=False,
        skills=None,
        subagents=None,
        mcp_toolsets=None,
        config=ctx.deps.config,
    )


async def run_subagent(
    ctx: RunContext[WorkspaceDeps],
    prompt: str,
    agent_name: str = "",
    max_result_chars: int = 6000,
) -> str:
    manager: SubAgentManager | None = ctx.deps.subagents
    definition = None
    if manager is not None and agent_name:
        definition = manager.get(agent_name)
        if definition is None:
            available = ", ".join(d.name for d in manager.list()) or "(无)"
            return f"错误: 未找到子代理 '{agent_name}'，可用: {available}"

    system_prompt = (
        f"你是子代理 {definition.name}，负责完成上级分配的任务。\n"
        f"职责: {definition.description}\n"
        f"请只使用只读工具收集信息，然后用简洁的中文总结关键结论返回。\n\n"
        f"{definition.system_prompt}"
        if definition
        else (
            "你是一个只读探索子代理。请使用只读工具调查并回答上级的问题，"
            "用简洁的中文总结关键结论返回。不要修改任何文件，不要执行写操作。"
        )
    )

    cache_key = (
        definition.name if definition else "general",
        definition.model if definition else "",
    )
    agent = None
    if manager is not None:
        agent = manager._agent_cache.get(cache_key)
    if agent is None:
        agent = create_subagent_agent(
            ctx.deps.config,
            ctx.deps.http_client,
            system_prompt,
            model_name=definition.model if definition else None,
            tools=definition.tools if definition else None,
        )
        if manager is not None:
            manager._agent_cache[cache_key] = agent

    ctx.deps.tool_tracker.count("task")
    ctx.deps.console.print(
        f"  [dim]子代理 {definition.name if definition else 'general'} 启动...[/dim]"
    )
    try:
        result = await agent.run(prompt, deps=_child_deps(ctx))
        output = result.output or ""
    except Exception as e:
        return f"错误: 子代理运行失败 - {e}"

    if len(output) > max_result_chars:
        output = output[:max_result_chars] + "\n... (结果已截断)"
    ctx.deps.console.print(
        f"  [dim]子代理 {definition.name if definition else 'general'} 完成[/dim]"
    )
    return output


def register(agent):
    @agent.tool(args_validator=permission_validator("task"))
    async def task(
        ctx: RunContext[WorkspaceDeps],
        prompt: str,
        agent: str = "",
    ) -> str:
        return await run_subagent(ctx, prompt, agent)
