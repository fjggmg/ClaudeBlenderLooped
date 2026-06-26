import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blendahbot import supervisor
from blendahbot.config import BotConfig
from blendahbot.discovery import DiscoveryError
from blendahbot.supervisor import BlenderSupervisor, _parse_netstat_pids


class _Console:
    """No-op console with the methods the supervisor calls."""

    def info(self, *_a, **_k):
        pass

    warn = success = error = info


def _config(**over) -> BotConfig:
    cfg = BotConfig(request="x")
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


class ParseNetstatTests(unittest.TestCase):
    SAMPLE = """
  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:9876         0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:9876         127.0.0.1:55000        ESTABLISHED     5678
  TCP    0.0.0.0:80             0.0.0.0:0              LISTENING       4321
  TCP    127.0.0.1:19876        0.0.0.0:0              LISTENING       9999
"""

    def test_only_listening_on_the_port(self):
        # The ESTABLISHED client conn, the :80 listener, and :19876 (a different
        # port that merely ends in the digits) are all excluded.
        self.assertEqual(_parse_netstat_pids(self.SAMPLE, 9876), {1234})

    def test_empty_when_no_match(self):
        self.assertEqual(_parse_netstat_pids(self.SAMPLE, 1234), set())

    def test_listener_detected_on_localized_windows(self):
        # German netstat localizes the State column ("ABHÖREN"); the listener must
        # still be found via its wildcard foreign address (0.0.0.0:0).
        sample = "  TCP    127.0.0.1:9876    0.0.0.0:0    ABHÖREN    4242\n"
        self.assertEqual(_parse_netstat_pids(sample, 9876), {4242})


class KillPidTests(unittest.TestCase):
    def test_false_when_process_survives(self):
        # taskkill exit code can't be trusted (128 = not-found OR access-denied), so
        # kill_pid verifies the process is actually gone.
        with mock.patch("blendahbot.supervisor.subprocess.run"), \
             mock.patch("blendahbot.supervisor._pid_running", return_value=True):
            self.assertFalse(supervisor.kill_pid(1234))

    def test_true_when_gone(self):
        with mock.patch("blendahbot.supervisor.subprocess.run"), \
             mock.patch("blendahbot.supervisor._pid_running", return_value=False):
            self.assertTrue(supervisor.kill_pid(1234))


class CheckTests(unittest.TestCase):
    def test_owned_exited_process_is_crashed(self):
        proc = mock.Mock()
        proc.poll.return_value = 1  # exited
        sup = BlenderSupervisor(_config(), _Console(), proc=proc)
        self.assertEqual(sup.check(), "crashed")

    def test_delegates_to_health_check_when_alive(self):
        proc = mock.Mock()
        proc.poll.return_value = None  # still running
        sup = BlenderSupervisor(_config(), _Console(), proc=proc)
        sup.client.health_check = lambda timeout: ("hung", "x")
        self.assertEqual(sup.check(), "hung")


class EnsureHealthyTests(unittest.TestCase):
    def test_healthy_does_not_restart(self):
        sup = BlenderSupervisor(_config(), _Console())
        sup.check = lambda timeout=None: "ok"
        sup.restart = mock.Mock()
        self.assertEqual(sup.ensure_healthy(), (True, False))
        sup.restart.assert_not_called()

    def test_disabled_reports_only(self):
        sup = BlenderSupervisor(_config(auto_restart_blender=False), _Console())
        sup.check = lambda timeout=None: "hung"
        sup.restart = mock.Mock()
        self.assertEqual(sup.ensure_healthy(), (False, False))
        sup.restart.assert_not_called()

    def test_restart_succeeds(self):
        sup = BlenderSupervisor(_config(), _Console())
        sup.check = lambda timeout=None: "crashed"
        sup.restart = mock.Mock(return_value=True)
        self.assertEqual(sup.ensure_healthy(), (True, True))
        sup.restart.assert_called_once()

    def test_gives_up_after_attempts(self):
        sup = BlenderSupervisor(_config(blender_restart_attempts=2), _Console())
        sup.check = lambda timeout=None: "crashed"
        sup.restart = mock.Mock(return_value=False)
        self.assertEqual(sup.ensure_healthy(), (False, True))
        self.assertEqual(sup.restart.call_count, 2)

    def test_recovers_when_check_passes_after_failed_restart(self):
        # The kill cleared the hang and Blender came back, even though restart()
        # reported failure (its wait timed out). check() flips crashed -> ok.
        sup = BlenderSupervisor(_config(), _Console())
        states = iter(["crashed", "ok"])
        sup.check = lambda timeout=None: next(states)
        sup.restart = mock.Mock(return_value=False)
        self.assertEqual(sup.ensure_healthy(), (True, True))
        sup.restart.assert_called_once()

    def test_forwards_checkpoint_to_restart(self):
        sup = BlenderSupervisor(_config(), _Console())
        sup.check = lambda timeout=None: "crashed"
        sup.restart = mock.Mock(return_value=True)
        sup.ensure_healthy(checkpoint=Path(r"C:\run\checkpoint.blend"))
        self.assertEqual(
            sup.restart.call_args.kwargs.get("checkpoint"), Path(r"C:\run\checkpoint.blend")
        )


class RestartTests(unittest.TestCase):
    def test_relaunches_and_waits(self):
        sup = BlenderSupervisor(_config(), _Console())
        sup.client.wait_until_ready = lambda timeout=90.0: (True, "blender 4.2")
        with mock.patch("blendahbot.supervisor.find_blender_executable", return_value=r"C:\b\blender.exe"), \
             mock.patch("blendahbot.supervisor.find_pids_on_port", return_value=[]), \
             mock.patch("blendahbot.supervisor.launch_blender") as lb, \
             mock.patch("blendahbot.supervisor.time.sleep"):
            ok = sup.restart(checkpoint=None)
        self.assertTrue(ok)
        self.assertEqual(sup.restarts, 1)
        lb.assert_called_once()

    def test_returns_false_when_exe_missing(self):
        sup = BlenderSupervisor(_config(), _Console())
        with mock.patch("blendahbot.supervisor.find_blender_executable",
                        side_effect=DiscoveryError("nope")):
            self.assertFalse(sup.restart())

    def test_kill_stale_uses_owned_handle(self):
        proc = mock.Mock()
        sup = BlenderSupervisor(_config(), _Console(), proc=proc)
        with mock.patch("blendahbot.supervisor.find_pids_on_port", return_value=[]):
            sup._kill_stale()
        proc.kill.assert_called_once()
        self.assertIsNone(sup.proc)

    def test_kill_stale_by_port_when_unowned(self):
        # User opened Blender themselves (proc is None) and it hung — find + kill by port.
        sup = BlenderSupervisor(_config(), _Console())  # proc=None
        with mock.patch("blendahbot.supervisor.find_pids_on_port", return_value=[111, 222]), \
             mock.patch("blendahbot.supervisor.kill_pid") as kp:
            sup._kill_stale()
        self.assertEqual(kp.call_count, 2)
        kp.assert_any_call(111)
        kp.assert_any_call(222)

    def test_restart_forwards_existing_checkpoint(self):
        fd, p = tempfile.mkstemp(suffix=".blend")
        os.close(fd)
        try:
            sup = BlenderSupervisor(_config(), _Console())
            sup.client.wait_until_ready = lambda timeout=90.0: (True, "ok")
            with mock.patch("blendahbot.supervisor.find_blender_executable", return_value=r"C:\b\blender.exe"), \
                 mock.patch("blendahbot.supervisor.find_pids_on_port", return_value=[]), \
                 mock.patch("blendahbot.supervisor.launch_blender") as lb, \
                 mock.patch("blendahbot.supervisor.time.sleep"):
                sup.restart(checkpoint=Path(p))
            self.assertEqual(lb.call_args.kwargs.get("blend_file"), str(Path(p)))
        finally:
            os.unlink(p)

    def test_restart_drops_missing_checkpoint(self):
        sup = BlenderSupervisor(_config(), _Console())
        sup.client.wait_until_ready = lambda timeout=90.0: (True, "ok")
        with mock.patch("blendahbot.supervisor.find_blender_executable", return_value=r"C:\b\blender.exe"), \
             mock.patch("blendahbot.supervisor.find_pids_on_port", return_value=[]), \
             mock.patch("blendahbot.supervisor.launch_blender") as lb, \
             mock.patch("blendahbot.supervisor.time.sleep"):
            sup.restart(checkpoint=Path(r"C:\nope\does-not-exist.blend"))
        self.assertIsNone(lb.call_args.kwargs.get("blend_file"))


if __name__ == "__main__":
    unittest.main()
