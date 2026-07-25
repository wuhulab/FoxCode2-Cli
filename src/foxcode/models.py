from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from pydantic import BaseModel
import httpx
from rich.console import Console


class ActionPlan(BaseModel):
    explanation: str
    files_modified: list[str] = []
    code_snippets: list[str] = []


@dataclass
class UndoEntry:
    operation: str
    file_path: str
    old_content: Optional[str] = None
    new_content: Optional[str] = None


class UndoManager:
    def __init__(self):
        self._history: list[UndoEntry] = []

    def record(
        self,
        operation: str,
        file_path: str,
        old_content: Optional[str] = None,
        new_content: Optional[str] = None,
    ):
        self._history.append(
            UndoEntry(
                operation=operation,
                file_path=file_path,
                old_content=old_content,
                new_content=new_content,
            )
        )

    def undo(self, workspace_dir: Path, n: int = 1) -> str:
        if not self._history:
            return "没有可撤销的操作"
        results = []
        for _ in range(min(n, len(self._history))):
            entry = self._history.pop()
            full_path = workspace_dir / entry.file_path
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
                    results.append(f"撤销修改: {entry.file_path}")
                elif entry.operation == "rename":
                    src = workspace_dir / entry.old_content
                    dst = workspace_dir / entry.file_path
                    if dst.exists():
                        dst.rename(src)
                    results.append(
                        f"撤销重命名: {entry.file_path} -> {entry.old_content}"
                    )
            except Exception as e:
                results.append(f"撤销失败 ({entry.file_path}): {e}")
        return "\n".join(results) if results else "没有可撤销的操作"

    @property
    def history_length(self) -> int:
        return len(self._history)

    def history_summary(self) -> str:
        if not self._history:
            return "暂无操作历史"
        lines = ["操作历史:"]
        for i, entry in enumerate(reversed(self._history[-10:]), 1):
            lines.append(f"  {i}. [{entry.operation}] {entry.file_path}")
        return "\n".join(lines)


@dataclass
class ToolTracker:
    _counts: Counter = field(default_factory=Counter)

    def reset(self):
        self._counts.clear()

    def count(self, tool_name: str):
        self._counts[tool_name] += 1

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def summary(self) -> dict[str, int]:
        return dict(self._counts)

    def summary_str(self, lang: str = "zh") -> str:
        if not self._counts:
            return ""
        words_map = {
            "read_file": ("读取", "read"),
            "write_file": ("编辑", "edit"),
            "write_file_complete": ("覆盖", "overwrite"),
            "create_file": ("创建", "create"),
            "delete_file": ("删除", "delete"),
            "rename_file": ("重命名", "rename"),
            "append_file": ("追加", "append"),
            "list_files": ("列出", "list"),
            "web_search": ("搜索", "search"),
            "run_shell": ("命令", "shell"),
            "run_file": ("运行", "run"),
        }
        parts = []
        for name, count in sorted(self._counts.items()):
            label = words_map.get(name, (name, name))[0 if lang == "zh" else 1]
            parts.append(f"{count}次{label}")
        return "，".join(parts) if parts else ""


@dataclass
class WorkspaceDeps:
    workspace_dir: Path
    http_client: httpx.AsyncClient
    undo_manager: UndoManager
    console: Console = Console()
    tool_tracker: ToolTracker = field(default_factory=ToolTracker)
    shell_timeout: int = 30
