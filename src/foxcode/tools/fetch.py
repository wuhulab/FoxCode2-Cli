import re
import html
import ipaddress
import socket
from urllib.parse import urljoin, urlparse
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


# 内网 / 保留 / 链路本地 / 云元数据地址段（SSRF 防护）
_PRIVATE_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::ffff:0:0/96"),
]

# 内部主机名后缀：这些域名只可能指向内网服务
_INTERNAL_HOSTNAME_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".lan",
    ".localdomain",
    ".home",
    ".corp",
    ".intranet",
)


def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断 IP 是否属于内网/保留地址段（回环地址除外，预览服务器使用 127.0.0.1）。"""
    for net in _PRIVATE_NETS:
        if ip in net:
            return True
    return False


def _validate_fetch_url(url: str) -> str | None:
    """校验 URL 是否安全。返回错误消息字符串或 None（安全）。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return "错误: 无效的 URL"
    if parsed.scheme not in ("http", "https"):
        return "错误: 仅支持 http/https 协议（已阻止 file://、ftp:// 等协议）"
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        return "错误: 无效的 URL"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if _ip_is_private(ip):
            return f"错误: 目标地址 {host} 属于内网/保留地址，已阻止 SSRF 请求"
    else:
        if host.endswith(_INTERNAL_HOSTNAME_SUFFIXES):
            return f"错误: 目标主机 {host} 属于内部主机名，已阻止 SSRF 请求"
        # 解析 DNS 并检查所有解析结果（尽力而为，注意 DNS rebinding 边界）
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            infos = []
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _ip_is_private(addr):
                return f"错误: 目标主机 {host} 解析到内网地址 {addr}，已阻止 SSRF 请求"
    return None


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

        error = _validate_fetch_url(url)
        if error:
            return error

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
            # 手动跟随重定向，每跳都校验目标地址，防止重定向到内网
            current_url = url
            response = None
            for _ in range(5):
                redirect_error = _validate_fetch_url(current_url)
                if redirect_error:
                    return redirect_error
                response = await ctx.deps.http_client.get(
                    current_url,
                    headers=headers,
                    follow_redirects=False,
                    timeout=20,
                )
                if response.status_code in (
                    301,
                    302,
                    303,
                    307,
                    308,
                ) and response.headers.get("location"):
                    current_url = urljoin(current_url, response.headers["location"])
                    continue
                break
            else:
                return "错误: 重定向次数过多"

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
