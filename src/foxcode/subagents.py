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
from pydantic_ai.usage import UsageLimits

from .models import WorkspaceDeps, fork_workspace_deps
from .permissions import is_write_tool
from .tools import permission_validator


# NOTE:子代理定义模型：包含名称、描述、系统提示、路径、可选模型与工具白名单
@dataclass
class SubAgentDef:
    name: str
    description: str
    system_prompt: str
    path: Path
    model: str | None = None
    tools: list[str] | None = None


# NOTE:子代理管理器：从 .foxcode/agents/ 加载定义文件，缓存已创建的 Agent 实例
@dataclass
class SubAgentManager:
    agents_dir: Path
    defs: dict[str, SubAgentDef] = field(default_factory=dict)
    _agent_cache: dict[str, Agent] = field(default_factory=dict)

    # NOTE:扫描 agents 目录下所有 .md/.txt 文件，解析 YAML 前置元数据
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
                system_prompt=body.strip()
                or f"You are a dedicated subagent named {name}.",
                path=f,
                model=model,
                tools=tools,
            )

    def list(self) -> list[SubAgentDef]:
        return sorted(self.defs.values(), key=lambda d: d.name)

    def get(self, name: str) -> SubAgentDef | None:
        return self.defs.get(name.strip().lower())


# NOTE:分割 Markdown 前置 YAML 元数据与正文内容
def _split_frontmatter(text: str) -> tuple[str | None, str]:
    import re

    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        return m.group(1), text[m.end() :]
    return None, text


# NOTE:子代理工具筛选器：默认仅暴露只读工具，防止子代理修改文件
def _subagent_prepare(
    ctx: RunContext[WorkspaceDeps], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """子代理只暴露只读工具。"""
    return [td for td in tool_defs if not is_write_tool(td.name)]


# NOTE:创建只读子代理 Agent，可定制模型与允许的工具白名单
def create_subagent_agent(
    config: dict,
    http_client,
    system_prompt: str,
    model_name: str | None = None,
    tools: list[str] | None = None,
    output_type: type = str,
) -> Agent[WorkspaceDeps, str]:
    """创建只读子代理 Agent。

    output_type 可指定结构化输出模型（如 GoalVerification），默认返回 str。
    """
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
        output_type=output_type,
        system_prompt=system_prompt,
        model_settings=model_settings,
        capabilities=capabilities,
        retries=3,
    )

    from .tools import register_core_tools

    register_core_tools(child)

    return child


# NOTE:运行指定子代理执行只读探索任务，结果超长时自动截断并返回摘要
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
        f"You are subagent {definition.name}, responsible for the task delegated by the parent agent.\n"
        f"Role: {definition.description}\n"
        f"Use only read-only tools to gather information, then return the key findings in a concise summary.\n\n"
        f"{definition.system_prompt}"
        if definition
        else (
            "You are a read-only exploration subagent. Use read-only tools to investigate and answer "
            "the parent's question, then return a concise summary of the key findings. "
            "Do not modify any files or perform write operations. Do not overthink; investigate "
            "efficiently and report the essentials."
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

    async def _dummy_event_handler(_ctx, stream):
        # 强制底层走 stream 路径，避免长思考被中间代理截断
        async for _ in stream:
            pass

    try:
        # 子代理可能需要大量只读调查，使用与主 agent 相同的无限请求限制
        result = await agent.run(
            prompt,
            deps=fork_workspace_deps(ctx.deps),
            usage_limits=UsageLimits(request_limit=None),
            event_stream_handler=_dummy_event_handler,
        )
        output = result.output or ""
    except Exception as e:
        return f"错误: 子代理运行失败 - {e}"

    if len(output) > max_result_chars:
        output = output[:max_result_chars] + "\n... (结果已截断)"
    ctx.deps.console.print(
        f"  [dim]子代理 {definition.name if definition else 'general'} 完成[/dim]"
    )
    return output


# NOTE:注册 task 工具：主代理通过此工具委派只读子代理完成探索/分析任务
def register(agent):
    @agent.tool(args_validator=permission_validator("task"))
    async def task(
        ctx: RunContext[WorkspaceDeps],
        prompt: str,
        agent: str = "",
    ) -> str:
        return await run_subagent(ctx, prompt, agent)
