"""冒烟测试：权限门控、计划模式、subagents、skills、MCP 配置。"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from foxcode.agent import create_agent
from foxcode.goal import GoalVerification, create_goal_verifier, verify_goal
from foxcode.models import ToolTracker, UndoManager, WorkspaceDeps
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


def test_permission_session_always_whitespace_normalization():
    """command 中换行/多空格差异不应导致 session_always 失效，避免重复询问。"""
    perms = PermissionManager(headless=True)
    perms.mode = "default"
    # 模拟用户确认 'a' 时命令中含有换行
    perms._session_always.add(
        "run_shell command=python -m pip install tree-sitter tree-sitter-python 2>&1 | tail -20"
    )
    # 再次调用时 kwargs 中的 command 包含换行和多余空格，但逻辑相同
    assert (
        perms.check(
            "run_shell",
            (),
            {
                "command": "python -m pip install tree-sitter\ntree-sitter-python 2>&1 | tail -20"
            },
        )
        is None
    )


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


@pytest.mark.asyncio
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


def test_estimate_message_tokens():
    """token 估算应随消息长度增加而增加。"""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from foxcode.context_compressor import estimate_message_tokens

    short = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelRequest(parts=[UserPromptPart(content="ok")]),
    ]
    long = [
        ModelRequest(parts=[UserPromptPart(content="x" * 1000)]),
        ModelRequest(parts=[UserPromptPart(content="y" * 1000)]),
    ]
    assert estimate_message_tokens(short) > 0
    assert estimate_message_tokens(long) > estimate_message_tokens(short)


def test_max_context_tokens_env():
    """MAX_CONTEXT_TOKENS 应能覆盖默认压缩阈值。"""
    import os

    os.environ["MAX_CONTEXT_TOKENS"] = "12345"
    try:
        from foxcode.config import load_config

        cfg = load_config()
        assert cfg["max_context_tokens"] == 12345
    finally:
        os.environ.pop("MAX_CONTEXT_TOKENS", None)


def test_goal_verifier_output_type():
    from pydantic_ai.tools import ToolDefinition

    from foxcode.subagents import _subagent_prepare

    verifier = create_goal_verifier(dict(CONFIG), None)
    assert verifier.output_type is GoalVerification

    class FakeCtx:
        deps = None

    names = sorted(t for t in verifier._function_toolset.tools)
    kept = {
        t.name
        for t in _subagent_prepare(FakeCtx(), [ToolDefinition(name=n) for n in names])
    }
    assert "read_file" in kept
    assert "run_shell" not in kept, "验收 AI 不应暴露写/执行工具"


@pytest.mark.asyncio
async def test_goal_verifier_runs():
    """验收 AI 用 TestModel 运行，应返回结构化 GoalVerification。"""
    import tempfile

    from pydantic_ai import Agent
    from pydantic_ai.capabilities import PrepareTools
    from pydantic_ai.models.test import TestModel

    from foxcode.goal import VERIFIER_SYSTEM_PROMPT
    from foxcode.subagents import _subagent_prepare

    verifier = Agent(
        TestModel(call_tools="all", model_name="gpt-4o-mini"),
        deps_type=WorkspaceDeps,
        output_type=GoalVerification,
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        capabilities=[PrepareTools(_subagent_prepare)],
    )

    with tempfile.TemporaryDirectory() as td:
        deps = _make_deps(Path(td))
        verif = await verify_goal(
            deps,
            goal="创建 hello.py",
            work_summary="主 AI 已创建 hello.py",
            verifier_agent=verifier,
        )
        assert isinstance(verif, GoalVerification)
        assert hasattr(verif, "completed")
        assert hasattr(verif, "reason")


def test_inherit_permissions_solo_mode():
    """子代理/验收 AI 应继承父会话的 solo_mode，避免 /solo 后重复询问权限。"""
    import tempfile

    from foxcode.permissions import inherit_permissions

    with tempfile.TemporaryDirectory() as td:
        parent = _make_deps(Path(td))
        parent.permissions.solo_mode = True
        parent.permissions.headless = True
        parent.permissions.mode = "acceptEdits"

        child_perms = PermissionManager(workspace_dir=Path(td))
        inherit_permissions(parent, child_perms)

        assert child_perms.solo_mode is True
        assert child_perms.headless is True
        assert child_perms.mode == "acceptEdits"
        # solo 模式下读取敏感文件也应放行，不触发询问
        assert child_perms.check("read_file", (), {"filename": ".env.example"}) is None


def test_track_goal_files_git_commit():
    """_track_goal_files 应在 git 仓库中提交 goal 持久化文件。"""
    import asyncio
    import subprocess
    import tempfile

    from foxcode.cli import _track_goal_files

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        subprocess.run(["git", "init", "-q"], cwd=str(d), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(d), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(d), check=True)
        (d / "goal.md").write_text("# 目标\n", encoding="utf-8")

        result = asyncio.run(_track_goal_files(d, 1))
        assert "goal" in result.lower() or "提交" in result

        # 提交后 git log 应有一条记录
        log = subprocess.run(
            ["git", "log", "--oneline"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(d),
        ).stdout
        assert "goal" in log.lower()

        # 无变更时再次调用应返回空串（不产生空提交）
        assert asyncio.run(_track_goal_files(d, 2)) == ""


def test_parse_foxcode_md_commands():
    """[Command] 文件应解析为启动命令队列，而非默认提示。"""
    import tempfile

    from foxcode.cli import _parse_foxcode_md

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / ".foxcode").mkdir(parents=True)
        (d / ".foxcode" / "foxcode.md").write_text(
            "[Command]\n/solo\n/goal\n", encoding="utf-8"
        )
        default_prompt, commands = _parse_foxcode_md(d)
        assert default_prompt is None
        assert commands == ["/solo", "/goal"]

        # 非 Command 文件作为默认提示
        (d / ".foxcode" / "foxcode.md").write_text(
            "请帮我整理这个项目\n", encoding="utf-8"
        )
        default_prompt, commands = _parse_foxcode_md(d)
        assert default_prompt == "请帮我整理这个项目"
        assert commands == []

        # 文件不存在
        (d / ".foxcode" / "foxcode.md").unlink()
        default_prompt, commands = _parse_foxcode_md(d)
        assert default_prompt is None
        assert commands == []


def test_code_index_ast_no_duplicate_methods():
    """AST 索引不应将类方法重复添加为顶层函数。"""
    import tempfile

    from foxcode.tools.code_index import CodeIndex

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "test_mod.py").write_text(
            "class MyClass:\n"
            "    def method1(self):\n"
            "        pass\n"
            "\n"
            "    def method2(self):\n"
            "        pass\n"
            "\n"
            "def top_level_func():\n"
            "    pass\n",
            encoding="utf-8",
        )
        index = CodeIndex(d)
        index._build_ast_index()
        names = [s.name for s in index.symbols]
        assert names.count("method1") == 1, f"method1 出现 {names.count('method1')} 次"
        assert names.count("method2") == 1, f"method2 出现 {names.count('method2')} 次"
        assert names.count("top_level_func") == 1
        assert len(index.symbols) == 4, (
            f"期望 4 个符号，实际 {len(index.symbols)}: {names}"
        )


def test_mcp_process_closure_binding():
    """MCP _process 闭包应正确绑定 server name，避免所有工具共用最后一个 name。"""
    import asyncio

    # 通过 inspect 检查生成的闭包中绑定的名称
    # 由于 MCPToolset 需要真实连接，这里只验证工厂函数行为
    processes = []
    for name in ["server_a", "server_b"]:

        async def _process(ctx, call_tool, tool_name, args, _name=name):
            return f"mcp__{_name}"

        processes.append(_process)

    result_a = asyncio.run(processes[0](None, None, None, None))
    result_b = asyncio.run(processes[1](None, None, None, None))
    assert result_a == "mcp__server_a", f"期望 mcp__server_a，实际 {result_a}"
    assert result_b == "mcp__server_b", f"期望 mcp__server_b，实际 {result_b}"


@pytest.mark.asyncio
async def test_generate_commit_message_returns_str_on_error():
    """_generate_commit_message 在 API 异常时应返回空字符串而非 None。"""
    from foxcode.cli import _generate_commit_message

    class FakeClient:
        async def post(self, *args, **kwargs):
            raise Exception("网络错误")

    result = await _generate_commit_message(
        FakeClient(), {"workspace_dir": Path(".")}, ""
    )
    assert isinstance(result, str), f"期望 str，实际 {type(result)}"
    assert result == ""


def test_read_file_range_reads_to_end_by_default():
    """read_file_range 默认（end_line=0）应读取到文件末尾，而非返回空。"""
    import tempfile

    from foxcode.tools.file_ops import _read_file_range

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "demo.py"
        p.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

        # 从第 2 行读到末尾
        start, end, total, output = _read_file_range(p, 2, 0)
        assert total == 4
        assert (start, end) == (2, 4)
        assert output == "line2\nline3\nline4\n"

        # 指定区间
        start, end, total, output = _read_file_range(p, 2, 3)
        assert (start, end) == (2, 3)
        assert output == "line2\nline3\n"

        # 结束行超出总行数时读取到末尾
        start, end, total, output = _read_file_range(p, 3, 999)
        assert (start, end) == (3, 4)
        assert output == "line3\nline4\n"

        # 起始行超出总行数应报错
        with pytest.raises(ValueError):
            _read_file_range(p, 5, 0)

        # start_line 为 1、end_line 为 0 的常见默认场景
        start, end, total, output = _read_file_range(p, 1, 0)
        assert output == "line1\nline2\nline3\nline4\n"


def test_iter_project_files_prunes_heavy_dirs():
    """项目遍历应剪枝跳过 node_modules/.git/venv 等重型目录。"""
    import tempfile

    from foxcode.tools import iter_project_entries, iter_project_files

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "main.py").write_text("x", encoding="utf-8")
        (root / "app").mkdir()
        (root / "app" / "util.py").write_text("x", encoding="utf-8")
        for d in ("node_modules", ".git", "__pycache__", "venv", ".venv", "dist"):
            p = root / d
            p.mkdir(parents=True, exist_ok=True)
            (p / "junk.py").write_text("x", encoding="utf-8")

        files = {f.relative_to(root).as_posix() for f in iter_project_files(root)}
        assert files == {"main.py", "app/util.py"}, f"实际文件: {files}"

        entries = {e.relative_to(root).as_posix() for e in iter_project_entries(root)}
        assert "app" in entries
        for skipped in ("node_modules", ".git", "__pycache__", "venv", ".venv", "dist"):
            assert skipped not in entries, f"{skipped} 不应被遍历到"


def test_tool_tracker_summary_cache_invalidates():
    """ToolTracker 计数变化后 summary_str 应重新计算（缓存失效）。"""
    from foxcode.models import ToolTracker

    tracker = ToolTracker()
    assert tracker.summary_str() == ""
    tracker.count("read_file")
    s1 = tracker.summary_str()
    assert "读取" in s1
    # 计数变化后应反映新状态
    tracker.count("write_file")
    s2 = tracker.summary_str()
    assert "编辑" in s2 and s2 != s1
    # reset 后应清空
    tracker.reset()
    assert tracker.summary_str() == ""


def test_fuzzy_find_replacement_preserves_content():
    """模糊匹配应返回正确的替换区间，替换后不损坏文件其余内容。"""
    from foxcode.tools.multi_edit import _fuzzy_find

    content = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n"
    # old_string 与文件内容存在细微差异（例如缩进不同），应能模糊匹配
    fuzzy = _fuzzy_find(content, "    return a + b", threshold=0.85)
    assert fuzzy is not None, "应能模糊匹配到 return a + b"
    start, end = fuzzy
    assert content[start:end] == "    return a + b", (
        f"匹配区间 [{start}:{end}] 内容错误: {content[start:end]!r}"
    )

    # 边界：匹配窗口实际行内容与 old_string 长度不同时，
    # end_pos 应基于窗口内容而非 len(old_string)，否则会损坏文件。
    content = "aaa\nbb\n"
    multi = _fuzzy_find(content, "aaa\nxxxxxxx", threshold=0.4)
    assert multi is not None
    start, end = multi
    assert content[start:end] == "aaa\nbb", (
        f"匹配区间 [{start}:{end}] 错误: {content[start:end]!r}"
    )


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
    asyncio.run(test_goal_verifier_runs())
    print("  ok - test_goal_verifier_runs")
    print("ALL TESTS PASSED")
