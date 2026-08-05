# 整体实施计划

## 当前阶段
已完成：所有测试通过，所有 lint 检查通过（ruff + flake8 均为 0 错误）。

## 关键决策
- 每修复一个文件的 bug 集合，立即提交一次 git，保持提交粒度清晰
- 优先修复 F841（未使用变量）、F541（无意义 f-string）、E741（易混淆变量名）
- 同时清理 F401（未使用导入）降低导入开销
- 修复 E402（cli.py 模块级导入位置）以避免条件导入后的相对导入问题
- 修复 E203（切片表达式冒号前空格）以统一代码风格

## 提交记录
1. 修复 agent.py 未使用的 typing.Any 导入
2. 修复 skills.py 易混淆变量名 l 改为 line
3. 修复 code_index.py 未使用的异常变量 e
4. 修复 copy_file.py 未使用的 Path 导入
5. 修复 format.py 未使用的 Path 导入
6. 修复 health.py 未使用的 json 导入和无意义 f-string
7. 修复 lsp_bridge.py 易混淆变量名和未使用 jedi 导入方式
8. 修复 multi_edit.py 未使用的 re 导入
9. 修复 preview.py 未使用的 Path 导入
10. 修复 review.py 无意义 f-string
11. 修复 tests.py 未使用的 FileNotFoundError 变量 e
12. 修复 tree.py 未使用的 rel 变量
13. 修复 cli.py 多项代码质量问题（未使用导入、变量、变量名、f-string）
14. 修复 cli.py E402 模块级导入位置问题
15. 修复多处 E203 切片表达式中冒号前多余空格
