# FoxCode - AI 编码代理工具

基于 **pydantic-ai** 框架的终端交互式 AI 编程助手，支持文件操作、命令执行、网络搜索等功能。

## 功能

- **文件操作**: 读取、创建、编辑（精确替换/覆盖）、追加、删除、重命名、列出文件
- **命令执行**: 运行 shell 命令、执行脚本文件（支持 Python/JS/TS/Go/Rust 等）
- **网络搜索**: 通过 Bing 搜索获取信息（无需 API Key）
- **撤销恢复**: 支持操作撤销，避免误操作
- **对话记忆**: 保持多轮对话上下文
- **结构化输出**: AI 返回清晰的 ActionPlan（解释、修改文件、代码片段）

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

## 使用方法

在交互式终端中输入你的编程需求，AI 会自动调用工具完成。

### CLI 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
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

## 项目结构

```
foxcode/
├── main.py                 # 入口文件
├── pyproject.toml          # 项目配置
├── requirements.txt        # 依赖列表
├── .env                    # API 配置（勿提交）
├── .env.example            # 配置示例
├── workspace/              # 工作区目录
└── src/foxcode/
    ├── __init__.py
    ├── __main__.py         # python -m 入口
    ├── config.py           # 配置加载
    ├── models.py           # 数据模型
    ├── agent.py            # Agent 创建与工具注册
    ├── cli.py              # 交互式终端
    ├── session.py          # 会话管理
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
        └── security.py     # 安全检测工具
```
