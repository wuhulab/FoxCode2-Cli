import asyncio
import sys
from pathlib import Path
import httpx
from httpx import AsyncHTTPTransport
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from rich import box

from .config import load_config
from .models import ActionPlan, WorkspaceDeps, UndoManager
from .agent import create_agent

console = Console()


class RetryClient(httpx.AsyncClient):
    RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})

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
                    console.print(
                        f"  [yellow]服务暂不可用 ({response.status_code})，{wait}秒后重试 (第{retry_count}次)[/yellow]"
                    )
                    await asyncio.sleep(wait)
                    continue

                return response

            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
            ) as e:
                retry_count += 1
                wait = min(15 * retry_count, 120)
                console.print(
                    f"  [yellow]请求异常: {e}，{wait}秒后重试 (第{retry_count}次)[/yellow]"
                )
                await asyncio.sleep(wait)


def print_welcome():
    title = Panel.fit(
        "[bold cyan]FoxCode Cli[/bold cyan] v1.0.0\n"
        "[yellow]/help[/yellow] 查看命令  "
        "[yellow]/undo[/yellow] 撤销操作  "
        "[yellow]/history[/yellow] 操作历史  "
        "[yellow]/clear[/yellow] 清屏  "
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
    table.add_row("/undo [n]", "撤销最近 n 步操作（默认 1 步）")
    table.add_row("/history", "显示操作历史")
    table.add_row("/clear", "清屏")
    table.add_row("/exit 或 /quit", "退出程序")
    console.print(table)


def print_action_plan(plan: ActionPlan):
    console.print()
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


async def run_undo(deps: WorkspaceDeps, steps: int = 1):
    result = deps.undo_manager.undo(deps.workspace_dir, steps)
    console.print(f"[yellow]撤销结果:[/yellow]\n{result}")


async def show_history(deps: WorkspaceDeps):
    result = deps.undo_manager.history_summary()
    console.print(f"[cyan]{result}[/cyan]")


async def main_async():
    print_welcome()

    config = load_config()
    workspace_dir = config["workspace_dir"].resolve()

    if not config["api_key"]:
        console.print("[red]错误: 未设置 OPENAI_API_KEY[/red]")
        console.print("请复制 .env.example 为 .env 并填入 API 密钥")
        sys.exit(1)

    workspace_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]工作目录: {workspace_dir}[/dim]")
    console.print(f"[dim]模型: {config['model']}[/dim]")
    console.print()

    proxy_mounts = {}
    for scheme, proxy_url in [
        ("http://", config["http_proxy"]),
        ("https://", config["https_proxy"]),
    ]:
        if proxy_url:
            proxy_mounts[scheme] = httpx.AsyncHTTPTransport(proxy=proxy_url)

    async with RetryClient(mounts=proxy_mounts or None) as http_client:
        undo_manager = UndoManager()
        deps = WorkspaceDeps(
            workspace_dir=workspace_dir,
            http_client=http_client,
            undo_manager=undo_manager,
            console=console,
            shell_timeout=config["shell_timeout"],
        )
        agent = create_agent(config, http_client)

        all_messages = []

        while True:
            try:
                prompt = console.input(">> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not prompt.strip():
                continue

            if prompt.startswith("/"):
                cmd = prompt.strip().lower()
                if cmd in ("/exit", "/quit"):
                    break
                elif cmd == "/help":
                    print_help()
                    continue
                elif cmd == "/clear":
                    console.clear()
                    print_welcome()
                    continue
                elif cmd == "/history":
                    await show_history(deps)
                    continue
                elif cmd.startswith("/undo"):
                    parts = cmd.split()
                    steps = 1
                    if len(parts) > 1 and parts[1].isdigit():
                        steps = int(parts[1])
                    await run_undo(deps, steps)
                    continue
                else:
                    console.print(
                        f"[red]未知命令: {cmd} (输入 /help 查看可用命令)[/red]"
                    )
                    continue

            try:
                deps.tool_tracker.reset()
                console.print("[dim]────────────────────────────────────────[/dim]")

                spinner_chars = "-/\\-"
                frame = 0

                status_text = Text("")
                live = Live(
                    status_text,
                    refresh_per_second=10,
                    console=console,
                    transient=True,
                )
                live.start()

                async def updater():
                    nonlocal frame
                    try:
                        while True:
                            sp = spinner_chars[frame % len(spinner_chars)]
                            frame += 1
                            line = deps.tool_tracker.status_line(sp)
                            t = Text(line)
                            t.stylize("bold cyan")
                            live.update(t)
                            await asyncio.sleep(0.1)
                    except asyncio.CancelledError:
                        pass

                update_task = asyncio.create_task(updater())
                try:
                    result = await agent.run(
                        prompt,
                        message_history=all_messages,
                        deps=deps,
                    )
                finally:
                    update_task.cancel()
                    live.stop()

                all_messages = result.all_messages()
                plan = result.output

                summary = deps.tool_tracker.summary_str()
                if summary:
                    console.print(f"  [bold cyan]工具调用: {summary}[/bold cyan]")

                print_action_plan(plan)

            except Exception as e:
                console.print(f"[red]错误: {e}[/red]")
                import traceback

                console.print(f"[dim]{traceback.format_exc()}[/dim]")


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
