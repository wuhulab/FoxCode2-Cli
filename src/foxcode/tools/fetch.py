import re
import html
from urllib.parse import urlparse
from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool, permission_validator


def _clean_html(text: str) -> str:
    """简单清理 HTML 标签和实体。"""
    text = re.sub(
        r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def register(agent):
    @agent.tool(args_validator=permission_validator("fetch_url"))
    async def fetch_url(
        ctx: RunContext[WorkspaceDeps], url: str, max_length: int = 8000
    ) -> str:
        log_tool(ctx, "fetch_url", url)
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return "错误: 无效的 URL"
        except Exception:
            return "错误: 无效的 URL"

        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            response = await ctx.deps.http_client.get(
                url, headers=headers, follow_redirects=True, timeout=20
            )
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" in content_type:
                text = response.text
            else:
                text = _clean_html(response.text)

            if len(text) > max_length:
                text = text[:max_length] + "\n... (内容已截断)"
            return text
        except Exception as e:
            return f"抓取失败: {e}"
