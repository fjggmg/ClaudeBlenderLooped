import json
import socket
import threading
import unittest

from blendahbot.blender import BlenderClient, BlenderUnavailable


class FakeBlender:
    """A one-shot TCP server speaking the add-on's NUL-framed JSON protocol."""

    def __init__(self, response: dict):
        self.response = response
        self.requests: list[dict] = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("localhost", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            buf = bytearray()
            while b"\0" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            line, _, _ = buf.partition(b"\0")
            if line:
                self.requests.append(json.loads(line.decode("utf-8")))
            conn.sendall((json.dumps(self.response) + "\0").encode("utf-8"))

    def close(self) -> None:
        self._srv.close()


class BlenderProtocolTests(unittest.TestCase):
    def test_execute_roundtrip(self):
        fake = FakeBlender({"status": "ok", "result": 2, "stdout": "2\n"})
        try:
            client = BlenderClient("localhost", fake.port, timeout=5)
            resp = client.execute("print(1+1)")
        finally:
            fake.close()
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(fake.requests[0]["type"], "execute")
        self.assertEqual(fake.requests[0]["code"], "print(1+1)")
        self.assertIn("strict_json", fake.requests[0])

    def test_ping_ok(self):
        fake = FakeBlender({"status": "ok", "stdout": "blender 4.2.0"})
        try:
            ok, detail = BlenderClient("localhost", fake.port, timeout=5).ping()
        finally:
            fake.close()
        self.assertTrue(ok)
        self.assertIn("blender", detail)

    def test_render_still_detects_sentinel(self):
        fake = FakeBlender({"status": "ok", "stdout": "BB_RENDER_OK\n"})
        try:
            ok, detail = BlenderClient("localhost", fake.port, timeout=5).render_still("out.png")
        finally:
            fake.close()
        self.assertTrue(ok)
        # The rendered path must have been embedded in the executed code.
        self.assertIn("out.png", fake.requests[0]["code"])

    def test_render_still_reports_reason_on_error(self):
        fake = FakeBlender({"status": "error", "message": "no render engine"})
        try:
            ok, detail = BlenderClient("localhost", fake.port, timeout=5).render_still("out.png")
        finally:
            fake.close()
        self.assertFalse(ok)
        self.assertIn("no render engine", detail)

    def test_save_blend_detects_sentinel(self):
        fake = FakeBlender({"status": "ok", "stdout": "BB_SAVE_OK\n"})
        try:
            ok = BlenderClient("localhost", fake.port, timeout=5).save_blend("scene.blend")
        finally:
            fake.close()
        self.assertTrue(ok)

    def test_connection_refused_raises(self):
        # Port 1 is virtually never open.
        client = BlenderClient("localhost", 1, timeout=2)
        with self.assertRaises(BlenderUnavailable):
            client.execute("print(1)")

    def test_ping_never_raises_when_down(self):
        ok, detail = BlenderClient("localhost", 1, timeout=2).ping()
        self.assertFalse(ok)
        self.assertTrue(detail)


if __name__ == "__main__":
    unittest.main()
