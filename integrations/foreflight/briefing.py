"""Bounded PDF download without forwarding Dispatch credentials or following redirects."""

import asyncio
import ipaddress
import socket
from io import BytesIO
from urllib.parse import urlsplit

import httpx
from pypdf import PdfReader


async def download_briefing(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError("Invalid briefing URL")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parts.hostname, 443)
    if not addresses or any(
        not ipaddress.ip_address(row[4][0]).is_global for row in addresses
    ):
        raise ValueError("Briefing host is not public")
    async with (
        httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as http,
        http.stream("GET", url) as response,
    ):
        if response.status_code != 200:
            raise ValueError("Briefing download failed")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > 15 * 1024 * 1024:
                raise ValueError("Briefing exceeds size limit")
    return await asyncio.to_thread(pdf_text, bytes(content))


def pdf_text(content):
    if not content.startswith(b"%PDF-"):
        raise ValueError("Briefing is not a PDF")
    reader = PdfReader(BytesIO(content))
    if reader.is_encrypted or len(reader.pages) > 300:
        raise ValueError("Unsupported briefing PDF")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text) > 2_000_000:
        raise ValueError("Briefing text exceeds size limit")
    return text
