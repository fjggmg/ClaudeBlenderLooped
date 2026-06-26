"""Download free CC0 assets (PolyHaven HDRIs + PBR texture sets).

Real PBR textures and HDRI lighting are the single biggest lever for making a
render look like a real material instead of a hand-rolled procedural shader. This
module fetches them (no API key) so the builder can apply them via bpy.

    python -m blendahbot.assets hdri "studio"          --out assets
    python -m blendahbot.assets texture "scratched metal" --out assets
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_API = "https://api.polyhaven.com"
_UA = "blendahbot/0.1 (+https://github.com/fjggmg/ClaudeBlenderLooped)"
# PolyHaven map names we care about for a Principled BSDF.
_TEXTURE_MAPS = ["Diffuse", "nor_gl", "Rough", "Metal", "AO", "Displacement"]


def _get_json(url: str, timeout: float = 25.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - trusted host
        return json.load(resp)


def _download(url: str, dest: Path, timeout: float = 120.0) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = resp.read()
    dest.write_bytes(data)
    return dest


def _match(assets: dict, query: str, n: int) -> list[str]:
    terms = [t for t in query.lower().split() if t]
    scored: list[tuple[int, str]] = []
    for aid, meta in assets.items():
        tags = meta.get("tags", []) if isinstance(meta, dict) else []
        cats = meta.get("categories", []) if isinstance(meta, dict) else []
        hay = (aid + " " + " ".join(tags) + " " + " ".join(cats)).lower().replace("_", " ")
        score = sum(1 for t in terms if t in hay)
        if score:
            scored.append((score, aid))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored:
        return [aid for _, aid in scored[:n]]
    return list(assets)[:n]  # fall back to anything rather than nothing


def search_hdris(query: str, n: int = 5, timeout: float = 25.0) -> list[str]:
    try:
        return _match(_get_json(f"{_API}/assets?t=hdris", timeout), query, n)
    except Exception:
        return []


def search_textures(query: str, n: int = 5, timeout: float = 25.0) -> list[str]:
    try:
        return _match(_get_json(f"{_API}/assets?t=textures", timeout), query, n)
    except Exception:
        return []


def download_hdri(asset_id: str, out_dir: Path, res: str = "2k", timeout: float = 120.0) -> Path | None:
    try:
        files = _get_json(f"{_API}/files/{urllib.parse.quote(asset_id)}", timeout)
    except Exception:
        return None
    hdri = files.get("hdri", {})
    entry = hdri.get(res) or (next(iter(hdri.values())) if hdri else {})
    fmt = entry.get("hdr") or entry.get("exr")
    if not fmt or not fmt.get("url"):
        return None
    url = fmt["url"]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{asset_id}_{res}{Path(urllib.parse.urlparse(url).path).suffix or '.hdr'}"
    try:
        return _download(url, dest, timeout)
    except Exception:
        return None


def download_texture(
    asset_id: str, out_dir: Path, res: str = "2k", fmt: str = "jpg", timeout: float = 120.0
) -> dict[str, Path]:
    """Download a PolyHaven texture's maps. Returns {map_name: path} for what exists."""
    try:
        files = _get_json(f"{_API}/files/{urllib.parse.quote(asset_id)}", timeout)
    except Exception:
        return {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    maps: dict[str, Path] = {}
    for mtype in _TEXTURE_MAPS:
        block = files.get(mtype)
        if not isinstance(block, dict):
            continue
        entry = block.get(res) or (next(iter(block.values())) if block else {})
        chosen = entry.get(fmt) or entry.get("jpg") or entry.get("png")
        if not chosen and entry:
            chosen = next(iter(entry.values()))
        if not isinstance(chosen, dict) or not chosen.get("url"):
            continue
        url = chosen["url"]
        dest = out_dir / f"{asset_id}_{mtype}{Path(urllib.parse.urlparse(url).path).suffix or '.jpg'}"
        try:
            _download(url, dest, timeout)
            maps[mtype] = dest
        except Exception:
            pass
    return maps


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="blendahbot.assets", description="Download free CC0 assets from PolyHaven.")
    parser.add_argument("kind", choices=["hdri", "texture"])
    parser.add_argument("query")
    parser.add_argument("--out", default="assets")
    parser.add_argument("--res", default="2k")
    parser.add_argument("-n", type=int, default=1, help="How many matching assets.")
    args = parser.parse_args(argv)

    found = False
    if args.kind == "hdri":
        for aid in search_hdris(args.query, args.n):
            path = download_hdri(aid, Path(args.out), args.res)
            if path:
                found = True
                print(path)
    else:
        for aid in search_textures(args.query, args.n):
            for mtype, path in download_texture(aid, Path(args.out), args.res).items():
                found = True
                print(f"{mtype}\t{path}")
    if not found:
        print("(no assets found)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
