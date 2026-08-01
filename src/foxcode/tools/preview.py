"""Web 预览工具：启动本地 HTTP 服务器预览静态网站。"""

import subprocess
from pathlib import Path

from pydantic_ai import RunContext

from ..models import WorkspaceDeps
from . import log_tool, permission_validator
from .file_ops import _resolve_safe_path


_preview_process: subprocess.Popen | None = None
_preview_port: int = 0


def register(agent):
    @agent.tool(args_validator=permission_validator("start_preview"))
    async def start_preview(
        ctx: RunContext[WorkspaceDeps],
        path: str = ".",
        port: int = 8080,
    ) -> str:
        """启动一个本地 HTTP 服务器预览静态网站。

        参数:
            path: 要服务的目录路径（相对于工作区）
            port: 端口号（默认 8080）
        """
        global _preview_process, _preview_port

        log_tool(ctx, "start_preview", path, f"port={port}")

        try:
            serve_path = _resolve_safe_path(ctx.deps.workspace_dir, path)
        except ValueError as e:
            return f"错误: {e}"
        if not serve_path.exists():
            return f"错误: 路径 {path} 不存在"
        if not serve_path.is_dir():
            return f"错误: {path} 不是一个目录"

        # 停止已有的预览
        if _preview_process is not None:
            try:
                _preview_process.terminate()
                _preview_process.wait(timeout=3)
            except Exception:
                pass
            _preview_process = None

        try:
            _preview_process = subprocess.Popen(
                [
                    "python",
                    "-m",
                    "http.server",
                    str(port),
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    str(serve_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _preview_port = port
            return (
                f"预览服务器已启动: http://127.0.0.1:{port}\n"
                f"服务目录: {serve_path}\n"
                f"你可以使用 fetch_url 抓取 http://127.0.0.1:{port} 查看页面内容"
            )
        except Exception as e:
            return f"错误: 启动预览服务器失败 - {e}"

    @agent.tool(args_validator=permission_validator("stop_preview"))
    async def stop_preview(ctx: RunContext[WorkspaceDeps]) -> str:
        """停止当前运行的预览服务器。"""
        global _preview_process, _preview_port
        log_tool(ctx, "stop_preview")
        if _preview_process is None:
            return "没有正在运行的预览服务器"
        try:
            _preview_process.terminate()
            _preview_process.wait(timeout=3)
            port = _preview_port
            _preview_process = None
            _preview_port = 0
            return f"预览服务器已停止 (端口 {port})"
        except Exception as e:
            return f"停止预览服务器失败: {e}"
