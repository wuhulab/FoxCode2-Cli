# 整体实施计划

## 当前阶段
所有 bug 已修复并提交，测试全部通过。

## 关键决策
- 将工作目录中的多个未提交修复按功能模块独立提交，共 12 次提交
- 清理了临时测试文件
- 所有 21 个冒烟测试通过

## 修复清单（全部完成）
1. [x] config.py + context_compressor.py: 添加 MAX_CONTEXT_TOKENS 支持
2. [x] mcp_manager.py: 修复 MCP 闭包绑定问题
3. [x] tools/code_index.py: 修复 AST 索引类方法重复问题
4. [x] cli.py: 修复 _generate_commit_message 异常返回类型
5. [x] models.py + tools/copy_file.py: 修复 copy 操作撤销缺失
6. [x] tools/fetch.py: 修复 localhost SSRF 绕过
7. [x] tools/format.py: 修复 JSON 格式化器配置
8. [x] tools/multi_edit.py: 修复非 fuzzy 模式替换逻辑
9. [x] tools/tests.py: 修复测试运行变量引用
10. [x] cli.py: 修复其他交互模式问题（diff 判断、headless http_client、自定义命令匹配等）
11. [x] 测试文件: 添加回归测试
12. [x] 文档更新
