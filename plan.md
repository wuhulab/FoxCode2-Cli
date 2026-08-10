# 整体实施计划

## 当前阶段
Phase 1: 架构重构 - 拆分 cli.py

## 关键决策
- 将 cli.py 拆分为 5 个模块：
  - `cli_ui.py` — 欢迎界面、帮助、ActionPlan 打印、diff 展示、错误打印
  - `cli_commands.py` — 所有 `/xxx` 命令的处理逻辑（除核心循环外）
  - `cli_agent.py` — Agent 创建工厂、运行循环、流式处理、状态更新
  - `cli_session.py` — Goal 模式循环、session 管理、自动保存
  - `cli_git.py` — git commit 信息生成、diff 展示、goal 文件追踪
- 保持 cli.py 作为入口，仅保留参数解析、主分发逻辑和 `_run_loop`
- 使用 `_build_agent` 工厂函数统一创建 Agent，消除 /free、/openai、/model 中的重复代码

## 实施步骤
1. [ ] 创建 `cli_ui.py`，迁移 print_welcome、print_help、print_action_plan 等
2. [ ] 创建 `cli_git.py`，迁移 _generate_commit_message、_show_colored_diff、_track_goal_files
3. [ ] 创建 `cli_agent.py`，迁移 RetryClient、_run_with_narration、_run_status_loop、_build_agent
4. [ ] 创建 `cli_commands.py`，迁移所有命令处理函数
5. [ ] 创建 `cli_session.py`，迁移 session 管理、goal loop
6. [ ] 精简 cli.py，保留入口和核心循环
7. [ ] 运行测试验证无回归
