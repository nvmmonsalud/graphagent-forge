"""URL and document content extraction."""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


async def extract_from_url(url: str) -> dict[str, Any]:
    """Fetch a URL and extract clean text content."""
    # SSRF guard: block requests to private/reserved IPs
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    try:
        resolved = socket.getaddrinfo(hostname, None)
        ip = ipaddress.ip_address(resolved[0][4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return {"url": url, "content": "", "error": f"Blocked: {hostname} resolves to private/reserved IP {ip}"}
    except (socket.gaierror, ValueError) as e:
        return {"url": url, "content": "", "error": f"Failed to resolve hostname {hostname}: {e}"}

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        except Exception as e:
            log.error("Failed to fetch %s: %s", url, e)
            return {"url": url, "content": "", "error": str(e)}

    # Parse HTML
    soup = BeautifulSoup(response.text, "html.parser")

    # Remove noise
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    # Extract title
    title = ""
    if soup.title:
        title = soup.title.string or ""

    # Extract main content
    # Try article/main tags first, fall back to body
    main = soup.find("article") or soup.find("main") or soup.find("body")
    if main:
        text = main.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)

    # Clean up whitespace
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    clean_text = "\n".join(lines)

    # Truncate to manageable size (Kimi has 1M context, but keep it focused)
    max_chars = 50_000
    if len(clean_text) > max_chars:
        clean_text = clean_text[:max_chars] + "\n\n[...truncated...]"

    return {
        "url": url,
        "title": title,
        "domain": urlparse(url).netloc,
        "content": clean_text,
        "char_count": len(clean_text),
        "error": None,
    }


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
