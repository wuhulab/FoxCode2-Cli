import json
from datetime import datetime
from pathlib import Path
from typing import Optional


class SessionManager:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, name: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return self.sessions_dir / f"{safe}.json"

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

    def save_session(self, name: str, messages: list) -> str:
        path = self._session_path(name)
        try:
            data = []
            for msg in messages:
                if hasattr(msg, "model_dump"):
                    data.append(msg.model_dump())
                elif hasattr(msg, "dict"):
                    data.append(msg.dict())
                else:
                    data.append(str(msg))
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
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

    def load_session(self, name: str) -> Optional[list]:
        path = self._session_path(name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("data", [])
        except Exception:
            return None

    def delete_session(self, name: str) -> bool:
        path = self._session_path(name)
        if path.exists():
            try:
                path.unlink()
                return True
            except Exception:
                pass
        return False

    def get_auto_save_name(self) -> str:
        return datetime.now().strftime("auto_%Y%m%d_%H%M%S")
