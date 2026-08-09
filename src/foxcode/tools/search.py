import re
import html
from urllib.parse import quote_plus
from pydantic_ai import RunContext
from ..models import WorkspaceDeps
from . import log_tool, permission_validator


# NOTE:注册网页搜索工具：通过必应搜索抓取结果，解析标题/链接/摘要
# NOTE:注册网络搜索工具（Bing），解析 HTML 提取标题、链接与摘要片段
def register(agent):
    @agent.tool(args_validator=permission_validator("web_search"))
    async def web_search(
        ctx: RunContext[WorkspaceDeps], query: str, num_results: int = 5
    ) -> str:
        log_tool(ctx, "web_search", f'"{query}"')
        if num_results < 1:
            num_results = 1
        if num_results > 15:
            num_results = 15
        try:
            url = (
                f"https://www.bing.com/search?q={quote_plus(query)}&count={num_results}"
            )
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            response = await ctx.deps.http_client.get(
                url, headers=headers, follow_redirects=True, timeout=15
            )
            text = response.text

            results = []
            pattern = r'<li class="b_algo">(.*?)</li>'
            items = re.findall(pattern, text, re.DOTALL)

            for item in items[:num_results]:
                title_match = re.search(
                    r'<h2><a[^>]*href="([^"]*)"[^>]*>(.*?)</a></h2>', item, re.DOTALL
                )
                snippet_match = re.search(r"<p[^>]*>(.*?)</p>", item, re.DOTALL)
                if title_match:
                    link = title_match.group(1)
                    title = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
                    snippet = ""
                    if snippet_match:
                        snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
                    results.append(
                        f"标题: {html.unescape(title)}\n链接: {link}\n摘要: {html.unescape(snippet)}"
                    )

            if results:
                return "\n---\n".join(results)
            return f"未找到关于 '{query}' 的搜索结果"
        except Exception as e:
            return f"搜索失败: {e}"
