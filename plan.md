# 整体实施计划

## 当前阶段
已完成：自动加载 `.foxcode/foxcode.md` 功能已实现并通过测试。

## 关键决策
- 在 `_run_interactive` 函数中，构建完 agent 后、进入 `_run_loop` 前，检测 `.foxcode/foxcode.md`
- 使用 `default_prompt` 变量在 `_run_loop` 的第一次迭代中自动注入，避免大幅重构循环逻辑
- 保持与现有 prompt 处理流程完全一致（文件引用展开、skill 注入、图片解析、plan_mode 等）
- 仅影响交互模式，headless 模式保持原样

## 实施步骤
1. [x] 在 `cli.py` 的 `_run_interactive` 中，于 `_run_loop` 定义前读取 `.foxcode/foxcode.md`
2. [x] 修改 `_run_loop`，在 while True 开始时优先使用 `default_prompt`，用完后置空
3. [x] 添加加载成功提示信息
4. [x] 运行测试验证（23 passed，ruff + flake8 通过）
