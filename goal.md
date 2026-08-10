# 目标：大幅优化 Foxcode

## 验收标准
1. cli.py 从 93KB 拆分为职责清晰的多个模块（ui、commands、agent_runner、session、git_helpers）
2. 消除创建 Agent 的重复代码，统一通过工厂函数创建
3. 添加文件内容缓存（LRU），减少重复磁盘读取
4. 完善核心模块的类型注解，消除不必要的 Any
5. 优化 _expand_file_refs 和 _parse_image_refs 的性能
6. 所有现有 41 个测试继续通过
7. 新增至少 5 个单元测试覆盖拆分后的模块
8. ruff + flake8 代码检查通过

## 当前完成状态
- [ ] Phase 1: 架构重构 - 拆分 cli.py
- [ ] Phase 2: 性能优化 - 缓存、减少重复计算
- [ ] Phase 3: 代码质量 - 类型注解、常量提取
- [ ] Phase 4: 测试增强

## 已完成事项
- 项目全面调研完成，识别主要瓶颈

## 未完成事项
- cli.py 拆分
- Agent 创建去重
- 缓存机制
- 类型注解完善
- 测试增强
