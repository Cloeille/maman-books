import httpx
import os
import logging

logger = logging.getLogger(__name__)


def _client() -> httpx.AsyncClient:
    base_url = os.environ.get("PROWLARR_URL", "").rstrip("/")
    api_key = os.environ.get("PROWLARR_API_KEY", "")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"X-Api-Key": api_key},
        timeout=20,
    )


async def search(query: str) -> list[dict]:
    """Search Prowlarr for books. Returns list of result dicts."""
    if not os.environ.get("PROWLARR_URL"):
        return []
    params = {
        "query": query,
        "categories[]": ["7000", "7020"],
        "type": "search",
    }
    async with _client() as client:
        try:
            resp = await client.get("/api/v1/search", params=params)
            resp.raise_for_status()
            items = resp.json()
        except Exception as e:
            logger.error(f"Prowlarr search failed: {e}")
            return []

    results = []
    for item in items:
        dl_url = item.get("downloadUrl") or ""
        guid = item.get("guid") or ""
        if not dl_url and not guid:
            continue

        magnet = item.get("magnetUrl") or ""
        if not magnet and dl_url.lower().startswith("magnet:"):
            magnet = dl_url
        # Prowlarr's API exposes the protocol as "protocol" ("torrent" / "usenet").
        # "downloadProtocol" is kept as a fallback for other API versions.
        # Values can be null, so coerce to str before lower().
        protocol = str(item.get("protocol") or item.get("downloadProtocol") or "").lower()
        is_torrent = (
            protocol == "torrent"
            or bool(magnet)
            or dl_url.lower().split("?", 1)[0].endswith(".torrent")
        )

        results.append({
            "source": "prowlarr",
            "title": item.get("title") or "",
            "author": "",
            "ext": _guess_ext(item),
            "size_bytes": item.get("size") or 0,
            "guid": guid,
            "indexer_id": item.get("indexerId") or 0,
            "download_url": dl_url,
            "magnet_url": magnet,
            "is_torrent": is_torrent,
            "seeders": item.get("seeders") or 0,
        })

    return results


def _guess_ext(item: dict) -> str:
    title = (item.get("title") or "").lower()
    for ext in ["epub", "pdf", "mobi", "azw3"]:
        if ext in title:
            return ext
    return "epub"


async def grab(indexer_id: int, guid: str) -> None:
    """Tell Prowlarr to grab (send to download client) a result by guid.

    Prowlarr's API grabs a release with POST /api/v1/search (same resource as the
    search, body = the release's guid + indexerId). There is no POST /api/v1/download
    (it answers 405 Method Not Allowed).
    """
    payload = {"guid": guid, "indexerId": indexer_id}
    async with _client() as client:
        try:
            resp = await client.post("/api/v1/search", json=payload)
            resp.raise_for_status()
            logger.info(f"Prowlarr grab successful for guid={guid}")
        except Exception as e:
            logger.error(f"Prowlarr grab failed: {e}")
            raise
