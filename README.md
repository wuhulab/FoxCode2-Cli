# FoxCode - AI 编码代理工具

基于 **pydantic-ai** 框架的终端交互式 AI 编程助手，支持文件操作、命令执行、网络搜索等功能。

## 功能

- **文件操作**: 读取、创建、编辑（精确替换/覆盖）、追加、删除、重命名、列出文件
- **命令执行**: 运行 shell 命令、执行脚本文件（支持 Python/JS/TS/Go/Rust 等）
- **网络搜索**: 通过 Bing 搜索获取信息（无需 API Key）
- **撤销恢复**: 支持操作撤销，避免误操作
- **对话记忆**: 保持多轮对话上下文
- **结构化输出**: AI 返回清晰的 ActionPlan（解释、修改文件、代码片段）
- **权限确认系统**: allow / ask / deny 规则、高危行为拦截、交互式审批、只读命令自动放行
- **计划模式**: 切换后隐藏所有写工具，AI 只读探索并先给出方案
- **Subagents**: 通过 `.foxcode/agents/` 定义只读子代理，用 `task` 工具调用
- **Skills**: 通过 `.foxcode/skills/` 定义技能，按需注入提示
- **MCP 支持**: 通过 `.foxcode/mcp.json` 接入任意 MCP 服务器（stdio / HTTP）
- **Headless 模式**: 一条命令或管道输入即可无人值守运行，支持 JSON 输出

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

或使用 pip 安装包本身：

```bash
pip install -e .
```

### 2. 配置 API

复制 `.env.example` 为 `.env`，填入你的 API 信息：

```bash
cp .env.example .env
```

编辑 `.env`：

```ini
OPENAI_MODEL=gpt-4o
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-xxxxxxx
```

支持任何 OpenAI 兼容 API（如 DeepSeek、Claude、Kimi 等）。

### 3. 启动

```bash
python main.py
```

或安装后：

```bash
foxcode
```

### Headless 模式（无人值守 / CI）

```bash
# 直接给提示
foxcode -p "列出当前目录所有文件"

# 指定工作目录与模型，JSON 输出
foxcode -p "重构 main.py" --cwd /path/to/project --model gpt-4o --output-format json

# 管道输入
echo "总结 README.md" | foxcode

# 跳过所有权限确认（仅限可信的 CI 环境）
foxcode -p "安装依赖并运行测试" --dangerously-skip-permissions
```

> 说明：headless 模式下需要确认的操作会直接拒绝（除非显式配置了 allow 规则或使用 `--dangerously-skip-permissions`）。

## 权限系统

### 权限模式（`.foxcode/settings.json` 中 `permissions.defaultMode`）

| 模式 | 说明 |
|------|------|
| `default` | 读操作放行，写/执行操作每次询问 |
| `acceptEdits` | 自动接受文件编辑，命令仍询问 |
| `plan` | 拒绝所有写/执行操作 |
| `bypass` | 放行一切（谨慎使用） |

### 规则配置示例

```json
{
  "permissions": {
    "defaultMode": "default",
    "allow": ["Bash(git status)", "Read(*)", "Edit(*)", "Bash(ls .*)"],
    "ask": ["Bash(npm install)", "WebFetch(*)"],
    "deny": ["Bash(rm -rf /)"]
  }
}
```

- 工具名支持通配符（`*`），括号内为正则匹配目标参数
- 内置高危操作（`rm -rf /`、`git push --force`、磁盘格式化等）**无条件拦截**
- 只读 shell 命令（`ls`、`cat`、`git status`、`git diff`、`pip list` 等）默认自动放行
- 交互式审批支持 `y` 允许 / `n` 拒绝 / `a` 本次会话总是允许

## 计划模式

交互中输入 `/plan` 切换。开启后：

- 所有写/执行工具对 AI **不可见**（run_shell、write_file、git 提交等）
- AI 只能读取、搜索、分析，最终返回方案
- AI 也可自行调用 `enter_plan_mode` / `exit_plan_mode` 工具完成"先分析后动手"

## Skills（技能）

`.foxcode/skills/<名称>/SKILL.md` 格式：

```markdown
---
name: code-review
description: 对代码做安全与性能审查
---

# 代码审查流程

1. 先读取变更文件
2. 检查安全与性能问题
3. 输出修复建议
```

- `/skills` 列出所有技能；`/skill <名称>` 把内容注入下一条提示
- AI 也可通过 `list_skills` / `use_skill` 工具按需获取

## Subagents（子代理）

`.foxcode/agents/<名称>.md` 格式（frontmatter 可省略）：

```markdown
---
name: reviewer
description: 代码审查者
model: gpt-4o-mini   # 可选，覆盖默认模型
tools: [read_file]   # 可选，限制可用工具
---

你负责审查代码质量，只读探索后用中文总结。
```

- 默认**只读**（写工具自动过滤），适合隔离上下文的探索任务
- AI 用 `task(prompt, agent="reviewer")` 调用；`/agents` 列出所有定义

## MCP 支持

`.foxcode/mcp.json`（或项目根 `.mcp.json`）格式：

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "${GITHUB_TOKEN}" }
    },
    "filesystem": {
      "url": "https://example.com/mcp"
    }
  }
}
```

- 支持 `stdio`（`command`/`args`/`env`）和 `http`（`url`/`headers`）两种传输
- 支持 `${ENV_VAR}` 环境变量展开（可带默认值 `${VAR:-default}`）
- 工具以 `服务器名__工具名` 暴露给 AI，并受权限系统门控
- `/mcp` 列出已配置的服务器；配置错误会在启动时提示

## 项目配置目录 `.foxcode/`

```
.foxcode/
├── instructions.md    # 项目指南（注入系统提示）
├── settings.json      # 权限与运行参数
├── commands/          # 自定义 /命令（.md 文件，文件名即命令名）
├── skills/            # Skills（每技能一个目录）
├── agents/            # 子代理定义（.md 文件）
├── mcp.json           # MCP 服务器配置
└── sessions/          # 保存的会话
```

## 使用方法

在交互式终端中输入你的编程需求，AI 会自动调用工具完成。

### CLI 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/plan` | 切换计划模式（只读探索，先出方案） |
| `/permissions` | 查看当前权限模式与规则 |
| `/mcp` | 列出已配置的 MCP 服务器 |
| `/skills` | 列出可用 Skills |
| `/skill <名称>` | 将指定 Skill 内容注入下一条提示 |
| `/agents` | 列出可用子代理 |
| `/term` | 切换终端模式 (Ctrl+X 切换) |
| `/commit [信息]` | 暂存所有变更并用 AI 生成提交信息后提交 |
| `/session list` | 列出所有已保存的会话 |
| `/session save [名称]` | 保存当前会话 |
| `/session load <名称>` | 加载指定会话 |
| `/session del <名称>` | 删除指定会话 |
| `/export [文件名]` | 导出当前会话为 Markdown |
| `/undo [n]` | 撤销最近 n 步操作（默认 1 步） |
| `/history` | 显示操作历史 |
| `/usage` | 显示本次会话的 API 用量和费用统计 |
| `/clear` | 清屏 |
| `/exit` 或 `/quit` | 退出（自动保存会话） |

### AI 可用工具

AI 在推理过程中会自动调用以下工具：

- `read_file` - 读取文件内容
- `read_file_range` - 读取指定行范围
- `create_file` - 创建新文件
- `write_file` - 查找替换（要求唯一匹配）
- `write_file_complete` - 覆盖写入整个文件
- `append_file` - 追加内容到文件末尾
- `delete_file` - 删除文件
- `rename_file` - 重命名文件
- `copy_file` - 复制文件或目录
- `list_files` - 列出工作区文件
- `tree` - 以树形结构展示目录（支持深度限制和过滤）
- `run_shell` - 执行 shell 命令
- `run_file` - 运行脚本文件
- `run_tests` - 自动检测并运行测试（pytest/npm/go/cargo 等）
- `format_code` - 格式化代码（black/prettier/gofmt 等）
- `install_deps` - 自动检测并安装依赖（pip/npm/cargo/go 等）
- `web_search` - 搜索网络
- `fetch_url` - 抓取网页内容
- `search_in_files` - 在项目中搜索文本
- `git_status` / `git_diff` / `git_log` / `git_add` / `git_commit` / `git_branch` / `git_checkout` - Git 操作
- `undo_last` - 撤销操作
- `show_history` - 查看操作历史
- `task` - 调用只读子代理（`agent` 参数指定名称）
- `use_skill` / `list_skills` - 获取 / 列出 Skills
- `enter_plan_mode` / `exit_plan_mode` - AI 自主进入 / 退出计划模式
- `mcp__<服务器>__<工具>` - MCP 服务器提供的工具（前缀区分来源）

## 项目结构

```
foxcode/
├── main.py                 # 入口文件
├── pyproject.toml          # 项目配置
├── requirements.txt        # 依赖列表
├── .env                    # API 配置（勿提交）
├── .env.example            # 配置示例
├── workspace/              # 工作区目录
├── tests/                  # 冒烟测试
└── src/foxcode/
    ├── __init__.py
    ├── __main__.py         # python -m 入口
    ├── config.py           # 配置加载
    ├── models.py           # 数据模型
    ├── agent.py            # Agent 创建与工具注册
    ├── cli.py              # 交互式终端 / headless 入口
    ├── session.py          # 会话管理
    ├── permissions.py      # 权限确认系统
    ├── skills.py           # Skills 管理
    ├── subagents.py        # 子代理管理
    ├── mcp_manager.py      # MCP 服务器加载
    └── tools/
        ├── file_ops.py     # 文件操作工具
        ├── shell.py        # 命令执行工具
        ├── search.py       # 网络搜索工具
        ├── fetch.py        # URL 抓取工具
        ├── git.py          # Git 操作工具
        ├── grep.py         # 文件搜索工具
        ├── tree.py         # 目录树工具
        ├── copy_file.py    # 文件复制工具
        ├── tests.py        # 测试运行工具
        ├── format.py       # 代码格式化工具
        ├── deps.py         # 依赖安装工具
        ├── undo.py         # 撤销管理工具
        ├── mode.py         # 计划模式切换工具
        └── security.py     # 安全检测工具
```

# License

AGPLv3