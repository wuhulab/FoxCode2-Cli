import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


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

    stream_output = os.getenv("STREAM_OUTPUT", "false").lower() in ("true", "1", "yes")

    return {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "workspace_dir": Path(os.getenv("WORKSPACE_DIR", ".")).resolve(),
        "temperature": temperature,
        "shell_timeout": shell_timeout,
        "request_timeout": request_timeout,
        "stream_output": stream_output,
        "http_proxy": os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or "",
        "https_proxy": os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "",
        "no_proxy": os.getenv("NO_PROXY") or os.getenv("no_proxy") or "",
    }
