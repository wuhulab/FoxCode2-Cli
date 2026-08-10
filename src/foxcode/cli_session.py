"""FoxCode CLI 的会话管理、Goal 模式与 Headless 模式。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from rich.panel import Panel
from rich.markdown import Markdown

from .cli_ui import console, print_action_plan, _print_run_error
from .cli_git import _show_colored_diff, _track_goal_files
from .cli_agent import _run_with_narration, _run_status_loop

if TYPE_CHECKING:
    from .models import WorkspaceDeps
    from .permissions import PermissionManager


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


# NOTE:解析 .foxcode/foxcode.md：若含 [Command] 头则作为启动命令列表，否则作为默认提示
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
    deps: "WorkspaceDeps",
    config: dict,
    max_iterations: int = 8,
):
    """Goal 模式：执行目标 → 独立验收 AI 确认 → 未完成则继续，直到确认为止。

    每轮先用主 AI 处理目标（可访问完整上下文与全部工具），完成后启动一个
    独立上下文、只读的验收 AI（ai-a）核对工作区真实状态。若验收不通过，
    把验收反馈作为后续指令交给主 AI 继续工作，循环直到验收通过或达到上限。
    """
    from .goal import create_goal_verifier, verify_goal
    from .context_compressor import TokenEstimator, compress_messages

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
    perms: "PermissionManager",
    skills_mgr,
    subagents_mgr,
    mcp_toolsets,
    args,
    project_config: dict,
):
    from .models import WorkspaceDeps, UndoManager
    from .agent import create_agent
    from .cli_agent import RetryClient, _build_proxy_mounts, _agent_lists
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
    if getattr(args, "dangerously_skip_permissions", False):
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
