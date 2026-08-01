"""冒烟测试：权限门控、计划模式、subagents、skills、MCP 配置。"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from foxcode.agent import create_agent
from foxcode.models import ActionPlan, ToolTracker, UndoManager, WorkspaceDeps
from foxcode.permissions import PermissionManager
from foxcode.skills import SkillsManager
from foxcode.subagents import SubAgentManager, create_subagent_agent
from foxcode.mcp_manager import discover_mcp_configs


CONFIG = {
    "model": "gpt-4o-mini",
    "base_url": "https://api.openai.com/v1",
    "api_key": "test-key",
    "temperature": 0.7,
}


def _make_deps(tmpdir, perms=None, plan_mode=False, headless=True, tracker=None):
    if perms is None:
        perms = PermissionManager(workspace_dir=tmpdir)
        perms.headless = headless
    skills = SkillsManager(tmpdir / ".foxcode" / "skills")
    skills.load()
    subs = SubAgentManager(tmpdir / ".foxcode" / "agents")
    subs.load()
    return WorkspaceDeps(
        workspace_dir=tmpdir,
        http_client=httpx.AsyncClient(),
        undo_manager=UndoManager(),
        tool_tracker=tracker or ToolTracker(),
        permissions=perms,
        plan_mode=plan_mode,
        skills=skills,
        subagents=subs,
        config=CONFIG,
    )


async def _run_test_model(agent: Agent, deps: WorkspaceDeps, prompt: str):
    agent.set_model(TestModel(call_tools="all", model_name="gpt-4o-mini"))
    result = await agent.run(prompt, deps=deps)
    return result


def test_permission_deny_dangerous():
    perms = PermissionManager(headless=True)
    err = perms.check("run_shell", (), {"command": "rm -rf /"})
    assert err is not None, "高危命令应被拒绝"
    err = perms.check("run_shell", (), {"command": "git push --force origin main"})
    assert err is not None, "强制推送应被拒绝"
    assert perms.check("run_shell", (), {"command": "git status"}) is None, (
        "只读命令应放行"
    )


def test_permission_plan_mode_denies_write():
    perms = PermissionManager(headless=True, plan_mode=True)
    assert perms.check("write_file", (), {"filename": "a.py"}) is not None
    assert perms.check("run_shell", (), {"command": "ls"}) is not None, (
        "计划模式 shell 应拒绝"
    )
    assert perms.check("read_file", (), {"filename": "a.py"}) is None, (
        "计划模式读应放行"
    )


def test_permission_ask_headless_denies():
    perms = PermissionManager(headless=True)
    assert perms.check("run_shell", (), {"command": "npm install"}) is not None


def test_permission_allow_rules():
    perms = PermissionManager(headless=True)
    perms.load_settings(
        {
            "permissions": {
                "allow": ["Bash(git status)", "Read(*)"],
                "defaultMode": "default",
            }
        }
    )
    assert perms.check("run_shell", (), {"command": "git status"}) is None
    assert perms.check("read_file", (), {"filename": "x"}) is None
    assert perms.check("run_shell", (), {"command": "npm install"}) is not None


def test_permission_session_always_stable_target():
    """kwargs 顺序不同也应匹配 session_always，避免重复询问。"""
    perms = PermissionManager(headless=True)
    perms.mode = "default"
    # 模拟用户输入 'a'（总是允许）；target 内部已按 key 字母序稳定
    perms._session_always.add("write_file filename=x.py new_string=b old_string=a")
    # 下次调用时 kwargs 顺序不同，但 target 稳定，应匹配 session_always
    assert (
        perms.check(
            "write_file", (), {"old_string": "a", "filename": "x.py", "new_string": "b"}
        )
        is None
    )


def test_permission_default_mode_accepts_edits():
    """默认模式应为 acceptEdits，写文件自动放行，不再每次询问。"""
    perms = PermissionManager(headless=True)
    # 默认 mode 已是 acceptEdits
    assert perms.mode == "acceptEdits"
    assert (
        perms.check(
            "write_file", (), {"filename": "x.py", "old_string": "a", "new_string": "b"}
        )
        is None
    )
    assert perms.check("create_file", (), {"filename": "x.py", "content": "hi"}) is None
    # 但 run_shell 仍需拒绝/询问（headless 下拒绝）
    assert perms.check("run_shell", (), {"command": "npm install"}) is not None


async def test_agent_tool_schema_and_validator():
    agent = create_agent(dict(CONFIG), None)
    names = sorted(t for t in agent._function_toolset.tools)
    for required in ("run_shell", "write_file", "task", "use_skill", "enter_plan_mode"):
        assert required in names, f"缺少工具 {required}"

    from foxcode.tools import permission_validator
    from pydantic_ai.exceptions import ToolFailed

    class FakeCtx:
        def __init__(self, deps):
            self.deps = deps

    with tempfile.TemporaryDirectory() as td:
        deps = _make_deps(Path(td))
        deps.permissions.headless = True

        v = permission_validator("run_shell")
        try:
            await v(FakeCtx(deps), command="npm install")
            raise AssertionError("headless 下应拒绝")
        except ToolFailed:
            pass
        await v(FakeCtx(deps), command="git status")

        vw = permission_validator("write_file")
        deps.permissions.mode = "default"  # 显式切回严格模式测试拒绝逻辑
        try:
            await vw(FakeCtx(deps), filename="x.py", old_string="a", new_string="b")
            raise AssertionError("headless 下写文件应拒绝")
        except ToolFailed:
            pass


def test_plan_mode_prepare_hides_write_tools():
    import tempfile

    class FakeCtx:
        def __init__(self, deps):
            self.deps = deps

    async def main():
        with tempfile.TemporaryDirectory() as td:
            from foxcode.agent import _prepare_main_tools
            from pydantic_ai.tools import ToolDefinition

            defs = [
                ToolDefinition(
                    name="run_shell",
                    parameters_json_schema={"type": "object", "properties": {}},
                ),
                ToolDefinition(
                    name="read_file",
                    parameters_json_schema={"type": "object", "properties": {}},
                ),
                ToolDefinition(
                    name="write_file",
                    parameters_json_schema={"type": "object", "properties": {}},
                ),
            ]
            deps = _make_deps(Path(td), plan_mode=True)
            kept = await _prepare_main_tools(FakeCtx(deps), defs)
            names = {d.name for d in kept}
            assert "run_shell" not in names
            assert "write_file" not in names
            assert "read_file" in names

    asyncio.run(main())


def test_skills_loading():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / ".foxcode" / "skills" / "review"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: code-review\ndescription: 代码审查\n---\n\n# 代码审查\n请检查安全、性能。",
            encoding="utf-8",
        )
        mgr = SkillsManager(Path(td) / ".foxcode" / "skills")
        mgr.load()
        assert "code-review" in mgr.skills
        assert mgr.get("code-review").description == "代码审查"


def test_subagents_loading():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / ".foxcode" / "agents"
        d.mkdir(parents=True)
        (d / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: 代码审查者\n---\n\n你负责审查代码质量。",
            encoding="utf-8",
        )
        mgr = SubAgentManager(d)
        mgr.load()
        assert "reviewer" in mgr.defs
        assert mgr.get("reviewer").description == "代码审查者"


def test_mcp_config_discovery():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / ".foxcode"
        d.mkdir(parents=True)
        (d / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "filesystem": {
                            "command": "npx",
                            "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        servers = discover_mcp_configs(Path(td))
        assert "filesystem" in servers


def test_subagent_agent_readonly_filter():
    from pydantic_ai.tools import ToolDefinition

    from foxcode.subagents import _subagent_prepare

    class FakeCtx:
        deps = None

    agent = create_subagent_agent(dict(CONFIG), None, "test")
    names = sorted(agent._function_toolset.tools)
    assert len(names) >= 20, f"子代理工具过少: {len(names)}"
    kept = {
        t.name
        for t in _subagent_prepare(FakeCtx(), [ToolDefinition(name=n) for n in names])
    }
    for write in ("write_file", "run_shell", "git_commit", "run_tests"):
        assert write not in kept, f"子代理不应暴露写工具 {write}"
    for ro in ("read_file", "search_in_files"):
        assert ro in kept, f"子代理应保留只读工具 {ro}"


def test_headless_cli_args():
    from foxcode.cli import parse_args

    args = parse_args(["-p", "hello", "--cwd", "/tmp", "--model", "gpt-4o"])
    assert args.prompt == "hello"
    assert args.cwd == "/tmp"
    assert args.model == "gpt-4o"


if __name__ == "__main__":
    import json

    tests = [
        test_permission_deny_dangerous,
        test_permission_plan_mode_denies_write,
        test_permission_ask_headless_denies,
        test_permission_allow_rules,
        test_permission_session_always_stable_target,
        test_permission_default_mode_accepts_edits,
        test_skills_loading,
        test_subagents_loading,
        test_mcp_config_discovery,
        test_headless_cli_args,
        test_subagent_agent_readonly_filter,
    ]
    for t in tests:
        t()
        print(f"  ok - {t.__name__}")

    asyncio.run(test_agent_tool_schema_and_validator())
    print("  ok - test_agent_tool_schema_and_validator")
    test_plan_mode_prepare_hides_write_tools()
    print("  ok - test_plan_mode_prepare_hides_write_tools")
    print("ALL TESTS PASSED")
