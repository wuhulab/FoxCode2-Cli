"""FoxCode CLI 的 Agent 创建、网络客户端与运行循环核心。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Sequence

import httpx
from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.usage import UsageLimits
from pydantic_ai._agent_graph import ModelRequestNode
from pydantic_graph import End

from .cli_ui import console

if TYPE_CHECKING:
    from pydantic_ai import Agent
    from .models import WorkspaceDeps, ActionPlan


# NOTE:带指数退避重试的 HTTP 客户端，自动处理 403/429/5xx 与网络抖动
class RetryClient(httpx.AsyncClient):
    RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})

    def __init__(self, *args, **kwargs):
        # 为长思考/流式场景优化：连接10s、读取5分钟、写入10s、pool 10s
        kwargs.setdefault(
            "timeout",
            httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
        )
        # 保持更多长连接，防止中间代理因空闲关闭导致 incomplete chunked read
        kwargs.setdefault(
            "limits",
            httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=300.0,
            ),
        )
        super().__init__(*args, **kwargs)

    @staticmethod
    def _retry_wait(retry_count: int) -> int:
        """无限重试等待间隔：第1次15s、第2次30s、第3次起60s。"""
        if retry_count == 1:
            return 15
        if retry_count == 2:
            return 30
        return 60

    async def send(self, request, *args, **kwargs):
        retry_count = 0
        body = await request.aread()
        headers = dict(request.headers)
        url = str(request.url)
        method = request.method

        while True:
            try:
                new_request = httpx.Request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=body,
                )
                response = await super().send(new_request, *args, **kwargs)

                if response.status_code in self.RETRY_STATUSES:
                    retry_count += 1
                    wait = self._retry_wait(retry_count)
                    await response.aread()
                    console.print(
                        f"  [yellow]服务暂不可用 ({response.status_code})，{wait}秒后重试 (第{retry_count}次)[/yellow]"
                    )
                    await asyncio.sleep(wait)
                    continue

                if response.is_error:
                    await response.aread()
                    try:
                        detail = response.text[:300] if response.text else ""
                    except Exception:
                        detail = ""
                    raise httpx.HTTPStatusError(
                        f"API 请求失败 ({response.status_code}): {detail}",
                        request=new_request,
                        response=response,
                    )

                return response

            except httpx.TransportError:
                retry_count += 1
                wait = self._retry_wait(retry_count)
                console.print(
                    f"  [yellow]网络异常，{wait}秒后重试 (第{retry_count}次)[/yellow]"
                )
                await asyncio.sleep(wait)
            except httpx.ProtocolError:
                # ProtocolError（如 RemoteProtocolError / LocalProtocolError /
                # incomplete chunked read）是 TransportError 子类，理论上已被
                # 上面捕获；此处兜底防止因 httpx 版本差异导致漏网
                retry_count += 1
                wait = self._retry_wait(retry_count)
                console.print(
                    f"  [yellow]网络异常，{wait}秒后重试 (第{retry_count}次)[/yellow]"
                )
                await asyncio.sleep(wait)


# NOTE:根据环境变量与配置构建 httpx 代理挂载映射（http/https/no_proxy）
def _build_proxy_mounts(config: dict) -> dict:
    proxy_mounts = {}
    if config["no_proxy"]:
        for host in config["no_proxy"].split(","):
            host = host.strip().lower()
            if not host:
                continue
            if host.startswith("."):
                proxy_mounts[f"all://*{host}"] = httpx.AsyncHTTPTransport()
            else:
                proxy_mounts[f"all://{host}"] = httpx.AsyncHTTPTransport()

    for scheme, proxy_url in [
        ("http://", config["http_proxy"]),
        ("https://", config["https_proxy"]),
    ]:
        if proxy_url:
            proxy_mounts[scheme] = httpx.AsyncHTTPTransport(proxy=proxy_url)
    return proxy_mounts


# NOTE:初始化会话所需的全部管理器：权限、Skills、子代理、MCP（项目配置设置覆盖）
def _build_managers(config: dict):
    """构建权限、skills、子代理、MCP 等运行时组件。"""
    from .config import load_project_config, apply_project_settings
    from .skills import SkillsManager
    from .subagents import SubAgentManager
    from .mcp_manager import load_mcp_toolsets
    from .permissions import PermissionManager

    workspace_dir = config["workspace_dir"].resolve()
    project_config = load_project_config(workspace_dir)
    config = apply_project_settings(config, project_config)

    perms = PermissionManager(console=console, workspace_dir=workspace_dir)
    perms.load_settings(project_config["settings"])

    skills_mgr = SkillsManager(workspace_dir / ".foxcode" / "skills")
    skills_mgr.load()

    # 加载内置 skills（用户 skills 优先，可覆盖内置）
    builtin_dir = Path(__file__).parent / "builtin_skills"
    if builtin_dir.is_dir():
        builtin_mgr = SkillsManager(builtin_dir)
        builtin_mgr.load()
        for name, skill in builtin_mgr.skills.items():
            if name not in skills_mgr.skills:
                skills_mgr.skills[name] = skill

    subagents_mgr = SubAgentManager(workspace_dir / ".foxcode" / "agents")
    subagents_mgr.load()

    try:
        mcp_toolsets = load_mcp_toolsets(workspace_dir, perms)
    except Exception as e:
        console.print(f"  [yellow]⚠ MCP 工具加载失败: {e}[/yellow]")
        mcp_toolsets = []

    return config, project_config, perms, skills_mgr, subagents_mgr, mcp_toolsets


# NOTE:提取 Skills 与子代理列表，用于注入系统提示的可用资源段
def _agent_lists(skills_mgr, subagents_mgr):
    skills_list = [(s.name, s.description) for s in skills_mgr.list()]
    subagent_list = [
        (d.name, d.description or "通用子代理") for d in subagents_mgr.list()
    ]
    return skills_list, subagent_list


# NOTE:通过 OpenAI 兼容接口拉取 /v1/models 列表，失败时友好提示并返回空列表
async def _fetch_model_list(base_url: str, api_key: str) -> list[str]:
    """通过 /v1/models 获取可用模型名称列表。"""
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return [m.get("id") for m in data.get("data", []) if m.get("id")]
    except Exception as e:
        console.print(f"  [yellow]获取模型列表失败: {e}[/yellow]")
        return []


# NOTE:交互式模型选择：展示可用列表，支持序号或名称输入，回车保留默认值
async def _select_model_interactive(
    base_url: str, api_key: str, current: str = ""
) -> str:
    """获取模型列表展示给用户，让用户输入名称或序号选择模型。"""
    models = await _fetch_model_list(base_url, api_key)
    if not models:
        return current
    default = current or models[0]
    console.print("[bold cyan]可用模型:[/bold cyan]")
    for i, name in enumerate(models, 1):
        console.print(f"  [yellow]{i}[/yellow]. {name}")
    console.print()
    choice = console.input(
        f"[bold cyan]输入模型名称或序号 [/bold cyan][dim](回车默认 {default})[/dim]: "
    ).strip()
    if not choice:
        return default
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(models):
            return models[idx - 1]
        console.print("[red]序号无效，使用默认模型[/red]")
        return default
    if choice in models:
        return choice
    console.print("[red]模型名称不在列表中，仍将使用输入的名称[/red]")
    return choice


# NOTE:统一创建 Agent 的工厂函数，消除 /free、/openai、/model 中的重复代码
def build_agent(
    config: dict,
    http_client: httpx.AsyncClient,
    project_config: dict,
    mcp_toolsets: list | None = None,
    skills_list: list | None = None,
    subagent_list: list | None = None,
) -> "Agent[WorkspaceDeps, ActionPlan]":
    """统一创建 Agent，避免在多处重复调用 create_agent。"""
    from .agent import create_agent

    return create_agent(
        config,
        http_client,
        project_config["instructions"],
        mcp_toolsets=mcp_toolsets,
        skills_list=skills_list,
        subagent_list=subagent_list,
        rules=project_config["rules"],
        memory=project_config["memory"],
    )


# NOTE:异步初始化 MCP（如需要），失败时打印警告但继续
async def init_agent_mcp(agent: "Agent") -> bool:
    """尝试异步进入 agent 上下文以初始化 MCP。

    返回 True 表示成功，False 表示失败。
    """
    if not getattr(agent, "_mcp_toolsets", None) and not getattr(
        agent, "toolsets", None
    ):
        return True
    try:
        await agent.__aenter__()
        return True
    except Exception as e:
        console.print(f"  [yellow]新 Agent MCP 初始化失败: {e}[/yellow]")
        return False


# NOTE:底层强制流式请求（避免长思考被中间代理截断），前台仍按非流式方式展示结果
async def _run_with_narration(
    agent: "Agent[WorkspaceDeps, ActionPlan]",
    prompt: str | Sequence[Any] | None,
    all_messages: list,
    deps: "WorkspaceDeps",
    config: dict,
    status=None,
):
    """底层强制流式运行 agent，前台非流式输出结果。

    对每个 ModelRequestNode 走 stream() 路径并完整 drain，确保 API 请求始终使用
    stream=true，避免长思考/长响应被中间代理或网关半途截断。工具调用间隙的旁白/
    思考文本仍按原逻辑打印。返回 (all_messages, plan, usage)。
    """
    status_paused = False

    def _pause_status_live():
        nonlocal status_paused
        if status is not None and not status_paused:
            try:
                status.stop()
            except Exception:
                pass
            status_paused = True

    def _resume_status_live():
        nonlocal status_paused
        if status is not None and status_paused:
            try:
                status.start()
            except Exception:
                pass
            status_paused = False

    async with agent.iter(
        prompt,
        message_history=all_messages,
        deps=deps,
        usage_limits=UsageLimits(request_limit=None),
    ) as agent_run:
        next_node = agent_run.next_node
        while not isinstance(next_node, End):
            if isinstance(next_node, ModelRequestNode):
                async with next_node.stream(agent_run.ctx) as agent_stream:
                    await agent_stream.drain()
                next_node = await agent_run.next(next_node)
            else:
                next_node = await agent_run.next(next_node)

            response = getattr(next_node, "model_response", None)
            if response is None:
                continue
            has_tool_calls = any(isinstance(p, ToolCallPart) for p in response.parts)
            if not has_tool_calls:
                continue
            narration = "\n".join(
                p.content
                for p in response.parts
                if isinstance(p, (TextPart, ThinkingPart)) and p.content
            )
            if narration:
                _pause_status_live()
                console.print()
                console.print(Markdown(narration), style="dim")
                _resume_status_live()
        result = agent_run.result
    all_messages = result.all_messages()
    plan = result.output
    usage = result.usage
    if usage:
        deps.tool_tracker.record_usage(
            usage.input_tokens or 0,
            usage.output_tokens or 0,
            config["model"],
        )
    return all_messages, plan, usage


# NOTE:包装单次 agent 运行的状态循环：启动 spinner 更新器、处理审批暂停、收集结果
async def _run_status_loop(
    agent: "Agent[WorkspaceDeps, ActionPlan]",
    prompt: str | Sequence[Any] | None,
    all_messages: list,
    deps: "WorkspaceDeps",
    config: dict,
):
    """在 console.status 内执行一次 agent 运行，处理审批暂停。

    底层模型请求强制走流式（避免长思考被中间代理截断），前台仍按非流式方式
    展示最终结果。遍历模型响应时直接输出 AI 在调用工具时附带的话，
    便于协作开发确认，同时避免前台流式输出与 console.status 交错导致显示问题。
    """
    with console.status("", spinner="fox") as status:
        deps.permissions.status = status
        deps.permissions.tool_tracker = deps.tool_tracker
        deps.tool_tracker.status = status

        async def status_updater():
            try:
                while True:
                    if deps.tool_tracker.paused:
                        await asyncio.sleep(0.1)
                        continue
                    msg = deps.tool_tracker.status_line("").strip()
                    status.update(msg)
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass

        update_task = asyncio.create_task(status_updater())
        try:
            all_messages, plan, usage = await _run_with_narration(
                agent, prompt, all_messages, deps, config, status=status
            )
        finally:
            update_task.cancel()
            try:
                await update_task
            except asyncio.CancelledError:
                pass

    return all_messages, plan
