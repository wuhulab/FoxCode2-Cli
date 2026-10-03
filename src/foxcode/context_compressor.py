"""智能上下文压缩：长对话自动摘要，保留关键信息减少 token 消耗。

核心缓存友好策略：
- 保留 message_history 的前缀不变，以最大化 LLM API 的 prompt cache 命中率
- 压缩产生的摘要不再插入 message_history（避免破坏前缀 stability）
- 摘要写入 `.foxcode/.session_context.md`，新回合开始时通过 `inject_context_hint`
  自动提示 AI 读取恢复

超限保护策略：
- token 估算完整统计工具返回与工具参数（不截断），避免巨型工具输出被严重低估
- 压缩后若仍超预算（例如少量消息里夹着巨型工具返回），执行硬裁剪：
  先按档位截断超长工具返回，再从最旧处丢弃消息，保证下一次请求不被 API 以
  “input exceeds the supported context size” 拒绝
"""

import re
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

# NOTE:安全水位：估算 token 达到 max_context_tokens 的该比例即压缩，
# 为模型响应与下一轮输入预留空间（避免刚好卡在上限时被 API 拒绝）
CONTEXT_SAFETY_RATIO = 0.85
# NOTE:硬裁剪时单个工具返回保留的最大字符数，按档位递减直到估算值落入预算
TOOL_RETURN_PRUNE_CAPS = (2000, 500, 120)
# NOTE:硬裁剪兜底：无论怎样都至少保留最后 N 条消息，保证最新上下文可用
HARD_TRIM_MIN_MESSAGES = 2

# NOTE:中日韩文字（含全角标点、假名、谚文）按 1 token/字估算，其余字符按 4 字符/token 估算
_CJK_RE = re.compile(
    r"[\u2e80-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]"
)


# NOTE:估算单段文本的 token 数（中日韩按字、其余按 4 字符/token）
def estimate_text_tokens(text: str) -> int:
    """估算文本 token 数。

    中文语境下 1 个汉字约等于 1 个 token，直接按字符数 /4 会低估约 4 倍，
    导致压缩阈值迟迟不触发，最终被 API 以“上下文超限”拒绝。
    """
    if not text:
        return 0
    cjk = sum(1 for _ in _CJK_RE.finditer(text))
    others = len(text) - cjk
    # ceil(others / 4)
    return cjk + others // 4 + (1 if others % 4 else 0)


# NOTE:从 pydantic-ai 各类消息对象中提取可读的文本内容用于摘要/估算
def _extract_text(msg: Any, max_part_chars: int | None = None) -> str:
    """从 pydantic-ai 消息对象中提取文本内容。

    Args:
        msg: pydantic-ai 消息对象（ModelRequest / ModelResponse / 兼容 dict）。
        max_part_chars: 单个部分（工具返回、工具参数等）的字符上限；
            None 表示完整保留 —— token 估算必须用 None，否则大体积工具输出会被
            截断成固定长度，估算值严重偏低。
    """
    role = "unknown"
    parts_text = []

    def _cut(text: str) -> str:
        if max_part_chars is not None and len(text) > max_part_chars:
            return text[:max_part_chars] + "..."
        return text

    if hasattr(msg, "kind"):
        role = "user" if msg.kind == "request" else "assistant"
        if hasattr(msg, "parts"):
            for part in msg.parts:
                if hasattr(part, "content"):
                    parts_text.append(_cut(str(part.content)))
                elif hasattr(part, "part_kind"):
                    # tool call / tool return
                    if part.part_kind == "tool-call":
                        parts_text.append(
                            f"[调用工具 {part.tool_name}: {_cut(str(part.args))}]"
                        )
                    elif part.part_kind == "tool-return":
                        parts_text.append(
                            f"[工具 {part.tool_name} 返回: {_cut(str(part.content))}]"
                        )
        elif hasattr(msg, "content"):
            parts_text.append(_cut(str(msg.content)))
    elif hasattr(msg, "role"):
        role = msg.role
        if hasattr(msg, "content"):
            parts_text.append(_cut(str(msg.content)))
    elif isinstance(msg, dict):
        role = msg.get("role", "unknown")
        content = msg.get("content", msg.get("data", ""))
        parts_text.append(_cut(str(content)))
    else:
        parts_text.append(_cut(str(msg)))

    return f"[{role}]\n" + "\n".join(parts_text)


# NOTE:估算消息列表的 token 数（完整统计工具返回与参数，不做截断）
def estimate_message_tokens(messages: list[Any]) -> int:
    """粗略估算消息列表的总 token 数。

    用于判断是否超过压缩预算，从而在请求前强制触发压缩/硬裁剪。
    """
    return sum(estimate_text_tokens(_extract_text(msg)) for msg in messages)


# NOTE:计算压缩触发预算：配置上限 × 安全水位，为响应与下一轮输入预留空间
def context_token_budget(config: dict) -> int:
    """返回触发压缩的 token 预算（max_context_tokens × CONTEXT_SAFETY_RATIO）。"""
    try:
        limit = int(config.get("max_context_tokens", 100000) or 100000)
    except (TypeError, ValueError):
        limit = 100000
    return max(int(limit * CONTEXT_SAFETY_RATIO), 1000)


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


def _part_kind(part: Any) -> str:
    """安全获取消息部分类型（tool-call / tool-return / text ...）。"""
    return getattr(part, "part_kind", "") or ""


def _has_part_kind(msg: Any, kind: str) -> bool:
    """判断消息是否包含指定类型的部分。"""
    parts = getattr(msg, "parts", None)
    if not parts:
        return False
    return any(_part_kind(p) == kind for p in parts)


# NOTE:截断超长工具返回，避免单条巨型工具输出撑爆上下文
def _truncate_tool_returns(messages: list[Any], keep_chars: int) -> list[Any]:
    """将消息中超长的工具返回内容截断，返回新列表。

    未发生截断的消息沿用原对象，尽量保持历史前缀稳定（利于 prompt cache）。
    重建消息对象失败时保留原对象，避免破坏历史结构。
    """
    from dataclasses import replace

    result: list[Any] = []
    for msg in messages:
        parts = getattr(msg, "parts", None)
        if not parts:
            result.append(msg)
            continue
        changed = False
        new_parts = []
        for part in parts:
            if _part_kind(part) == "tool-return":
                content = getattr(part, "content", "")
                text = content if isinstance(content, str) else str(content)
                if len(text) > keep_chars:
                    try:
                        new_parts.append(
                            replace(
                                part,
                                content=text[:keep_chars]
                                + f"\n...[已截断，原长 {len(text)} 字符]",
                            )
                        )
                        changed = True
                        continue
                    except Exception:
                        pass
            new_parts.append(part)
        if changed:
            try:
                result.append(replace(msg, parts=new_parts))
                continue
            except Exception:
                pass
        result.append(msg)
    return result


# NOTE:硬裁剪：截断超长工具返回 + 从最旧处丢弃消息，直到估算值落入预算
def _hard_trim(messages: list[Any], budget: int) -> list[Any]:
    """硬裁剪消息列表，保证估算 token 不超过预算。

    步骤：先按档位截断超长工具返回；仍超预算时从最旧处逐条丢弃消息
    （至少保留最后 HARD_TRIM_MIN_MESSAGES 条），并在丢弃后清理孤立的工具返回
    （对应工具调用已被丢弃，孤立的工具返回会被 API 拒绝）。
    """
    result = list(messages)
    if estimate_message_tokens(result) <= budget:
        return result

    for cap in TOOL_RETURN_PRUNE_CAPS:
        result = _truncate_tool_returns(result, cap)
        if estimate_message_tokens(result) <= budget:
            return result

    est = estimate_message_tokens(result)
    while len(result) > HARD_TRIM_MIN_MESSAGES and est > budget:
        est -= estimate_message_tokens([result[0]])  # 逐条扣减，避免重复全量估算
        result.pop(0)
        while len(result) > 1 and _has_part_kind(result[0], "tool-return"):
            est -= estimate_message_tokens([result[0]])
            result.pop(0)
    return result


# NOTE:对中间消息生成摘要，失败返回 None（由调用方按丢弃处理）
async def _summarize(
    messages: list[Any],
    http_client: httpx.AsyncClient,
    config: dict,
) -> str | None:
    """调用模型把中间消息压缩成摘要文本，异常时返回 None。"""
    lines = [
        "Below is the conversation history to be summarized. Use this summary to keep assisting the user:\n"
    ]
    for i, msg in enumerate(messages, 1):
        # 单部分限量 800 字符、单条消息总量再截到 2000 字符，控制摘要请求体积
        text = _extract_text(msg, max_part_chars=800)[:2000]
        lines.append(f"--- message {i} ---\n{text}\n")

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
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except Exception:
        return None


# NOTE:对长对话历史进行压缩：保留首尾消息，中间部分生成摘要并持久化到文件
async def compress_messages(
    messages: list[Any],
    http_client: httpx.AsyncClient,
    config: dict,
    force: bool = False,
    token_budget: int | None = None,
) -> tuple[list[Any], str]:
    """压缩消息历史，必要时硬裁剪，确保压缩结果不超过 token 预算。

    常规路径：保留最前面的 KEEP_FIRST_MESSAGES 和最后面的 KEEP_LAST_MESSAGES 条完整消息，
    对中间的消息生成摘要，写入 `.foxcode/.session_context.md`。
    **不把摘要插入 message_history**，以保持前缀不变、提高 LLM API 的 prompt cache 命中率。

    兜底路径：若压缩后仍超预算（例如少量消息中夹着巨型工具返回），调用 `_hard_trim`
    截断超长工具返回并丢弃最旧消息，避免下一次请求被 API 以“上下文超限”拒绝。

    Args:
        force: True 时跳过“消息数不多就跳过”的默认判断，并把压缩目标收紧为
            当前体积的一半，强制执行一次实质缩减（用于 API 已报上下文超限后的恢复重试）。
        token_budget: 压缩目标 token 预算，默认取 `context_token_budget(config)`。

    返回 (新消息列表, 摘要文本)。
    """
    budget = token_budget if token_budget is not None else context_token_budget(config)
    est = estimate_message_tokens(messages)
    if force:
        # 强制压缩（API 已报超限）：把目标压到当前体积的一半，
        # 即使本地估算未超预算也要产生实质缩减，让重试有机会成功
        # （下限取 200 tokens，避免预算被压到 0 导致裁剪失控）
        budget = min(budget, max(est // 2, 200))
    if not force and len(messages) <= COMPRESS_THRESHOLD and est <= budget:
        return messages, ""

    total = len(messages)
    first_chunk = list(messages[:KEEP_FIRST_MESSAGES])
    last_start = max(total - KEEP_LAST_MESSAGES, KEEP_FIRST_MESSAGES)
    last_chunk = list(messages[last_start:])
    middle_chunk = list(messages[KEEP_FIRST_MESSAGES:last_start])

    notes: list[str] = []

    if middle_chunk:
        summary = await _summarize(middle_chunk, http_client, config)
        if summary is None:
            notes.append(f"上下文摘要生成失败，已移除中间 {len(middle_chunk)} 条消息")
        elif _write_session_context(Path(config.get("workspace_dir", ".")), summary):
            notes.append(
                f"中间 {len(middle_chunk)} 条消息已压缩，摘要保存至 {CONTEXT_FILE_NAME}"
            )
        else:
            notes.append(f"中间 {len(middle_chunk)} 条消息已丢弃（摘要文件写入失败）")

        # 边界修正：避免出现“工具调用已丢弃但保留了工具返回”的孤立消息（会被 API 拒绝）
        while last_chunk and _has_part_kind(last_chunk[0], "tool-return"):
            last_chunk.pop(0)
        # 首段末尾若有工具调用，其返回可能落在被丢弃的中间段，同样需要截掉
        while first_chunk and _has_part_kind(first_chunk[-1], "tool-call"):
            first_chunk.pop()

        # 新消息列表 = 前缀 + 后缀（前缀与之前有 N 条完全相同，利于 API cache）
        new_messages = first_chunk + last_chunk
    else:
        # 消息数不多（无中间段可压缩），可能体积仍然很大：直接走体积裁剪
        new_messages = list(messages)

    if estimate_message_tokens(new_messages) > budget:
        before = estimate_message_tokens(new_messages)
        new_messages = _hard_trim(new_messages, budget)
        notes.append(
            f"压缩后仍超预算（约 {before} tokens），已硬裁剪至约 "
            f"{estimate_message_tokens(new_messages)} tokens"
        )

    return new_messages, "；".join(notes)


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
