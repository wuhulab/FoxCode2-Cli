"""智能上下文压缩：长对话自动摘要，保留关键信息减少 token 消耗。"""

from dataclasses import dataclass
from typing import Any

import httpx


KEEP_FIRST_MESSAGES = 3  # 保留最开始的 N 条完整消息
KEEP_LAST_MESSAGES = 15  # 保留最近的 N 条完整消息
COMPRESS_THRESHOLD = 35  # 超过此数量时触发压缩
SUMMARY_MAX_TOKENS = 500


def _extract_text(msg: Any) -> str:
    """从 pydantic-ai 消息对象中提取文本内容。"""
    role = "unknown"
    parts_text = []

    if hasattr(msg, "kind"):
        role = "user" if msg.kind == "request" else "assistant"
        if hasattr(msg, "parts"):
            for part in msg.parts:
                if hasattr(part, "content"):
                    parts_text.append(str(part.content))
                elif hasattr(part, "part_kind"):
                    # tool call / tool return
                    if part.part_kind == "tool-call":
                        parts_text.append(f"[调用工具 {part.tool_name}: {part.args}]")
                    elif part.part_kind == "tool-return":
                        ret = str(part.content)[:200]
                        parts_text.append(f"[工具 {part.tool_name} 返回: {ret}...]")
        elif hasattr(msg, "content"):
            parts_text.append(str(msg.content))
    elif hasattr(msg, "role"):
        role = msg.role
        if hasattr(msg, "content"):
            parts_text.append(str(msg.content))
    elif isinstance(msg, dict):
        role = msg.get("role", "unknown")
        content = msg.get("content", msg.get("data", ""))
        parts_text.append(str(content))
    else:
        parts_text.append(str(msg))

    return f"[{role}]\n" + "\n".join(parts_text)


def estimate_message_tokens(messages: list[Any]) -> int:
    """粗略估算消息列表的总 token 数（按字符数 /4 估算）。

    用于判断是否超过 MAX_CONTEXT_TOKENS 阈值，从而强制触发压缩总结。
    """
    total_chars = 0
    for msg in messages:
        total_chars += len(_extract_text(msg))
    return total_chars // 4


@dataclass
class TokenEstimator:
    """增量 token 估算器：只对新追加的消息做序列化，避免每轮全量重算。

    当消息列表被压缩/缩短时自动重建基线。
    """

    _msg_count: int = 0
    _char_count: int = 0

    def estimate(self, messages: list[Any]) -> int:
        start = self._msg_count
        if len(messages) < start:
            # 列表被压缩/替换，重建基线
            start = 0
            self._char_count = 0
        for msg in messages[start:]:
            self._char_count += len(_extract_text(msg))
        self._msg_count = len(messages)
        return self._char_count // 4


async def compress_messages(
    messages: list[Any],
    http_client: httpx.AsyncClient,
    config: dict,
) -> tuple[list[Any], str]:
    """压缩消息历史。

    保留最前面的 KEEP_FIRST_MESSAGES 和最后面的 KEEP_LAST_MESSAGES 条完整消息，
    对中间的消息生成摘要，替换为一条 summary 消息。

    返回 (新消息列表, 摘要文本)。
    """
    if len(messages) <= COMPRESS_THRESHOLD:
        return messages, ""

    total = len(messages)
    first_chunk = messages[:KEEP_FIRST_MESSAGES]
    middle_chunk = messages[KEEP_FIRST_MESSAGES : total - KEEP_LAST_MESSAGES]
    last_chunk = messages[total - KEEP_LAST_MESSAGES :]

    if not middle_chunk:
        return messages, ""

    # 将中间的消息转为文本用于摘要
    lines = [
        "Below is the conversation history to be summarized. Use this summary to keep assisting the user:\n"
    ]
    for i, msg in enumerate(middle_chunk, 1):
        text = _extract_text(msg)
        # 截断过长的工具返回
        lines.append(f"--- message {i} ---\n{text[:800]}\n")

    prompt_text = "\n".join(lines)

    try:
        response = await http_client.post(
            f"{config['base_url']}/chat/completions",
            json={
                "model": config["model"],
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a conversation summarizer. Compress the following conversation history "
                            "into a concise summary that preserves all key information: the user's questions, "
                            "the AI's actions, important code changes, decisions, and conclusions. "
                            "Convey the most information with the fewest words. Do not overthink; produce "
                            "the summary directly."
                        ),
                    },
                    {"role": "user", "content": prompt_text},
                ],
                "temperature": 0.1,
                "max_tokens": SUMMARY_MAX_TOKENS,
            },
            headers={"Authorization": f"Bearer {config['api_key']}"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        summary = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        # 摘要失败，回退到简单截断
        return (
            first_chunk + last_chunk,
            f"上下文压缩失败 ({e})，已移除中间 {len(middle_chunk)} 条消息",
        )

    # 构造 summary 消息——使用 pydantic-ai 的 ModelRequest 保证类型安全
    from pydantic_ai.messages import ModelRequest, SystemPromptPart

    summary_msg = ModelRequest(
        parts=[SystemPromptPart(content=f"[上下文摘要] 之前对话的关键信息:\n{summary}")]
    )

    new_messages = first_chunk + [summary_msg] + last_chunk
    return new_messages, summary
