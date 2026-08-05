# 目标：添加启动后自动加载 .foxcode/foxcode.md 文件作为默认命令

## 验收标准
1. 启动交互模式时，自动检测工作目录下 `.foxcode/foxcode.md` 文件是否存在
2. 若存在且内容非空，自动将其内容作为第一条用户提示发送给 AI
3. 正常显示加载提示信息（如字符数）
4. 不影响原有的 headless 模式和其他命令行参数行为
5. 代码通过现有测试

## 当前完成状态
- [x] 读取并理解现有启动逻辑
- [x] 实现自动加载 foxcode.md 功能
- [x] 验证功能正常（23 个冒烟测试全部通过，ruff + flake8 通过）
- [x] 更新持久化文件

## 已完成事项
- 修改 `cli.py` 的 `_run_interactive`，在交互循环启动前检测 `.foxcode/foxcode.md`
- 使用 `default_prompt` 机制在 `_run_loop` 第一次迭代时自动注入文件内容
- 保持与现有 prompt 处理流程完全一致（文件引用展开、skill 注入、图片解析、plan_mode 等）
- 仅影响交互模式，headless 模式保持原样

## 未完成事项
- 无
