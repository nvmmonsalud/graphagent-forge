"""URL and document content extraction."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml")
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 3 * 1024 * 1024  # 3 MB hard cap on downloaded bytes
MAX_CHARS = 50_000  # Kimi has 1M context, but keep the prompt focused
REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Single shared client (connection pooling); created lazily on first use.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first use.

    No awaits inside, so this is atomic with respect to other coroutines on the
    same event loop — two callers can never race into building two clients.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            # Redirects are followed manually so the SSRF guard can re-run on
            # every hop (a public host must not be able to 302 us to 169.254.169.254).
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )
    return _client


async def aclose_client() -> None:
    """Close the shared client (for app shutdown / tests)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _envelope(
    url: str,
    *,
    title: str = "",
    domain: str = "",
    content: str = "",
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the standard extraction envelope callers branch on."""
    return {
        "url": url,
        "title": title,
        "domain": domain,
        "content": content,
        "char_count": len(content),
        "error": error,
        **extra,
    }


# 100.64.0.0/10 (carrier-grade NAT) is not flagged by is_private on newer Pythons
# but routes to infrastructure in several cloud environments.
_EXTRA_BLOCKED_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"),)


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if the address is not a routable public destination."""
    # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1 would otherwise look public).
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip.version == net.version and ip in net for net in _EXTRA_BLOCKED_NETWORKS):
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_url_shape(url: str) -> str | None:
    """Validate scheme/userinfo/hostname. Returns an error message or None.

    Kept synchronous and dependency-free so it can be unit-tested on its own.
    """
    try:
        parsed = urlparse(url)
    except ValueError as e:
        return f"Blocked: malformed URL ({e})"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return f"Blocked: unsupported URL scheme '{parsed.scheme}' (only http/https allowed)"

    netloc = parsed.netloc or ""
    if "@" in netloc:
        return "Blocked: URLs containing userinfo (user@host) are not allowed"

    try:
        hostname = parsed.hostname
    except ValueError as e:
        return f"Blocked: malformed URL host ({e})"
    if not hostname:
        return "Blocked: URL has no hostname"

    return None


async def guard_url(url: str) -> str | None:
    """Full SSRF guard for a single URL. Returns an error message or None.

    Residual risk — DNS rebinding (TOCTOU): the name is resolved here, but httpx
    resolves it again when it opens the socket, so an attacker controlling a DNS
    zone with a ~0s TTL could return a public IP to this check and a private one
    to the connection. Closing that hole requires pinning the validated IP at
    connect time (custom transport / connecting to the IP with a Host header),
    which is deliberately deferred.
    """
    shape_error = check_url_shape(url)
    if shape_error:
        return shape_error

    hostname = urlparse(url).hostname or ""

    # Literal IP hostnames never hit DNS.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if is_blocked_ip(literal):
            return f"Blocked: {hostname} is a private/reserved address ({literal})"
        return None

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, OSError, UnicodeError) as e:
        return f"Failed to resolve hostname {hostname}: {e}"

    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue

    if not addresses:
        return f"Failed to resolve hostname {hostname}: no usable A/AAAA records"

    # Every record must be public — one private answer poisons the whole name.
    for address in addresses:
        if is_blocked_ip(address):
            return f"Blocked: {hostname} resolves to private/reserved IP {address}"

    return None


def _parse_html(html: str) -> tuple[str, str]:
    """Parse + clean HTML. CPU-bound; always call via asyncio.to_thread."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    title = ""
    if soup.title:
        title = soup.title.string or ""

    # Try article/main tags first, fall back to body
    main = soup.find("article") or soup.find("main") or soup.find("body")
    text = (main or soup).get_text(separator="\n", strip=True)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    clean_text = "\n".join(lines)

    if len(clean_text) > MAX_CHARS:
        clean_text = clean_text[:MAX_CHARS] + "\n\n[...truncated...]"

    return title.strip(), clean_text


async def _read_capped_body(response: httpx.Response) -> tuple[bytes, bool]:
    """Read a streaming body up to MAX_BODY_BYTES; never materialize more."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes(65_536):
        remaining = MAX_BODY_BYTES - total
        if len(chunk) >= remaining:
            chunks.append(chunk[:remaining])
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def _decode(raw: bytes, response: httpx.Response) -> str:
    """Decode bytes defensively — a bad charset must not break extraction."""
    for encoding in (response.encoding, response.charset_encoding, "utf-8"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


async def extract_from_url(url: str) -> dict[str, Any]:
    """Fetch a URL and extract clean text content."""
    try:
        domain = urlparse(url).netloc
    except (ValueError, AttributeError, TypeError):
        domain = ""
    client = _get_client()
    current = url
    body_truncated = False

    for _hop in range(MAX_REDIRECTS + 1):
        # Re-run the FULL guard on every hop, including redirect targets.
        error = await guard_url(current)
        if error:
            log.warning("SSRF guard rejected %s: %s", current, error)
            return _envelope(url, domain=domain, error=error)

        try:
            async with client.stream("GET", current) as response:
                if httpx.codes.is_redirect(response.status_code):
                    location = response.headers.get("location")
                    if not location:
                        return _envelope(
                            url,
                            domain=domain,
                            error=f"Redirect from {current} had no Location header",
                        )
                    # Relative Location headers resolve against the current URL.
                    current = urljoin(current, location)
                    continue

                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                media_type = content_type.split(";", 1)[0].strip().lower()
                if not media_type.startswith(ALLOWED_CONTENT_TYPES):
                    return _envelope(
                        url,
                        domain=domain,
                        error=(
                            f"Blocked: unsupported content type "
                            f"'{media_type or 'unknown'}' (expected HTML or plain text)"
                        ),
                    )

                raw, body_truncated = await _read_capped_body(response)
                html = _decode(raw, response)
        except httpx.HTTPStatusError as e:
            log.error("Failed to fetch %s: %s", current, e)
            return _envelope(
                url,
                domain=domain,
                error=f"HTTP {e.response.status_code} fetching {current}",
            )
        except Exception as e:
            log.error("Failed to fetch %s: %s", current, e)
            return _envelope(url, domain=domain, error=f"{type(e).__name__}: {e}")

        break
    else:
        return _envelope(
            url, domain=domain, error=f"Blocked: more than {MAX_REDIRECTS} redirects"
        )

    # BeautifulSoup + text cleanup is CPU-bound — keep it off the event loop.
    title, clean_text = await asyncio.to_thread(_parse_html, html)

    return _envelope(
        url,
        title=title,
        domain=domain,
        content=clean_text,
        error=None,
        final_url=current,
        body_truncated=body_truncated,
    )


def extract_from_text(text: str, source: str = "manual") -> dict[str, Any]:
    """Wrap raw text input in the standard extraction format."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    clean_text = "\n".join(lines)

    return {
        "url": None,
        "title": source,
        "domain": source,
        "content": clean_text,
        "char_count": len(clean_text),
        "error": None,
    }
