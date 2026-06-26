"""Local Hunyuan3D-2.1 backend — talks to the model's api_server over HTTP.

Run the server from the Hunyuan3D-2.1 WinPortable build (its "API 2.1" option) or the
repo's ``api_server.py``; it binds 0.0.0.0:8081 and exposes:
  POST /send {image:<base64>, texture:<bool>, ...} -> {"uid": ...}
  GET  /status/{uid} -> {"status": "processing"|"texturing"|"completed"|"error",
                         "model_base64": <glb>, "message": <err>}
  GET  /health -> {"status": ...}
Override the URL with BLENDAHBOT_HUNYUAN_URL. Hunyuan3D is image-only (use --image).
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


def _server_url() -> str:
    return os.environ.get("BLENDAHBOT_HUNYUAN_URL", "http://localhost:8081").rstrip("/")


class HunyuanLocalBackend(Gen3DBackend):
    name = "hunyuan"

    def available(self, req: GenRequest) -> tuple[bool, str]:
        url = _server_url()
        try:
            urllib.request.urlopen(url + "/health", timeout=5)  # noqa: S310 - localhost
        except urllib.error.HTTPError:
            pass  # any HTTP response (e.g. 404 — some builds have no /health) means it's up
        except Exception as ex:  # noqa: BLE001 - connection refused etc.
            return False, (
                f"Hunyuan3D server not reachable at {url} ({ex}). "
                "Start it with 5-start-api-server.bat."
            )
        if not req.image_path:
            return False, "Hunyuan3D is image-only — provide --image (or use Tripo for text-only)."
        return True, "ok"

    def generate(
        self, req: GenRequest, out: Path, on_progress: ProgressFn | None = None, timeout: float = 600.0
    ) -> GenResult:
        if not req.image_path or not Path(req.image_path).exists():
            raise Gen3DError("Hunyuan3D needs an existing --image path.")
        img_b64 = base64.b64encode(Path(req.image_path).read_bytes()).decode("ascii")
        payload: dict[str, object] = {"image": img_b64, "texture": bool(req.texture)}
        if req.seed is not None:
            payload["seed"] = req.seed
        if req.face_count is not None:
            payload["face_count"] = req.face_count

        uid = self._send(payload, timeout)
        return self._poll(uid, out, on_progress, timeout)

    def _send(self, payload: dict, timeout: float) -> str:
        url = _server_url() + "/send"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                data = json.load(r)
        except urllib.error.HTTPError as ex:
            raise Gen3DError(f"Hunyuan /send failed: {ex.code} {ex.read().decode()[:300]}") from ex
        except Exception as ex:  # noqa: BLE001
            raise Gen3DError(f"Hunyuan /send failed: {ex}") from ex
        uid = data.get("uid")
        if not uid:
            raise Gen3DError(f"Hunyuan /send returned no uid: {data}")
        return str(uid)

    def _poll(self, uid: str, out: Path, on_progress: ProgressFn | None, timeout: float) -> GenResult:
        deadline = time.time() + timeout
        delay = 2.0
        last = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(_server_url() + f"/status/{uid}", timeout=30) as r:  # noqa: S310
                    data = json.load(r)
            except Exception as ex:  # noqa: BLE001
                raise Gen3DError(f"Hunyuan /status failed: {ex}") from ex
            status = str(data.get("status", ""))
            if on_progress and status and status != last:
                on_progress(status)
                last = status
            if status == "completed":
                b64 = data.get("model_base64")
                if not b64:
                    raise Gen3DError("Hunyuan reported completed but returned no model_base64.")
                out.write_bytes(base64.b64decode(b64))
                return GenResult(path=out, backend=self.name)
            if status == "error":
                raise Gen3DError(f"Hunyuan generation error: {data.get('message')}")
            time.sleep(delay)
            delay = min(delay * 1.3, 5.0)
        raise Gen3DError(f"Hunyuan generation timed out after {timeout:.0f}s (uid {uid}).")
