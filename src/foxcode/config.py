import json
import os
import time
from pathlib import Path
from dotenv import load_dotenv

# NOTE:提前加载 .env 环境变量，供后续配置读取
load_dotenv()

# NOTE:内置免费 API 端点（开箱即用，无需用户自行配置密钥）
BUILTIN_FREE_BASE_URL = "https://fai.shunx.top/v1"
BUILTIN_FREE_API_KEY = "sk-C4Dy0S5OFKJ7QoPu8erQc2tTDklW2fBIry34CA8tmFcC1tGr"

# NOTE:项目配置短 TTL 缓存，避免初始化阶段或切换配置时重复读取磁盘
_PROJECT_CONFIG_CACHE_TTL = 10.0
_project_config_cache: dict[str, tuple[float, dict]] = {}


# NOTE:从工作区 .foxcode/ 目录加载项目级配置（指南、规则、记忆、设置、自定义命令）
def load_project_config(workspace_dir: Path) -> dict:
    key = str(workspace_dir.resolve())
    now = time.monotonic()
    entry = _project_config_cache.get(key)
    if entry is not None and now - entry[0] < _PROJECT_CONFIG_CACHE_TTL:
        return entry[1]

    foxcode_dir = workspace_dir / ".foxcode"
    config = {
        "instructions": "",
        "settings": {},
        "commands": {},
        "rules": "",
        "memory": "",
    }

    instructions_file = foxcode_dir / "instructions.md"
    if instructions_file.exists():
        try:
            config["instructions"] = instructions_file.read_text(
                encoding="utf-8"
            ).strip()
        except Exception:
            pass

    rules_file = foxcode_dir / "Rules.md"
    if rules_file.exists():
        try:
            config["rules"] = rules_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    memory_file = foxcode_dir / "Memory.md"
    if memory_file.exists():
        try:
            config["memory"] = memory_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    settings_file = foxcode_dir / "settings.json"
    if settings_file.exists():
        try:
            config["settings"] = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    config["commands"] = load_custom_commands(foxcode_dir)

    _project_config_cache[key] = (now, config)
    return config


# NOTE:加载 .foxcode/commands/ 目录下的自定义快捷命令（每文件对应一个 /command）
def load_custom_commands(foxcode_dir: Path) -> dict[str, str]:
    commands = {}
    commands_dir = foxcode_dir / "commands"
    if not commands_dir.is_dir():
        return commands
    try:
        for f in sorted(commands_dir.iterdir()):
            if f.suffix.lower() in (".md", ".txt") and f.is_file():
                name = f.stem.lower()
                content = f.read_text(encoding="utf-8").strip()
                if content:
                    commands[name] = content
    except Exception:
        pass
    return commands


# NOTE:将项目级 settings.json 中的参数覆盖到运行时配置（模型、超时、流式输出等）
def apply_project_settings(config: dict, project_config: dict) -> dict:
    settings = project_config.get("settings", {})
    if not settings:
        return config
    config = dict(config)
    for key in (
        "model",
        "base_url",
        "api_key",
        "temperature",
        "shell_timeout",
        "request_timeout",
    ):
        if key in settings:
            config[key] = settings[key]
    if "stream_output" in settings:
        val = settings["stream_output"]
        config["stream_output"] = (
            str(val).lower() in ("true", "1", "yes")
            if not isinstance(val, bool)
            else val
        )
    return config


# NOTE:从环境变量与 .env 组装全局运行时配置，提供各类默认值兜底
def load_config():
    try:
        temperature = float(os.getenv("TEMPERATURE", "0.7"))
    except ValueError:
        temperature = 0.7

    try:
        shell_timeout = int(os.getenv("SHELL_TIMEOUT", "30"))
    except ValueError:
        shell_timeout = 30

    try:
        request_timeout = float(os.getenv("REQUEST_TIMEOUT", "120"))
    except ValueError:
        request_timeout = 120

    try:
        max_context_tokens = int(os.getenv("MAX_CONTEXT_TOKENS", "100000"))
    except ValueError:
        max_context_tokens = 100000

    stream_output = os.getenv("STREAM_OUTPUT", "false").lower() in ("true", "1", "yes")

    # NOTE:聚合全部配置项，供 cli.py 与 Agent 使用
    return {
        "model": os.getenv("OPENAI_MODEL", ""),
        "base_url": os.getenv("OPENAI_BASE_URL", BUILTIN_FREE_BASE_URL),
        "api_key": os.getenv("OPENAI_API_KEY", BUILTIN_FREE_API_KEY),
        "workspace_dir": Path(os.getenv("WORKSPACE_DIR", ".")).resolve(),
        "temperature": temperature,
        "shell_timeout": shell_timeout,
        "request_timeout": request_timeout,
        "max_context_tokens": max_context_tokens,
        "stream_output": stream_output,
        "http_proxy": os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or "",
        "https_proxy": os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "",
        "no_proxy": os.getenv("NO_PROXY") or os.getenv("no_proxy") or "",
    }
