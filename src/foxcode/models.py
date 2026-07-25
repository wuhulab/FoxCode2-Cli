from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from pydantic import BaseModel
import httpx


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
class WorkspaceDeps:
    workspace_dir: Path
    http_client: httpx.AsyncClient
    undo_manager: UndoManager
    shell_timeout: int = 30
