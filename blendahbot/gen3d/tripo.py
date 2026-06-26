"""Tripo (tripo3d.ai) hosted backend — text->3D and image->3D, GLB + PBR.

Secondary/fallback to the local Hunyuan backend. Needs TRIPO_API_KEY (tsk_...).
Contract: POST {BASE}/task (text_to_model | image_to_model) -> task_id; poll
GET {BASE}/task/{id} until status success|failed; download output.pbr_model (GLB).
Image path: POST {BASE}/upload/sts (multipart) -> file_token first.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .base import Gen3DBackend, Gen3DError, GenRequest, GenResult, ProgressFn

_BASE = "https://api.tripo3d.ai/v2/openapi"


class TripoBackend(Gen3DBackend):
    name = "tripo"

    def _key(self) -> str | None:
        return os.environ.get("TRIPO_API_KEY")

    def _headers(self, json_body: bool = True) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._key()}"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def available(self, req: GenRequest) -> tuple[bool, str]:
        if not self._key():
            return False, "set TRIPO_API_KEY (free key + 300 credits at tripo3d.ai)"
        if not req.prompt and not req.image_path:
            return False, "provide a prompt or --image"
        return True, "ok"

    def generate(
        self, req: GenRequest, out: Path, on_progress: ProgressFn | None = None, timeout: float = 600.0
    ) -> GenResult:
        if not self._key():
            raise Gen3DError("TRIPO_API_KEY not set.")
        if req.image_path and Path(req.image_path).exists():
            token = self._upload(Path(req.image_path), timeout)
            task: dict[str, object] = {
                "type": "image_to_model",
                "file": {"type": (Path(req.image_path).suffix.lstrip(".") or "png"), "file_token": token},
            }
        elif req.prompt:
            task = {"type": "text_to_model", "prompt": req.prompt}
        else:
            raise Gen3DError("Tripo needs a prompt or an --image.")
        if req.texture is False:
            task["texture"] = False
        if req.seed is not None:
            task["model_seed"] = req.seed
        task_id = self._create(task, timeout)
        return self._poll(task_id, out, on_progress, timeout)

    def _create(self, task: dict, timeout: float) -> str:
        req = urllib.request.Request(
            f"{_BASE}/task", data=json.dumps(task).encode(), headers=self._headers(), method="POST"
        )
        data = _json(req, timeout, "Tripo /task")
        tid = (data.get("data") or {}).get("task_id") or data.get("task_id")
        if not tid:
            raise Gen3DError(f"Tripo /task returned no task_id: {data}")
        return str(tid)

    def _upload(self, path: Path, timeout: float) -> str:
        boundary = "----blendahbot" + os.urandom(8).hex()
        body = (
            f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode()
            + b"Content-Type: application/octet-stream\r\n\r\n"
            + path.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        req = urllib.request.Request(
            f"{_BASE}/upload/sts", data=body, method="POST",
            headers={"Authorization": f"Bearer {self._key()}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        data = _json(req, timeout, "Tripo /upload/sts")
        d = data.get("data") or {}
        token = d.get("file_token") or d.get("image_token") or d.get("token")
        if not token:
            raise Gen3DError(f"Tripo upload returned no file_token: {data}")
        return str(token)

    def _poll(self, task_id: str, out: Path, on_progress: ProgressFn | None, timeout: float) -> GenResult:
        deadline = time.time() + timeout
        delay = 2.0
        last = -1
        while time.time() < deadline:
            req = urllib.request.Request(f"{_BASE}/task/{task_id}", headers=self._headers(False))
            data = _json(req, 30, "Tripo /task/{id}")
            d = data.get("data") or {}
            status = d.get("status")
            progress = int(d.get("progress", 0) or 0)
            if on_progress and progress != last:
                on_progress(f"{status} {progress}%")
                last = progress
            if status == "success":
                out_block = d.get("output") or {}
                url = out_block.get("pbr_model") or out_block.get("model") or out_block.get("base_model")
                if not url:
                    raise Gen3DError(f"Tripo success but no model url: {out_block}")
                _download(str(url), out, timeout)
                return GenResult(path=out, backend=self.name)
            if status in ("failed", "cancelled", "unknown", "banned"):
                raise Gen3DError(f"Tripo task {status}: {d}")
            time.sleep(delay)
            delay = min(delay * 1.3, 5.0)
        raise Gen3DError(f"Tripo task timed out after {timeout:.0f}s (id {task_id}).")


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
        raise Gen3DError(f"Tripo download failed: {ex}") from ex
