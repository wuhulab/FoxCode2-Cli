"""FoxCode CLI 的输入处理与提示解析辅助函数。"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Sequence

from pydantic_ai.messages import ImageUrl

from .cli_ui import console


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


class DummyPromptSession:
    """当 prompt_toolkit 不可用时回退到 input()。"""

    def __init__(self, *args, **kwargs):
        pass

    async def prompt_async(self, prompt_text: str = "") -> str:
        return input(prompt_text)


# NOTE:将用户提示中的 @filename 引用替换为文件实际内容（仅展示前 60 行防刷屏）
def _expand_file_refs(prompt: str, workspace_dir: Path) -> str:
    """将 prompt 中的 @filename 替换为文件内容。

    支持两种写法：
    - @filename 单独一行 → 读取文件内容
    - @filename 在行内 → 在行内插入内容说明
    """

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
def _parse_image_refs(prompt: str, workspace_dir: Path) -> str | Sequence:
    """解析 prompt 中的 Markdown 图片语法 ![alt](path)，提取为 ImageUrl。

    返回 str（无图片）或 list[str | ImageUrl]。
    """
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
