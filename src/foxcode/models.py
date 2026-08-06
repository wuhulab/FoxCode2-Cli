from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel
import httpx
from rich.console import Console


MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-3-opus-20240229": (15.00, 75.00),
    "claude-4-sonnet-20250514": (3.00, 15.00),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-pro": (2.00, 5.00),
}


def estimate_cost(
    model_name: str, input_tokens: int, output_tokens: int
) -> Optional[float]:
    for key, (input_price, output_price) in MODEL_PRICING.items():
        if key in model_name or model_name in key:
            cost = (input_tokens / 1_000_000 * input_price) + (
                output_tokens / 1_000_000 * output_price
            )
            return round(cost, 6)
    return None


STATUS_NAMES = {
    "read_file": "读取中",
    "create_file": "创建中",
    "write_file": "编辑中",
    "write_file_complete": "写入中",
    "append_file": "追加中",
    "delete_file": "删除中",
    "rename_file": "重命名中",
    "copy_file": "复制中",
    "list_files": "探索中",
    "tree": "浏览中",
    "web_search": "搜索中",
    "run_shell": "执行中",
    "run_file": "运行中",
    "run_tests": "测试中",
    "format_code": "格式化中",
    "install_deps": "安装依赖中",
    "git_status": "Git状态中",
    "git_diff": "Git对比中",
    "git_log": "Git历史中",
    "git_add": "Git添加中",
    "git_commit": "Git提交中",
    "git_branch": "Git分支中",
    "git_checkout": "Git切换中",
    "search_in_files": "搜索中",
    "read_file_range": "读取中",
    "fetch_url": "抓取中",
    "multi_write_file": "批量编辑中",
    "apply_diff": "应用补丁中",
    "batch_create": "批量创建中",
    "index_codebase": "索引中",
    "search_symbols": "符号搜索中",
    "get_symbol_context": "符号分析中",
    "start_preview": "启动预览中",
    "stop_preview": "停止预览中",
    "review_changes": "代码审查中",
    "project_health": "健康检查中",
    "go_to_definition": "跳转定义中",
    "find_references": "查找引用中",
    "get_type_info": "类型分析中",
    "get_docstring": "文档查询中",
    "use_skill": "加载技能中",
    "list_skills": "列出技能中",
    "use_skill_file": "读取技能文件中",
    "list_skill_files": "列出技能文件中",
}

COUNT_LABELS = {
    "read_file": ("读取", "read"),
    "write_file": ("编辑", "edit"),
    "write_file_complete": ("覆盖", "overwrite"),
    "create_file": ("创建", "create"),
    "delete_file": ("删除", "delete"),
    "rename_file": ("重命名", "rename"),
    "copy_file": ("复制", "copy"),
    "append_file": ("追加", "append"),
    "list_files": ("列出", "list"),
    "tree": ("目录树", "tree"),
    "web_search": ("搜索", "search"),
    "run_shell": ("命令", "shell"),
    "run_file": ("运行", "run"),
    "run_tests": ("测试", "test"),
    "format_code": ("格式化", "format"),
    "install_deps": ("安装依赖", "install"),
    "git_status": ("Git状态", "git status"),
    "git_diff": ("Git对比", "git diff"),
    "git_log": ("Git历史", "git log"),
    "git_add": ("Git添加", "git add"),
    "git_commit": ("Git提交", "git commit"),
    "git_branch": ("Git分支", "git branch"),
    "git_checkout": ("Git切换", "git checkout"),
    "search_in_files": ("文件搜索", "grep"),
    "read_file_range": ("范围读取", "read range"),
    "fetch_url": ("抓取", "fetch"),
    "multi_write_file": ("批量编辑", "multi-edit"),
    "apply_diff": ("应用补丁", "apply-diff"),
    "batch_create": ("批量创建", "batch-create"),
    "index_codebase": ("索引", "index"),
    "search_symbols": ("符号搜索", "symbol-search"),
    "get_symbol_context": ("符号分析", "symbol-context"),
    "start_preview": ("启动预览", "preview"),
    "stop_preview": ("停止预览", "stop-preview"),
    "review_changes": ("代码审查", "review"),
    "project_health": ("健康检查", "health"),
    "go_to_definition": ("跳转定义", "goto-def"),
    "find_references": ("查找引用", "find-refs"),
    "get_type_info": ("类型分析", "type-info"),
    "get_docstring": ("文档查询", "docstring"),
    "use_skill": ("加载技能", "use-skill"),
    "list_skills": ("列出技能", "list-skills"),
    "use_skill_file": ("读取技能文件", "use-skill-file"),
    "list_skill_files": ("列出技能文件", "list-skill-files"),
}


class ActionPlan(BaseModel):
    explanation: str
    files_modified: list[str] = []
    code_snippets: list[str] = []
    operations_detail: list[str] = []


@dataclass
class UndoEntry:
    operation: str
    file_path: str
    old_content: Optional[str] = None
    group_tag: Optional[str] = None


class UndoManager:
    def __init__(self):
        self._history: list[UndoEntry] = []
        self._active_group: Optional[str] = None

    def record(
        self,
        operation: str,
        file_path: str,
        old_content: Optional[str] = None,
        group_tag: Optional[str] = None,
    ):
        tag = group_tag or self._active_group
        self._history.append(
            UndoEntry(
                operation=operation,
                file_path=file_path,
                old_content=old_content,
                group_tag=tag,
            )
        )

    def start_group(self, tag: str):
        """开始一个组合操作组，后续 record 的 entry 会带上相同的 group_tag。"""
        self._active_group = tag

    def end_group(self):
        """结束组合操作组。"""
        self._active_group = None

    def undo(self, workspace_dir: Path, n: int = 1) -> str:
        if not self._history:
            return "没有可撤销的操作"
        results = []
        undone_groups: set[str] = set()
        count = 0
        while count < n and self._history:
            entry = self._history.pop()
            full_path = workspace_dir / entry.file_path

            # 如果这是组合操作的一部分，回滚整个组
            if entry.group_tag and entry.group_tag not in undone_groups:
                group_tag = entry.group_tag
                undone_groups.add(group_tag)
                # 收集当前栈中所有同组的 entry（包括刚 pop 的这个）
                group_entries = [entry]
                while self._history and self._history[-1].group_tag == group_tag:
                    group_entries.append(self._history.pop())

                for g_entry in group_entries:
                    g_path = workspace_dir / g_entry.file_path
                    try:
                        if g_entry.operation == "create":
                            if g_path.exists():
                                g_path.unlink()
                            results.append(f"撤销创建: {g_entry.file_path}")
                        elif g_entry.operation == "delete":
                            g_path.parent.mkdir(parents=True, exist_ok=True)
                            g_path.write_text(
                                g_entry.old_content or "", encoding="utf-8"
                            )
                            results.append(f"撤销删除: {g_entry.file_path}")
                        elif g_entry.operation in ("write", "overwrite", "append"):
                            g_path.write_text(
                                g_entry.old_content or "", encoding="utf-8"
                            )
                            results.append(f"撤销编辑: {g_entry.file_path}")
                        elif g_entry.operation == "rename":
                            src = workspace_dir / (g_entry.old_content or "")
                            dst = workspace_dir / g_entry.file_path
                            if dst.exists():
                                dst.rename(src)
                            results.append(
                                f"撤销重命名: {g_entry.file_path} -> {g_entry.old_content}"
                            )
                        elif g_entry.operation == "copy":
                            if g_path.exists():
                                import shutil

                                if g_path.is_dir():
                                    shutil.rmtree(g_path)
                                else:
                                    g_path.unlink()
                            results.append(f"撤销复制: {g_entry.file_path}")
                    except Exception as e:
                        self._history.append(g_entry)
                        results.append(f"撤销失败 ({g_entry.file_path}): {e}")
                        break
                count += 1
                continue

            # 普通单条撤销
            try:
                if entry.operation == "create":
                    if full_path.exists():
                        full_path.unlink()
                    results.append(f"撤销创建: {entry.file_path}")
                elif entry.operation == "delete":
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(entry.old_content or "", encoding="utf-8")
                    results.append(f"撤销删除: {entry.file_path}")
                elif entry.operation == "write":
                    full_path.write_text(entry.old_content or "", encoding="utf-8")
                    results.append(f"撤销编辑: {entry.file_path}")
                elif entry.operation == "overwrite":
                    full_path.write_text(entry.old_content or "", encoding="utf-8")
                    results.append(f"撤销覆盖: {entry.file_path}")
                elif entry.operation == "append":
                    full_path.write_text(entry.old_content or "", encoding="utf-8")
                    results.append(f"撤销追加: {entry.file_path}")
                elif entry.operation == "rename":
                    src = workspace_dir / (entry.old_content or "")
                    dst = workspace_dir / entry.file_path
                    if dst.exists():
                        dst.rename(src)
                    results.append(
                        f"撤销重命名: {entry.file_path} -> {entry.old_content}"
                    )
                elif entry.operation == "copy":
                    if full_path.exists():
                        import shutil

                        if full_path.is_dir():
                            shutil.rmtree(full_path)
                        else:
                            full_path.unlink()
                    results.append(f"撤销复制: {entry.file_path}")
            except Exception as e:
                self._history.append(entry)
                results.append(f"撤销失败 ({entry.file_path}): {e}")
                break
            count += 1
        return "\n".join(results) if results else "没有可撤销的操作"

    @property
    def history_length(self) -> int:
        return len(self._history)

    def history_summary(self) -> str:
        if not self._history:
            return "暂无操作历史"
        lines = ["操作历史:"]
        seen_groups: set[str] = set()
        for i, entry in enumerate(reversed(self._history[-10:]), 1):
            if entry.group_tag:
                if entry.group_tag in seen_groups:
                    continue
                seen_groups.add(entry.group_tag)
                lines.append(f"  {i}. [组合操作] {entry.group_tag}")
            else:
                lines.append(f"  {i}. [{entry.operation}] {entry.file_path}")
        return "\n".join(lines)


@dataclass
class ToolTracker:
    _counts: Counter = field(default_factory=Counter)
    _current_tool: str = ""
    _total_chars: int = 0
    max_tokens: int = 128000
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    session_cost: float = 0.0
    session_requests: int = 0
    paused: bool = False
    status: Any = None

    def reset(self):
        self._counts.clear()
        self._current_tool = ""
        self._total_chars = 0

    def count(self, tool_name: str, chars: int = 0):
        self._counts[tool_name] += 1
        self._current_tool = tool_name
        self._total_chars += chars

    def add_chars(self, n: int):
        self._total_chars += n

    def record_usage(self, input_tokens: int, output_tokens: int, model_name: str = ""):
        self.cumulative_input_tokens += input_tokens
        self.cumulative_output_tokens += output_tokens
        self.session_requests += 1
        cost = estimate_cost(model_name, input_tokens, output_tokens)
        if cost is not None:
            self.session_cost += cost

    @property
    def current_tool(self) -> str:
        return self._current_tool

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    @property
    def estimated_tokens(self) -> int:
        return self._total_chars // 4

    def summary(self) -> dict[str, int]:
        return dict(self._counts)

    def summary_str(self, lang: str = "zh") -> str:
        if not self._counts:
            return ""
        parts = []
        for name, count in sorted(self._counts.items()):
            label = COUNT_LABELS.get(name, (name, name))[0 if lang == "zh" else 1]
            parts.append(f"{count}次{label}")
        return "，".join(parts) if parts else ""

    def status_line(self, spinner: str, lang: str = "zh") -> str:
        status = STATUS_NAMES.get(self._current_tool, "")
        summary = self.summary_str(lang)
        if summary:
            parts = [f"{spinner} {status} ({summary})"]
        else:
            parts = [f"{spinner} {status}"]

        tokens = self.estimated_tokens
        if tokens > 500:
            pct = min(tokens * 100 // self.max_tokens, 99)
            if pct > 0:
                parts.append(f"{tokens / 1000:.1f}k({pct}%) token")
            else:
                parts.append(f"{tokens / 1000:.1f}k token")

        return "  ".join(parts) + " Thinking..."

    def usage_summary(self, model_name: str = "") -> str:
        parts = []
        if self.session_requests > 0:
            parts.append(f"请求: {self.session_requests}次")
        if self.cumulative_input_tokens > 0:
            parts.append(f"输入: {self.cumulative_input_tokens / 1000:.1f}k token")
        if self.cumulative_output_tokens > 0:
            parts.append(f"输出: {self.cumulative_output_tokens / 1000:.1f}k token")
        if self.session_cost > 0:
            parts.append(f"费用: ${self.session_cost:.6f}")
        return " | ".join(parts) if parts else "暂无使用数据"


@dataclass
class WorkspaceDeps:
    workspace_dir: Path
    http_client: httpx.AsyncClient
    undo_manager: UndoManager
    console: Console = Console()
    tool_tracker: ToolTracker = field(default_factory=ToolTracker)
    shell_timeout: int = 30
    project_instructions: str = ""
    permissions: Any = None
    plan_mode: bool = False
    skills: Any = None
    subagents: Any = None
    mcp_toolsets: Any = None
    config: dict = field(default_factory=dict)
