"""Download real reference images so the builder has something concrete to match.

The agent can web-search, but to ground the model it needs actual image *files* on
disk that it can open and look at. This module pulls a handful of reference photos
from key-free sources (Openverse, then Wikimedia Commons as a fallback) and saves
them into the run's ``reference/`` folder. Both the builder (before modelling) and
the critic (when judging the render) are pointed at these files.

Also runnable directly so the agent can grab more mid-build:

    python -m blendahbot.refs "porsche 911 turbo wheel" --out reference -n 4
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.parse
import urllib.request
from itertools import zip_longest
from pathlib import Path

# Rendering-pipeline words that pull irrelevant hits (we want photos of the
# subject, not 3D art). Stripped from the search query.
_STYLE_TOKENS = {
    "render", "rendered", "cgi", "voxel", "isometric", "blender", "octane",
    "unreal", "3d", "stylized", "stylised", "lowpoly", "photorealistic",
    "render", "scene",
}
_STYLE_PHRASES = ("low poly", "low-poly", "photo realistic", "photo-realistic",
                  "hyper realistic", "3 d", "concept art")
_STOP = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "to", "for",
         "by", "into", "from", "over", "under", "near", "this", "that", "some",
         "very", "really", "cozy", "beautiful", "nice", "epic"}

# Filenames/extensions that are almost never useful visual references.
_BAD_EXT = (".svg", ".pdf", ".tif", ".tiff", ".djvu", ".ogv", ".webm", ".gif", ".ogg")
_BAD_NAME = ("cover", "title", "frontispiece", "map_", "_map", "logo", "seal",
             "coat_of_arms", "flag_", "diagram", "chart", "manuscript", "stamp",
             "icon", "_page", "page_", "banner", "schematic", "letter", "document",
             "newspaper", "poster", "label", "scan")

_OPENVERSE = "https://api.openverse.org/v1/images/"
_WIKIMEDIA = "https://commons.wikimedia.org/w/api.php"
_UA = "blendahbot/0.1 (+https://github.com/fjggmg/ClaudeBlenderLooped)"
_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}
_MAX_BYTES = 8_000_000
_MIN_BYTES = 2_048


def fetch_references(query: str, out_dir: Path, n: int = 6, timeout: float = 20.0) -> list[Path]:
    """Download up to ``n`` reference images for ``query`` into ``out_dir``.

    Never raises — returns whatever it managed to save (possibly empty).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cleaned = clean_query(query)
    urls = _gather_urls(cleaned, n * 3, timeout)
    if len(urls) < n:  # long phrase too narrow — retry with the first few nouns
        short = " ".join(cleaned.split()[:3])
        if short and short != cleaned:
            urls += _gather_urls(short, n * 3, timeout)

    seen: set[str] = set()
    saved: list[Path] = []
    for url in urls:
        if len(saved) >= n:
            break
        if url in seen:
            continue
        seen.add(url)
        path = _download(url, out_dir, len(saved), timeout)
        if path is not None:
            saved.append(path)
    return saved


def clean_query(query: str, max_words: int = 5) -> str:
    """Reduce a request to a few subject keywords for image search.

    Drops rendering-style words and stop-words, keeps the salient nouns, and
    caps the length (keyword image search narrows fast with more terms).
    """
    s = query.lower()
    for phrase in _STYLE_PHRASES:
        s = s.replace(phrase, " ")
    words = [
        w for w in re.findall(r"[a-z0-9']+", s)
        if w not in _STYLE_TOKENS and w not in _STOP
    ]
    return " ".join(words[:max_words]).strip() or query


def _gather_urls(query: str, count: int, timeout: float) -> list[str]:
    """Interleave Wikimedia + Openverse results, filtered to likely photos."""
    wiki = _search_wikimedia(query, count, timeout)
    openverse = _search_openverse(query, count, timeout)
    merged = [u for pair in zip_longest(wiki, openverse) for u in pair if u]
    return [u for u in merged if _is_usable(u)]


def _is_usable(url: str) -> bool:
    name = url.lower().rsplit("/", 1)[-1]
    if any(e in url.lower() for e in _BAD_EXT):
        return False
    return not any(b in name for b in _BAD_NAME)


def _get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - trusted hosts
            return json.load(resp)
    except Exception:
        return {}


def _search_openverse(query: str, limit: int, timeout: float) -> list[str]:
    params = urllib.parse.urlencode({"q": query, "page_size": str(max(1, limit)), "mature": "false"})
    data = _get_json(f"{_OPENVERSE}?{params}", timeout)
    return [item["url"] for item in data.get("results", []) if item.get("url")]


def _search_wikimedia(query: str, limit: int, timeout: float) -> list[str]:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "generator": "search",
            "gsrnamespace": "6",  # File: namespace
            "gsrsearch": query,
            "gsrlimit": str(max(1, limit)),
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": "1280",
            "format": "json",
        }
    )
    data = _get_json(f"{_WIKIMEDIA}?{params}", timeout)
    pages = (data.get("query") or {}).get("pages") or {}
    urls: list[str] = []
    for page in pages.values():
        for info in page.get("imageinfo") or []:
            url = info.get("thumburl") or info.get("url")
            if url:
                urls.append(url)
    return urls


def download_urls(urls: list[str], out_dir: Path, timeout: float = 20.0) -> list[Path]:
    """Download explicit image URLs (e.g. ones the agent found via web search).

    Use this for fictional/game subjects where keyword stock-photo search fails —
    the agent finds real images of the actual subject, then downloads them here.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for url in urls:
        path = _download(url, out_dir, len(saved), timeout)
        if path is not None:
            saved.append(path)
    return saved


def _download(url: str, out_dir: Path, idx: int, timeout: float) -> Path | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                return None
            data = resp.read(_MAX_BYTES + 1)
    except Exception:
        return None
    if len(data) < _MIN_BYTES or len(data) > _MAX_BYTES:
        return None
    ext = _EXT.get(ctype, ".jpg")
    path = out_dir / f"ref_{idx:02d}{ext}"
    try:
        path.write_bytes(data)
    except OSError:
        return None
    return path


# --------------------------------------------------------------------------
# User-supplied references (files the user already has on disk)
# --------------------------------------------------------------------------

# Image suffixes we accept when the user points us at their own files/folders.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def parse_path_tokens(line: str) -> list[str]:
    """Split a line of typed/dragged input into individual path tokens.

    Windows wraps a dragged path in double quotes only when it contains spaces,
    and several files dragged together arrive space-separated. We honour single
    and double quotes (so spaced paths stay intact), treat unquoted whitespace as
    a separator, and strip any ``file://`` scheme. Backslashes are left as-is —
    they are path separators here, not escapes.
    """
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in line.strip():
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        tokens.append("".join(buf))

    cleaned: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low.startswith("file:///"):
            tok = urllib.parse.unquote(tok[8:])
        elif low.startswith("file://"):
            tok = urllib.parse.unquote(tok[7:])
        cleaned.append(tok)
    return cleaned


def resolve_reference_specs(specs: list[str]) -> tuple[list[Path], list[str]]:
    """Expand path specs into existing image files.

    A spec may be an image file or a directory (its image files are included).
    Returns ``(images, unusable)`` where ``unusable`` lists specs that pointed at
    nothing we can use (missing path, non-image file, or empty folder) so the
    caller can tell the user which inputs were ignored.
    """
    images: list[Path] = []
    unusable: list[str] = []
    for spec in specs:
        raw = str(spec).strip().strip('"').strip("'")
        if not raw:
            continue
        src = Path(raw).expanduser()
        if src.is_dir():
            found = [
                f for f in sorted(src.iterdir())
                if f.is_file() and f.suffix.lower() in _IMAGE_EXTS
            ]
            if found:
                images.extend(found)
            else:
                unusable.append(raw)
        elif src.is_file() and src.suffix.lower() in _IMAGE_EXTS:
            images.append(src)
        else:
            unusable.append(raw)
    return images, unusable


def ingest_user_references(
    images: list[Path], out_dir: Path, start_index: int = 0
) -> list[Path]:
    """Copy already-resolved user image files into ``out_dir`` as ``user_ref_NN``.

    The ``user_ref_`` prefix keeps them distinct from fetched ``ref_`` photos so
    the builder prompt can treat the user's own images as authoritative. Returns
    the files actually copied (skipping any that fail to copy).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    idx = start_index
    for img in images:
        img = Path(img)
        ext = img.suffix.lower() or ".png"
        dest = out_dir / f"user_ref_{idx:02d}{ext}"
        try:
            shutil.copy2(img, dest)
        except OSError:
            continue
        saved.append(dest)
        idx += 1
    return saved


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="blendahbot.refs", description="Download reference images.")
    parser.add_argument("query", nargs="?", default=None, help="What to find references of (keyword search).")
    parser.add_argument("--out", default="reference", help="Output directory (default: reference).")
    parser.add_argument("-n", type=int, default=6, help="How many images (default: 6).")
    parser.add_argument("--url", nargs="+", default=None, help="Download these exact image URLs instead of searching (best for fictional/game subjects).")
    args = parser.parse_args(argv)

    if args.url:
        paths = download_urls(args.url, Path(args.out))
    elif args.query:
        paths = fetch_references(args.query, Path(args.out), args.n)
    else:
        parser.error("provide a search query or --url <image urls>")
    for p in paths:
        print(p)
    if not paths:
        print("(no references found)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
