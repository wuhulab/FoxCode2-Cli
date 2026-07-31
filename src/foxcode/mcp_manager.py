"""MCP (Model Context Protocol) 服务器管理。"""

import json
import re
from pathlib import Path
from typing import Any

from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.mcp import MCPToolset, StdioTransport

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value):
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, str):

        def repl(m):
            name, default = m.group(1), m.group(2)
            env_val = __import__("os").environ.get(name)
            if env_val is not None:
                return env_val
            if default is not None:
                return default
            raise ValueError(f"环境变量 {name} 未定义")

        return _ENV_RE.sub(repl, value)
    return value


def discover_mcp_configs(workspace_dir: Path) -> dict[str, Any]:
    """从 .foxcode/mcp.json 或 .mcp.json 读取 mcpServers 配置。"""
    servers: dict[str, Any] = {}
    candidates = [
        workspace_dir / ".foxcode" / "mcp.json",
        workspace_dir / ".mcp.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        got = data.get("mcpServers") if isinstance(data, dict) else None
        if isinstance(got, dict):
            servers.update(got)
    return servers


def load_mcp_toolsets(workspace_dir: Path, permissions) -> list[MCPToolset]:
    """根据配置创建 MCP toolsets，并为每个工具调用加上权限门控。"""
    servers = discover_mcp_configs(workspace_dir)
    if not servers:
        return []

    toolsets: list[MCPToolset] = []
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        try:
            server = _expand_env(server)
        except ValueError as e:
            print(f"  [red]MCP 服务器 {name} 配置错误: {e}[/red]")
            continue

        async def _process(ctx, call_tool, tool_name, args):
            if permissions is not None:
                target = (
                    f"{tool_name} args={json.dumps(args, ensure_ascii=False)[:500]}"
                )
                err = permissions.check(f"mcp__{name}", (), {"command": target})
                if err:
                    raise ToolFailed(err)
            return await call_tool(tool_name, args)

        try:
            if "command" in server:
                transport = StdioTransport(
                    command=str(server["command"]),
                    args=list(server.get("args") or []),
                    env=server.get("env"),
                    cwd=str(server["cwd"]) if server.get("cwd") else None,
                )
                toolset = MCPToolset(transport, id=name, process_tool_call=_process)
            elif "url" in server:
                toolset = MCPToolset(
                    str(server["url"]),
                    id=name,
                    headers=server.get("headers"),
                    process_tool_call=_process,
                )
            else:
                continue
        except Exception as e:
            print(f"  [red]MCP 服务器 {name} 初始化失败: {e}[/red]")
            continue
        toolsets.append(toolset)
        print(f"  [dim]MCP 服务器已加载: {name}[/dim]")
    return toolsets
