from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from .models import ActionPlan, WorkspaceDeps


def create_agent(config: dict) -> Agent[WorkspaceDeps, ActionPlan]:
    model = OpenAIChatModel(
        config["model"],
        provider=OpenAIProvider(
            base_url=config["base_url"],
            api_key=config["api_key"],
        ),
    )

    agent: Agent[WorkspaceDeps, ActionPlan] = Agent(
        model,
        deps_type=WorkspaceDeps,
        output_type=ActionPlan,
        system_prompt=(
            "你是一个专业的 AI 编程助手，可以帮助用户完成各种编程任务。"
            "你可以读取、创建、编辑、删除文件，执行 shell 命令，搜索网络等。"
            "\n\n"
            "重要规则：\n"
            "1. 所有文件操作都已经自动在工作环境，不要再询问用户了\n"
            "2. 在修改代码前，先使用 read_file 或 list_files 了解现有代码。\n"
            "3. 使用 write_file 时提供足够的上下文确保 old_string 唯一匹配。\n"
            "4. 使用 write_file_complete 可覆盖整个文件。\n"
            "5. 创建新文件使用 create_file。\n"
            "6. 修改文件后，使用 run_shell 或 run_file 测试代码。\n"
            "7. 如果操作出错，可以使用 undo_last 撤销。\n"
            "8. 每次操作后，在 ActionPlan 中清晰说明做了什么、修改了哪些文件。"
        ),
        model_settings=ModelSettings(temperature=config["temperature"]),
    )

    from .tools import file_ops, shell, search, undo

    file_ops.register(agent)
    shell.register(agent)
    search.register(agent)
    undo.register(agent)

    return agent
