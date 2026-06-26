"""Acquire Blender extensions, add-ons, asset libraries and Python packages on demand.

The builder reaches for this mid-build when the scene needs a capability the base
Blender install lacks: a procedural-tree / scatter generator, a format importer, a
node or shader pack, an asset library to drag from, or a Python package its bpy code
wants to ``import``. Everything is installed straight into the LIVE Blender session
(over the same add-on socket the orchestrator uses), so the new operators, panels and
modules are available immediately — no restart.

    python -m blendahbot.addons search "tree generator"
    python -m blendahbot.addons install sapling_tree_gen        # by extension id
    python -m blendahbot.addons install "scatter objects"       # or best-match a query
    python -m blendahbot.addons install-url https://example.com/some_addon.zip
    python -m blendahbot.addons enable bl_ext.blender_org.node_wrangler
    python -m blendahbot.addons list
    python -m blendahbot.addons asset-library "C:/packs/kitbash" --name Kitbash
    python -m blendahbot.addons pip trimesh shapely

Extensions come from the official Blender Extensions Platform
(https://extensions.blender.org) — no API key needed. Installing runs third-party code
inside Blender, so the default ``install`` path only touches that vetted repository;
``install-url`` fetches an arbitrary URL and should be pointed only at trusted sources.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .blender import BlenderClient, BlenderUnavailable

_INDEX_URL = "https://extensions.blender.org/api/v1/extensions/"
# The extensions CDN 403s the default ``Python-urllib`` agent — a UA is mandatory.
_UA = "blendahbot/0.1 (+https://github.com/fjggmg/ClaudeBlenderLooped)"
# Downloaded archives are installed into this local repo; its enabled modules are
# named ``bl_ext.user_default.<id>``.
_DEFAULT_REPO = "user_default"
# The remote repo to fall back to for an online install-by-id.
_REMOTE_REPO = "blender_org"


class AddonError(RuntimeError):
    """Raised when an extension/library cannot be found, fetched or installed."""


# --------------------------------------------------------------------------
# extensions.blender.org index (key-free HTTP)
# --------------------------------------------------------------------------

def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": _UA})


def fetch_index(timeout: float = 60.0) -> list[dict]:
    """Return the ``data`` array of the Blender Extensions Platform listing.

    Each entry has at least: ``id``, ``name``, ``version``, ``tagline``, ``type``
    (``"add-on"``/``"theme"``), ``archive_url``, ``blender_version_min`` and ``tags``.
    """
    with urllib.request.urlopen(_request(_INDEX_URL), timeout=timeout) as resp:  # noqa: S310
        data = json.load(resp)
    rows = data.get("data", []) if isinstance(data, dict) else []
    return [e for e in rows if isinstance(e, dict)]  # skip any malformed (non-dict) members


def _ver(s: str | None) -> tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", s or "")[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _compatible(entry: dict, blender_version: tuple[int, int, int]) -> bool:
    """True if ``entry`` supports ``blender_version`` (min/max bounds permitting)."""
    if _ver(entry.get("blender_version_min")) > blender_version:
        return False
    bmax = entry.get("blender_version_max")
    if bmax and _ver(bmax) <= blender_version:  # max is exclusive in Blender's manifest
        return False
    return True


def search(
    query: str,
    index: list[dict],
    n: int = 8,
    kind: str | None = None,
    blender_version: tuple[int, int, int] | None = None,
) -> list[dict]:
    """Rank index entries against ``query`` (over id / name / tagline / tags)."""
    terms = [t for t in re.split(r"\W+", query.lower()) if t]
    scored: list[tuple[float, int, dict]] = []
    for i, e in enumerate(index):
        if kind and e.get("type") != kind:
            continue
        eid = (e.get("id") or "").lower()
        name = (e.get("name") or "").lower()
        tags = " ".join(str(t) for t in (e.get("tags") or []))
        hay = " ".join([eid, name, (e.get("tagline") or "").lower(), tags.lower()])
        score = 0.0
        for t in terms:
            if t == eid:
                score += 5
            elif t in eid or t in name:
                score += 2
            elif t in hay:
                score += 1
        if score <= 0:
            continue
        if blender_version is not None and not _compatible(e, blender_version):
            score -= 1.5  # de-rank but keep — the user may be on a different Blender
        # tie-break: prefer shorter names (closer to an exact match), stable by index
        scored.append((score, -len(name), e))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    out: list[dict] = []
    seen: set[str] = set()
    for _s, _l, e in scored:  # dedup by id (the index lists per-platform variants)
        eid = (e.get("id") or "").lower()
        if eid in seen:
            continue
        seen.add(eid)
        out.append(e)
        if len(out) >= n:
            break
    return out


def resolve(
    query_or_id: str,
    index: list[dict],
    kind: str | None = None,
    blender_version: tuple[int, int, int] | None = None,
) -> dict | None:
    """An exact id match (case-insensitive) if present, else the best search hit."""
    qid = query_or_id.strip()
    for e in index:
        if (e.get("id") or "").lower() == qid.lower():
            return e
    hits = search(qid, index, n=1, kind=kind, blender_version=blender_version)
    return hits[0] if hits else None


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

def _cache_dir() -> Path:
    base = os.environ.get("BLENDAHBOT_HOME")
    root = Path(base) if base else (Path.home() / ".blendahbot")
    d = root / "addons_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _archive_name(url: str, entry: dict | None = None) -> str:
    base = os.path.basename(urllib.parse.urlparse(url).path)
    if base and base.lower().endswith((".zip", ".py")):
        return base
    if entry:
        suffix = ".py" if url.lower().endswith(".py") else ".zip"
        return f"{entry.get('id', 'extension')}-{entry.get('version', '0')}{suffix}"
    return base or "download.zip"


def _download(url: str, dest: Path, timeout: float = 180.0) -> Path:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise AddonError(f"Refusing to download from a non-http(s) URL: {url!r}")
    with urllib.request.urlopen(_request(url), timeout=timeout) as resp:  # noqa: S310
        dest.write_bytes(resp.read())
    return dest


# --------------------------------------------------------------------------
# live-Blender operations (over the add-on socket)
# --------------------------------------------------------------------------

def _client(host: str, port: int, client: BlenderClient | None = None) -> BlenderClient:
    return client if client is not None else BlenderClient(host=host, port=port)


def _exec(client: BlenderClient, code: str) -> dict:
    """Run ``code`` in Blender; return the ``result`` dict it assigns.

    Raises :class:`AddonError` on a transport failure or a Python error inside
    Blender (so the caller and the agent get one clear message).
    """
    try:
        resp = client.execute(code, strict_json=False)
    except BlenderUnavailable as ex:
        raise AddonError(str(ex)) from ex
    if resp.get("status") != "ok":
        raise AddonError(str(resp.get("message") or resp.get("stderr") or "Blender returned an error"))
    result = resp.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return {"raw": result}
    return result if isinstance(result, dict) else {}


# Every snippet redirects stdout/stderr around the operator call: over the MCP tool
# path stdout is None, so a third-party add-on that ``print()``s in its register()
# would crash mid-registration. Redirecting to a real buffer lets register() finish.

def _install_files_code(filepath: Path, repo: str) -> str:
    return (
        "import bpy, sys, io\n"
        "_so, _se = sys.stdout, sys.stderr\n"
        "buf = io.StringIO()\n"
        "before = set(bpy.context.preferences.addons.keys())\n"
        "err = None\n"
        "sys.stdout = sys.stderr = buf\n"
        "try:\n"
        f"    bpy.ops.extensions.package_install_files(filepath={json.dumps(str(filepath))}, "
        f"repo={json.dumps(repo)}, enable_on_install=True, overwrite=True)\n"
        "except Exception as e:\n"
        "    err = repr(e)\n"
        "finally:\n"
        "    sys.stdout, sys.stderr = _so, _se\n"
        "after = set(bpy.context.preferences.addons.keys())\n"
        "new = sorted(after - before)\n"
        "result = {'error': err, 'new_modules': new, "
        "'enabled_ext': [m for m in new if m.startswith('bl_ext.')], "
        "'log': buf.getvalue()[-400:]}\n"
    )


def _install_online_code(pkg_id: str, repo_module: str) -> str:
    return (
        "import bpy, sys, io\n"
        "_so, _se = sys.stdout, sys.stderr\n"
        "buf = io.StringIO()\n"
        "before = set(bpy.context.preferences.addons.keys())\n"
        "err = None\n"
        "sys.stdout = sys.stderr = buf\n"
        "try:\n"
        "    repos = bpy.context.preferences.extensions.repos\n"
        f"    idx = next((i for i, r in enumerate(repos) if r.module == {json.dumps(repo_module)}), -1)\n"
        "    if idx < 0:\n"
        f"        err = 'remote repo not found: ' + {json.dumps(repo_module)}\n"
        "    else:\n"
        "        for _op in ('repo_sync_all', 'repo_refresh_all'):\n"
        "            try:\n"
        "                getattr(bpy.ops.extensions, _op)()\n"
        "            except Exception:\n"
        "                pass\n"
        f"        bpy.ops.extensions.package_install(repo_index=idx, pkg_id={json.dumps(pkg_id)}, "
        "enable_on_install=True)\n"
        "except Exception as e:\n"
        "    err = repr(e)\n"
        "finally:\n"
        "    sys.stdout, sys.stderr = _so, _se\n"
        "after = set(bpy.context.preferences.addons.keys())\n"
        "result = {'error': err, 'new_modules': sorted(after - before), 'log': buf.getvalue()[-400:]}\n"
    )


def _install_legacy_code(filepath: Path) -> str:
    return (
        "import bpy, sys, io, addon_utils\n"
        "_so, _se = sys.stdout, sys.stderr\n"
        "buf = io.StringIO()\n"
        "keys_before = set(bpy.context.preferences.addons.keys())\n"
        "names_before = {m.__name__ for m in addon_utils.modules()}\n"
        "err = None\n"
        "sys.stdout = sys.stderr = buf\n"
        "try:\n"
        f"    bpy.ops.preferences.addon_install(filepath={json.dumps(str(filepath))}, overwrite=True)\n"
        "    new_names = sorted({m.__name__ for m in addon_utils.modules()} - names_before)\n"
        "    for _m in new_names:\n"
        "        try:\n"
        "            bpy.ops.preferences.addon_enable(module=_m)\n"
        "        except Exception as e2:\n"
        "            err = repr(e2)\n"
        "except Exception as e:\n"
        "    err = repr(e)\n"
        "finally:\n"
        "    sys.stdout, sys.stderr = _so, _se\n"
        "after = set(bpy.context.preferences.addons.keys())\n"
        "result = {'error': err, 'new_modules': sorted(after - keys_before), 'log': buf.getvalue()[-400:]}\n"
    )


def _enable_code(module: str, enable: bool = True) -> str:
    op = "addon_enable" if enable else "addon_disable"
    return (
        "import bpy, sys, io\n"
        "_so, _se = sys.stdout, sys.stderr\n"
        "buf = io.StringIO()\n"
        "err = None\n"
        "sys.stdout = sys.stderr = buf\n"
        "try:\n"
        f"    bpy.ops.preferences.{op}(module={json.dumps(module)})\n"
        "except Exception as e:\n"
        "    err = repr(e)\n"
        "finally:\n"
        "    sys.stdout, sys.stderr = _so, _se\n"
        f"result = {{'error': err, 'enabled': {json.dumps(module)} in set(bpy.context.preferences.addons.keys())}}\n"
    )


_LIST_CODE = (
    "import bpy\n"
    "keys = list(bpy.context.preferences.addons.keys())\n"
    "result = {'extensions': sorted(m for m in keys if m.startswith('bl_ext.')), "
    "'legacy': sorted(m for m in keys if not m.startswith('bl_ext.'))}\n"
)

_PY_EXE_CODE = "import sys\nresult = {'exe': sys.executable, 'version': sys.version.split()[0]}\n"

_ASSET_LIB_LIST_CODE = (
    "import bpy\n"
    "result = {'libraries': [{'name': a.name, 'path': a.path} "
    "for a in bpy.context.preferences.filepaths.asset_libraries]}\n"
)


def _asset_lib_add_code(path: str, name: str | None) -> str:
    return (
        "import bpy, sys, io, os\n"
        f"path = {json.dumps(path)}\n"
        f"name = {json.dumps(name)}\n"
        "_so, _se = sys.stdout, sys.stderr\n"
        "buf = io.StringIO()\n"
        "err = None\n"
        "sys.stdout = sys.stderr = buf\n"
        "try:\n"
        "    libs = bpy.context.preferences.filepaths.asset_libraries\n"
        "    existing = next((a for a in libs if os.path.normpath(a.path) == os.path.normpath(path)), None)\n"
        "    if existing is None:\n"
        "        bpy.ops.preferences.asset_library_add(directory=path)\n"
        "        if name:\n"
        "            libs[-1].name = name\n"
        "    elif name:\n"
        "        existing.name = name\n"
        "except Exception as e:\n"
        "    err = repr(e)\n"
        "finally:\n"
        "    sys.stdout, sys.stderr = _so, _se\n"
        "result = {'error': err, 'libraries': [{'name': a.name, 'path': a.path} "
        "for a in bpy.context.preferences.filepaths.asset_libraries]}\n"
    )


# --------------------------------------------------------------------------
# high-level actions
# --------------------------------------------------------------------------

def install_extension(
    query_or_id: str,
    *,
    host: str = "localhost",
    port: int = 9876,
    repo: str = _DEFAULT_REPO,
    index: list[dict] | None = None,
    client: BlenderClient | None = None,
    timeout: float = 180.0,
) -> dict:
    """Resolve, download and install an extension from extensions.blender.org.

    Returns ``{entry, modules, already, result, archive}``. ``modules`` is the list
    of newly-enabled module names (e.g. ``["bl_ext.user_default.sapling_tree_gen"]``).
    """
    index = index if index is not None else fetch_index()
    entry = resolve(query_or_id, index, kind="add-on") or resolve(query_or_id, index)
    if entry is None:
        raise AddonError(
            f"No extension on extensions.blender.org matches {query_or_id!r}. "
            "Run `python -m blendahbot.addons search \"<keywords>\"` to find one."
        )
    eid = entry.get("id", "")
    cl = _client(host, port, client)

    # If this extension is already enabled (in any repo), don't re-download or install
    # a duplicate — just report it.
    pre = _exec(cl, _LIST_CODE)
    already_on = [m for m in pre.get("extensions", []) if m.split(".")[-1].lower() == eid.lower()]
    if already_on:
        return {"entry": entry, "modules": [], "already": already_on,
                "result": {"skipped": "already enabled"}, "archive": None}

    archive_url = entry.get("archive_url")
    if not archive_url:
        raise AddonError(f"Extension {eid!r} has no download URL in the index.")
    # The curated `install` path must only fetch from the official platform / its CDN.
    host_ = urllib.parse.urlparse(archive_url).hostname or ""
    if not host_.endswith("blender.org"):
        raise AddonError(
            f"Refusing to install {eid!r}: its archive_url host ({host_ or '?'}) is not blender.org. "
            "Use `install-url` for off-platform sources you trust."
        )
    archive = _download(archive_url, _cache_dir() / _archive_name(archive_url, entry), timeout)
    res = _exec(cl, _install_files_code(archive, repo))
    modules = list(res.get("new_modules") or [])
    already = [m for m in (res.get("enabled_ext") or []) if m.split(".")[-1].lower() == eid.lower()]
    error = res.get("error")
    if not modules and not already:
        # The local-file install enabled nothing — try the online repo by id.
        online = _exec(cl, _install_online_code(eid, _REMOTE_REPO))
        modules = list(online.get("new_modules") or [])
        error = online.get("error") or res.get("error")  # readable reason if both fail
        res = {"file": res, "online": online}
    return {"entry": entry, "modules": modules, "already": already, "error": error,
            "result": res, "archive": str(archive)}


def install_url(
    url: str,
    *,
    host: str = "localhost",
    port: int = 9876,
    legacy: bool | None = None,
    repo: str = _DEFAULT_REPO,
    client: BlenderClient | None = None,
    timeout: float = 180.0,
) -> dict:
    """Download an arbitrary add-on/extension archive and install it.

    ``legacy=None`` auto-detects: a ``.py`` file is a legacy add-on; a ``.zip`` is
    tried as an extension first. Point this only at trusted sources.
    """
    cl = _client(host, port, client)
    archive = _download(url, _cache_dir() / _archive_name(url), timeout)
    is_py = archive.suffix.lower() == ".py"
    if legacy is None:
        legacy = is_py
    if legacy:
        res = _exec(cl, _install_legacy_code(archive))
        return {"url": url, "modules": list(res.get("new_modules") or []), "result": res, "archive": str(archive)}
    res = _exec(cl, _install_files_code(archive, repo))
    modules = list(res.get("new_modules") or [])
    if not modules:
        # The extension installer enabled nothing (not a valid extension package, or a bare
        # legacy add-on) — fall back to the legacy add-on installer.
        res2 = _exec(cl, _install_legacy_code(archive))
        modules = list(res2.get("new_modules") or [])
        res = {"extension": res, "legacy": res2}
    return {"url": url, "modules": modules, "result": res, "archive": str(archive)}


def set_enabled(
    module: str, enable: bool = True, *, host: str = "localhost", port: int = 9876,
    client: BlenderClient | None = None,
) -> dict:
    return _exec(_client(host, port, client), _enable_code(module, enable))


def list_installed(*, host: str = "localhost", port: int = 9876, client: BlenderClient | None = None) -> dict:
    return _exec(_client(host, port, client), _LIST_CODE)


def register_asset_library(
    path: str, name: str | None = None, *, host: str = "localhost", port: int = 9876,
    client: BlenderClient | None = None,
) -> dict:
    p = str(Path(path).expanduser().resolve())
    if not Path(p).is_dir():
        raise AddonError(f"Asset-library path is not a folder: {p}")
    return _exec(_client(host, port, client), _asset_lib_add_code(p, name))


def list_asset_libraries(*, host: str = "localhost", port: int = 9876, client: BlenderClient | None = None) -> dict:
    return _exec(_client(host, port, client), _ASSET_LIB_LIST_CODE)


# pip name -> import name where they differ, so the post-install import check isn't a false negative.
_PIP_IMPORT_ALIASES = {
    "opencv-python": "cv2", "opencv-contrib-python": "cv2", "pillow": "PIL",
    "scikit-image": "skimage", "scikit-learn": "sklearn", "beautifulsoup4": "bs4",
    "pyyaml": "yaml", "python-dateutil": "dateutil",
}


def _import_name(pkg: str) -> str:
    """Best-effort module name for a pip package (for the post-install import check)."""
    base = re.split(r"[<>=!~\[ ]", pkg, maxsplit=1)[0].strip()
    return _PIP_IMPORT_ALIASES.get(base.lower(), base.replace("-", "_"))


def pip_install(
    packages: list[str], *, host: str = "localhost", port: int = 9876,
    client: BlenderClient | None = None, timeout: float = 600.0,
) -> dict:
    """``pip install --user`` packages into Blender's *bundled* Python.

    Targets the interpreter bpy code actually runs in (resolved over the socket), so
    the new package is importable from ``execute_blender_code`` immediately. ``--user``
    avoids needing write access to a read-only Blender install dir.
    """
    cl = _client(host, port, client)
    info = _exec(cl, _PY_EXE_CODE)
    exe = info.get("exe")
    if not exe:
        raise AddonError("Could not determine Blender's Python executable.")
    # `--` ends option parsing so a package token can't masquerade as a pip flag
    # (e.g. `--index-url=...` / `-e git+...` redirecting the install source).
    cmd = [exe, "-m", "pip", "install", "--user", "--no-input", "--", *packages]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603
    except subprocess.TimeoutExpired as ex:
        raise AddonError(f"pip timed out after {timeout:.0f}s installing {' '.join(packages)}.") from ex
    importable: dict[str, bool] = {}
    if proc.returncode == 0:
        check = "import importlib\nimportlib.invalidate_caches()\nok = {}\n"
        for pkg in packages:
            mod = _import_name(pkg)
            check += (
                f"try:\n    __import__({json.dumps(mod)})\n    ok[{json.dumps(mod)}] = True\n"
                f"except Exception:\n    ok[{json.dumps(mod)}] = False\n"
            )
        check += "result = {'importable': ok}\n"
        try:
            importable = _exec(cl, check).get("importable", {})
        except AddonError:
            importable = {}
    return {
        "exe": exe,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-1500:],
        "stderr": proc.stderr[-1500:],
        "importable": importable,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _host_port(args: argparse.Namespace) -> tuple[str, int]:
    host = args.host or os.environ.get("BLENDER_MCP_HOST") or "localhost"
    port = args.port or int(os.environ.get("BLENDER_MCP_PORT") or 9876)
    return host, port


def _emit_install(label: str, info: dict) -> int:
    mods = info.get("modules") or []
    already = info.get("already") or []
    if mods:
        print(f"INSTALLED + ENABLED: {label}")
        for m in mods:
            print(f"  module: {m}")
        print("Its operators/panels are live in this Blender session now. Use the module "
              "name above with bpy.ops.preferences.addon_disable(...) if you need to remove it.")
        return 0
    if already:
        print(f"ALREADY ENABLED: {label}")
        for m in already:
            print(f"  module: {m}")
        return 0
    err = info.get("error") or _result_error(info.get("result"))
    print(f"FAILED to install {label}: {err}", file=sys.stderr)
    return 1


def _result_error(result: object) -> str:
    """Pull a readable error out of a (possibly nested) Blender result dict."""
    if not isinstance(result, dict):
        return str(result)
    if result.get("error"):
        return str(result["error"])
    nested = "; ".join(
        f"{k}: {v.get('error')}" for k, v in result.items()
        if isinstance(v, dict) and v.get("error")
    )
    return nested or json.dumps(result, default=str)[:400]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="blendahbot.addons",
        description="Download/install Blender extensions, add-ons, asset libraries and "
        "Python packages into the live Blender session.",
    )
    p.add_argument("--host", default=None, help="Blender add-on host (default: localhost).")
    p.add_argument("--port", type=int, default=None, help="Blender add-on port (default: 9876).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="Search extensions.blender.org.")
    sp.add_argument("query")
    sp.add_argument("-n", type=int, default=8)
    sp.add_argument("--type", dest="kind", choices=["add-on", "theme"], default=None)

    ip = sub.add_parser("install", help="Install an extension by id or best-match query.")
    ip.add_argument("query")
    ip.add_argument("--repo", default=_DEFAULT_REPO, help="Local repo to install into (default: user_default).")

    up = sub.add_parser("install-url", help="Install an add-on/extension from a URL (trusted sources only).")
    up.add_argument("url")
    up.add_argument("--legacy", action="store_true", help="Force the legacy add-on installer.")

    ep = sub.add_parser("enable", help="Enable an installed/bundled module.")
    ep.add_argument("module")
    dp = sub.add_parser("disable", help="Disable a module.")
    dp.add_argument("module")

    sub.add_parser("list", help="List enabled extensions and add-ons.")

    ap = sub.add_parser("asset-library", help="Register an asset-library folder (or --list).")
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--list", action="store_true", dest="list_only")

    pp = sub.add_parser("pip", help="pip install packages into Blender's bundled Python.")
    pp.add_argument("packages", nargs="+")

    args = p.parse_args(argv)
    host, port = _host_port(args)

    try:
        if args.cmd == "search":
            index = fetch_index()
            hits = search(args.query, index, n=args.n, kind=args.kind, blender_version=(5, 1, 0))
            if not hits:
                print("(no matching extensions)", file=sys.stderr)
                return 1
            for e in hits:
                tags = ",".join(str(t) for t in (e.get("tags") or []))
                print(f"{e.get('id'):28} {str(e.get('version')):8} {e.get('type'):7} "
                      f"Bv>={e.get('blender_version_min'):8} {(e.get('tagline') or '')[:48]:48} [{tags}]")
            print("\nInstall one with:  python -m blendahbot.addons install <id>")
            return 0

        if args.cmd == "install":
            info = install_extension(args.query, host=host, port=port, repo=args.repo)
            return _emit_install(info["entry"].get("id", args.query), info)

        if args.cmd == "install-url":
            info = install_url(args.url, host=host, port=port, legacy=args.legacy or None)
            return _emit_install(args.url, info)

        if args.cmd in ("enable", "disable"):
            res = set_enabled(args.module, enable=(args.cmd == "enable"), host=host, port=port)
            on = res.get("enabled")
            if res.get("error") and not (args.cmd == "disable" and on is False):
                print(f"{args.cmd} {args.module} failed: {res['error']}", file=sys.stderr)
                return 1
            print(f"{args.module}: {'enabled' if on else 'disabled'}")
            return 0

        if args.cmd == "list":
            res = list_installed(host=host, port=port)
            print("Extensions:")
            for m in res.get("extensions", []):
                print(f"  {m}")
            print("Legacy add-ons:")
            for m in res.get("legacy", []):
                print(f"  {m}")
            return 0

        if args.cmd == "asset-library":
            if args.list_only or not args.path:
                res = list_asset_libraries(host=host, port=port)
            else:
                res = register_asset_library(args.path, args.name, host=host, port=port)
            for a in res.get("libraries", []):
                print(f"  {a['name']}: {a['path']}")
            return 0

        if args.cmd == "pip":
            res = pip_install(args.packages, host=host, port=port)
            tail = (res.get("stderr") or res.get("stdout") or "").strip().splitlines()[-3:]
            if res["returncode"] != 0:
                print(f"pip failed (exit {res['returncode']}):", file=sys.stderr)
                for line in tail:
                    print(f"  {line}", file=sys.stderr)
                return 1
            print(f"pip installed into Blender's Python ({res['exe']}):")
            for pkg, ok in (res.get("importable") or {}).items():
                print(f"  {pkg}: {'importable' if ok else 'installed (import name differs)'}")
            print("bpy code can `import` these now.")
            return 0
    except AddonError as ex:
        print(f"[addons] {ex}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as ex:
        # network hiccup fetching the index / archive, or a garbled response
        print(f"[addons] could not reach extensions.blender.org or download the archive: {ex}",
              file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
