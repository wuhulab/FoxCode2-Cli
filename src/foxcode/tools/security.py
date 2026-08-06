import re
from typing import Optional

SECURITY_PATTERNS: list[dict] = [
    {
        "id": "command-injection",
        "name": "命令注入",
        "description": "检测到 shell 命令注入风险",
        "patterns": [
            r"os\.system\s*\(",
            r"os\.popen\s*\(",
            r"subprocess\.call\s*\(",
            r"subprocess\.Popen\s*\(",
            r"eval\s*\(",
            r"exec\s*\(",
            r"compile\s*\(",
        ],
        "severity": "high",
        "context": "code",
    },
    {
        "id": "dangerous-html",
        "name": "危险 HTML/JS",
        "description": "检测到可能存在 XSS 风险的 HTML/JS 代码",
        "patterns": [
            r"innerHTML\s*=",
            r"outerHTML\s*=",
            r"document\.write\s*\(",
            r"<script>",
            r"onerror\s*=",
            r"onload\s*=",
        ],
        "severity": "medium",
        "context": "code",
    },
    {
        "id": "pickle-deserialization",
        "name": "反序列化风险",
        "description": "检测到 pickle/cpickle 反序列化，可能存在安全风险",
        "patterns": [
            r"pickle\.loads?\s*\(",
            r"cpickle\.loads?\s*\(",
            r"shelve\.open\s*\(",
        ],
        "severity": "high",
        "context": "code",
    },
    {
        "id": "sql-injection",
        "name": "SQL 注入风险",
        "description": "检测到可能的 SQL 注入风险（字符串拼接 SQL）",
        "patterns": [
            r'execute\s*\(\s*["\'].*\{.*["\']\s*%',
            r"execute\s*\(\s*[\"'].*\+",
            r"raw\(.*\{.*\)",
        ],
        "severity": "high",
        "context": "code",
    },
    {
        "id": "hardcoded-secret",
        "name": "硬编码密钥",
        "description": "检测到可能的硬编码密钥或密码",
        "patterns": [
            r"(?:api[_-]?key|secret|password|token|credential)\s*[:=]\s*['\"][^'\"]{8,}",
            r"(?:AKIA[0-9A-Z]{16})",
            r"(?:sk-[a-zA-Z0-9]{20,})",
        ],
        "severity": "medium",
        "context": "code",
    },
    {
        "id": "dangerous-shell-cmd",
        "name": "危险命令",
        "description": "检测到危险 shell 命令",
        "patterns": [
            r"\brm\s+-rf\s+/",
            r"\bchmod\s+777\s+",
            r"\bdd\s+if=",
            r"\bmkfs\b",
            r"\bmkswap\b",
            r"\bfdisk\b",
            r"\bmv\s+/\s+",
        ],
        "severity": "high",
        "context": "shell",
    },
    {
        "id": "network-download",
        "name": "网络下载",
        "description": "检测到网络下载命令",
        "patterns": [
            r"\bcurl\s+",
            r"\bwget\s+",
            r"\b(?:Invoke-WebRequest|iwr)\s+",
        ],
        "severity": "low",
        "context": "shell",
    },
]

SHELL_PATTERNS = [p for p in SECURITY_PATTERNS if p["context"] == "shell"]
CODE_PATTERNS = [p for p in SECURITY_PATTERNS if p["context"] == "code"]

# 预编译所有正则，避免每次安全检查时重新编译
_COMPILED_SHELL = [
    {
        **p,
        "compiled": [re.compile(pat, re.IGNORECASE) for pat in p["patterns"]],
    }
    for p in SHELL_PATTERNS
]
_COMPILED_CODE = [
    {
        **p,
        "compiled": [re.compile(pat, re.IGNORECASE) for pat in p["patterns"]],
    }
    for p in CODE_PATTERNS
]


def check_content_security(content: str) -> list[dict]:
    findings = []
    for pattern in _COMPILED_CODE:
        for regex in pattern["compiled"]:
            m = regex.search(content)
            if m:
                line_no = content[: m.start()].count("\n") + 1
                findings.append(
                    {
                        "id": pattern["id"],
                        "name": pattern["name"],
                        "description": pattern["description"],
                        "severity": pattern["severity"],
                        "match": m.group()[:60],
                        "line": line_no,
                    }
                )
                break
    return findings


def check_shell_security(command: str) -> list[dict]:
    findings = []
    for pattern in _COMPILED_SHELL:
        for regex in pattern["compiled"]:
            m = regex.search(command)
            if m:
                findings.append(
                    {
                        "id": pattern["id"],
                        "name": pattern["name"],
                        "description": pattern["description"],
                        "severity": pattern["severity"],
                        "match": m.group()[:60],
                        "line": 0,
                    }
                )
                break
    return findings


def format_security_warnings(findings: list[dict]) -> Optional[str]:
    if not findings:
        return None
    high = [f for f in findings if f["severity"] == "high"]
    medium = [f for f in findings if f["severity"] == "medium"]

    lines = []
    if high:
        lines.append("[bold red]⚠ 高风险安全警告:[/bold red]")
        for f in high:
            loc = f" (第 {f['line']} 行)" if f.get("line") else ""
            lines.append(f"  [red]![/red] {f['name']}{loc}: {f['description']}")
    if medium:
        lines.append("[bold yellow]⚠ 中风险安全提示:[/bold yellow]")
        for f in medium:
            loc = f" (第 {f['line']} 行)" if f.get("line") else ""
            lines.append(f"  [yellow]![/yellow] {f['name']}{loc}: {f['description']}")
    return "\n".join(lines)
