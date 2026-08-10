from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from rich import box
from rich.spinner import SPINNERS

from .config import (
    load_config,
    load_project_config,
    apply_project_settings,
    BUILTIN_FREE_BASE_URL,
    BUILTIN_FREE_API_KEY,
)

# NOTE:版本号声明
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("foxcode2")
except Exception:
    __version__ = "0.5.1"
from .permissions import PermissionManager
from .tools import run_subprocess

if TYPE_CHECKING:
    from .models import ActionPlan, WorkspaceDeps

# NOTE:旋转动画
SPINNERS["fox"] = {"interval": 100, "frames": ["-", "/", "\\", "-"]}


# NOTE:延迟导入 prompt_toolkit 构建增强输入会话，失败时回退到 input()
def _make_prompt_session(history_file: Path):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
    except ImportError:
        return DummyPromptSession()

    # NOTE:命令自动补全器
    class FoxCodeCompleter(Completer):
        COMMANDS = [
            ("/help", "显示帮助"),
            ("/goal ", "设定目标并自动验收循环"),
            ("/plan", "切换计划模式"),
            ("/solo", "切换无人值守模式"),
            ("/permissions", "查看权限设置"),
            ("/free", "切换到内置免费 API 并选择模型"),
            ("/openai", "切换回 .env 配置的模型"),
            ("/model", "配置模型参数"),
            ("/mcp", "列出 MCP 服务器"),
            ("/skills", "列出可用 Skills"),
            ("/skill ", "加载指定 Skill"),
            ("/agents", "列出可用子代理"),
            ("/spec ", "生成技术规格说明"),
            ("/term", "切换终端模式"),
            ("/clear", "清屏"),
            ("/history", "显示操作历史"),
            ("/usage", "显示用量统计"),
            ("/session list", "列出已保存会话"),
            ("/session save ", "保存当前会话"),
            ("/session load ", "加载指定会话"),
            ("/session del ", "删除指定会话"),
            ("/export ", "导出会话为 Markdown"),
            ("/undo", "撤销最近操作"),
            ("/commit", "智能提交 Git 变更"),
            ("/exit", "退出程序"),
            ("/quit", "退出程序"),
        ]

        # NOTE:放行/以外的内容抓取，触发补全功能
        def get_completions(self, document, complete_event):
            text = document.text
            if not text.startswith("/"):
                return
            for cmd, desc in self.COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)

    # NOTE:属性定义：completer定义一个自动补全，history：历史持久化，multiline：不支持换行
    return PromptSession(
        completer=FoxCodeCompleter(),
        history=FileHistory(str(history_file)),
        multiline=False,
    )


# NOTE:prompt_toolkit 缺失时的降级回退：直接使用标准 input()
class DummyPromptSession:
    """当 prompt_toolkit 不可用时回退到 input()。"""

    def __init__(self, *args, **kwargs):
        pass

    async def prompt_async(self, prompt_text: str = "") -> str:
        return input(prompt_text)


# NOTE:全局 rich 控制台实例，负责所有终端输出格式化
console = Console()


# NOTE:带指数退避重试的 HTTP 客户端，自动处理 403/429/5xx 与网络抖动
class RetryClient(httpx.AsyncClient):
    RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})

    def __init__(self, *args, **kwargs):
        # 为长思考/流式场景优化：连接10s、读取5分钟、写入10s、pool 10s
        # read 超时限制的是两次数据包之间的间隔（非总耗时），5分钟足够长思考模型
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


# NOTE:解析命令行参数，支持 headless、交互式、模型覆盖、权限绕过等多种模式
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
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="直接给出的提示，等价于 -p（headless 模式）",
    )
    parser.add_argument("--cwd", default=None, help="工作目录")
    parser.add_argument("--model", default=None, help="覆盖默认模型")
    parser.add_argument(
        "--free",
        action="store_true",
        help="使用内置免费 API 并选择模型",
    )
    parser.add_argument(
        "-solo",
        "--solo",
        action="store_true",
        help="无人值守模式（自动放行，只拦截高危命令）",
    )
    parser.add_argument(
        "-build",
        "--build",
        action="store_true",
        help="正常模式（默认，为后续功能保留）",
    )
    parser.add_argument(
        "-read",
        "--read",
        action="store_true",
        help="AI 只读模式（为后续功能保留）",
    )
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


# NOTE:打印欢迎面板，展示版本号与常用快捷命令入口
def print_welcome():
    title = Panel.fit(
        f"[bold cyan]FoxCode Cli[/bold cyan] v{__version__}\n"
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


# NOTE:打印帮助表格，列出所有内置斜杠命令及其说明
def print_help():
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


# NOTE:将用户提示中的 @filename 引用替换为文件实际内容（仅展示前 60 行防刷屏）
def _expand_file_refs(prompt: str, workspace_dir: Path) -> str:
    """将 prompt 中的 @filename 替换为文件内容。

    支持两种写法：
    - @filename 单独一行 → 读取文件内容
    - @filename 在行内 → 在行内插入内容说明
    """
    import re

    def _read_file(match: re.Match) -> str:
        filename = match.group(1).strip()
        try:
            from .tools.file_ops import _resolve_safe_path

            filepath = _resolve_safe_path(workspace_dir, filename)
            if filepath.is_file():
                content = filepath.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                if len(lines) > 60:
                    content = "\n".join(lines[:60]) + "\n... (文件过长，仅展示前60行)"
                return f"\n```\n{content}\n```\n"
            return f"\n[文件不存在: {filename}]\n"
        except Exception as e:
            return f"\n[读取失败: {filename} - {e}]\n"

    # 匹配 @filename（空格或行首/行尾分隔）
    pattern = r"(?<![\w/])@([\w\./-]+(?:/\w[\w\./-]*)?)"
    return re.sub(pattern, _read_file, prompt)


# NOTE:解析用户提示中的 Markdown 图片语法，转为 pydantic-ai ImageUrl 供多模态模型使用
def _parse_image_refs(prompt: str, workspace_dir: Path) -> str | list:
    """解析 prompt 中的 Markdown 图片语法 ![alt](path)，提取为 ImageUrl。

    返回 str（无图片）或 list[str | ImageUrl]。
    """
    import base64
    import re
    from pydantic_ai.messages import ImageUrl

    IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

    pattern = r"!\[([^\]]*)\]\(([^\)]+)\)"
    matches = list(re.finditer(pattern, prompt))
    if not matches:
        return prompt

    parts: list = []
    last_end = 0
    for m in matches:
        img_path = m.group(2).strip()
        # 只处理已知图片扩展名
        if not img_path.lower().endswith(IMAGE_EXTS):
            continue
        try:
            from .tools.file_ops import _resolve_safe_path

            filepath = _resolve_safe_path(workspace_dir, img_path)
            if not filepath.is_file():
                continue
            data = filepath.read_bytes()
            ext = filepath.suffix.lower().lstrip(".")
            if ext == "jpg":
                ext = "jpeg"
            media_type = f"image/{ext}"
            b64 = base64.b64encode(data).decode()
            data_url = f"data:{media_type};base64,{b64}"

            # 添加图片前的文本
            text_part = prompt[last_end : m.start()]
            if text_part:
                parts.append(text_part)
            parts.append(ImageUrl(url=data_url, media_type=media_type))
            last_end = m.end()
        except Exception:
            continue

    # 尾部文本
    tail = prompt[last_end:]
    if tail:
        parts.append(tail)

    if not parts:
        return prompt
    return parts


# NOTE:交互模式入口检测未提交 git 变更并自动提示（当前已弃用但保留以备恢复）
def _show_git_status_hint(workspace_dir: Path):
    """如果有未提交的变更，自动提示。"""
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=str(workspace_dir),
        )
        if result.returncode != 0:
            return
        lines = result.stdout.strip().splitlines()
        if not lines:
            return
        # 第一行是分支信息
        branch_line = lines[0] if lines else ""
        changes = [line for line in lines[1:] if line.strip()]
        if not changes:
            return
        console.print(f"[yellow]检测到未提交变更 ({len(changes)} 个文件):[/yellow]")
        for line in changes[:8]:
            console.print(f"  [dim]{line}[/dim]")
        if len(changes) > 8:
            console.print(f"  [dim]... 及其他 {len(changes) - 8} 个文件[/dim]")
        console.print(f"[dim]  分支: {branch_line}[/dim]\n")
    except Exception:
        pass


# NOTE:从 AI 响应中提取 <thinking> 标签内容，分离思考过程与正式回答
def _extract_thinking(text: str) -> tuple[str, str]:
    """从文本中提取 <thinking>...</thinking> 标签内容。"""
    import re

    matches = re.findall(r"<thinking>(.*?)</thinking>", text, re.DOTALL)
    if not matches:
        return "", text
    thinking = "\n\n".join(m.strip() for m in matches)
    # 移除 thinking 标签后的文本
    cleaned = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned.strip())
    return thinking, cleaned


# NOTE:格式化并打印 AI 返回的 ActionPlan（思考过程、解释文本、修改文件、代码片段）
def print_action_plan(plan: ActionPlan, skip_explanation: bool = False):
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


# NOTE:展示本轮修改文件的 git diff --stat 摘要，避免全仓库扫描
async def _show_colored_diff(workspace_dir: Path, files: list[str]):
    """展示有修改的文件的 diff 摘要。"""
    if not files:
        return
    # 只对本次修改的文件做 git diff，避免整仓库扫描；
    # 使用参数列表形式避免文件名被解释为 shell 命令。
    stat = await _exec_shell_args(
        ["git", "diff", "--stat", "--"] + files, workspace_dir, timeout=10
    )
    if (
        stat
        and "退出码" not in stat
        and stat.strip()
        and stat.strip() != "(命令执行成功，无输出)"
    ):
        console.print(f"  [dim]变更摘要:\n{stat}[/dim]")


# NOTE:快捷撤销最近 n 步操作并打印结果
def run_undo(deps: WorkspaceDeps, steps: int = 1):
    result = deps.undo_manager.undo(deps.workspace_dir, steps)
    console.print(f"[yellow]撤销结果:[/yellow]\n{result}")


# NOTE:展示撤销管理器中的最近操作历史
def show_history(deps: WorkspaceDeps):
    result = deps.undo_manager.history_summary()
    console.print(f"[cyan]{result}[/cyan]")


# NOTE:字符串命令入口，底层转接为参数列表形式并启用 shell 解析
async def _exec_shell(command: str, cwd: Path, timeout: int = 120) -> str:
    return await _exec_shell_args(command, cwd, timeout, shell=True)


# NOTE:底层命令执行器：优先使用参数列表（shell=False）防止注入，超时后返回错误文本
async def _exec_shell_args(
    args: list[str] | str, cwd: Path, timeout: int = 120, shell: bool = False
) -> str:
    """执行命令并返回结果文本。

    参数列表形式（shell=False）不经 shell，避免参数内容被解释为 shell 命令。
    """
    try:
        result = await run_subprocess(
            args,
            shell=shell,
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
    except TimeoutError:
        return f"错误: 命令执行超时 ({timeout}秒)"
    except Exception as e:
        return f"错误: 命令执行失败 - {e}"


# NOTE:调用独立 API 请求为暂存区变更生成符合 Conventional Commits 规范的提交信息
async def _generate_commit_message(
    http_client: httpx.AsyncClient, config: dict, diff: str
) -> str:
    diff_stat = await _exec_shell("git diff --cached --stat", config["workspace_dir"])
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
    except Exception:
        return ""


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


# NOTE:初始化会话所需的全部管理器：权限、Skills、子代理、MCP（项目配置设​​置覆盖）
def _build_managers(config: dict):
    """构建权限、skills、子代理、MCP 等运行时组件。"""
    from .skills import SkillsManager
    from .subagents import SubAgentManager
    from .mcp_manager import load_mcp_toolsets

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


# NOTE:底层强制流式请求（避免长思考被中间代理截断），前台仍按非流式方式展示结果
async def _run_with_narration(
    agent,
    prompt: str | Sequence[Any] | None,
    all_messages: list,
    deps: WorkspaceDeps,
    config: dict,
    status=None,
):
    """底层强制流式运行 agent，前台非流式输出结果。

    对每个 ModelRequestNode 走 stream() 路径并完整 drain，确保 API 请求始终使用
    stream=true，避免长思考/长响应被中间代理或网关半途截断。工具调用间隙的旁白/
    思考文本仍按原逻辑打印。返回 (all_messages, plan, usage)。
    """
    from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart
    from pydantic_ai.usage import UsageLimits
    from pydantic_ai._agent_graph import ModelRequestNode
    from pydantic_graph import End

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
                # 强制走 stream 路径并完整 drain，不在前台逐字输出
                async with next_node.stream(agent_run.ctx) as agent_stream:
                    await agent_stream.drain()
                # stream 完成后 _result 已设置，再让 graph 继续推进
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


# NOTE:分类打印 AI 运行时错误（API 格式错误、HTTP 错误、超时、连接中断等），可继续时返回 True
# NOTE:区分各类 API/网络错误并给出友好提示，返回 True 表示会话可继续而非直接崩溃
# NOTE:错误分类处理器：区分临时性 API 抖动与致命错误，尽量保持交互会话不中断
def _print_run_error(e: Exception) -> bool:
    """打印 AI 运行错误，返回 True 表示会话可继续。

    临时性 API 错误（超时、连接中断、HTTP 5xx、格式错误等）打印友好提示并返回 True，
    让交互式会话不因一次 API 抖动而崩溃，用户可稍后重试。
    """
    import traceback

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


# NOTE:包装单次 agent 运行的状态循环：启动 spinner 更新器、处理审批暂停、收集结果
async def _run_status_loop(
    agent,
    prompt: str | Sequence[Any] | None,
    all_messages: list,
    deps: WorkspaceDeps,
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


# NOTE:Goal 模式持久化协议提示：指导 AI 在上下文可能被压缩时通过 goal.md/plan.md/todo.md 保持进度
GOAL_PERSIST_INSTRUCTION = """You are in /goal mode. Context may be auto-compressed, so to keep working without losing progress, strictly follow this persistence protocol:

1. Maintain three persistent files in the workspace root (create them if missing; read then update if they exist):
   - goal.md  records: the goal definition, acceptance criteria, current completion status, done/not-done items
   - plan.md  records: the overall implementation plan, current phase approach, key decisions and reasons
   - todo.md  records: the task checklist, unfinished items marked with `- [ ]`, finished items with `- [x]`, each with a short note

2. At the start of each round, read these three files first and continue based on their content (especially after the conversation history is compressed — these files are your only reliable memory).

3. After each important step, immediately update the corresponding file so they always reflect the latest progress. Keep it concise but complete.

4. In your final ActionPlan, state which persistent files you updated and what phase you are in.

Do not overthink this: just keep the files accurate and move on."""


# NOTE:Goal 检查点：将 goal.md/plan.md/todo.md 自动提交到 git，便于断点续作
async def _track_goal_files(workspace_dir: Path, iteration: int) -> str:
    """将 goal 持久化文件 (goal.md/plan.md/todo.md) 提交到 git 作为进度检查点。

    仅当目录是 git 仓库且这些文件有变更时才提交，不影响其他工作区文件。
    返回提交结果摘要；非 git 仓库或无变更时返回空串。
    """
    if not (workspace_dir / ".git").exists():
        return ""
    check = await _exec_shell("git rev-parse --is-inside-work-tree", workspace_dir, 10)
    if "true" not in check:
        return ""
    files = [
        f for f in ("goal.md", "plan.md", "todo.md") if (workspace_dir / f).exists()
    ]
    if not files:
        return ""
    status = await _exec_shell_args(
        ["git", "status", "--short", "--"] + files, workspace_dir, 10
    )
    if (
        not status.strip()
        or "退出码" in status
        or "没有" in status
        or "无输出" in status
    ):
        return ""
    add = await _exec_shell_args(["git", "add", "--"] + files, workspace_dir, 10)
    if "退出码" in add:
        return ""
    msg = f"goal: 第 {iteration} 轮进度 (goal.md/plan.md/todo.md)"
    result = await _exec_shell_args(
        ["git", "commit", "-m", msg, "--"] + files, workspace_dir, 10
    )
    return result


# NOTE:解析 .foxcode/foxcode.md：若含 [Command] 头则作为启动命令列表，否则作为默认提示
# NOTE:加载用户自定义的默认启动文件，支持命令列表或默认提示两种模式
def _parse_foxcode_md(workspace_dir: Path) -> tuple[str | None, list[str]]:
    """解析 .foxcode/foxcode.md。

    - 以 [Command] 开头：返回 (None, [命令列表])，每行 `/xxx` 作为启动命令
    - 其他内容：返回 (内容, [])，整个文件作为默认提示
    文件不存在或为空返回 (None, [])。
    """
    foxcode_md = workspace_dir / ".foxcode" / "foxcode.md"
    if not foxcode_md.exists():
        return None, []
    try:
        raw = foxcode_md.read_text(encoding="utf-8").strip()
    except Exception:
        return None, []
    if not raw:
        return None, []
    if raw.startswith("[Command]"):
        commands = [
            line.strip()
            for line in raw.splitlines()[1:]
            if line.strip() and line.strip().startswith("/")
        ]
        return None, commands
    return raw, []


# NOTE:Goal 模式主循环：执行 → 验收 → 反馈迭代，支持上下文压缩与 git 检查点
async def _run_goal_loop(
    agent,
    goal: str,
    all_messages: list,
    deps: WorkspaceDeps,
    config: dict,
    max_iterations: int = 8,
):
    """Goal 模式：执行目标 → 独立验收 AI 确认 → 未完成则继续，直到确认为止。

    每轮先用主 AI 处理目标（可访问完整上下文与全部工具），完成后启动一个
    独立上下文、只读的验收 AI（ai-a）核对工作区真实状态。若验收不通过，
    把验收反馈作为后续指令交给主 AI 继续工作，循环直到验收通过或达到上限。
    """
    from .goal import create_goal_verifier, verify_goal
    from .context_compressor import TokenEstimator

    verifier = create_goal_verifier(config, deps.http_client)
    max_context_tokens = config.get("max_context_tokens", 100000)
    token_estimator = TokenEstimator()

    for iteration in range(1, max_iterations + 1):
        console.print()
        console.print(
            Panel(
                Markdown(f"## 目标执行 第 {iteration} 轮\n\n{goal}"),
                title="[bold cyan]/goal[/bold cyan]",
                border_style="cyan",
            )
        )

        deps.tool_tracker.reset()
        work_prompt = (
            f"Complete the following goal:\n\n{goal}\n\n{GOAL_PERSIST_INSTRUCTION}"
        )
        # 若会话历史已压缩，提示 AI 读取持久化上下文摘要
        from .context_compressor import inject_context_hint

        work_prompt = inject_context_hint(work_prompt, deps.workspace_dir, all_messages)
        try:
            all_messages, plan = await _run_status_loop(
                agent, work_prompt, all_messages, deps, config
            )
        except Exception as e:
            _print_run_error(e)
            return all_messages

        from .context_compressor import compress_messages

        if (
            len(all_messages) > 50
            or token_estimator.estimate(all_messages) > max_context_tokens
        ):
            try:
                with console.status("[dim]智能压缩上下文中...[/dim]", spinner="fox"):
                    all_messages, summary_text = await compress_messages(
                        all_messages, deps.http_client, config
                    )
            except Exception as e:
                _print_run_error(e)
                console.print(
                    "  [yellow]上下文压缩失败，将跳过压缩继续本轮验收[/yellow]"
                )
                summary_text = ""
            if summary_text:
                console.print(
                    f"  [dim]{summary_text} (持久化文件 goal.md/plan.md/todo.md 可用于恢复进度)[/dim]"
                )

        console.print(
            f"  [bold cyan]工具调用: {deps.tool_tracker.summary_str()}[/bold cyan]"
        )
        print_action_plan(plan)

        track_result = await _track_goal_files(deps.workspace_dir, iteration)
        if track_result:
            console.print(f"  [dim]git 已追踪本轮 goal 文件: {track_result}[/dim]")

        console.print()
        console.print("[yellow]正在启动独立上下文验收 AI 核验目标完成情况...[/yellow]")
        work_summary = f"目标: {goal}\n\n主 AI 说明: {plan.explanation}\n" + (
            f"修改的文件: {', '.join(plan.files_modified)}\n"
            if plan.files_modified
            else ""
        )
        try:
            verification = await verify_goal(deps, goal, work_summary, verifier)
        except Exception as e:
            console.print(f"[red]验收失败: {e}[/red]")
            console.print("[yellow]请人工确认目标是否完成。[/yellow]")
            return all_messages

        if verification.completed:
            console.print(
                Panel(
                    Markdown(
                        f"## 目标已确认完成 ✓\n\n**验收结论**: {verification.reason}"
                    ),
                    title="[bold green]验收通过[/bold green]",
                    border_style="green",
                )
            )
            return all_messages

        console.print(
            Panel(
                Markdown(
                    f"## 目标尚未完成\n\n**验收结论**: {verification.reason}\n\n"
                    "**未达标事项**:\n"
                    + "\n".join(f"- {g}" for g in verification.gaps)
                    or "*（验收 AI 未给出具体缺口）*"
                ),
                title="[bold yellow]验收未通过，继续工作[/bold yellow]",
                border_style="yellow",
            )
        )

        gaps_text = "\n".join(f"- {g}" for g in verification.gaps)
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        all_messages.append(
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content=(
                            "An independent verification AI determined the goal above is not yet complete. "
                            "Keep working according to the following feedback until the goal is actually achieved:\n\n"
                            f"Verdict: {verification.reason}\n"
                            f"Outstanding items:\n{gaps_text}"
                        )
                    )
                ]
            )
        )

    console.print(f"[red]达到最大迭代次数 ({max_iterations})，目标仍未确认完成。[/red]")
    console.print("[yellow]请人工介入确认，或再次执行 /goal 继续。[/yellow]")
    return all_messages


# NOTE:退出时自动保存当前会话来避免数据丢失
def _save_session(session_manager, all_messages: list):
    if all_messages:
        name = session_manager.get_auto_save_name()
        session_manager.save_session(name, all_messages)
        console.print(f"[dim]会话已自动保存: {name}[/dim]")


# NOTE:headless 模式：单次执行给定提示后退出，支持 JSON/text 两种输出格式
async def _run_headless(
    config: dict,
    perms: PermissionManager,
    skills_mgr,
    subagents_mgr,
    mcp_toolsets,
    args,
    project_config: dict,
):
    from .models import WorkspaceDeps, UndoManager
    from .agent import create_agent
    from pydantic_ai.exceptions import UnexpectedModelBehavior, ModelHTTPError

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
    proxy_mounts = _build_proxy_mounts(config)

    async with RetryClient(
        mounts=proxy_mounts or None,
        timeout=httpx.Timeout(config["request_timeout"]),
    ) as http_client:
        agent = create_agent(
            config,
            http_client,
            project_config["instructions"],
            mcp_toolsets=mcp_toolsets,
            skills_list=skills_list,
            subagent_list=subagent_list,
            rules=project_config["rules"],
            memory=project_config["memory"],
        )
        deps = WorkspaceDeps(
            workspace_dir=config["workspace_dir"].resolve(),
            http_client=http_client,
            undo_manager=UndoManager(),
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

        all_messages: list = []
        plan = None
        usage = None
        try:
            async with agent:
                all_messages, plan, usage = await _run_with_narration(
                    agent, prompt.strip(), all_messages, deps, config
                )
        except Exception as e:
            if mcp_toolsets:
                console.print(f"[yellow]⚠ MCP 初始化失败: {e}[/yellow]")
                console.print("[dim]  已自动禁用 MCP，继续运行...[/dim]")
                agent = create_agent(
                    config,
                    http_client,
                    project_config["instructions"],
                    mcp_toolsets=None,
                    skills_list=skills_list,
                    subagent_list=subagent_list,
                    rules=project_config["rules"],
                    memory=project_config["memory"],
                )
                try:
                    async with agent:
                        all_messages, plan, usage = await _run_with_narration(
                            agent, prompt.strip(), all_messages, deps, config
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
            else:
                console.print(f"[red]错误: {e}[/red]", stderr=True)
                return

        if plan is None:
            return

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


# NOTE:交互模式主入口：构建 Agent、初始化会话、启动命令读取与处理主循环
async def _run_interactive(config: dict, args):
    from .models import WorkspaceDeps, UndoManager
    from .agent import create_agent
    from .session import SessionManager
    from .context_compressor import TokenEstimator

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
    if args.solo:
        perms.solo_mode = True

    console.print("FoxCode")
    console.print(f"[dim]工作目录: {workspace_dir} | 模型: {config['model']}[/dim]")
    if project_config["instructions"]:
        console.print(
            f"[dim]项目指南: .foxcode/instructions.md 已加载 ({len(project_config['instructions'])} 字符)[/dim]"
        )
    if project_config["rules"]:
        console.print(
            f"[dim]用户规则: .foxcode/Rules.md 已加载 ({len(project_config['rules'])} 字符, AI 只读)[/dim]"
        )
    if project_config["memory"]:
        console.print(
            f"[dim]项目记忆: .foxcode/Memory.md 已加载 ({len(project_config['memory'])} 字符)[/dim]"
        )
    if mcp_toolsets:
        console.print(f"[dim]MCP 服务器: {len(mcp_toolsets)} 个已配置[/dim]")
    console.print()

    # 展示未提交的 git 变更 (弃用，请勿删除)
    # _show_git_status_hint(workspace_dir)

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
            rules=project_config["rules"],
            memory=project_config["memory"],
        )

        all_messages = []
        max_history_messages = 50
        max_context_tokens = config.get("max_context_tokens", 100000)
        token_estimator = TokenEstimator()

        terminal_mode = False
        terminal_cwd = workspace_dir
        pending_skill = None

        # 初始化 prompt_toolkit session
        history_file = workspace_dir / ".foxcode" / "history"
        pt_session = _make_prompt_session(history_file)

        # 自动加载默认命令文件
        # 若文件以 [Command] 开头：其后每一行作为命令在启动时逐条执行
        # 否则整个文件内容作为默认提示发送给 AI
        default_prompt, startup_commands = _parse_foxcode_md(workspace_dir)
        if startup_commands:
            console.print(
                f"[dim]已加载默认命令: .foxcode/foxcode.md "
                f"({len(startup_commands)} 条启动命令)[/dim]"
            )
        elif default_prompt:
            console.print(
                f"[dim]已加载默认提示: .foxcode/foxcode.md "
                f"({len(default_prompt)} 字符)[/dim]"
            )

        async def _run_loop():
            nonlocal \
                all_messages, \
                terminal_mode, \
                terminal_cwd, \
                pending_skill, \
                default_prompt, \
                agent
            while True:
                try:
                    if startup_commands:
                        prompt = startup_commands.pop(0)
                        console.print(Text(f">> {prompt}", style="dim"))
                    elif default_prompt is not None:
                        prompt = default_prompt
                        default_prompt = None
                    else:
                        if terminal_mode:
                            prompt_text = f"{terminal_cwd}> "
                        else:
                            prompt_text = ">> "
                        prompt = (await pt_session.prompt_async(prompt_text)).strip()
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
                    elif cmd.startswith("/goal"):
                        goal_text = (
                            prompt.split(maxsplit=1)[1]
                            if len(prompt.split(maxsplit=1)) > 1
                            else ""
                        )
                        if not goal_text:
                            goal_text = console.input(
                                "[bold cyan]请输入目标: [/bold cyan]"
                            ).strip()
                        if not goal_text:
                            console.print("[yellow]已取消（目标为空）[/yellow]")
                            continue
                        try:
                            all_messages = await _run_goal_loop(
                                agent, goal_text, all_messages, deps, config
                            )
                        except Exception as e:
                            _print_run_error(e)
                            console.print(
                                "  [yellow]目标执行因错误中断，已保留当前进度，"
                                "可稍后再次执行 /goal 继续[/yellow]"
                            )
                        continue
                    elif cmd == "/plan":
                        deps.plan_mode = not deps.plan_mode
                        perms.plan_mode = deps.plan_mode
                        console.print(
                            f"[yellow]计划模式 {'开启' if deps.plan_mode else '关闭'}[/yellow]"
                        )
                        continue
                    elif cmd.startswith("/spec"):
                        parts = prompt.split(maxsplit=1)
                        spec_query = parts[1] if len(parts) > 1 else ""
                        if not spec_query:
                            # 无参数时读取并显示当前 SPEC.md
                            spec_path = workspace_dir / ".foxcode" / "SPEC.md"
                            if spec_path.exists():
                                content = spec_path.read_text(encoding="utf-8")
                                console.print(
                                    Panel(
                                        Markdown(content),
                                        title="[bold cyan]当前规格说明[/bold cyan]",
                                        border_style="cyan",
                                    )
                                )
                            else:
                                console.print(
                                    "[yellow]暂无规格说明文件 (.foxcode/SPEC.md)，"
                                    "可使用 /spec <需求描述> 生成[/yellow]"
                                )
                        else:
                            # 有参数时进入 spec 生成流程
                            spec_prompt = (
                                "[Spec mode] STRICT RULES:\n"
                                "1. FIRST investigate the codebase using read-only tools "
                                "(tree, read_file, search_symbols, etc.) to understand existing structure.\n"
                                "2. THEN produce a structured technical specification following this template:\n"
                                "   - 1. Requirements Overview (background, goals, scope)\n"
                                "   - 2. Technical Solution (stack, architecture, data flow)\n"
                                "   - 3. API Design (endpoints, parameters, auth)\n"
                                "   - 4. Data Model (entities, schema, state machine)\n"
                                "   - 5. Implementation Steps (phases, risks, rollback)\n"
                                "   - 6. Test Plan (unit/integration/performance coverage)\n"
                                "   - 7. Acceptance Criteria (checklist, non-functional metrics, deliverables)\n"
                                "3. You MUST call `generate_spec` to save the spec to .foxcode/SPEC.md. "
                                "Only writing to explanation is NOT enough — the user needs a saved file.\n"
                                "4. Do NOT write any implementation code, tests, or config files yet. "
                                "Only produce the specification document.\n\n"
                                f"Requirement: {spec_query}"
                            )
                            send_prompt = _expand_file_refs(spec_prompt, workspace_dir)
                            send_prompt = _parse_image_refs(send_prompt, workspace_dir)
                            try:
                                all_messages, plan = await _run_status_loop(
                                    agent, send_prompt, all_messages, deps, config
                                )
                                summary = deps.tool_tracker.summary_str()
                                if summary:
                                    console.print(
                                        f"  [bold cyan]工具调用: {summary}[/bold cyan]"
                                    )
                                print_action_plan(plan)
                                if plan.files_modified:
                                    await _show_colored_diff(
                                        workspace_dir, plan.files_modified
                                    )
                            except Exception as e:
                                _print_run_error(e)
                        continue
                    elif cmd == "/solo":
                        perms.solo_mode = not perms.solo_mode
                        status_text = "开启" if perms.solo_mode else "关闭"
                        console.print(
                            f"[yellow]无人值守(Solo)模式 {status_text}[/yellow]\n"
                            "  [dim]高危命令仍会自动拦截，其他操作不再询问[/dim]"
                        )
                        continue
                    elif cmd == "/permissions":
                        console.print(f"[cyan]{perms.summary()}[/cyan]")
                        continue
                    elif cmd == "/free":
                        config["base_url"] = BUILTIN_FREE_BASE_URL
                        config["api_key"] = BUILTIN_FREE_API_KEY
                        selected = await _select_model_interactive(
                            config["base_url"],
                            config["api_key"],
                            config.get("model") or "",
                        )
                        if selected:
                            config["model"] = selected
                        settings_path = workspace_dir / ".foxcode" / "settings.json"
                        try:
                            if settings_path.exists():
                                settings = json.loads(
                                    settings_path.read_text(encoding="utf-8")
                                )
                            else:
                                settings = {}
                        except Exception:
                            settings = {}
                        settings["base_url"] = config["base_url"]
                        settings["api_key"] = config["api_key"]
                        settings["model"] = config["model"]
                        try:
                            settings_path.parent.mkdir(parents=True, exist_ok=True)
                            settings_path.write_text(
                                json.dumps(settings, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                        except Exception as e:
                            console.print(f"  [yellow]保存配置失败: {e}[/yellow]")
                        new_agent = create_agent(
                            config,
                            http_client,
                            project_config["instructions"],
                            mcp_toolsets=mcp_toolsets,
                            skills_list=skills_list,
                            subagent_list=subagent_list,
                            rules=project_config["rules"],
                            memory=project_config["memory"],
                        )
                        if mcp_toolsets:
                            try:
                                await new_agent.__aenter__()
                            except Exception as e:
                                console.print(
                                    f"  [yellow]新 Agent MCP 初始化失败: {e}[/yellow]"
                                )
                        agent = new_agent
                        console.print(
                            f"[green]已切换到内置免费 API，当前模型: {config['model']}[/green]"
                        )
                        continue
                    elif cmd == "/openai":
                        env_config = load_config()
                        for key in ("model", "base_url", "api_key"):
                            config[key] = env_config[key]

                        settings_path = workspace_dir / ".foxcode" / "settings.json"
                        try:
                            if settings_path.exists():
                                settings = json.loads(
                                    settings_path.read_text(encoding="utf-8")
                                )
                            else:
                                settings = {}
                        except Exception:
                            settings = {}
                        for key in ("model", "base_url", "api_key"):
                            settings.pop(key, None)
                        try:
                            settings_path.parent.mkdir(parents=True, exist_ok=True)
                            settings_path.write_text(
                                json.dumps(settings, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                        except Exception as e:
                            console.print(f"  [yellow]保存配置失败: {e}[/yellow]")

                        new_agent = create_agent(
                            config,
                            http_client,
                            project_config["instructions"],
                            mcp_toolsets=mcp_toolsets,
                            skills_list=skills_list,
                            subagent_list=subagent_list,
                            rules=project_config["rules"],
                            memory=project_config["memory"],
                        )
                        if mcp_toolsets:
                            try:
                                await new_agent.__aenter__()
                            except Exception as e:
                                console.print(
                                    f"  [yellow]新 Agent MCP 初始化失败: {e}[/yellow]"
                                )
                        agent = new_agent
                        console.print(
                            f"[green]已切换回 .env 配置，当前模型: {config['model']}[/green]"
                        )
                        continue
                    elif cmd == "/model":
                        console.print(
                            "[dim]兼容 openai-url 配置，直接回车保留原参数[/dim]"
                        )
                        new_url = console.input(
                            f"[bold cyan]API URL [/bold cyan][dim]({config.get('base_url', '')})[/dim]: "
                        ).strip()
                        new_key = console.input(
                            "[bold cyan]API Key [/bold cyan][dim](留空保留原值)[/dim]: "
                        ).strip()
                        new_model = console.input(
                            f"[bold cyan]模型名称 [/bold cyan][dim]({config.get('model', '')})[/dim]: "
                        ).strip()

                        settings_path = workspace_dir / ".foxcode" / "settings.json"
                        try:
                            if settings_path.exists():
                                settings = json.loads(
                                    settings_path.read_text(encoding="utf-8")
                                )
                            else:
                                settings = {}
                        except Exception:
                            settings = {}

                        updated = False
                        if new_url:
                            config["base_url"] = new_url
                            settings["base_url"] = new_url
                            updated = True
                        if new_key:
                            config["api_key"] = new_key
                            settings["api_key"] = new_key
                            updated = True
                        if new_model:
                            config["model"] = new_model
                            settings["model"] = new_model
                            updated = True

                        if updated:
                            try:
                                settings_path.parent.mkdir(parents=True, exist_ok=True)
                                settings_path.write_text(
                                    json.dumps(settings, ensure_ascii=False, indent=2),
                                    encoding="utf-8",
                                )
                                console.print("[green]已保存模型配置[/green]")
                            except Exception as e:
                                console.print(f"[red]保存失败: {e}[/red]")
                        else:
                            console.print("[dim]未修改任何参数[/dim]")
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
                            skill_name = parts[1].strip().split()[0]
                            skill = skills_mgr.get(skill_name)
                            if skill is None:
                                console.print(f"[red]未找到 skill: {skill_name}[/red]")
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
                            raw_parts = prompt.split(maxsplit=2)
                            name = (
                                raw_parts[2]
                                if len(raw_parts) > 2
                                else session_manager.get_auto_save_name()
                            )
                            result = session_manager.save_session(name, all_messages)
                            console.print(f"[green]{result}[/green]")
                        elif action == "load":
                            raw_parts = prompt.split(maxsplit=2)
                            name = raw_parts[2] if len(raw_parts) > 2 else ""
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
                            raw_parts = prompt.split(maxsplit=2)
                            name = raw_parts[2] if len(raw_parts) > 2 else ""
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
                        try:
                            from .tools.file_ops import _resolve_safe_path

                            out_path = _resolve_safe_path(workspace_dir, out_name)
                        except ValueError as e:
                            console.print(f"[red]导出路径非法: {e}[/red]")
                            continue
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
                        add_result = await _exec_shell(
                            "git add .", workspace_dir, config["shell_timeout"]
                        )
                        diff = await _exec_shell(
                            "git diff --cached",
                            workspace_dir,
                            config["shell_timeout"],
                        )
                        if "退出码" in add_result and "没有" not in add_result:
                            console.print(f"[red]git add 失败: {add_result}[/red]")
                            continue
                        if (
                            not diff.strip()
                            or "退出码" in diff
                            or diff.strip() == "(命令执行成功，无输出)"
                        ):
                            console.print("[yellow]没有检测到变更，无需提交[/yellow]")
                            continue
                        if msg:
                            result = await _exec_shell_args(
                                ["git", "commit", "-m", msg],
                                workspace_dir,
                                config["shell_timeout"],
                            )
                            console.print(f"[green]{result}[/green]")
                        else:
                            stat = await _exec_shell(
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
                                result = await _exec_shell_args(
                                    ["git", "commit", "-m", ai_msg],
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
                                    result = await _exec_shell_args(
                                        ["git", "commit", "-m", manual_msg],
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
                            if cmd == f"/{cname}" or cmd.startswith(f"/{cname} "):
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

                    send_prompt = _expand_file_refs(prompt, workspace_dir)
                    if pending_skill:
                        send_prompt = (
                            f"Please read the following skill content first and strictly follow its guidance:\n\n"
                            f"---\n{pending_skill}\n---\n\n{send_prompt}"
                        )
                        pending_skill = None
                    if deps.plan_mode:
                        send_prompt = (
                            "[Plan mode] Use only read-only tools to investigate; do not modify files or run commands. "
                            "After investigating, give a clear step-by-step implementation plan in the ActionPlan.\n\n"
                            + send_prompt
                        )

                    # 解析图片引用
                    send_prompt = _parse_image_refs(send_prompt, workspace_dir)

                    # 若会话历史已压缩，自动提示 AI 读取持久化上下文摘要
                    from .context_compressor import inject_context_hint

                    send_prompt = inject_context_hint(
                        send_prompt, workspace_dir, all_messages
                    )

                    all_messages, plan = await _run_status_loop(
                        agent, send_prompt, all_messages, deps, config
                    )

                    from .context_compressor import compress_messages

                    if (
                        len(all_messages) > max_history_messages
                        or token_estimator.estimate(all_messages) > max_context_tokens
                    ):
                        with console.status(
                            "[dim]智能压缩上下文中...[/dim]", spinner="fox"
                        ):
                            all_messages, summary_text = await compress_messages(
                                all_messages, http_client, config
                            )
                        if summary_text:
                            console.print(f"  [dim]{summary_text}[/dim]")

                    summary = deps.tool_tracker.summary_str()
                    if summary:
                        console.print(f"  [bold cyan]工具调用: {summary}[/bold cyan]")

                    print_action_plan(plan)
                    # 展示本轮变更摘要
                    if plan.files_modified:
                        await _show_colored_diff(workspace_dir, plan.files_modified)

                    usage_summary = deps.tool_tracker.usage_summary(config["model"])
                    if usage_summary:
                        console.print(f"  [dim]用量: {usage_summary}[/dim]")

                except Exception as e:
                    _print_run_error(e)

        if mcp_toolsets:
            try:
                async with agent:
                    await _run_loop()
            except Exception as e:
                console.print(f"[yellow]⚠ MCP 初始化失败: {e}[/yellow]\n")
                agent = create_agent(
                    config,
                    http_client,
                    project_config["instructions"],
                    mcp_toolsets=None,
                    skills_list=skills_list,
                    subagent_list=subagent_list,
                    rules=project_config["rules"],
                    memory=project_config["memory"],
                )
                async with agent:
                    await _run_loop()
        else:
            await _run_loop()


# NOTE:异步主入口：解析参数、加载配置、区分 headless 与交互模式并分发执行
async def main_async():
    args = parse_args()
    if args.version:
        console.print(f"FoxCode v{__version__}")
        return

    if args.prompt is None and args.query:
        args.prompt = args.query

    config = load_config()
    if args.cwd:
        config["workspace_dir"] = Path(args.cwd).resolve()

    if args.free:
        config["base_url"] = BUILTIN_FREE_BASE_URL
        config["api_key"] = BUILTIN_FREE_API_KEY

    using_free = config.get("base_url") == BUILTIN_FREE_BASE_URL
    interactive = not (args.prompt or not sys.stdin.isatty())

    if args.model:
        config["model"] = args.model
    elif interactive and (args.free or (using_free and not config.get("model"))):
        selected = await _select_model_interactive(
            config["base_url"], config["api_key"], config.get("model") or ""
        )
        if selected:
            config["model"] = selected

    if not config.get("model"):
        if using_free:
            models = await _fetch_model_list(config["base_url"], config["api_key"])
            config["model"] = models[0] if models else "gpt-4o"
        else:
            config["model"] = "gpt-4o"

    if not config["api_key"]:
        console.print("[red]错误: 未设置 OPENAI_API_KEY[/red]")
        console.print("请复制 .env.example 为 .env 并填入 API 密钥")
        sys.exit(1)

    if args.prompt or not sys.stdin.isatty():
        # headless 模式
        config, project_config, perms, skills_mgr, subagents_mgr, mcp_toolsets = (
            _build_managers(config)
        )
        if args.solo:
            perms.solo_mode = True
        try:
            await _run_headless(
                config,
                perms,
                skills_mgr,
                subagents_mgr,
                mcp_toolsets,
                args,
                project_config,
            )
        finally:
            pass
        return

    await _run_interactive(config, args)


# NOTE:程序入口：捕获 Ctrl+C 优雅退出
def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
