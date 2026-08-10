"""FoxCode CLI 的 UI 渲染与输出辅助函数。"""

from __future__ import annotations

import re
import sys
import traceback
from typing import TYPE_CHECKING

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box
from rich.spinner import SPINNERS

if TYPE_CHECKING:
    from .models import ActionPlan, WorkspaceDeps

# NOTE:全局 rich 控制台实例，负责所有终端输出格式化
console = Console()

# NOTE:旋转动画
SPINNERS["fox"] = {"interval": 100, "frames": ["-", "/", "\\", "-"]}


def print_welcome(version: str):
    """打印欢迎面板，展示版本号与常用快捷命令入口。"""
    title = Panel.fit(
        f"[bold cyan]FoxCode Cli[/bold cyan] v{version}\n"
        "[yellow]/help[/yellow] 查看命令  "
        "[yellow]/goal[/yellow] 目标验收  "
        "[yellow]/plan[/yellow] 计划模式  "
        "[yellow]/solo[/yellow] 无人值守  "
        "[yellow]/permissions[/yellow] 权限设置  "
        "[yellow]/mcp[/yellow] MCP 服务  "
        "[yellow]/skills[/yellow] Skills  "
        "[yellow]/agents[/yellow] 子代理  "
        "[yellow]/term[/yellow] 终端模式  "
        "[yellow]/spec[/yellow] 规格说明  "
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
    """打印帮助表格，列出所有内置斜杠命令及其说明。"""
    table = Table(title="可用命令", box=box.SIMPLE)
    table.add_column("命令", style="yellow")
    table.add_column("说明", style="white")
    table.add_row("/help", "显示此帮助")
    table.add_row(
        "/goal <目标>", "设定目标，AI 完成后自动验收，未完成则继续直到确认达成"
    )
    table.add_row("/plan", "切换计划模式（只读探索，先出方案）")
    table.add_row("/spec <需求>", "生成技术规格说明文档（SPEC.md），先出方案再编码")
    table.add_row("/solo", "切换无人值守模式（自动放行，只拦截高危命令）")
    table.add_row("/permissions", "查看当前权限模式与规则")
    table.add_row("/free", "切换到内置免费 API 并选择模型")
    table.add_row("/openai", "切换回 .env 中配置的模型参数")
    table.add_row("/model", "配置模型参数（兼容 OpenAI URL 格式）")
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


def _extract_thinking(text: str) -> tuple[str, str]:
    """从文本中提取 <thinking>...</thinking> 标签内容，分离思考过程与正式回答。"""
    matches = re.findall(r"<thinking>(.*?)</thinking>", text, re.DOTALL)
    if not matches:
        return "", text
    thinking = "\n\n".join(m.strip() for m in matches)
    cleaned = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned.strip())
    return thinking, cleaned


def print_action_plan(plan: "ActionPlan", skip_explanation: bool = False):
    """格式化并打印 AI 返回的 ActionPlan（思考过程、解释文本、修改文件、代码片段）。"""
    console.print()
    if not skip_explanation:
        thinking, explanation = _extract_thinking(plan.explanation)
        if thinking:
            think_panel = Panel(
                Markdown(thinking),
                title="[dim]思考过程[/dim]",
                border_style="grey50",
            )
            console.print(think_panel)
            console.print()
        panel = Panel(
            Markdown(explanation or plan.explanation),
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


def run_undo(deps: "WorkspaceDeps", steps: int = 1):
    """快捷撤销最近 n 步操作并打印结果。"""
    result = deps.undo_manager.undo(deps.workspace_dir, steps)
    console.print(f"[yellow]撤销结果:[/yellow]\n{result}")


def show_history(deps: "WorkspaceDeps"):
    """展示撤销管理器中的最近操作历史。"""
    result = deps.undo_manager.history_summary()
    console.print(f"[cyan]{result}[/cyan]")


def _print_run_error(e: Exception) -> bool:
    """打印 AI 运行错误，返回 True 表示会话可继续。

    临时性 API 错误（超时、连接中断、HTTP 5xx、格式错误等）打印友好提示并返回 True，
    让交互式会话不因一次 API 抖动而崩溃，用户可稍后重试。
    """
    from pydantic_ai.exceptions import (
        ModelAPIError,
        ModelHTTPError,
        UnexpectedModelBehavior,
    )

    if isinstance(e, UnexpectedModelBehavior):
        console.print(f"[red]API 响应格式错误: {e}[/red]")
        if e.__cause__ is not None:
            console.print(f"  [dim]根本原因: {e.__cause__}[/dim]")
        console.print(
            "  [yellow]该模型可能不完全兼容 OpenAI 格式。"
            "请检查 API 地址、密钥是否正确，或尝试其他模型[/yellow]"
        )
        return True
    if isinstance(e, ModelHTTPError):
        detail = ""
        if e.body:
            detail = str(e.body)[:300]
        console.print(
            f"[red]API HTTP 错误 ({e.status_code}) 模型={e.model_name}: {detail}[/red]"
        )
        return True
    if isinstance(e, ModelAPIError):
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
        return True
    if isinstance(e, (httpx.ReadTimeout, httpx.ReadError)):
        console.print(
            f"[red]读取超时: 与服务器的连接读取数据超时[/red]\n"
            f"  [yellow]原因: {e}[/yellow]\n"
            f"  [dim]提示: 可在 .foxcode/settings.json 中调大 request_timeout[/dim]"
        )
        return True
    if isinstance(e, (httpx.RemoteProtocolError, httpx.LocalProtocolError)):
        console.print("[red]网络连接错误: 与 API 服务器的连接中断[/red]")
        return True
    if isinstance(e, (httpx.HTTPStatusError, httpx.TransportError)):
        console.print(f"[red]API 请求错误: {e}[/red]")
        return True
    console.print(f"[red]错误: {e}[/red]")
    console.print(f"[dim]{traceback.format_exc()}[/dim]")
    return True
