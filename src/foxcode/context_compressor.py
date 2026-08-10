"""智能上下文压缩：长对话自动摘要，保留关键信息减少 token 消耗。

核心缓存友好策略：
- 保留 message_history 的前缀不变，以最大化 LLM API 的 prompt cache 命中率
- 压缩产生的摘要不再插入 message_history（避免破坏前缀 stability）
- 摘要写入 `.foxcode/.session_context.md`，新回合开始时通过 `inject_context_hint`
  自动提示 AI 读取恢复
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# NOTE:上下文压缩策略参数：保留首尾消息数、触发阈值、摘要长度上限
# 增大 KEEP_FIRST_MESSAGES 以保护更长稳定前缀，提高 API prompt cache 命中率
KEEP_FIRST_MESSAGES = 6  # 保留最开始的 N 条完整消息
KEEP_LAST_MESSAGES = 10  # 保留最近的 N 条完整消息
COMPRESS_THRESHOLD = 30  # 超过此数量时触发压缩（对应首尾保留总量）
SUMMARY_MAX_TOKENS = 500
CONTEXT_FILE_NAME = ".foxcode/.session_context.md"


# NOTE:从 pydantic-ai 各类消息对象中提取可读的文本内容用于摘要
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


# NOTE:按字符数/4 粗略估算消息列表的 token 数，用于判断是否超过上下文阈值
def estimate_message_tokens(messages: list[Any]) -> int:
    """粗略估算消息列表的总 token 数（按字符数 /4 估算）。

    用于判断是否超过 MAX_CONTEXT_TOKENS 阈值，从而强制触发压缩总结。
    """
    total_chars = 0
    for msg in messages:
        total_chars += len(_extract_text(msg))
    return total_chars // 4


# NOTE:增量 token 估算器：仅对新追加消息做序列化，避免压缩后全量重算
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


def _write_session_context(workspace_dir: Path, summary: str) -> bool:
    """将摘要写入隐藏的会话上下文文件。"""
    try:
        ctx_path = workspace_dir / CONTEXT_FILE_NAME
        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        ctx_path.write_text(
            "# Session Context Summary\n\n"
            "This file is auto-generated when the conversation history is compressed. "
            "Read it at the start of a turn if you need to recall earlier decisions, "
            "file changes, or user requirements that are no longer in the active message history.\n\n"
            f"{summary}\n",
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


# NOTE:对长对话历史进行压缩：保留首尾消息，中间部分生成摘要并持久化到文件
async def compress_messages(
    messages: list[Any],
    http_client: httpx.AsyncClient,
    config: dict,
) -> tuple[list[Any], str]:
    """压缩消息历史。

    保留最前面的 KEEP_FIRST_MESSAGES 和最后面的 KEEP_LAST_MESSAGES 条完整消息，
    对中间的消息生成摘要，写入 `.foxcode/.session_context.md`。
    **不再将摘要插入 message_history**，以保留下一条消息之前的所有前缀不变，
    提高 LLM API 的 prompt cache 命中率。

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
        # 摘要失败，回退到简单截断（不插入 summary，直接丢弃中间消息）
        return (
            first_chunk + last_chunk,
            f"上下文压缩失败 ({e})，已移除中间 {len(middle_chunk)} 条消息",
        )

    # 将摘要写入文件，而不是插入 message_history
    workspace_dir = Path(config.get("workspace_dir", "."))
    wrote = _write_session_context(workspace_dir, summary)

    if wrote:
        summary_text = (
            f"中间 {len(middle_chunk)} 条消息已压缩，摘要保存至 {CONTEXT_FILE_NAME}"
        )
    else:
        summary_text = f"中间 {len(middle_chunk)} 条消息已丢弃（摘要文件写入失败）"

    # 新消息列表 = 前缀 + 后缀（前缀与之前有 N 条完全相同，利于 API cache）
    new_messages = first_chunk + last_chunk
    return new_messages, summary_text


def inject_context_hint(
    prompt: str, workspace_dir: Path, all_messages: list[Any]
) -> str:
    """若会话上下文摘要文件存在且消息列表已触发过压缩，在 prompt 前注入恢复提示。

    这帮助 AI 在 message_history 被截断后仍能回顾之前的决策。
    """
    ctx_path = workspace_dir / CONTEXT_FILE_NAME
    if not ctx_path.exists():
        return prompt

    # 只在 message_history 长度表明已丢弃过消息时才提示读取
    # 使用一个略低于阈值的值，确保只要曾经压缩过就会触发
    if len(all_messages) < COMPRESS_THRESHOLD:
        return prompt

    # 避免重复注入（如果 prompt 已经包含读取指令）
    marker = f"Read `{CONTEXT_FILE_NAME}`"
    if marker in prompt or CONTEXT_FILE_NAME in prompt:
        return prompt

    ctx_mtime = ctx_path.stat().st_mtime
    # 如果上下文文件很旧（超过 30 分钟）且消息列表已清空，则不提示
    import time

    if (
        time.time() - ctx_mtime > 1800
        and len(all_messages) <= KEEP_FIRST_MESSAGES + KEEP_LAST_MESSAGES
    ):
        return prompt

    return (
        f"[Context Recovery] The conversation history has been compressed. "
        f"Read `{CONTEXT_FILE_NAME}` first if you need to recall earlier decisions, "
        f"then proceed with the user request.\n\n"
        f"User request:\n{prompt}"
    )
