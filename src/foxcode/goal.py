"""Goal 模式：AI 完成工作后，由独立上下文的验收 AI 确认目标是否真正完成。

若验收不通过，则带着验收反馈让主 AI 继续工作，直到确认为止。
"""

from pydantic import BaseModel, Field
from pydantic_ai.usage import UsageLimits

from .models import WorkspaceDeps, fork_workspace_deps
from .subagents import create_subagent_agent


# NOTE:验收 AI 的结构化输出模型：是否完成、依据理由、未完成缺口列表
class GoalVerification(BaseModel):
    completed: bool = Field(description="目标是否真正完成")
    reason: str = Field(description="判断依据 / 验收结论")
    gaps: list[str] = Field(
        default_factory=list, description="未完成或不达标的具体事项"
    )


# NOTE:验收 AI 的系统提示：要求其独立验证、不信任主 AI 自述、必须列出具体缺口
VERIFIER_SYSTEM_PROMPT = """You are a strict Goal Verifier. Your only job is to objectively check, in a fully independent context, whether the user's goal has actually been completed.

Rules:
1. Do not trust the main AI's self-report. You must use read-only tools to inspect the actual files, run results, and code in the workspace yourself.
2. Go through the goal item by item: are all requirements met? Any missing pieces, half-finished work, placeholders, or implementations that diverge from the goal?
3. Only output completed=true when the goal is truly 100% done; output completed=false if anything is unfinished or doubtful.
4. Output the JSON structure:
   - completed: bool, whether the goal is done
   - reason: str, a short verdict
   - gaps: string[], concrete items that are unfinished / not up to standard / still need work (empty array if none)
5. If completed=false, gaps must list specific, actionable directions for the main AI to continue.
6. Do not overthink. Inspect efficiently with the minimum tool calls needed and give a decisive verdict."""


# NOTE:创建专门用于验收目标的只读子代理，返回结构化 GoalVerification
def create_goal_verifier(config: dict, http_client):
    """创建独立上下文的验收 AI（只读、结构化输出）。"""
    return create_subagent_agent(
        config,
        http_client,
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        output_type=GoalVerification,
    )


# NOTE:调用验收 AI 对目标完成情况进行独立核查，返回结构化验收结果
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
        f"User's goal:\n{goal}\n\n"
        f"Work the main AI claims to have done:\n{work_summary}\n\n"
        f"Inspect the workspace yourself with read-only tools, rigorously verify whether the goal is "
        f"actually complete, and output the structured verdict."
    )

    async def _dummy_event_handler(_ctx, stream):
        # 强制底层走 stream 路径，避免长思考被中间代理截断
        async for _ in stream:
            pass

    # 验收 AI 可能需要大量只读检查（文件/搜索/历史），使用与主 agent 相同的无限请求限制
    result = await verifier_agent.run(
        prompt,
        deps=fork_workspace_deps(deps),
        usage_limits=UsageLimits(request_limit=None),
        event_stream_handler=_dummy_event_handler,
    )
    return result.output
