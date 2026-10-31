import httpx
import re
import tempfile
import os
import logging
from urllib.parse import urlparse, urlunparse
from bs4 import BeautifulSoup
from utils import _is_safe_url

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("ANNA_ARCHIVE_URL", "").rstrip("/")
_MD5_RE = re.compile(r'^[a-f0-9]{32}$')


def _validate_md5(md5: str) -> bool:
    return bool(_MD5_RE.match(md5))


def _sanitize_ext(ext: str) -> str:
    cleaned = re.sub(r'[^a-z0-9]', '', (ext or '').lower())[:10]
    return cleaned or 'epub'


def _redact_url(url: str) -> str:
    """Strip query params from logged URLs (may contain auth tokens)."""
    try:
        p = urlparse(url)
        return urlunparse(p._replace(query="[redacted]" if p.query else ""))
    except Exception:
        return "[url]"


def _is_trusted_url(url: str) -> bool:
    """Like _is_safe_url, but also allows URLs built from _BASE_URL (admin-configured)."""
    if _BASE_URL and url.startswith(_BASE_URL):
        return True
    return _is_safe_url(url)


async def _check_redirect(response: httpx.Response) -> None:
    """httpx hook: block redirects to internal IPs (SSRF protection on redirects)."""
    if response.is_redirect:
        location = str(response.headers.get("location", ""))
        if location:
            # Location can be relative (e.g. "/search?q=..."): resolve it against
            # the URL that was just requested before checking it.
            location = str(response.url.join(location))
        if location and not _is_trusted_url(location):
            raise ValueError(f"Redirect blocked (SSRF): {_redact_url(location)}")


MAX_HTML_SIZE = 5 * 1024 * 1024  # 5 MB max for intermediate HTML pages
BOOK_PAGE_URL = _BASE_URL + "/md5/{md5}"
VALID_CONTENT_TYPES = {
    "application/epub+zip",
    "application/pdf",
    "application/x-mobipocket-ebook",
    "application/octet-stream",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


async def search(query: str) -> list[dict]:
    """Search Anna's Archive for books. Returns list of result dicts."""
    if not _BASE_URL:
        return []
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True, event_hooks={"response": [_check_redirect]}) as client:
        return await _search_html(client, query)


async def _search_html(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Fallback: parse Anna's Archive HTML search page."""
    try:
        resp = await client.get(
            f"{_BASE_URL}/search",
            params={"q": query, "lang": "", "content": "book_any", "ext": "epub,pdf,mobi"},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        seen_md5 = {}
        for a in soup.select("a[href^='/md5/']"):
            href = a.get("href", "")
            md5 = href.split("/md5/")[-1].split("?")[0].strip()
            if not md5 or not _validate_md5(md5):
                continue
            text = a.get_text(" ", strip=True)
            if not text:
                continue
            # Prefer entry with longer/richer title for same md5
            if md5 in seen_md5:
                if len(text) > len(seen_md5[md5]["title"]):
                    seen_md5[md5]["title"] = text[:120]
                continue
            ext = "epub"
            for e in ["epub", "pdf", "mobi"]:
                if e in text.lower():
                    ext = e
                    break
            seen_md5[md5] = {
                "source": "anna",
                "title": text[:120],
                "author": "",
                "ext": _sanitize_ext(ext),
                "size_bytes": _parse_size_from_text(text),
                "md5": md5,
                "download_url": f"https://libgen.rocks/get.php?md5={md5}",
                "is_torrent": False,
            }
            if len(seen_md5) >= 10:
                break
        return list(seen_md5.values())
    except Exception as e:
        logger.error(f"Anna's Archive HTML fallback failed: {e}")
        return []


def _parse_size_from_text(text: str) -> int:
    """Try to extract file size in bytes from a text string like '2.3 MB' or '450 KB'."""
    m = re.search(r"([\d.,]+)\s*(MB|KB|GB|Mo|Ko|Go)", text, re.IGNORECASE)
    if not m:
        return 0
    try:
        value = float(m.group(1).replace(",", "."))
        unit = m.group(2).upper()
        if unit in ("KB", "KO"):
            return int(value * 1024)
        if unit in ("MB", "MO"):
            return int(value * 1024 * 1024)
        if unit in ("GB", "GO"):
            return int(value * 1024 * 1024 * 1024)
    except ValueError:
        pass
    return 0


def _extract_download_link(html: str, source_url: str) -> str | None:
    """Extract the real file download link from an intermediate HTML page (e.g. libgen.li/ads.php)."""
    soup = BeautifulSoup(html, "html.parser")
    # Look for links ending with book extensions or containing get.php / download
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href:
            continue
        lower = href.lower()
        if any(lower.endswith(ext) for ext in [".epub", ".pdf", ".mobi", ".azw3", ".fb2"]):
            if href.startswith("http"):
                return href
            # Relative URL — build absolute from source
            from urllib.parse import urljoin
            return urljoin(source_url, href)
        if "get.php" in lower and "md5" in lower:
            if href.startswith("http"):
                return href
            from urllib.parse import urljoin
            return urljoin(source_url, href)
    return None


async def _get_download_links(client: httpx.AsyncClient, md5: str) -> list[str]:
    """Scrape the Anna's Archive book page to get real download links."""
    page_url = BOOK_PAGE_URL.format(md5=md5)
    try:
        resp = await client.get(page_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        links = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            text = a.get_text(strip=True).lower()
            # Pick links that look like download links
            if any(kw in text for kw in ["download", "télécharger", "get", "mirror", "libgen", "lol"]):
                if href.startswith("http") and md5.lower() in href.lower() and _is_safe_url(href):
                    links.append(href)
            elif href.startswith("http") and md5.lower() in href.lower() and _is_safe_url(href):
                links.append(href)
        # Also try the slow_download endpoint directly
        links.append(f"{_BASE_URL}/slow_download/{md5}/0/0")
        logger.info(f"Found {len(links)} download links for md5={md5}: {[_redact_url(u) for u in links]}")
        return links
    except Exception as e:
        logger.warning(f"Could not scrape book page for md5={md5}: {e}")
        return [f"{_BASE_URL}/slow_download/{md5}/0/0"]


async def download(md5: str, ext: str, progress_callback=None, max_bytes: int = 0) -> str:
    """Download a book by md5. Returns path to temp file."""
    ext = _sanitize_ext(ext)
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=90, follow_redirects=True,
        event_hooks={"response": [_check_redirect]},
    ) as client:
        links = await _get_download_links(client, md5)
        for url in links:
            try:
                if ".onion" in url or not _is_trusted_url(url):
                    continue
                logger.info(f"Trying download URL: {_redact_url(url)}")
                # Stream from the start — check content-type from headers only
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        logger.warning(f"URL {_redact_url(url)} returned {resp.status_code}")
                        continue
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                    if "text/html" in ctype:
                        # Read HTML to find real link — limité à MAX_HTML_SIZE
                        chunks, size = [], 0
                        async for chunk in resp.aiter_bytes(65536):
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > MAX_HTML_SIZE:
                                logger.warning(f"HTML page too large (>{MAX_HTML_SIZE // 1024 // 1024} MB), skipping")
                                break
                        html = b"".join(chunks)
                        real_url = _extract_download_link(html.decode("utf-8", errors="ignore"), url)
                        if real_url:
                            if not _is_safe_url(real_url):
                                logger.warning(f"Real link rejected (SSRF): {_redact_url(real_url)}")
                                continue
                            logger.info(f"Found real link in HTML: {_redact_url(real_url)}")
                            result = await _stream_to_file(client, real_url, ext, progress_callback, max_bytes)
                            if result:
                                return result
                        logger.warning(f"URL {_redact_url(url)} returned HTML, no real link found")
                        continue
                    # Stream the file directly
                    result = await _stream_resp_to_file(resp, ext, progress_callback, max_bytes)
                    if result:
                        logger.info(f"Downloaded from {_redact_url(url)}")
                        return result
            except Exception as e:
                logger.warning(f"URL {_redact_url(url)} failed: {e}")
    raise RuntimeError(f"All mirrors failed for md5={md5}")


async def _stream_to_file(client, url: str, ext: str, progress_callback=None, max_bytes: int = 0) -> str | None:
    """Open a new streaming GET request and save to file."""
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                return None
            ctype = resp.headers.get("content-type", "").split(";")[0].strip()
            if "text/html" in ctype:
                return None
            return await _stream_resp_to_file(resp, ext, progress_callback, max_bytes)
    except Exception as e:
        logger.warning(f"Stream failed for {_redact_url(url)}: {e}")
        return None


async def _stream_resp_to_file(resp, ext: str, progress_callback=None, max_bytes: int = 0) -> str | None:
    """Stream an already-open httpx response to a temp file with progress updates."""
    import time
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    last_report = 0.0
    last_pct = -1
    suffix = f".{ext}" if ext else ".epub"
    path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="maman_") as f:
            path = f.name
            async for chunk in resp.aiter_bytes(65536):
                f.write(chunk)
                downloaded += len(chunk)
                if max_bytes and downloaded > max_bytes:
                    raise RuntimeError(f"File too large (>{max_bytes // 1024 // 1024} MB)")
                if progress_callback:
                    now = time.monotonic()
                    pct = int(downloaded / total * 100) if total else 0
                    if now - last_report >= 2.0 and pct != last_pct:
                        last_report = now
                        last_pct = pct
                        try:
                            await progress_callback(downloaded, total)
                        except Exception:
                            pass
        if downloaded < 1024:
            os.remove(path)
            return None
        return path
    except Exception as e:
        logger.warning(f"Stream to file failed: {e}")
        if path:
            try:
                os.remove(path)
            except Exception:
                pass
        return None
