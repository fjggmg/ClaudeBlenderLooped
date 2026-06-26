import json
import os
import socket
import threading
import unittest
from unittest import mock

from blendahbot.blender import BlenderClient, BlenderUnavailable, launch_blender


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


class SilentServer:
    """Accepts a connection but never replies — simulates a frozen main thread.

    The add-on services its socket from a main-thread timer, so a hung Blender
    still completes the TCP handshake (kernel) but sends no response.
    """

    def __init__(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("localhost", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            # Drain input but never respond, until told to stop.
            while not self._stop.is_set():
                try:
                    conn.settimeout(0.1)
                    conn.recv(4096)
                except OSError:
                    if self._stop.wait(0.05):
                        break

    def close(self) -> None:
        self._stop.set()
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


class WaitUntilReadyTests(unittest.TestCase):
    def test_returns_true_when_server_up(self):
        fake = FakeBlender({"status": "ok", "stdout": "blender 4.2.0"})
        try:
            ok, detail = BlenderClient("localhost", fake.port, timeout=5).wait_until_ready(
                timeout=5, interval=0.1
            )
        finally:
            fake.close()
        self.assertTrue(ok)
        self.assertIn("blender", detail)

    def test_times_out_when_down(self):
        progress: list[str] = []
        ok, detail = BlenderClient("localhost", 1, timeout=1).wait_until_ready(
            timeout=0.3, interval=0.1, on_progress=progress.append
        )
        self.assertFalse(ok)
        self.assertTrue(detail)
        self.assertTrue(progress)  # the progress callback fired while waiting

    def test_each_probe_is_bounded_not_operational_timeout(self):
        # The whole point of wait_until_ready: even with a long operational client
        # timeout, each readiness probe must use the SHORT probe_timeout, or a
        # blocking connect to a not-yet-open port would defeat the poll interval.
        client = BlenderClient("localhost", 9, timeout=300.0)
        seen: list[float] = []

        def fake_health(timeout):
            seen.append(timeout)
            return ("crashed", "down")

        client.health_check = fake_health
        ok, _ = client.wait_until_ready(timeout=0.3, interval=0.05, probe_timeout=0.2)
        self.assertFalse(ok)
        self.assertTrue(seen)
        self.assertTrue(all(t <= 0.2 + 1e-9 for t in seen))  # never the 300s timeout


class HealthCheckTests(unittest.TestCase):
    def test_ok_when_server_replies(self):
        fake = FakeBlender({"status": "ok", "stdout": "bb_health 4.2.0"})
        try:
            state, detail = BlenderClient("localhost", fake.port).health_check(timeout=5)
        finally:
            fake.close()
        self.assertEqual(state, "ok")
        self.assertIn("4.2.0", detail)

    def test_ok_even_when_probe_errors(self):
        # A reply of ANY status proves the main thread is servicing the socket.
        fake = FakeBlender({"status": "error", "message": "boom"})
        try:
            state, _ = BlenderClient("localhost", fake.port).health_check(timeout=5)
        finally:
            fake.close()
        self.assertEqual(state, "ok")

    def test_crashed_when_refused(self):
        # A refusal classifies as "crashed". Some systems (and firewalls) silently
        # drop connects to a closed port instead of RST-ing, so force the refusal
        # deterministically rather than relying on a real closed port.
        import blendahbot.blender as bl

        class _Refusing:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, *a):
                pass

            def connect(self, *a):
                raise ConnectionRefusedError(10061, "refused")

            def sendall(self, *a):
                pass

            def recv(self, *a):
                return b""

        with mock.patch.object(bl.socket, "socket", return_value=_Refusing()):
            state, detail = BlenderClient("localhost", 9999).health_check(timeout=2)
        self.assertEqual(state, "crashed")
        self.assertTrue(detail)

    def test_hung_when_no_reply(self):
        srv = SilentServer()
        try:
            state, detail = BlenderClient("localhost", srv.port).health_check(timeout=0.5)
        finally:
            srv.close()
        self.assertEqual(state, "hung")
        self.assertTrue(detail)


class LaunchBlenderTests(unittest.TestCase):
    def test_spawns_detached_with_startup_script(self):
        with mock.patch("blendahbot.blender.subprocess.Popen") as popen:
            launch_blender(r"C:\fake\blender.exe", port=9999)
        self.assertEqual(popen.call_count, 1)
        args = popen.call_args.args[0]
        self.assertEqual(args[0], r"C:\fake\blender.exe")
        # The official add-on only starts its server with online access enabled.
        self.assertIn("--online-mode", args)
        self.assertIn("--python", args)
        script_path = args[args.index("--python") + 1]
        self.assertTrue(os.path.exists(script_path))
        try:
            with open(script_path, encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("9999", body)
            # Official operator id (not the old ahujasid `blendermcp.start_server`).
            self.assertIn("server_start", body)
            # Crash-dialog suppression is baked into the startup script too.
            self.assertIn("SetErrorMode", body)
            # The timer call must use the real kwarg — `first_delay` would TypeError
            # and abort the whole startup script before it could start the server.
            self.assertIn("register(_bb_start_mcp, first_interval=", body)
            self.assertNotIn("register(_bb_start_mcp, first_delay=", body)
        finally:
            os.unlink(script_path)

    def test_restores_caller_error_mode(self):
        # launch_blender disables the crash dialog across the spawn; it must put the
        # caller's own error mode back afterward (don't leak the change to blendahbot).
        if os.name != "nt":
            self.skipTest("Windows-only crash-dialog suppression")
        import ctypes

        before = ctypes.windll.kernel32.GetErrorMode()
        with mock.patch("blendahbot.blender.subprocess.Popen") as popen:
            launch_blender(r"C:\fake\blender.exe", port=9876)
        after = ctypes.windll.kernel32.GetErrorMode()
        self.assertEqual(before, after)
        args = popen.call_args.args[0]
        script_path = args[args.index("--python") + 1]
        if os.path.exists(script_path):
            os.unlink(script_path)

    def test_reopens_checkpoint_blend(self):
        with mock.patch("blendahbot.blender.subprocess.Popen") as popen:
            launch_blender(r"C:\fake\blender.exe", port=9876, blend_file=r"C:\run\checkpoint.blend")
        args = popen.call_args.args[0]
        try:
            self.assertIn(r"C:\run\checkpoint.blend", args)
            # The .blend must load before --python runs against it.
            self.assertLess(args.index(r"C:\run\checkpoint.blend"), args.index("--python"))
        finally:
            script_path = args[args.index("--python") + 1]
            if os.path.exists(script_path):
                os.unlink(script_path)


if __name__ == "__main__":
    unittest.main()
