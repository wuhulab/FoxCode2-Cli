import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def load_config():
    return {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "workspace_dir": Path(os.getenv("WORKSPACE_DIR", "workspace")),
        "temperature": float(os.getenv("TEMPERATURE", "0.7")),
        "shell_timeout": int(os.getenv("SHELL_TIMEOUT", "30")),
    }
