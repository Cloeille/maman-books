import asyncio
import hashlib
import logging
import os

import httpx

logger = logging.getLogger(__name__)

VT_API_KEY = os.environ.get("VIRUSTOTAL_API_KEY", "")
VT_BASE = "https://www.virustotal.com/api/v3"
VT_MAX_SIZE = 32 * 1024 * 1024  # 32 MB — free tier upload limit
# The free tier allows 4 requests/min: poll slowly and wait out 429 responses
# instead of failing (polling every 5 s used to exhaust the quota within a minute).
VT_POLL_INTERVAL = 20  # seconds between two status checks
VT_MAX_WAIT = 240      # seconds before giving up on the whole analysis
VT_RATE_LIMIT_WAIT = 30  # seconds to wait after a 429 when no Retry-After is given


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def _request(client: httpx.AsyncClient, method: str, url: str, deadline: float, **kwargs) -> httpx.Response:
    """Send a request, sleeping and retrying while VirusTotal answers 429 (rate limit)."""
    loop = asyncio.get_running_loop()
    while True:
        resp = await client.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        try:
            wait = int(resp.headers.get("retry-after", VT_RATE_LIMIT_WAIT))
        except ValueError:
            wait = VT_RATE_LIMIT_WAIT
        wait = max(1, min(wait, VT_RATE_LIMIT_WAIT * 2))
        if loop.time() + wait > deadline:
            resp.raise_for_status()  # out of time: raises the 429
        logger.info(f"VirusTotal: rate limited (429), retrying in {wait}s")
        await asyncio.sleep(wait)


async def scan_file(file_path: str) -> dict | None:
    """
    Scan a file with VirusTotal. Returns stats dict or None if VT disabled or file too large.
    Stats keys: malicious, suspicious, undetected, harmless, timeout, failure, type-unsupported.
    """
    if not VT_API_KEY:
        return None

    size = os.path.getsize(file_path)
    if size > VT_MAX_SIZE:
        logger.info(f"VirusTotal: file too large ({size / 1024 / 1024:.1f} MB), skipping scan")
        return None

    headers = {"x-apikey": VT_API_KEY.strip()}
    deadline = asyncio.get_running_loop().time() + VT_MAX_WAIT

    async with httpx.AsyncClient(headers=headers) as client:
        # Check by hash first — avoids a redundant upload if VT already knows the file
        sha256 = await asyncio.get_running_loop().run_in_executor(None, _sha256, file_path)
        resp = await _request(client, "GET", f"{VT_BASE}/files/{sha256}", deadline, timeout=15)
        if resp.status_code == 200:
            stats = resp.json()["data"]["attributes"]["last_analysis_stats"]
            logger.info(f"VirusTotal: existing report for {sha256[:12]}… — {stats}")
            return stats

        # Not yet known — upload the file
        logger.info(f"VirusTotal: uploading file ({size / 1024 / 1024:.1f} MB)…")
        with open(file_path, "rb") as f:
            resp = await _request(
                client, "POST", f"{VT_BASE}/files", deadline,
                files={"file": (os.path.basename(file_path), f)},
                timeout=120,
            )
        resp.raise_for_status()
        analysis_id = resp.json()["data"]["id"]

        # Poll until analysis is complete
        loop = asyncio.get_running_loop()
        while loop.time() + VT_POLL_INTERVAL <= deadline:
            await asyncio.sleep(VT_POLL_INTERVAL)
            resp = await _request(client, "GET", f"{VT_BASE}/analyses/{analysis_id}", deadline, timeout=15)
            resp.raise_for_status()
            data = resp.json()["data"]
            if data["attributes"]["status"] == "completed":
                stats = data["attributes"]["stats"]
                logger.info(f"VirusTotal: analysis complete — {stats}")
                return stats

    raise TimeoutError(f"VirusTotal analysis timed out after {VT_MAX_WAIT} s")
