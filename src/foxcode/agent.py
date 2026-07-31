from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from .models import ActionPlan, WorkspaceDeps
import httpx


def create_agent(
    config: dict,
    http_client: httpx.AsyncClient | None = None,
    project_instructions: str = "",
) -> Agent[WorkspaceDeps, ActionPlan]:
    model = OpenAIChatModel(
        config["model"],
        provider=OpenAIProvider(
            base_url=config["base_url"],
            api_key=config["api_key"],
            http_client=http_client,
        ),
    )

    model_settings = ModelSettings(temperature=config["temperature"])

    system_prompt = (
        "你是一个专业的 AI 编程助手，可以帮助用户完成各种编程任务。"
        "你可以读取、创建、编辑、删除、复制文件，执行 shell 命令，搜索网络，操作 Git，在项目中搜索文本等。"
        "\n\n"
        "重要规则：\n"
        "1. 所有文件操作都已经自动在工作环境，不要再询问用户了\n"
        "2. 在修改代码前，先使用 read_file 或 list_files 了解现有代码。\n"
        "3. 对于大文件，优先使用 read_file_range 读取指定行范围，避免一次性加载过多内容。\n"
        "4. 使用 write_file 时提供足够的上下文确保 old_string 唯一匹配。\n"
        "5. 使用 write_file_complete 可覆盖整个文件。\n"
        "6. 创建新文件使用 create_file。\n"
        "7. 需要在项目中查找代码时，使用 search_in_files 快速定位。\n"
        "8. 修改文件后，使用 run_shell 或 run_file 测试代码。\n"
        "9. 如果操作出错，可以使用 undo_last 撤销。\n"
        "10. 查看目录结构时优先使用 tree，比 list_files 更直观。\n"
        "11. 需要运行测试时使用 run_tests，支持自动检测 pytest/npm/go/cargo 等框架。\n"
        "12. 需要格式化代码时使用 format_code，支持 Python/JS/TS/Go/Rust 等。\n"
        "13. 需要安装依赖时使用 install_deps，支持 pip/npm/cargo/go 等。\n"
        "14. 每次操作后，在 ActionPlan 中清晰说明做了什么、修改了哪些文件。"
    )

    if project_instructions:
        system_prompt += (
            "\n\n---\n"
            "## 项目指南\n"
            "以下是项目提供的指南，请严格遵守：\n"
            f"{project_instructions}"
        )

    agent: Agent[WorkspaceDeps, ActionPlan] = Agent(
        model,
        deps_type=WorkspaceDeps,
        output_type=ActionPlan,
        system_prompt=system_prompt,
        model_settings=model_settings,
    )

    from .tools import file_ops, shell, search, undo, git, grep, fetch
    from .tools import tree, copy_file, tests, format as fmt, deps

    file_ops.register(agent)
    shell.register(agent)
    search.register(agent)
    undo.register(agent)
    git.register(agent)
    grep.register(agent)
    fetch.register(agent)
    tree.register(agent)
    copy_file.register(agent)
    tests.register(agent)
    fmt.register(agent)
    deps.register(agent)

    return agent
