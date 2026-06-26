"""Local TRELLIS.2 backend — talks to a TRELLIS.2 inference server over HTTP.

Microsoft's TRELLIS.2-4B (MIT) is a newer open-weights image->3D model with full PBR
and strong geometry on complex/thin shapes. It has no official HTTP server, so this
backend speaks the same minimal contract as the local Hunyuan one — stand up a small
server in front of the TRELLIS.2 pipeline that exposes:
  POST /send {image:<base64>, texture:<bool>, seed?, face_count?} -> {"uid": ...}
  GET  /status/{uid} -> {"status": "processing"|"completed"|"error",
                         "model_base64": <glb>, "message": <err>}
  GET  /health -> 200

Override the URL with BLENDAHBOT_TRELLIS_URL (default http://localhost:8084).

Caveats (see gen3d-model-landscape-2026 memory): TRELLIS.2 is **image->3D only** (no
native text->3D — pass --image), and its official VRAM floor is 24 GB, so fitting a
16 GB card is unproven and may need a low-VRAM/512-cascade server config.
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
    return os.environ.get("BLENDAHBOT_TRELLIS_URL", "http://localhost:8084").rstrip("/")


class TrellisLocalBackend(Gen3DBackend):
    name = "trellis"

    def available(self, req: GenRequest) -> tuple[bool, str]:
        url = _server_url()
        try:
            urllib.request.urlopen(url + "/health", timeout=5)  # noqa: S310 - localhost
        except urllib.error.HTTPError:
            pass  # any HTTP response means the server is up (some builds lack /health)
        except Exception as ex:  # noqa: BLE001 - connection refused etc.
            return False, (
                f"TRELLIS.2 server not reachable at {url} ({ex}). Start your TRELLIS.2 "
                "inference server (set BLENDAHBOT_TRELLIS_URL if it's elsewhere)."
            )
        if not req.image_path and not req.prompt:
            return False, "TRELLIS.2 is image->3D — provide --image."
        return True, "ok"

    def generate(
        self, req: GenRequest, out: Path, on_progress: ProgressFn | None = None, timeout: float = 600.0
    ) -> GenResult:
        payload: dict[str, object] = {"texture": bool(req.texture)}
        if req.image_path and Path(req.image_path).exists():
            payload["image"] = base64.b64encode(Path(req.image_path).read_bytes()).decode("ascii")
        elif req.prompt:
            # TRELLIS.2 has no native text->3D; pass it through so a server that bolts a
            # text->image front stage can use it, otherwise the server will reject it.
            payload["text"] = req.prompt
        else:
            raise Gen3DError("TRELLIS.2 needs an --image (it is image->3D only).")
        if req.seed is not None:
            payload["seed"] = req.seed
        if req.face_count is not None:
            payload["face_count"] = req.face_count

        uid = self._send(payload, timeout)
        return self._poll(uid, out, on_progress, timeout)

    def _send(self, payload: dict, timeout: float) -> str:
        req = urllib.request.Request(
            _server_url() + "/send", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                data = json.load(r)
        except urllib.error.HTTPError as ex:
            raise Gen3DError(f"TRELLIS /send failed: {ex.code} {ex.read().decode()[:300]}") from ex
        except Exception as ex:  # noqa: BLE001
            raise Gen3DError(f"TRELLIS /send failed: {ex}") from ex
        uid = data.get("uid")
        if not uid:
            raise Gen3DError(f"TRELLIS /send returned no uid: {data}")
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
                raise Gen3DError(f"TRELLIS /status failed: {ex}") from ex
            status = str(data.get("status", ""))
            if on_progress and status and status != last:
                on_progress(status)
                last = status
            if status == "completed":
                b64 = data.get("model_base64")
                if not b64:
                    raise Gen3DError("TRELLIS reported completed but returned no model_base64.")
                out.write_bytes(base64.b64decode(b64))
                return GenResult(path=out, backend=self.name)
            if status == "error":
                raise Gen3DError(f"TRELLIS generation error: {data.get('message')}")
            time.sleep(delay)
            delay = min(delay * 1.3, 5.0)
        raise Gen3DError(f"TRELLIS generation timed out after {timeout:.0f}s (uid {uid}).")
