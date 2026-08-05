"""Goal 模式：AI 完成工作后，由独立上下文的验收 AI 确认目标是否真正完成。

若验收不通过，则带着验收反馈让主 AI 继续工作，直到确认为止。
"""

from pydantic import BaseModel, Field
from pydantic_ai.usage import UsageLimits

from .models import ToolTracker, WorkspaceDeps
from .permissions import PermissionManager, inherit_permissions
from .subagents import create_subagent_agent


class GoalVerification(BaseModel):
    completed: bool = Field(description="目标是否真正完成")
    reason: str = Field(description="判断依据 / 验收结论")
    gaps: list[str] = Field(
        default_factory=list, description="未完成或不达标的具体事项"
    )


VERIFIER_SYSTEM_PROMPT = """你是一名严格的目标验收员（Goal Verifier）。你的唯一职责是：在完全独立、无偏见的上下文中，客观核验用户设定的目标是否真正完成。

规则：
1. 不要信任主 AI 的自我描述。必须使用只读工具亲自检查工作区的实际文件、运行结果、代码内容。
2. 逐条核对目标：是否所有要求都被满足？是否有遗漏、半成品、占位符、或与目标不符的实现？
3. 只有目标确实 100% 完成时才输出 completed=true；只要有任何一项未完成或存疑，就输出 completed=false。
4. 输出 JSON 结构：
   - completed: bool，是否完成
   - reason: str，简短说明验收结论
   - gaps: string[]，列出所有未完成 / 不达标 / 需要继续处理的具体事项（没有则为空数组）
5. 若 completed=false，gaps 必须给出具体、可执行的改进方向，供主 AI 继续工作。"""


def create_goal_verifier(config: dict, http_client):
    """创建独立上下文的验收 AI（只读、结构化输出）。"""
    return create_subagent_agent(
        config,
        http_client,
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        output_type=GoalVerification,
    )


def _verifier_deps(parent: WorkspaceDeps) -> WorkspaceDeps:
    """为验收 AI 构建隔离的只读 deps（继承父会话权限设置，避免重复询问）。"""
    perms = PermissionManager(
        console=parent.console,
        workspace_dir=parent.workspace_dir,
        tool_tracker=None,
    )
    inherit_permissions(parent, perms)
    perms.subagent_mode = True
    return WorkspaceDeps(
        workspace_dir=parent.workspace_dir,
        http_client=parent.http_client,
        undo_manager=parent.undo_manager,
        console=parent.console,
        tool_tracker=ToolTracker(),
        shell_timeout=parent.shell_timeout,
        project_instructions="",
        permissions=perms,
        plan_mode=False,
        skills=None,
        subagents=None,
        mcp_toolsets=None,
        config=parent.config,
    )


async def verify_goal(
    deps: WorkspaceDeps,
    goal: str,
    work_summary: str,
    verifier_agent=None,
) -> GoalVerification:
    """用独立上下文 AI 验收目标是否完成。"""
    if verifier_agent is None:
        verifier_agent = create_goal_verifier(deps.config, deps.http_client)

    prompt = (
        f"用户设定的目标：\n{goal}\n\n"
        f"主 AI 声称已完成的工作：\n{work_summary}\n\n"
        f"请使用只读工具亲自检查工作区，严格核验目标是否真正完成，并输出结构化结论。"
    )
    # 验收 AI 可能需要大量只读检查（文件/搜索/历史），使用与主 agent 相同的无限请求限制
    result = await verifier_agent.run(
        prompt,
        deps=_verifier_deps(deps),
        usage_limits=UsageLimits(request_limit=None),
    )
    return result.output
