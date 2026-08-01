"""权限确认系统：allow / ask / deny 规则、权限模式、计划模式门控、交互式审批。"""

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

# 只读工具：默认直接放行
READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "read_file_range",
        "list_files",
        "tree",
        "search_in_files",
        "web_search",
        "fetch_url",
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "show_history",
        "use_skill",
        "list_skills",
        "task",
        "enter_plan_mode",
        "exit_plan_mode",
    }
)

# 写/执行工具：默认需要确认
WRITE_TOOLS = frozenset(
    {
        "create_file",
        "write_file",
        "write_file_complete",
        "append_file",
        "delete_file",
        "rename_file",
        "copy_file",
        "run_shell",
        "run_file",
        "run_tests",
        "format_code",
        "install_deps",
        "git_add",
        "git_commit",
        "git_checkout",
        "undo_last",
    }
)

# 内置高危行为，无条件拦截
BUILTIN_DANGEROUS: list[tuple[str, str]] = [
    (r"rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)?/?\*|rm\s+-rf\s+/", "递归删除根目录"),
    (r"\bmkfs\.?\w*\b", "格式化磁盘"),
    (r"\bmkswap\b", "交换分区操作"),
    (r"\bfdisk\b[^;|&\n]*", "磁盘分区操作"),
    (r"\bdd\s+if=[^\s]+", "dd 磁盘写入"),
    (r"\bchmod\s+(-[a-zA-Z]*[Rr][a-zA-Z]*\s+)?777\s+/", "对根目录设置 777"),
    (r"git\s+push\s+.*--force", "强制推送"),
    (r"git\s+reset\s+--hard", "git 硬重置"),
    (r"\brm\s+-rf\s+(~|%USERPROFILE%)", "删除用户主目录"),
]

# 工具分类（默认 ask 的未知工具）
ACTION_TOOLS = WRITE_TOOLS | {"run_shell"}

# 只读 shell 命令（自动放行，无需确认）
READONLY_SHELL_PREFIXES = (
    "ls ",
    "ls\n",
    "cat ",
    "head ",
    "tail ",
    "grep ",
    "rg ",
    "find ",
    "pwd",
    "echo ",
    "which ",
    "type ",
    "where ",
    "python --version",
    "node --version",
    "npm --version",
    "git status",
    "git log",
    "git diff",
    "git branch",
    "git show",
    "git remote",
    "git config --list",
    "git tag",
    "git ls-files",
    "git shortlog",
    "pip list",
    "npm list",
    "python -m pip list",
    "dir ",
    "python -c",
)


def _classify(tool_name: str) -> str:
    """返回工具类别: read | action | unknown。"""
    if tool_name in READ_ONLY_TOOLS:
        return "read"
    if tool_name in WRITE_TOOLS:
        return "action"
    if tool_name.startswith(("mcp__",)) or "__" in tool_name:
        return "mcp"
    if tool_name in ACTION_TOOLS:
        return "action"
    return "action"


def is_write_tool(tool_name: str) -> bool:
    """是否属于写/执行类工具（非只读）。"""
    return _classify(tool_name) != "read"


@dataclass
class PermissionRule:
    action: str  # allow | ask | deny
    tool: str  # 支持 * 通配
    spec: str = ""  # 对目标串的正则，空表示不限制

    def matches_tool(self, tool_name: str) -> bool:
        if self.tool == "*":
            return True
        if "*" in self.tool:
            return fnmatch.fnmatch(tool_name, self.tool)
        return self.tool == tool_name

    def matches_target(self, target: str) -> bool:
        if not self.spec:
            return True
        try:
            return re.search(self.spec, target, re.IGNORECASE) is not None
        except re.error:
            return False


_RULE_RE = re.compile(r"^(?P<tool>[A-Za-z0-9_*\.-]+?)(?:\((?P<spec>.*)\))?$")


def parse_rule_string(raw: str) -> PermissionRule | None:
    raw = raw.strip()
    if not raw:
        return None
    m = _RULE_RE.match(raw)
    if not m:
        return None
    action = "allow"
    return PermissionRule(
        action=action, tool=m.group("tool"), spec=(m.group("spec") or "")
    )


@dataclass
class PermissionManager:
    console: Any = None
    workspace_dir: Any = None
    tool_tracker: Any = None  # 用于暂停状态显示
    status: Any = None  # rich Status 对象

    mode: str = "acceptEdits"
    plan_mode: bool = False
    headless: bool = False
    subagent_mode: bool = False

    allow_rules: list[PermissionRule] = field(default_factory=list)
    ask_rules: list[PermissionRule] = field(default_factory=list)
    deny_rules: list[PermissionRule] = field(default_factory=list)
    _session_always: set[str] = field(default_factory=set)
    _session_never: set[str] = field(default_factory=set)

    def load_settings(self, settings: dict):
        perms = settings.get("permissions") or {}
        if not isinstance(perms, dict):
            return
        mode = perms.get("defaultMode")
        if mode in ("default", "acceptEdits", "plan", "bypass"):
            self.mode = mode
        for action, key in (("allow", "allow"), ("ask", "ask"), ("deny", "deny")):
            for raw in perms.get(key, []) or []:
                rule = parse_rule_string(str(raw))
                if rule is None:
                    continue
                rule.action = action
                getattr(self, f"{action}_rules").append(rule)

    def _matches_dangerous(self, tool_name: str, target: str) -> str | None:
        if tool_name not in ("run_shell", "run_file", "install_deps", "run_tests"):
            return None
        for pattern, reason in BUILTIN_DANGEROUS:
            try:
                if re.search(pattern, target, re.IGNORECASE):
                    return reason
            except re.error:
                continue
        return None

    def target_str(
        self, tool_name: str, args: tuple = (), kwargs: dict | None = None
    ) -> str:
        kwargs = kwargs or {}
        parts = []
        for v in args:
            parts.append(str(v))
        for k, v in sorted(kwargs.items()):
            parts.append(f"{k}={v}")
        return " ".join(parts) if parts else ""

    def _shell_command(self, args: tuple, kwargs: dict | None) -> str:
        kwargs = kwargs or {}
        if "command" in kwargs:
            return str(kwargs["command"])
        for v in args:
            if isinstance(v, str):
                return v
        return ""

    def _is_readonly_shell(self, command: str) -> bool:
        cmd = command.strip()
        if not cmd:
            return False
        for prefix in READONLY_SHELL_PREFIXES:
            if cmd.lower().startswith(prefix):
                return True
        return False

    def decide(
        self, tool_name: str, target: str, args: tuple = (), kwargs: dict | None = None
    ) -> str:
        """返回 allow / ask / deny。"""
        cat = _classify(tool_name)

        # 1. 计划模式：写操作一律拒绝
        if self.plan_mode and cat != "read":
            return "deny"

        # 2. 内置高危行为
        reason = self._matches_dangerous(tool_name, target)
        if reason:
            return "deny"

        # 3. 会话级记忆
        key = f"{tool_name} {target}"
        if key in self._session_never:
            return "deny"
        if key in self._session_always:
            return "allow"

        # 4. 用户规则：deny > ask > allow
        denied = any(
            r.matches_tool(tool_name) and r.matches_target(target)
            for r in self.deny_rules
        )
        if denied:
            return "deny"
        asked = any(
            r.matches_tool(tool_name) and r.matches_target(target)
            for r in self.ask_rules
        )
        allowed = any(
            r.matches_tool(tool_name) and r.matches_target(target)
            for r in self.allow_rules
        )
        if asked and not allowed:
            return "ask"
        if allowed:
            return "allow"

        # 5. 只读 shell 命令自动放行
        if tool_name == "run_shell" and self._is_readonly_shell(
            self._shell_command(args, kwargs)
        ):
            return "allow"

        # 6. 按模式兜底
        if self.mode == "bypass":
            return "allow"
        if cat == "read":
            return "allow"
        if self.mode == "acceptEdits":
            if tool_name in WRITE_TOOLS and tool_name not in (
                "run_shell",
                "run_file",
                "install_deps",
                "run_tests",
                "format_code",
                "git_commit",
                "git_checkout",
            ):
                return "allow"
            return "ask"
        if self.mode == "plan":
            return "deny"
        return "ask"

    def check(
        self, tool_name: str, args: tuple = (), kwargs: dict | None = None
    ) -> str | None:
        """权限门控入口。返回错误消息字符串（拒绝）或 None（放行）。"""
        target = self.target_str(tool_name, args, kwargs)
        decision = self.decide(tool_name, target, args, kwargs)

        if decision == "deny":
            if self.plan_mode:
                return (
                    "权限被拒绝: 当前处于计划模式，禁止执行写/修改操作。"
                    "请只使用只读工具探索并给出方案。"
                )
            return f"权限被拒绝: {tool_name} 不允许执行"

        if decision == "ask":
            if self.headless:
                return (
                    f"权限被拒绝: 无交互模式下无法确认工具 {tool_name}，"
                    "请在权限配置中显式 allow，或使用 --dangerously-skip-permissions。"
                )
            if self._ask_user(tool_name, target):
                return None
            self._session_never.add(f"{tool_name} {target}")
            return f"权限被拒绝: 用户取消了 {tool_name}"

        return None

    def _ask_user(self, tool_name: str, target: str) -> bool:
        if self.console is None:
            return False
        self._pause_status()
        try:
            preview = target if len(target) <= 300 else target[:300] + "..."
            self.console.print()
            self.console.print(
                "[bold yellow]权限确认[/bold yellow] [dim]工具:[/dim] "
                f"[bold cyan]{tool_name}[/bold cyan]"
            )
            if preview:
                self.console.print(f"[dim]{preview}[/dim]")
            while True:
                answer = (
                    self.console.input(
                        "[yellow]允许此操作? [/yellow][cyan]y[/cyan]是 / "
                        "[cyan]n[/cyan]否 / [cyan]a[/cyan]本次会话总是允许: "
                    )
                    .strip()
                    .lower()
                )
                if answer in ("y", "yes", "允许", ""):
                    return False if answer == "" else True
                if answer in ("a", "always"):
                    self._session_always.add(f"{tool_name} {target}")
                    return True
                if answer in ("n", "no", "否", "q"):
                    return False
                self.console.print("[dim]请输入 y / n / a[/dim]")
        finally:
            self._resume_status()

    def _pause_status(self):
        if self.tool_tracker is not None:
            self.tool_tracker.paused = True
        if self.status is not None:
            try:
                self.status.stop()
            except Exception:
                pass

    def _resume_status(self):
        if self.tool_tracker is not None:
            self.tool_tracker.paused = False
        if self.status is not None:
            try:
                self.status.start()
            except Exception:
                pass

    def summary(self) -> str:
        lines = [
            f"权限模式: {self.mode}",
            f"计划模式: {'开' if self.plan_mode else '关'}",
            f"无交互(headless): {'开' if self.headless else '关'}",
        ]
        if self.deny_rules:
            lines.append(
                "deny 规则: "
                + ", ".join(f"{r.tool}({r.spec})" for r in self.deny_rules)
            )
        if self.ask_rules:
            lines.append(
                "ask 规则: " + ", ".join(f"{r.tool}({r.spec})" for r in self.ask_rules)
            )
        if self.allow_rules:
            lines.append(
                "allow 规则: "
                + ", ".join(f"{r.tool}({r.spec})" for r in self.allow_rules)
            )
        return "\n".join(lines)


def check_permission(
    ctx, tool_name: str, args: tuple = (), kwargs: dict | None = None
) -> str | None:
    """从 RunContext 中取权限管理器并执行门控。返回错误消息或 None。"""
    perms = getattr(ctx.deps, "permissions", None)
    if perms is None:
        return None
    return perms.check(tool_name, args, kwargs or {})
