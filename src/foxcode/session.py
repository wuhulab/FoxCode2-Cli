import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic_ai.messages import ModelMessagesTypeAdapter


# NOTE:会话管理器：负责保存、加载、列出和删除 pydantic-ai 消息历史
class SessionManager:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)

    # NOTE:将任意名称转换为安全的文件系统路径（非字母数字字符替换为下划线）
    def _session_path(self, name: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return self.sessions_dir / f"{safe}.json"

    # NOTE:列出所有已保存的会话文件，包含名称、大小与修改时间
    def list_sessions(self) -> list[dict]:
        sessions = []
        if not self.sessions_dir.is_dir():
            return sessions
        try:
            for f in sorted(self.sessions_dir.iterdir()):
                if f.suffix == ".json" and f.is_file():
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        sessions.append(
                            {
                                "name": f.stem,
                                "path": f.name,
                                "size": len(data.get("data", "")),
                                "modified": datetime.fromtimestamp(
                                    f.stat().st_mtime
                                ).strftime("%Y-%m-%d %H:%M"),
                            }
                        )
                    except Exception:
                        sessions.append(
                            {
                                "name": f.stem,
                                "path": f.name,
                                "size": 0,
                                "modified": datetime.fromtimestamp(
                                    f.stat().st_mtime
                                ).strftime("%Y-%m-%d %H:%M"),
                            }
                        )
        except Exception:
            pass
        return sessions

    # NOTE:将会话消息序列化为 JSON 并保存到磁盘（使用 pydantic-ai 的 TypeAdapter）
    def save_session(self, name: str, messages: list) -> str:
        path = self._session_path(name)
        try:
            data = ModelMessagesTypeAdapter.dump_python(
                messages, mode="json", exclude_none=True
            )
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "saved_at": datetime.now().isoformat(),
                        "message_count": len(messages),
                        "data": data,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return f"会话已保存: {path.name} ({len(messages)} 条消息)"
        except Exception as e:
            return f"保存失败: {e}"

    # NOTE:从磁盘加载会话并反序列化为 pydantic-ai 消息对象（兼容 v1/v2 格式）
    def load_session(self, name: str) -> Optional[list]:
        path = self._session_path(name)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            data = raw.get("data", [])
            if raw.get("version", 1) >= 2:
                return ModelMessagesTypeAdapter.validate_python(data)
            return data
        except Exception:
            return None

    # NOTE:删除指定名称的会话文件
    def delete_session(self, name: str) -> bool:
        path = self._session_path(name)
        if path.exists():
            try:
                path.unlink()
                return True
            except Exception:
                pass
        return False

    # NOTE:生成基于当前时间的自动保存名称（默认会话名）
    def get_auto_save_name(self) -> str:
        return datetime.now().strftime("auto_%Y%m%d_%H%M%S")
