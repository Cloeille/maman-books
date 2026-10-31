import asyncio
import os
import re
import logging
import unicodedata

logger = logging.getLogger(__name__)

BOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw3", ".azw", ".fb2"}
# Suffixes torrent clients use for files that are still downloading.
INCOMPLETE_SUFFIXES = (".!qb", ".part", ".parts", ".crdownload", ".tmp", ".aria2", ".td")


def _normalize(text: str) -> set[str]:
    """Return set of significant lowercase, accent-free words from a string."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[\W_]+", " ", text)
    return {w for w in text.split() if len(w) > 3}


def _score(name: str, title_words: set[str]) -> int:
    return len(title_words & _normalize(name))


def _matches(name: str, title_words: set[str]) -> bool:
    """Check if a file or folder name loosely matches the search title."""
    if not title_words:
        return False
    return _score(name, title_words) >= max(1, len(title_words) // 2)


def _is_book(filename: str) -> bool:
    lower = filename.lower()
    if lower.endswith(INCOMPLETE_SUFFIXES):
        return False
    return os.path.splitext(lower)[1] in BOOK_EXTENSIONS


def _book_files(path: str) -> list[str]:
    """Book files at `path` (a file) or anywhere below it (a folder)."""
    if os.path.isfile(path):
        return [path] if _is_book(os.path.basename(path)) else []
    found = []
    for root, _dirs, files in os.walk(path):
        for f in files:
            if _is_book(f):
                found.append(os.path.join(root, f))
    return found


def _pick(entry_path: str, title_words: set[str]) -> str | None:
    """
    Return the book file to send for a new entry of the download folder, or None.

    A torrent either saves a single file or creates a folder named after the release,
    whose book file can have a cleaner name than the release (e.g. the release is
    "La.Vallee.Aux.Echos.Scott.Alexander.Howard.2026.FR.[EPUB]-NoD" and the file inside
    is "La vallée aux échos.epub"). So a folder is accepted when its own name matches
    the title; otherwise a file inside it must match on its own name.
    """
    files = _book_files(entry_path)
    if not files:
        return None
    entry_name = os.path.basename(entry_path)
    if not _matches(entry_name, title_words):
        files = [f for f in files if _matches(os.path.basename(f), title_words)]
        if not files:
            return None
    return max(files, key=lambda f: (_score(os.path.basename(f), title_words), os.path.getsize(f)))


def snapshot(download_path: str) -> set[str]:
    """Names currently in the download folder. Take it BEFORE asking the client to download."""
    try:
        return set(os.listdir(download_path))
    except FileNotFoundError:
        return set()


async def wait_for_file(
    title: str,
    download_path: str,
    timeout_minutes: int = 15,
    existing: set[str] | None = None,
    poll_seconds: float = 5.0,
    stable_seconds: float = 10.0,
) -> str:
    """
    Poll download_path until a new book file matching title appears and has finished
    growing. Returns the full path of the file.
    `existing` is the folder content before the download was requested (see snapshot());
    if omitted, it is taken now.
    Raises TimeoutError if not found within timeout_minutes.
    """
    title_words = _normalize(title)
    timeout_seconds = timeout_minutes * 60
    if existing is None:
        existing = snapshot(download_path)

    logger.info(
        f"Watching {download_path!r} for {title!r} "
        f"(timeout={timeout_minutes}min, keywords={title_words})"
    )

    # path -> (size, elapsed time when this size was first seen)
    seen: dict[str, tuple[int, float]] = {}
    reported: set[str] = set()

    elapsed = 0.0
    while elapsed < timeout_seconds:
        await asyncio.sleep(poll_seconds)
        elapsed += poll_seconds
        try:
            current = set(os.listdir(download_path))
        except FileNotFoundError:
            continue

        for name in sorted(current - existing):
            if name.lower().endswith(INCOMPLETE_SUFFIXES):
                continue
            entry_path = os.path.join(download_path, name)
            try:
                candidate = _pick(entry_path, title_words)
            except OSError:
                continue  # file vanished or still being created

            if candidate is None:
                if name not in reported:
                    reported.add(name)
                    logger.info(f"New entry {name!r} ignored (no matching book file yet)")
                continue

            try:
                size = os.path.getsize(candidate)
            except OSError:
                continue
            # Only accept the file once its size has not changed for `stable_seconds`
            # (the torrent client may still be writing it).
            prev = seen.get(candidate)
            if prev is None or prev[0] != size:
                seen[candidate] = (size, elapsed)
            elif size > 0 and elapsed - prev[1] >= stable_seconds:
                logger.info(f"Found matching file: {candidate}")
                return candidate

    raise TimeoutError(
        f"File not found for '{title}' in {download_path} after {timeout_minutes} minutes."
    )
