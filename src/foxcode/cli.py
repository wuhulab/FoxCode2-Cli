import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
import httpx
from httpx import AsyncHTTPTransport
from pydantic_ai.exceptions import (
    UnexpectedModelBehavior,
    ModelHTTPError,
    ModelAPIError,
)
from pydantic_ai.usage import UsageLimits
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from rich import box
from rich.spinner import SPINNERS

SPINNERS["fox"] = {"interval": 100, "frames": ["-", "/", "\\", "-"]}

from .config import load_config, load_project_config, apply_project_settings
from .models import ActionPlan, WorkspaceDeps, UndoManager
from .agent import create_agent
from .session import SessionManager
from .permissions import PermissionManager
from .skills import SkillsManager
from .subagents import SubAgentManager
from .mcp_manager import load_mcp_toolsets

console = Console()


class RetryClient(httpx.AsyncClient):
    RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
    MAX_RETRIES = 5

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("timeout", httpx.Timeout(120.0))
        super().__init__(*args, **kwargs)

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
                    wait = min(15 * retry_count, 120)
                    await response.aread()
                    if retry_count > self.MAX_RETRIES:
                        raise httpx.HTTPStatusError(
                            f"服务不可用 ({response.status_code})，已达最大重试次数",
                            request=new_request,
                            response=response,
                        )
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

            except httpx.TransportError as e:
                retry_count += 1
                if retry_count > self.MAX_RETRIES:
                    console.print(
                        f"  [red]请求异常: {e}，已达最大重试次数 {self.MAX_RETRIES}[/red]"
                    )
                    raise
                wait = min(15 * retry_count, 120)
                console.print(
                    f"  [yellow]请求异常: {e}，{wait}秒后重试 (第{retry_count}次)[/yellow]"
                )
                await asyncio.sleep(wait)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="foxcode",
        description="FoxCode - AI 编码代理工具",
    )
    parser.add_argument(
        "-p",
        "--print",
        dest="prompt",
        default=None,
        metavar="QUERY",
        help="单次运行给定提示并退出（headless 模式）",
    )
    parser.add_argument("--cwd", default=None, help="工作目录")
    parser.add_argument("--model", default=None, help="覆盖默认模型")
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="headless 输出格式",
    )
    parser.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        help="绕过所有权限确认（谨慎使用）",
    )
    parser.add_argument("--version", action="store_true", help="显示版本")
    return parser.parse_args(argv)


def print_welcome():
    title = Panel.fit(
        "[bold cyan]FoxCode Cli[/bold cyan] v0.4.0\n"
        "[yellow]/help[/yellow] 查看命令  "
        "[yellow]/plan[/yellow] 计划模式  "
        "[yellow]/permissions[/yellow] 权限设置  "
        "[yellow]/mcp[/yellow] MCP 服务  "
        "[yellow]/skills[/yellow] Skills  "
        "[yellow]/agents[/yellow] 子代理  "
        "[yellow]/term[/yellow] 终端模式  "
        "[yellow]/commit[/yellow] 智能提交  "
        "[yellow]/session[/yellow] 会话管理  "
        "[yellow]/usage[/yellow] 用量统计  "
        "[yellow]/undo[/yellow] 撤销操作  "
        "[yellow]/exit[/yellow] 退出",
        box=box.HEAVY,
        border_style="cyan",
    )
    console.print(title)


def print_help():
    table = Table(title="可用命令", box=box.SIMPLE)
    table.add_column("命令", style="yellow")
    table.add_column("说明", style="white")
    table.add_row("/help", "显示此帮助")
    table.add_row("/plan", "切换计划模式（只读探索，先出方案）")
    table.add_row("/permissions", "查看当前权限模式与规则")
    table.add_row("/mcp", "列出已配置的 MCP 服务器")
    table.add_row("/skills", "列出可用 Skills")
    table.add_row("/skill <名称>", "将指定 Skill 内容注入下一条提示")
    table.add_row("/agents", "列出可用子代理")
    table.add_row("/term", "切换终端模式 (Ctrl+X)，输入直接作为命令执行")
    table.add_row("/commit [信息]", "暂存所有变更并用 AI 生成提交信息后提交")
    table.add_row("/session list", "列出所有已保存的会话")
    table.add_row("/session save [名称]", "保存当前会话（默认自动命名）")
    table.add_row("/session load <名称>", "加载指定会话")
    table.add_row("/session del <名称>", "删除指定会话")
    table.add_row("/export [文件名]", "导出当前会话为 Markdown 文件")
    table.add_row("/undo [n]", "撤销最近 n 步操作（默认 1 步）")
    table.add_row("/history", "显示操作历史")
    table.add_row("/usage", "显示本次会话的 API 用量和费用统计")
    table.add_row("/clear", "清屏")
    table.add_row("/exit 或 /quit", "退出程序（自动保存会话）")
    console.print(table)


def print_action_plan(plan: ActionPlan, skip_explanation: bool = False):
    console.print()
    if not skip_explanation:
        panel = Panel(
            Markdown(plan.explanation),
            title="[bold green]AI 响应[/bold green]",
            border_style="green",
        )
        console.print(panel)

    if plan.files_modified:
        table = Table(box=box.SIMPLE)
        table.add_column("修改的文件", style="cyan")
        for f in plan.files_modified:
            table.add_row(f)
        console.print(table)

    if plan.code_snippets:
        for i, snippet in enumerate(plan.code_snippets, 1):
            if len(snippet) > 500:
                snippet = snippet[:500] + "\n... (截断)"
            panel = Panel(
                snippet,
                title=f"[bold magenta]代码片段 {i}[/bold magenta]",
                border_style="magenta",
                highlight=True,
            )
            console.print(panel)

    console.print()


def run_undo(deps: WorkspaceDeps, steps: int = 1):
    result = deps.undo_manager.undo(deps.workspace_dir, steps)
    console.print(f"[yellow]撤销结果:[/yellow]\n{result}")


def show_history(deps: WorkspaceDeps):
    result = deps.undo_manager.history_summary()
    console.print(f"[cyan]{result}[/cyan]")


def _exec_shell(command: str, cwd: Path, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
        )
        output = ""
        if result.stdout:
            output += result.stdout.rstrip()
        if result.stderr:
            if output:
                output += "\n"
            output += f"[stderr]\n{result.stderr.rstrip()}"
        if result.returncode != 0:
            output += f"\n退出码: {result.returncode}"
        return output if output else "(命令执行成功，无输出)"
    except subprocess.TimeoutExpired:
        return f"错误: 命令执行超时 ({timeout}秒)"
    except Exception as e:
        return f"错误: 命令执行失败 - {e}"


async def _generate_commit_message(
    http_client: httpx.AsyncClient, config: dict, diff: str
) -> str:
    diff_stat = _exec_shell("git diff --cached --stat", config["workspace_dir"])
    try:
        response = await http_client.post(
            f"{config['base_url']}/chat/completions",
            json={
                "model": config["model"],
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a git commit message generator. "
                            "Generate concise conventional commit messages "
                            "(e.g., feat:, fix:, chore:, refactor:, docs:, test:, style:). "
                            "Output ONLY the commit message, no explanation, no quotes."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Generate a commit message for this diff:\n\n"
                            f"## Changes\n{diff_stat}\n\n## Full diff\n{diff[:2000]}"
                        ),
                    },
                ],
                "temperature": 0.3,
                "max_tokens": 100,
            },
            headers={"Authorization": f"Bearer {config['api_key']}"},
        )
        response.raise_for_status()
        data = response.json()
        msg = data["choices"][0]["message"]["content"].strip().strip("\"'")
        return msg
    except Exception as e:
        return None


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


def _build_managers(config: dict):
    """构建权限、skills、子代理、MCP 等运行时组件。"""
    workspace_dir = config["workspace_dir"].resolve()
    project_config = load_project_config(workspace_dir)
    config = apply_project_settings(config, project_config)

    perms = PermissionManager(console=console, workspace_dir=workspace_dir)
    perms.load_settings(project_config["settings"])

    skills_mgr = SkillsManager(workspace_dir / ".foxcode" / "skills")
    skills_mgr.load()
    subagents_mgr = SubAgentManager(workspace_dir / ".foxcode" / "agents")
    subagents_mgr.load()

    mcp_toolsets = load_mcp_toolsets(workspace_dir, perms)

    return config, project_config, perms, skills_mgr, subagents_mgr, mcp_toolsets


def _agent_lists(skills_mgr, subagents_mgr):
    skills_list = [(s.name, s.description) for s in skills_mgr.list()]
    subagent_list = [
        (d.name, d.description or "通用子代理") for d in subagents_mgr.list()
    ]
    return skills_list, subagent_list


async def _run_status_loop(
    agent,
    prompt: str,
    all_messages: list,
    deps: WorkspaceDeps,
    config: dict,
):
    """在 console.status 内执行一次 agent.run，处理审批暂停。"""
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
        streamed = False
        full_text = ""
        try:
            if config.get("stream_output"):
                streamed = True
                async with agent.run_stream(
                    prompt,
                    message_history=all_messages,
                    deps=deps,
                    usage_limits=UsageLimits(request_limit=None),
                ) as stream_result:
                    async for chunk in stream_result.stream_output():
                        full_text = chunk.explanation or full_text
                all_messages = stream_result.all_messages()
                plan = await stream_result.get_output()
                usage = stream_result.usage
                if usage:
                    deps.tool_tracker.record_usage(
                        usage.input_tokens or 0,
                        usage.output_tokens or 0,
                        config["model"],
                    )
            else:
                result = await agent.run(
                    prompt,
                    message_history=all_messages,
                    deps=deps,
                    usage_limits=UsageLimits(request_limit=None),
                )
                all_messages = result.all_messages()
                plan = result.output
                usage = result.usage
                if usage:
                    deps.tool_tracker.record_usage(
                        usage.input_tokens or 0,
                        usage.output_tokens or 0,
                        config["model"],
                    )
        finally:
            update_task.cancel()
            try:
                await update_task
            except asyncio.CancelledError:
                pass

    return all_messages, plan, streamed, full_text


def _save_session(session_manager: SessionManager, all_messages: list):
    if all_messages:
        name = session_manager.get_auto_save_name()
        session_manager.save_session(name, all_messages)
        console.print(f"[dim]会话已自动保存: {name}[/dim]")


async def _run_headless(
    config: dict,
    perms: PermissionManager,
    skills_mgr,
    subagents_mgr,
    mcp_toolsets,
    args,
):
    prompt = args.prompt
    if prompt is None and not sys.stdin.isatty():
        prompt = sys.stdin.read()
    if not prompt or not prompt.strip():
        console.print(
            '[yellow]未提供提示。使用: foxcode -p "你的需求" 或管道输入[/yellow]'
        )
        return

    if args.model:
        config["model"] = args.model
    if args.dangerously_skip_permissions:
        perms.mode = "bypass"
    perms.headless = True

    skills_list, subagent_list = _agent_lists(skills_mgr, subagents_mgr)
    agent = create_agent(
        config,
        mcp_toolsets=mcp_toolsets,
        skills_list=skills_list,
        subagent_list=subagent_list,
    )

    proxy_mounts = _build_proxy_mounts(config)

    async with RetryClient(
        mounts=proxy_mounts or None,
        timeout=httpx.Timeout(config["request_timeout"]),
    ) as http_client:
        deps = WorkspaceDeps(
            workspace_dir=config["workspace_dir"].resolve(),
            http_client=http_client,
            undo_manager=UndoManager(),
            console=console,
            shell_timeout=config["shell_timeout"],
            permissions=perms,
            plan_mode=False,
            skills=skills_mgr,
            subagents=subagents_mgr,
            mcp_toolsets=mcp_toolsets,
            config=config,
        )

        try:
            async with agent:
                result = await agent.run(
                    prompt.strip(),
                    deps=deps,
                    usage_limits=UsageLimits(request_limit=None),
                )
        except UnexpectedModelBehavior as e:
            console.print(f"[red]API 响应格式错误: {e}[/red]", stderr=True)
            return
        except ModelHTTPError as e:
            console.print(
                f"[red]API HTTP 错误 ({e.status_code}) 模型={e.model_name}: {e.body or ''}[/red]",
                stderr=True,
            )
            return
        except Exception as e:
            console.print(f"[red]错误: {e}[/red]", stderr=True)
            return

        plan = result.output
        usage = result.usage
        if usage:
            deps.tool_tracker.record_usage(
                usage.input_tokens or 0, usage.output_tokens or 0, config["model"]
            )
        if args.output_format == "json":
            payload = {
                "explanation": plan.explanation,
                "files_modified": plan.files_modified,
                "code_snippets": plan.code_snippets,
                "usage": {
                    "input_tokens": usage.input_tokens if usage else 0,
                    "output_tokens": usage.output_tokens if usage else 0,
                },
            }
            console.print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            console.print(Markdown(plan.explanation))


async def _run_interactive(config: dict, http_client, args):
    workspace_dir = config["workspace_dir"].resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if not workspace_dir.is_dir():
        console.print(f"[red]错误: 工作目录路径存在但不是目录: {workspace_dir}[/red]")
        sys.exit(1)

    config, project_config, perms, skills_mgr, subagents_mgr, mcp_toolsets = (
        _build_managers(config)
    )

    if args.model:
        config["model"] = args.model
    perms.headless = False

    console.print(f"[dim]工作目录: {workspace_dir}[/dim]")
    console.print(f"[dim]模型: {config['model']}[/dim]")
    if project_config["instructions"]:
        console.print(
            f"[dim]项目指南: .foxcode/instructions.md 已加载 ({len(project_config['instructions'])} 字符)[/dim]"
        )
    if mcp_toolsets:
        console.print(f"[dim]MCP 服务器: {len(mcp_toolsets)} 个已配置[/dim]")
    console.print()

    proxy_mounts = _build_proxy_mounts(config)

    async with RetryClient(
        mounts=proxy_mounts or None,
        timeout=httpx.Timeout(config["request_timeout"]),
    ) as http_client:
        undo_manager = UndoManager()
        session_manager = SessionManager(workspace_dir / ".foxcode" / "sessions")
        deps = WorkspaceDeps(
            workspace_dir=workspace_dir,
            http_client=http_client,
            undo_manager=undo_manager,
            console=console,
            shell_timeout=config["shell_timeout"],
            project_instructions=project_config["instructions"],
            permissions=perms,
            plan_mode=False,
            skills=skills_mgr,
            subagents=subagents_mgr,
            mcp_toolsets=mcp_toolsets,
            config=config,
        )

        skills_list, subagent_list = _agent_lists(skills_mgr, subagents_mgr)
        agent = create_agent(
            config,
            http_client,
            project_config["instructions"],
            mcp_toolsets=mcp_toolsets,
            skills_list=skills_list,
            subagent_list=subagent_list,
        )

        all_messages = []
        max_history_messages = 50

        terminal_mode = False
        terminal_cwd = workspace_dir
        pending_skill = None

        async def _run_loop():
            nonlocal all_messages, terminal_mode, terminal_cwd, pending_skill
            while True:
                try:
                    if terminal_mode:
                        prompt = console.input(
                            f"[bold yellow]{terminal_cwd}>[/bold yellow] "
                        ).strip()
                    else:
                        prompt = console.input("[bold cyan]>>[/bold cyan] ").strip()
                except (EOFError, KeyboardInterrupt):
                    break

                # Ctrl+X detection: toggle terminal mode
                if "\x18" in prompt:
                    prompt = prompt.replace("\x18", "").strip()
                    terminal_mode = not terminal_mode
                    status_text = "开启" if terminal_mode else "关闭"
                    console.print(
                        f"[yellow]终端模式 {status_text} (Ctrl+X 切换)[/yellow]"
                    )
                    if not prompt:
                        continue

                if not prompt:
                    continue

                if terminal_mode:
                    cmd_text = prompt.strip()
                    if cmd_text == "cd":
                        console.print(str(terminal_cwd))
                        continue
                    elif cmd_text.startswith("cd "):
                        target = cmd_text[3:].strip().strip('"').strip("'")
                        try:
                            new_cwd = Path(target)
                            if not new_cwd.is_absolute():
                                new_cwd = (terminal_cwd / new_cwd).resolve()
                            else:
                                new_cwd = new_cwd.resolve()
                            if new_cwd.is_dir():
                                terminal_cwd = new_cwd
                            else:
                                console.print(f"cd: {target}: 没有那个目录")
                        except (OSError, ValueError):
                            console.print(f"cd: {target}: 没有那个目录")
                        continue

                    console.print(Text(f"  执行: {prompt}", style="dim"))
                    try:
                        subprocess.run(
                            prompt,
                            shell=True,
                            cwd=str(terminal_cwd),
                        )
                    except KeyboardInterrupt:
                        console.print("\n[yellow]命令已中断[/yellow]")
                    except Exception as e:
                        console.print(f"[red]错误: 命令执行失败 - {e}[/red]")
                    continue

                if prompt.startswith("/"):
                    cmd = prompt.strip().lower()
                    if cmd in ("/exit", "/quit"):
                        _save_session(session_manager, all_messages)
                        return
                    elif cmd == "/help":
                        print_help()
                        continue
                    elif cmd == "/plan":
                        deps.plan_mode = not deps.plan_mode
                        perms.plan_mode = deps.plan_mode
                        console.print(
                            f"[yellow]计划模式 {'开启' if deps.plan_mode else '关闭'}[/yellow]"
                        )
                        continue
                    elif cmd == "/permissions":
                        console.print(f"[cyan]{perms.summary()}[/cyan]")
                        continue
                    elif cmd == "/mcp":
                        if mcp_toolsets:
                            console.print(
                                "[cyan]已配置 MCP 服务器:[/cyan]\n  "
                                + "\n  ".join(t.id or "?" for t in mcp_toolsets)
                            )
                        else:
                            console.print(
                                "[yellow]未配置 MCP 服务器（可创建 .foxcode/mcp.json）[/yellow]"
                            )
                        continue
                    elif cmd == "/skills":
                        if skills_mgr.list():
                            table = Table(title="可用 Skills", box=box.SIMPLE)
                            table.add_column("名称", style="cyan")
                            table.add_column("说明", style="white")
                            for s in skills_mgr.list():
                                table.add_row(s.name, s.description)
                            console.print(table)
                        else:
                            console.print(
                                "[yellow]暂无 Skills（可创建 .foxcode/skills/<name>/SKILL.md）[/yellow]"
                            )
                        continue
                    elif cmd.startswith("/skill "):
                        parts = cmd.split(maxsplit=1)
                        if len(parts) < 2:
                            console.print("[yellow]用法: /skill <名称>[/yellow]")
                        else:
                            skill = skills_mgr.get(parts[1])
                            if skill is None:
                                console.print(f"[red]未找到 skill: {parts[1]}[/red]")
                            else:
                                pending_skill = skill.content
                                console.print(
                                    f"[green]已注入 skill: {skill.name}，将在下一条提示生效[/green]"
                                )
                        continue
                    elif cmd == "/agents":
                        if subagents_mgr.list():
                            table = Table(title="可用子代理", box=box.SIMPLE)
                            table.add_column("名称", style="cyan")
                            table.add_column("说明", style="white")
                            for d in subagents_mgr.list():
                                table.add_row(d.name, d.description or "-")
                            console.print(table)
                        else:
                            console.print(
                                "[yellow]暂无子代理（可创建 .foxcode/agents/<name>.md）[/yellow]"
                            )
                        continue
                    elif cmd == "/term":
                        terminal_mode = not terminal_mode
                        status_text = "开启" if terminal_mode else "关闭"
                        console.print(
                            f"[yellow]终端模式 {status_text} (Ctrl+X 切换)[/yellow]"
                        )
                        continue
                    elif cmd == "/clear":
                        console.clear()
                        print_welcome()
                        continue
                    elif cmd == "/history":
                        show_history(deps)
                        continue
                    elif cmd == "/usage":
                        u = deps.tool_tracker.usage_summary(config["model"])
                        console.print(f"[cyan]会话用量统计:[/cyan]\n  {u}")
                        continue
                    elif cmd.startswith("/session"):
                        parts = cmd.split(maxsplit=2)
                        action = parts[1] if len(parts) > 1 else ""
                        if action == "list":
                            sessions = session_manager.list_sessions()
                            if not sessions:
                                console.print("[yellow]暂无保存的会话[/yellow]")
                            else:
                                table = Table(title="已保存的会话", box=box.SIMPLE)
                                table.add_column("名称", style="cyan")
                                table.add_column("消息数", style="white")
                                table.add_column("保存时间", style="dim")
                                for s in sessions:
                                    table.add_row(
                                        s["name"],
                                        str(s["size"])
                                        if isinstance(s["size"], int)
                                        else "?",
                                        s["modified"],
                                    )
                                console.print(table)
                        elif action == "save":
                            name = (
                                parts[2]
                                if len(parts) > 2
                                else session_manager.get_auto_save_name()
                            )
                            result = session_manager.save_session(name, all_messages)
                            console.print(f"[green]{result}[/green]")
                        elif action == "load":
                            name = parts[2] if len(parts) > 2 else ""
                            if not name:
                                console.print(
                                    "[yellow]用法: /session load <名称>[/yellow]"
                                )
                            else:
                                loaded = session_manager.load_session(name)
                                if loaded is None:
                                    console.print(f"[red]未找到会话: {name}[/red]")
                                else:
                                    all_messages.clear()
                                    all_messages.extend(loaded)
                                    console.print(
                                        f"[green]已加载会话: {name} ({len(loaded)} 条消息)[/green]"
                                    )
                        elif action in ("del", "delete", "rm"):
                            name = parts[2] if len(parts) > 2 else ""
                            if not name:
                                console.print(
                                    "[yellow]用法: /session del <名称>[/yellow]"
                                )
                            elif session_manager.delete_session(name):
                                console.print(f"[green]已删除会话: {name}[/green]")
                            else:
                                console.print(f"[red]删除失败: {name}[/red]")
                        else:
                            console.print(
                                "[yellow]用法: /session list|save [名称]|load <名称>|del <名称>[/yellow]"
                            )
                        continue
                    elif cmd.startswith("/export"):
                        parts = prompt.split(maxsplit=1)
                        default_name = (
                            f"session_{session_manager.get_auto_save_name()}.md"
                        )
                        out_name = parts[1] if len(parts) > 1 else default_name
                        out_path = workspace_dir / out_name
                        try:
                            lines = ["# FoxCode 会话导出\n"]
                            for i, msg in enumerate(all_messages, 1):
                                role = "unknown"
                                content = ""
                                if hasattr(msg, "kind"):
                                    if msg.kind == "request":
                                        role = "user"
                                        if hasattr(msg, "parts"):
                                            for part in msg.parts:
                                                if hasattr(part, "content"):
                                                    content += str(part.content)
                                        elif hasattr(msg, "content"):
                                            content = str(msg.content)
                                    elif msg.kind == "response":
                                        role = "assistant"
                                        if hasattr(msg, "parts"):
                                            for part in msg.parts:
                                                if hasattr(part, "content"):
                                                    content += str(part.content)
                                        elif hasattr(msg, "output"):
                                            content = str(msg.output)
                                        elif hasattr(msg, "data"):
                                            content = str(msg.data)
                                elif hasattr(msg, "role"):
                                    role = msg.role
                                    if hasattr(msg, "content"):
                                        content = str(msg.content)
                                elif isinstance(msg, dict):
                                    role = msg.get("role", "unknown")
                                    content = str(
                                        msg.get("content", msg.get("data", ""))
                                    )
                                else:
                                    content = str(msg)
                                if content.strip():
                                    lines.append(f"## [{i}] {role}\n")
                                    lines.append(f"{content.strip()}\n")
                            out_path.write_text("\n".join(lines), encoding="utf-8")
                            console.print(f"[green]会话已导出: {out_path}[/green]")
                        except Exception as e:
                            console.print(f"[red]导出失败: {e}[/red]")
                        continue
                    elif cmd.startswith("/undo"):
                        parts = cmd.split()
                        steps = 1
                        if len(parts) > 1 and parts[1].isdigit():
                            steps = int(parts[1])
                        run_undo(deps, steps)
                        continue
                    elif cmd.startswith("/commit"):
                        parts = cmd.split(maxsplit=1)
                        msg = parts[1] if len(parts) > 1 else ""
                        console.print("[dim]暂存所有变更...[/dim]")
                        add_result = _exec_shell(
                            "git add .", workspace_dir, config["shell_timeout"]
                        )
                        diff = _exec_shell(
                            "git diff --cached",
                            workspace_dir,
                            config["shell_timeout"],
                        )
                        if "退出码" in diff and "没有" not in add_result:
                            console.print(f"[red]git add 失败: {add_result}[/red]")
                            continue
                        if not diff.strip() or "退出码" in diff:
                            console.print("[yellow]没有检测到变更，无需提交[/yellow]")
                            continue
                        if msg:
                            result = _exec_shell(
                                f'git commit -m "{msg}"',
                                workspace_dir,
                                config["shell_timeout"],
                            )
                            console.print(f"[green]{result}[/green]")
                        else:
                            stat = _exec_shell(
                                "git diff --cached --stat",
                                workspace_dir,
                                config["shell_timeout"],
                            )
                            console.print(f"[cyan]变更文件:[/cyan]\n{stat}")
                            with console.status(
                                "[yellow]AI 正在生成提交信息...[/yellow]"
                            ):
                                ai_msg = await _generate_commit_message(
                                    http_client, config, diff
                                )
                            if ai_msg:
                                console.print(
                                    f"[green]生成提交信息:[/green] [bold]{ai_msg}[/bold]"
                                )
                                result = _exec_shell(
                                    f'git commit -m "{ai_msg}"',
                                    workspace_dir,
                                    config["shell_timeout"],
                                )
                                console.print(f"[green]{result}[/green]")
                            else:
                                console.print(
                                    "[yellow]AI 生成失败，请输入提交信息:[/yellow]"
                                )
                                manual_msg = console.input(
                                    "[bold cyan]提交信息: [/bold cyan]"
                                ).strip()
                                if manual_msg:
                                    result = _exec_shell(
                                        f'git commit -m "{manual_msg}"',
                                        workspace_dir,
                                        config["shell_timeout"],
                                    )
                                    console.print(f"[green]{result}[/green]")
                                else:
                                    console.print("[red]提交已取消[/red]")
                        continue
                    else:
                        custom_found = False
                        for cname, cprompt in project_config["commands"].items():
                            if cmd in (f"/{cname}", f"/{cname} "):
                                prompt = cprompt
                                console.print(f"[dim]执行自定义命令: {cname}[/dim]")
                                custom_found = True
                                break
                        if not custom_found:
                            console.print(
                                f"[red]未知命令: {cmd} (输入 /help 查看可用命令)[/red]"
                            )
                            continue

                try:
                    deps.tool_tracker.reset()
                    console.print("[dim]────────────────────────────────────────[/dim]")

                    send_prompt = prompt
                    if pending_skill:
                        send_prompt = (
                            f"请先阅读以下 skill 内容并严格遵循其中的指导：\n\n"
                            f"---\n{pending_skill}\n---\n\n{prompt}"
                        )
                        pending_skill = None
                    if deps.plan_mode:
                        send_prompt = (
                            "[计划模式] 请只使用只读工具调查，不要修改文件或执行命令。"
                            "调查后请在 ActionPlan 中给出清晰的分步实施计划。\n\n"
                            + send_prompt
                        )

                    all_messages, plan, streamed, full_text = await _run_status_loop(
                        agent, send_prompt, all_messages, deps, config
                    )

                    if len(all_messages) > max_history_messages:
                        cutoff = len(all_messages) - max_history_messages
                        all_messages = all_messages[cutoff:]

                    summary = deps.tool_tracker.summary_str()
                    if summary:
                        console.print(f"  [bold cyan]工具调用: {summary}[/bold cyan]")

                    usage_summary = deps.tool_tracker.usage_summary(config["model"])
                    if usage_summary:
                        console.print(f"  [dim]用量: {usage_summary}[/dim]")

                    if streamed and full_text:
                        console.print(Markdown(full_text))
                    print_action_plan(
                        plan, skip_explanation=bool(streamed and full_text)
                    )

                except UnexpectedModelBehavior as e:
                    console.print(f"[red]API 响应格式错误: {e}[/red]")
                    console.print(
                        "  [yellow]该模型可能不完全兼容 OpenAI 格式。"
                        "请检查 API 地址、密钥是否正确，或尝试其他模型[/yellow]"
                    )
                except ModelHTTPError as e:
                    detail = ""
                    if e.body:
                        detail = str(e.body)[:300]
                    console.print(
                        f"[red]API HTTP 错误 ({e.status_code}) 模型={e.model_name}: {detail}[/red]"
                    )
                except ModelAPIError as e:
                    detail = str(e)
                    seen = {detail}
                    cause = e.__cause__
                    while cause:
                        cause_str = str(cause)
                        if cause_str not in seen:
                            detail += f" -> {cause_str}"
                            seen.add(cause_str)
                        cause = cause.__cause__
                    console.print(f"[red]API 错误: {detail}[/red]")
                except (httpx.RemoteProtocolError, httpx.LocalProtocolError) as e:
                    console.print(
                        f"[red]网络连接错误: 与 API 服务器的连接中断[/red]\n"
                        f"  [yellow]原因: {e}[/yellow]\n"
                        f"  [dim]提示: 请检查网络连接是否稳定，或 API 服务器是否正常运行[/dim]"
                    )
                except Exception as e:
                    console.print(f"[red]错误: {e}[/red]")
                    import traceback

                    console.print(f"[dim]{traceback.format_exc()}[/dim]")

        if mcp_toolsets:
            try:
                async with agent:
                    await _run_loop()
            except Exception as e:
                console.print(
                    f"[red]MCP 初始化失败: {e}[/red]\n"
                    "  [yellow]请检查 .foxcode/mcp.json 配置后重试，或删除该文件禁用 MCP[/yellow]"
                )
        else:
            await _run_loop()


async def main_async():
    args = parse_args()
    if args.version:
        console.print("FoxCode v0.4.0")
        return

    config = load_config()
    if args.cwd:
        config["workspace_dir"] = Path(args.cwd).resolve()

    if not config["api_key"]:
        console.print("[red]错误: 未设置 OPENAI_API_KEY[/red]")
        console.print("请复制 .env.example 为 .env 并填入 API 密钥")
        sys.exit(1)

    if args.prompt or not sys.stdin.isatty():
        # headless 模式
        config, project_config, perms, skills_mgr, subagents_mgr, mcp_toolsets = (
            _build_managers(config)
        )
        try:
            await _run_headless(
                config, perms, skills_mgr, subagents_mgr, mcp_toolsets, args
            )
        finally:
            pass
        return

    await _run_interactive(config, args)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
