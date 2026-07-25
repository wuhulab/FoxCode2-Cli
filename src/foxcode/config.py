import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def load_config():
    return {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "workspace_dir": Path(os.getenv("WORKSPACE_DIR", ".")).resolve(),
        "temperature": float(os.getenv("TEMPERATURE", "0.7")),
        "shell_timeout": int(os.getenv("SHELL_TIMEOUT", "30")),
        "http_proxy": os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or "",
        "https_proxy": os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "",
        "no_proxy": os.getenv("NO_PROXY") or os.getenv("no_proxy") or "",
    }
