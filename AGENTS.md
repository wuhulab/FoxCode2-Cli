# FoxCode - Agent 开发指南

## 项目概述

FoxCode 是一个基于 `pydantic-ai` 框架的终端交互式 AI 编程助手，目标是成为与 Claude Code 同级别的 AI 编码代理工具。

## 架构设计

### 核心模块

```
foxcode/
├── cli.py              # 交互式终端 / headless 入口，命令解析与主循环
├── agent.py            # Agent 创建与工具注册
├── models.py           # 数据模型：ActionPlan, UndoManager, ToolTracker, WorkspaceDeps
├── config.py           # 配置加载（.env + .foxcode/settings.json + Rules.md/Memory.md）
├── permissions.py      # 权限确认系统：allow/ask/deny 规则、高危行为拦截
├── session.py          # 会话管理：保存/加载/导出
├── skills.py           # Skills 管理：可复用知识/工作流（支持目录型多文件 skill）
├── builtin_skills/     # 内置 Skills（随包分发）
│   └── novel-control-station/   # 中文长篇小说创作控制中枢
├── subagents.py        # 子代理管理：隔离上下文的只读探索
├── goal.py             # Goal 模式：独立上下文验收 AI，确认目标完成
├── mcp_manager.py      # MCP 服务器加载与权限门控
├── tools/              # AI 可用工具集
└── vscode-extension/   # VS Code 插件（一键右侧启动 foxcode）
    ├── __init__.py     # 工具注册辅助函数
    ├── file_ops.py     # 文件操作：读、写、创建、删除、重命名、追加、范围读取
    ├── shell.py        # 命令执行：shell、脚本文件运行
    ├── search.py       # 网络搜索（Bing）
    ├── fetch.py        # URL 抓取
    ├── git.py          # Git 操作
    ├── grep.py         # 项目内文本搜索（ripgrep + Python fallback）
    ├── tree.py         # 目录树展示
    ├── copy_file.py    # 文件/目录复制
    ├── tests.py        # 测试运行（自动检测框架）
    ├── format.py       # 代码格式化
    ├── deps.py         # 依赖安装
    ├── undo.py         # 撤销管理
    ├── mode.py         # 计划模式切换
    ├── security.py     # 安全检测
    ├── memory.py       # AI 长期记忆（update_memory：维护 .foxcode/Memory.md）
    └── multi_edit.py   # 多文件批量编辑、diff 应用、批量创建
```

### 数据流

1. `cli.py` 解析参数 → 加载配置 → 构建运行时组件
2. 创建 `Agent`（`agent.py`）→ 注册所有工具
3. 用户输入 → `_run_status_loop` → `agent.run()` 或 `agent.run_stream()`
4. AI 调用工具 → `permission_validator` 门控 → 执行 → 返回结果
5. 结果 → 更新 `ActionPlan` → 展示给用户

## 编码规范

### 工具注册规范

所有工具必须：
1. 在 `tools/` 目录下创建模块
2. 实现 `register(agent)` 函数
3. 使用 `permission_validator` 作为 `args_validator`
4. 使用 `log_tool` 记录调用
5. 返回字符串结果（错误以 `"错误:"` 前缀开头）
6. 文件操作使用 `_resolve_safe_path` 防止路径越权

工具注册统一通过 `tools/__init__.py` 的 `register_core_tools(agent)`（主代理与子代理共享的核心 12 模块）
与 `register_all_tools(agent)`（主代理全量注册）完成，避免注册列表在多个文件中漂移。
新增核心读/写工具时加入 `_CORE_TOOLS` 元组即可同时注册到主代理与子代理。
阻塞型子进程调用统一使用 `run_subprocess()`（内部 `asyncio.to_thread`），避免冻结事件循环。

```python
def register(agent):
    @agent.tool(args_validator=permission_validator("tool_name"))
    async def tool_name(ctx: RunContext[WorkspaceDeps], param: str) -> str:
        log_tool(ctx, "tool_name", param)
        # 实现
        return "结果"
```

### 新增工具步骤

1. 在 `tools/` 下创建模块，实现 `register(agent)`
2. 核心读/写工具加入 `tools/__init__.py` 的 `_CORE_TOOLS` 元组；主代理增强工具加入 `register_all_tools`
3. 在 `models.py` 的 `STATUS_NAMES` 和 `COUNT_LABELS` 中添加状态名
4. 如果涉及写操作，在 `permissions.py` 的 `WRITE_TOOLS` 中添加
5. 在 `system_prompt.md` 中更新规则说明
6. 更新 `README.md`

### 新增内置 Skill 步骤

1. 在 `builtin_skills/` 下创建目录（`<skill-name>/`），放入 `SKILL.md` 及附属文件
2. 在 `pyproject.toml` 的 `[tool.setuptools.package-data]` 中确认包含 `builtin_skills/**/*`
3. 更新 `AGENTS.md` 的架构图和内置清单
4. 测试 `list_skills` 和 `use_skill` 能正常加载新 skill

### 权限系统

- `READ_ONLY_TOOLS`: 只读工具自动放行
- `WRITE_TOOLS`: 写/执行工具默认需要确认
- `BUILTIN_DANGEROUS`: 内置高危命令无条件拦截
- `PermissionManager.decide()`: deny > ask > allow 优先级
- 受保护文件（`.foxcode/Rules.md` 用户规则只读、`.foxcode/Memory.md` 仅允许 `update_memory` 工具修改）：在 `file_ops.py` 的 `check_protected_write()` 中统一拦截，覆盖 file_ops/multi_edit/copy_file 全部写工具

## 与 Claude Code 的差距清单

### 已实现 ✅

- [x] 文件操作（读、写、创建、删除、重命名、复制）
- [x] 命令执行（shell、脚本）
- [x] 网络搜索与 URL 抓取
- [x] 权限系统（allow/ask/deny）
- [x] 计划模式
- [x] 子代理（只读）
- [x] Skills（支持单文件 skill 与目录型多文件 skill，内置 `novel-control-station`）
- [x] MCP 支持
- [x] Headless 模式
- [x] Git 基本操作
- [x] 代码搜索（grep）
- [x] 目录树
- [x] 测试运行
- [x] 代码格式化
- [x] 依赖安装
- [x] 多文件编辑（multi_edit、apply_diff、batch_create）
- [x] 撤销管理
- [x] 会话管理
- [x] 流式输出
- [x] 用量统计
- [x] 安全检测
- [x] 代码库索引（code_index）- 符号索引、项目结构理解
- [x] 智能上下文压缩 - 长对话自动摘要（支持 MAX_CONTEXT_TOKENS 阈值强制压缩）
- [x] Thinking/Reasoning 展示模式
- [x] 增强 diff 可视化（变更摘要）
- [x] 命令行自动补全（prompt_toolkit）
- [x] Web 预览工具
- [x] @filename 引用语法 - 提示中自动读取文件
- [x] 自动 Git 状态提示 - 交互模式入口检测未提交变更
- [x] AI 代码审查（review_changes）
- [x] 项目健康检查（project_health）
- [x] LSP 集成（基于 jedi） - 跳转定义、查找引用、类型信息
- [x] IDE 插件（VSCode）
- [x] Goal 模式（/goal）- AI 完成后由独立上下文验收 AI 确认目标，未完成则继续直到达成；支持 goal.md/plan.md/todo.md 持久化以抵抗上下文压缩，每轮自动 git 提交进度检查点
- [x] 项目记忆与用户规则 - `.foxcode/Memory.md` 为 AI 可写记忆（通过 `update_memory` 工具，普通文件工具拦截）；`.foxcode/Rules.md` 为用户规则（AI 只读，所有文件写工具强制拦截）

├── vscode-extension/   # VS Code 插件
│   ├── package.json
│   ├── extension.js
│   └── README.md

## 构建与测试

```bash
# 安装依赖
pip install -r requirements.txt

# 开发模式安装
pip install -e .

# 运行
python -m foxcode
# 或
foxcode
```

## 注意事项

- 所有文件操作限制在 `workspace_dir` 内（通过 `_resolve_safe_path`）
- 子代理默认为只读模式
- MCP 工具调用也受权限系统门控
- Headless 模式下需要确认的操作会被拒绝
- 工具注册顺序影响 AI 的认知优先级
