"""多文件批量编辑工具：支持原子性 multi_write、diff 应用、批量创建。"""

import difflib
from pathlib import Path
from typing import Optional

from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator
from .file_ops import _resolve_safe_path, check_protected_write
from .security import check_content_security, format_security_warnings


def _fuzzy_find(
    content: str, old_string: str, threshold: float = 0.85
) -> Optional[tuple[int, int]]:
    """在 content 中寻找与 old_string 最相似的片段，返回 (start, end)。

    使用 difflib.SequenceMatcher 滑动窗口匹配。
    """
    old_lines = old_string.splitlines()
    content_lines = content.splitlines()
    old_len = len(old_lines)
    if old_len == 0:
        return None

    best_ratio = 0.0
    best_start = -1

    for i in range(len(content_lines) - old_len + 1):
        window = content_lines[i : i + old_len]
        ratio = difflib.SequenceMatcher(None, old_lines, window).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_ratio >= threshold and best_start >= 0:
        # 计算字节位置（以实际匹配窗口的内容为准，避免行数与 old_string 不一致时偏移错误）
        start_pos = sum(len(line) + 1 for line in content_lines[:best_start])
        window = content_lines[best_start : best_start + old_len]
        end_pos = start_pos + len("\n".join(window))
        return start_pos, end_pos
    return None


def _apply_fuzzy_replace(
    content: str, old_string: str, new_string: str
) -> tuple[str, bool, str]:
    """尝试精确替换，失败则模糊匹配替换。

    返回 (new_content, success, message)。
    """
    count = content.count(old_string)
    if count == 1:
        return content.replace(old_string, new_string, 1), True, "精确匹配替换成功"
    if count > 1:
        return (
            content,
            False,
            f"找到 {count} 处精确匹配，请提供更多上下文以确保唯一匹配",
        )

    # 尝试模糊匹配
    fuzzy = _fuzzy_find(content, old_string)
    if fuzzy:
        start, end = fuzzy
        new_content = content[:start] + new_string + content[end:]
        return new_content, True, "模糊匹配替换成功"

    return (
        content,
        False,
        "未找到要替换的字符串（精确和模糊匹配均失败），请确认 old_string",
    )


def register(agent):
    @agent.tool(args_validator=permission_validator("multi_write_file"))
    async def multi_write_file(
        ctx: RunContext[WorkspaceDeps],
        edits: list[dict],
        fuzzy: bool = False,
    ) -> str:
        """原子性批量文件编辑。

        每个 edit 包含: {"filename": str, "old_string": str, "new_string": str}
        先验证所有编辑是否合法，再一次性应用。任一失败则全部回滚。
        """
        log_tool(ctx, "multi_write_file", f"{len(edits)} 个编辑")

        if not edits:
            return "错误: edits 列表为空"

        # 安全警告检查
        for edit in edits:
            _warn = check_content_security(edit.get("new_string", ""))
            warning = format_security_warnings(_warn)
            if warning:
                ctx.deps.console.print(warning)

        # 预验证阶段
        validated = []
        for i, edit in enumerate(edits):
            filename = edit.get("filename", "")
            old_string = edit.get("old_string", "")
            new_string = edit.get("new_string", "")

            if not filename:
                return f"错误: 第 {i + 1} 个编辑缺少 filename"
            protected = check_protected_write(filename)
            if protected:
                return f"错误: 第 {i + 1} 个编辑 - {protected}"
            try:
                filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
            except ValueError as e:
                return f"错误: 第 {i + 1} 个编辑路径非法 - {e}"
            if not filepath.exists():
                return f"错误: 第 {i + 1} 个编辑的文件 {filename} 不存在"

            try:
                content = filepath.read_text(encoding="utf-8")
            except Exception as e:
                return f"错误: 读取 {filename} 失败 - {e}"

            # 验证 old_string 是否可匹配
            count = content.count(old_string)
            if count == 0 and not fuzzy:
                return (
                    f"错误: 在 {filename} 中未找到要替换的字符串（第 {i + 1} 个编辑），"
                    "可将 fuzzy 设为 True 启用模糊匹配"
                )
            if count > 1 and not fuzzy:
                return (
                    f"错误: 在 {filename} 中找到 {count} 处匹配（第 {i + 1} 个编辑），"
                    "请提供更多上下文或将 fuzzy 设为 True"
                )

            validated.append(
                {
                    "filepath": filepath,
                    "filename": filename,
                    "old_string": old_string,
                    "new_string": new_string,
                    "original_content": content,
                }
            )

        # 执行阶段 + 回滚记录
        applied = []
        try:
            for item in validated:
                content = item["original_content"]
                old_string = item["old_string"]
                new_string = item["new_string"]
                filepath = item["filepath"]
                filename = item["filename"]

                if fuzzy:
                    new_content, ok, msg = _apply_fuzzy_replace(
                        content, old_string, new_string
                    )
                else:
                    # 预验证阶段已保证非 fuzzy 模式下精确匹配唯一
                    new_content = content.replace(old_string, new_string, 1)
                    ok = True
                    msg = "精确匹配替换成功"

                if not ok:
                    # 回滚已应用的
                    for prev in applied:
                        prev["filepath"].write_text(
                            prev["original_content"], encoding="utf-8"
                        )
                    return f"错误: 编辑 {filename} 失败 - {msg}，已回滚之前所有编辑"

                filepath.write_text(new_content, encoding="utf-8")
                applied.append(
                    {
                        "filepath": filepath,
                        "filename": filename,
                        "original_content": content,
                    }
                )
                ctx.deps.tool_tracker.add_chars(len(new_content))
        except Exception as e:
            for prev in applied:
                prev["filepath"].write_text(prev["original_content"], encoding="utf-8")
            return f"错误: 写入异常 - {e}，已回滚所有编辑"

        # 记录撤销（组合操作）
        for prev in reversed(applied):
            ctx.deps.undo_manager.record(
                "write", prev["filename"], old_content=prev["original_content"]
            )

        files = ", ".join(a["filename"] for a in applied)
        return f"已成功原子性编辑 {len(applied)} 个文件: {files}"

    @agent.tool(args_validator=permission_validator("apply_diff"))
    async def apply_diff(
        ctx: RunContext[WorkspaceDeps],
        diff_text: str,
        fuzzy: bool = False,
    ) -> str:
        """应用 unified diff 格式的补丁到对应文件。

        diff_text 应包含标准 diff 头（--- a/xxx\n+++ b/xxx）。
        支持模糊匹配上下文（fuzzy=True）。
        """
        log_tool(ctx, "apply_diff")

        if not diff_text.strip():
            return "错误: diff_text 为空"

        # 解析 diff
        patches = _parse_unified_diff(diff_text)
        if not patches:
            return "错误: 无法解析 diff 格式，请确保使用 unified diff（--- / +++ / @@）"

        applied = []
        try:
            for patch in patches:
                filename = patch["new_file"]
                # 去掉可能的 a/ b/ 前缀
                if filename.startswith("b/"):
                    filename = filename[2:]

                protected = check_protected_write(filename)
                if protected:
                    return f"错误: {protected}"

                try:
                    filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
                except ValueError as e:
                    return f"错误: 文件 {filename} 路径非法 - {e}"

                if patch.get("is_new"):
                    # 新文件
                    if filepath.exists():
                        return f"错误: 新文件 {filename} 已存在"
                    filepath.parent.mkdir(parents=True, exist_ok=True)
                    content = "\n".join(patch["lines"])
                    if not content.endswith("\n") and patch["lines"]:
                        content += "\n"
                    filepath.write_text(content, encoding="utf-8")
                    ctx.deps.undo_manager.record("create", filename)
                    applied.append(
                        {"filename": filename, "type": "create", "original": None}
                    )
                    continue

                if not filepath.exists():
                    return f"错误: 目标文件 {filename} 不存在"

                original = filepath.read_text(encoding="utf-8")
                new_content, ok, msg = _apply_patch(original, patch, fuzzy)
                if not ok:
                    _rollback_diff(applied, ctx.deps.workspace_dir)
                    return f"错误: 应用 diff 到 {filename} 失败 - {msg}"

                filepath.write_text(new_content, encoding="utf-8")
                ctx.deps.undo_manager.record("write", filename, old_content=original)
                applied.append(
                    {"filename": filename, "type": "write", "original": original}
                )
                ctx.deps.tool_tracker.add_chars(len(new_content))
        except Exception as e:
            _rollback_diff(applied, ctx.deps.workspace_dir)
            return f"错误: 应用 diff 异常 - {e}，已回滚"

        files = ", ".join(p["filename"] for p in applied)
        return f"已成功应用 diff 到 {len(applied)} 个文件: {files}"

    @agent.tool(args_validator=permission_validator("batch_create"))
    async def batch_create(
        ctx: RunContext[WorkspaceDeps],
        files: list[dict],
    ) -> str:
        """批量创建文件。

        每个文件包含: {"filename": str, "content": str}
        原子性操作：任一文件已存在则全部不创建。
        """
        log_tool(ctx, "batch_create", f"{len(files)} 个文件")

        if not files:
            return "错误: files 列表为空"

        # 预检查
        validated = []
        for i, item in enumerate(files):
            filename = item.get("filename", "")
            content = item.get("content", "")
            if not filename:
                return f"错误: 第 {i + 1} 个文件缺少 filename"
            protected = check_protected_write(filename)
            if protected:
                return f"错误: 第 {i + 1} 个文件 - {protected}"
            try:
                filepath = _resolve_safe_path(ctx.deps.workspace_dir, filename)
            except ValueError as e:
                return f"错误: 第 {i + 1} 个文件路径非法 - {e}"
            if filepath.exists():
                return f"错误: 文件 {filename} 已存在（第 {i + 1} 个），全部未创建"

            _warn = check_content_security(content)
            warning = format_security_warnings(_warn)
            if warning:
                ctx.deps.console.print(warning)

            validated.append(
                {"filepath": filepath, "filename": filename, "content": content}
            )

        created = []
        try:
            for item in validated:
                filepath = item["filepath"]
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_text(item["content"], encoding="utf-8")
                ctx.deps.undo_manager.record("create", item["filename"])
                ctx.deps.tool_tracker.add_chars(len(item["content"]))
                created.append(item["filename"])
        except Exception as e:
            for c in created:
                try:
                    cpath = _resolve_safe_path(ctx.deps.workspace_dir, c)
                    if cpath.exists():
                        cpath.unlink()
                except Exception:
                    pass
            return f"错误: 创建文件异常 - {e}，已删除已创建的文件"

        return f"已批量创建 {len(created)} 个文件: {', '.join(created)}"


def _parse_unified_diff(diff_text: str) -> list[dict]:
    """解析 unified diff 文本，返回 patch 列表。"""
    patches = []
    lines = diff_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            old_file = line[4:].split("\t")[0].strip()
            if i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                new_file = lines[i + 1][4:].split("\t")[0].strip()
                i += 2
                hunk_lines = []
                is_new = old_file == "/dev/null"
                while i < len(lines):
                    if lines[i].startswith("--- "):
                        break
                    if lines[i].startswith("@@"):
                        pass
                    elif lines[i].startswith("+"):
                        hunk_lines.append(lines[i][1:])
                    elif lines[i].startswith("-"):
                        pass  # 简单模式下忽略删除行（仅用于整文件替换场景）
                    elif lines[i].startswith(" "):
                        hunk_lines.append(lines[i][1:])
                    elif lines[i] == "\\ No newline at end of file":
                        pass
                    else:
                        hunk_lines.append(lines[i])
                    i += 1
                patches.append(
                    {
                        "old_file": old_file,
                        "new_file": new_file,
                        "lines": hunk_lines,
                        "is_new": is_new,
                    }
                )
                continue
        i += 1
    return patches


def _apply_patch(original: str, patch: dict, fuzzy: bool) -> tuple[str, bool, str]:
    """将解析后的 patch 应用到 original 内容。"""
    if patch.get("is_new"):
        content = "\n".join(patch["lines"])
        if patch["lines"] and not content.endswith("\n"):
            content += "\n"
        return content, True, "新文件"

    # 简单策略：将 patch 中的所有行（包括上下文）拼接后做模糊/精确替换
    # 更完整的 patch 应用需要 hunk 级别处理，这里先实现一个鲁棒的简化版
    target_lines = patch["lines"]
    if not target_lines:
        return original, True, "无内容变更"

    # 策略：在原始文件中寻找与 patch 行列表最接近的连续块
    original_lines = original.splitlines()
    patch_len = len(target_lines)

    best_ratio = 0.0
    best_start = -1
    for i in range(max(0, len(original_lines) - patch_len + 1)):
        window = original_lines[i : i + patch_len]
        ratio = difflib.SequenceMatcher(None, target_lines, window).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    threshold = 0.75 if fuzzy else 1.0
    if best_ratio >= threshold and best_start >= 0:
        # 替换这块
        new_lines = (
            original_lines[:best_start]
            + target_lines
            + original_lines[best_start + patch_len :]
        )
        return "\n".join(new_lines), True, f"匹配度 {best_ratio:.0%}"

    # 回退：精确子串匹配
    patch_text = "\n".join(target_lines)
    if patch_text in original:
        # 但这通常意味着 patch 只包含新增行，无法区分上下文
        # 简单处理：直接返回 patch 内容（假设是整文件覆盖）
        return patch_text, True, "子串精确匹配"

    return original, False, f"无法匹配 patch 内容（最佳匹配度 {best_ratio:.0%}）"


def _rollback_diff(applied: list[dict], workspace_dir: Path):
    """回滚已应用的 diff 操作。"""
    for item in reversed(applied):
        filename = item["filename"]
        try:
            filepath = _resolve_safe_path(workspace_dir, filename)
            if item["type"] == "create":
                if filepath.exists():
                    filepath.unlink()
            elif item["type"] == "write" and item["original"] is not None:
                filepath.write_text(item["original"], encoding="utf-8")
        except Exception:
            pass
