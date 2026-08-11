from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel
import httpx
from rich.console import Console


# NOTE:各主流模型的输入/输出单价（每百万 token），用于估算会话费用
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


# NOTE:根据模型名称模糊匹配单价，计算本次调用的估算费用（美元）
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


# NOTE:工具调用时的中文状态名，用于控制台旋转提示展示
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
    "update_memory": "更新记忆中",
    "generate_spec": "生成规格说明中",
    "read_spec": "读取规格说明中",
}

# NOTE:工具调用统计的双语标签（中文用于展示，英文用于日志）
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
    "update_memory": ("记忆更新", "memory"),
    "generate_spec": ("生成规格说明", "gen-spec"),
    "read_spec": ("读取规格说明", "read-spec"),
}


# NOTE:AI 结构化输出模型：包含解释文本、修改文件列表、代码片段与操作详情
class ActionPlan(BaseModel):
    explanation: str
    files_modified: list[str] = []
    code_snippets: list[str] = []
    operations_detail: list[str] = []


# NOTE:单次可撤销操作记录（含组合操作组标签，支持批量回滚）
@dataclass
class UndoEntry:
    operation: str
    file_path: str
    old_content: Optional[str] = None
    group_tag: Optional[str] = None


# NOTE:撤销管理器：维护操作历史栈，支持单条与组合操作回滚
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

    # NOTE:开启组合操作组，后续 record 自动带上相同 group_tag 实现批量回滚
    def start_group(self, tag: str):
        """开始一个组合操作组，后续 record 的 entry 会带上相同的 group_tag。"""
        self._active_group = tag

    # NOTE:关闭组合操作组，结束批量记录
    def end_group(self):
        """结束组合操作组。"""
        self._active_group = None

    # NOTE:撤销最近 n 步（含组合操作），按栈顺序弹出并恢复原内容
    def undo(self, workspace_dir: Path, n: int = 1) -> str:
        if not self._history:
            return "没有可撤销的操作"
        results = []
        undone_groups: set[str] = set()
        count = 0
        while count < n and self._history:
            entry = self._history.pop()
            full_path = workspace_dir / entry.file_path

            # NOTE:若发现组合操作标记，一次性回滚整个组内所有条目
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

            # NOTE:普通单条撤销，按操作类型恢复文件状态
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


# NOTE:工具调用追踪器：统计各工具调用次数、token 估算、费用累积，用于状态栏与用量报告
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
    _summary_cache: str | None = None

    # NOTE:重置本轮计数（保留累积 token 与费用）
    def reset(self):
        self._counts.clear()
        self._current_tool = ""
        self._total_chars = 0
        self._summary_cache = None

    # NOTE:记录一次工具调用及其输出字符量（触发缓存失效）
    def count(self, tool_name: str, chars: int = 0):
        self._counts[tool_name] += 1
        self._current_tool = tool_name
        self._total_chars += chars
        self._summary_cache = None

    # NOTE:累加字符量（用于大文件读取时的 token 估算）
    def add_chars(self, n: int):
        self._total_chars += n

    # NOTE:记录实际 API 输入/输出 token，并累加估算费用
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

    # NOTE:基于字符数粗略估算 token（按 4 字符 ≈ 1 token）
    @property
    def estimated_tokens(self) -> int:
        return self._total_chars // 4

    # NOTE:返回各工具调用次数字典
    def summary(self) -> dict[str, int]:
        return dict(self._counts)

    # NOTE:生成人类可读的工具调用摘要，带缓存避免重复拼接
    def summary_str(self, lang: str = "zh") -> str:
        if self._summary_cache is not None:
            return self._summary_cache
        if not self._counts:
            self._summary_cache = ""
            return ""
        parts = []
        for name, count in sorted(self._counts.items()):
            label = COUNT_LABELS.get(name, (name, name))[0 if lang == "zh" else 1]
            parts.append(f"{count}次{label}")
        self._summary_cache = "，".join(parts) if parts else ""
        return self._summary_cache

    # NOTE:构建当前状态栏文本（spinner + 当前工具 + 已用摘要 + token 预警）
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

    # NOTE:返回累积用量字符串（请求数、输入/输出 token、估算费用）
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


# NOTE:WorkspaceDeps 承载整个会话的运行时依赖（供 Agent 工具共享访问）
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
    cot_mode: bool = False
    skills: Any = None
    subagents: Any = None
    mcp_toolsets: Any = None
    config: dict = field(default_factory=dict)


# NOTE:基于父会话创建子代理/验收 AI 的隔离 deps，继承通用配置但重置易变状态
WORKSPACE_DEPS_CHILD_OVERRIDES = {
    "project_instructions": "",
    "plan_mode": False,
    "cot_mode": False,
    "skills": None,
    "subagents": None,
    "mcp_toolsets": None,
    "tool_tracker": None,
}


def fork_workspace_deps(parent: "WorkspaceDeps") -> "WorkspaceDeps":
    """复制一份隔离的子代理运行时依赖。

    - config 做浅拷贝，防止子代理意外修改影响父会话
    - 权限继承父会话设置（避免重复确认）
    - 工具统计、skills、子代理引用重新初始化，保持隔离
    """
    from dataclasses import replace
    from .permissions import PermissionManager, inherit_permissions

    perms = PermissionManager(
        console=parent.console,
        workspace_dir=parent.workspace_dir,
        tool_tracker=None,
    )
    inherit_permissions(parent.permissions, perms)

    child = replace(
        parent,
        tool_tracker=ToolTracker(),
        permissions=perms,
        project_instructions="",
        plan_mode=False,
        skills=None,
        subagents=None,
        mcp_toolsets=None,
        config=dict(parent.config),
    )
    return child
