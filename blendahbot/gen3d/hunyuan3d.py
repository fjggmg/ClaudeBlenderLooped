"""Hunyuan3D 3.1 (hosted) backend — Tencent's v3 generator via the Replicate API.

Hunyuan3D 3.x has NO open weights, so this is a hosted REST backend (text->3D and
image->3D, GLB + optional PBR). It's the "max-quality" tier above the free local
Hunyuan3D-2.1 server; opt in with ``--backend hunyuan3``. Needs REPLICATE_API_TOKEN.

Contract (Replicate HTTP API, model-level so no version hash to pin):
  POST {API}/models/{model}/predictions  {input:{...}} -> {id, status, urls:{get}, ...}
  GET  {prediction.urls.get}             -> poll until status succeeded|failed|canceled
  output -> a GLB url (str), a list of urls, or an object with a model-file field.

Because reseller input schemas drift between versions, the exact input field names
(prompt vs caption, image vs input_image, texture toggle, face count) are discovered
from the model's OpenAPI schema at run time and mapped onto GenRequest. If that probe
fails for any reason we fall back to a sensible default key set and proceed.

Overrides: BLENDAHBOT_HUNYUAN3D_MODEL (default ``tencent/hunyuan-3d-3.1``).
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .base import Gen3DBackend, Gen3DError, GenRequest, GenResult, ProgressFn

_API = "https://api.replicate.com/v1"
_DEFAULT_MODEL = "tencent/hunyuan-3d-3.1"

# Candidate input-field names, most-specific first. We send GenRequest values under
# whichever of these the model actually exposes (discovered from its schema).
_PROMPT_KEYS = ("prompt", "caption", "text_prompt", "text")
_IMAGE_KEYS = ("image", "input_image", "image_path", "front_image", "image_url")
_TEXTURE_KEYS = ("texture", "textured", "paint", "pbr", "enable_texture", "generate_texture")
_FACE_KEYS = ("face_count", "num_faces", "face_limit", "max_facenum", "target_face_num")
# Used when schema introspection is unavailable — the canonical Replicate names.
_DEFAULT_KEYS = frozenset({"prompt", "image", "seed", "texture", "face_count"})


def _model() -> str:
    return os.environ.get("BLENDAHBOT_HUNYUAN3D_MODEL", _DEFAULT_MODEL).strip("/")


def _token() -> str | None:
    return os.environ.get("REPLICATE_API_TOKEN")


def _first_present(candidates: tuple[str, ...], available: frozenset[str] | set[str]) -> str | None:
    for k in candidates:
        if k in available:
            return k
    return None


def _data_uri(path: Path) -> str:
    """Inline an image as a data URI so we don't need Replicate's file-upload step."""
    ext = (path.suffix.lstrip(".") or "png").lower()
    mime = {"jpg": "jpeg", "svg": "svg+xml"}.get(ext, ext)
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{b64}"


def _build_input(req: GenRequest, keys: frozenset[str] | set[str]) -> dict[str, object]:
    """Map a GenRequest onto the model's actual input field names."""
    inp: dict[str, object] = {}

    if req.image_path and Path(req.image_path).exists():
        ik = _first_present(_IMAGE_KEYS, keys)
        if not ik:
            raise Gen3DError(f"model '{_model()}' exposes no image input field; use a text prompt instead")
        inp[ik] = _data_uri(Path(req.image_path))

    if req.prompt:
        pk = _first_present(_PROMPT_KEYS, keys)
        if pk:
            inp[pk] = req.prompt
        elif not inp:  # no image either
            raise Gen3DError(f"model '{_model()}' exposes no text-prompt field; provide --image instead")

    tk = _first_present(_TEXTURE_KEYS, keys)
    if tk is not None:
        inp[tk] = bool(req.texture)
    if req.seed is not None and "seed" in keys:
        inp["seed"] = req.seed
    if req.face_count is not None:
        fk = _first_present(_FACE_KEYS, keys)
        if fk:
            inp[fk] = req.face_count

    if not inp:
        raise Gen3DError("Hunyuan3D 3.1 needs a prompt or an --image.")
    return inp


def _extract_glb_url(output: object) -> str | None:
    """Pull a model-file URL out of Replicate's flexible ``output`` shape."""
    if isinstance(output, str):
        return output or None
    if isinstance(output, list):
        for item in output:  # prefer an explicit mesh file
            if isinstance(item, str) and item.split("?")[0].lower().endswith((".glb", ".gltf")):
                return item
        for item in output:
            url = _extract_glb_url(item)
            if url:
                return url
        return None
    if isinstance(output, dict):
        preferred = (
            "mesh", "model", "glb", "model_file", "textured_mesh", "pbr_model",
            "model_glb", "mesh_glb", "output", "file", "url",
        )
        for k in preferred:
            url = _extract_glb_url(output.get(k))
            if url:
                return url
        for v in output.values():  # last resort: any nested url
            url = _extract_glb_url(v)
            if url:
                return url
    return None


class Hunyuan3DReplicateBackend(Gen3DBackend):
    name = "hunyuan3"

    def available(self, req: GenRequest) -> tuple[bool, str]:
        if not _token():
            return False, "set REPLICATE_API_TOKEN (create one at replicate.com/account/api-tokens)"
        if not req.prompt and not req.image_path:
            return False, "provide a prompt or --image"
        return True, "ok"

    def generate(
        self, req: GenRequest, out: Path, on_progress: ProgressFn | None = None, timeout: float = 600.0
    ) -> GenResult:
        token = _token()
        if not token:
            raise Gen3DError("REPLICATE_API_TOKEN not set.")
        keys = _input_keys(token) or _DEFAULT_KEYS
        payload = _build_input(req, keys)
        pred = self._create(payload, token, timeout)
        return self._poll(pred, out, on_progress, token, timeout)

    def _create(self, payload: dict, token: str, timeout: float) -> dict:
        url = f"{_API}/models/{_model()}/predictions"
        req = urllib.request.Request(
            url, data=json.dumps({"input": payload}).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        return _json(req, timeout, "Replicate create prediction")

    def _poll(
        self, pred: dict, out: Path, on_progress: ProgressFn | None, token: str, timeout: float
    ) -> GenResult:
        get_url = (pred.get("urls") or {}).get("get") or f"{_API}/predictions/{pred.get('id')}"
        deadline = time.time() + timeout
        delay = 2.0
        last = ""
        data = pred
        while True:
            status = str(data.get("status", ""))
            if on_progress and status and status != last:
                on_progress(status)
                last = status
            if status == "succeeded":
                url = _extract_glb_url(data.get("output"))
                if not url:
                    raise Gen3DError(f"Replicate succeeded but no GLB url in output: {data.get('output')}")
                _download(url, out, timeout)
                return GenResult(path=out, backend=self.name)
            if status in ("failed", "canceled"):
                raise Gen3DError(f"Replicate prediction {status}: {data.get('error') or data}")
            if time.time() >= deadline:
                raise Gen3DError(f"Replicate prediction timed out after {timeout:.0f}s (id {data.get('id')}).")
            time.sleep(delay)
            delay = min(delay * 1.3, 5.0)
            req = urllib.request.Request(get_url, headers={"Authorization": f"Bearer {token}"})
            data = _json(req, 30, "Replicate get prediction")


def _input_keys(token: str) -> frozenset[str] | None:
    """Discover the model's input field names from its OpenAPI schema (best-effort)."""
    try:
        req = urllib.request.Request(
            f"{_API}/models/{_model()}", headers={"Authorization": f"Bearer {token}"}
        )
        data = _json(req, 30, "Replicate model schema")
    except Gen3DError:
        return None
    schema = (
        (((data.get("latest_version") or {}).get("openapi_schema") or {}).get("components") or {})
        .get("schemas", {})
        .get("Input", {})
    )
    props = schema.get("properties") or {}
    keys = frozenset(props.keys())
    return keys or None


def _json(req: urllib.request.Request, timeout: float, what: str) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - trusted host
            return json.load(r)
    except urllib.error.HTTPError as ex:
        raise Gen3DError(f"{what} failed: {ex.code} {ex.read().decode()[:300]}") from ex
    except Exception as ex:  # noqa: BLE001
        raise Gen3DError(f"{what} failed: {ex}") from ex


def _download(url: str, out: Path, timeout: float) -> None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            out.write_bytes(r.read())
    except Exception as ex:  # noqa: BLE001
        raise Gen3DError(f"Replicate download failed: {ex}") from ex
