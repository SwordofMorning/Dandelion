##
 # @file test/test_remote_cli_lifecycle.py
 # @date 2026/11/03
 #
 # @brief Unit and integration tests for `python main.py remote ...` lifecycle CLI.
 #

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import main as root_main
from src.remote import cli as remote_cli


class TestRemoteCliLifecycle(unittest.TestCase):
    ##
     # @brief Set up an isolated temporary directory and DR_* environment variables.
     #
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.root_dir = self.tmp_path / "root"
        self.data_dir = self.root_dir / ".data" / "remote"
        self.ws_dir = self.root_dir / ".data" / "workspace"
        self.cfg_file = self.root_dir / ".env" / "remote.cfg"
        self.pid_file = self.data_dir / "remote.pid"
        self.log_file = self.data_dir / "remote.log"

        (self.root_dir / "llm" / "skill").mkdir(parents=True, exist_ok=True)
        (self.root_dir / "llm" / "skill" / "sample_skill.md").write_text("# Skill\n", encoding="utf-8")
        (self.root_dir / ".env").mkdir(parents=True, exist_ok=True)
        (self.root_dir / ".env" / "remote.cfg.example").write_text(
            "[Server]\nHOST=127.0.0.1\nPORT=8765\nACCESS_KEY=dr-replace-with-your-shared-secret-key\n",
            encoding="utf-8",
        )

        self._env_patch = patch.dict(
            os.environ,
            {
                "DR_ROOT_DIR": str(self.root_dir),
                "DR_DATA_DIR": str(self.data_dir),
                "DR_WORKSPACE_DIR": str(self.ws_dir),
                "DR_CONFIG_FILE": str(self.cfg_file),
            },
            clear=False,
        )
        self._env_patch.start()
    # End-def

    ##
     # @brief Clean up any leftover process and temporary directory.
     #
    def tearDown(self):
        if self.pid_file.is_file():
            try:
                leftover = int(self.pid_file.read_text(encoding="utf-8").strip())
                os.kill(leftover, signal.SIGKILL)
            except Exception:
                pass
            # End-try
        # End-if
        self._env_patch.stop()
        self._tmp.cleanup()
    # End-def

    ##
     # @brief Capture stdout from calling fn(*args, **kwargs).
     #
    def _capture(self, fn, *args, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = fn(*args, **kwargs)
        # End-with
        return result, buf.getvalue()
    # End-def

    ##
     # @brief Verify `python main.py remote status` works without cli.cfg and reports STOPPED.
     #
    def test_main_remote_status_stopped_without_cli_cfg(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                root_main.main(["remote", "status"])
            # End-with
        # End-with

        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("STOPPED", buf.getvalue())
    # End-def

    ##
     # @brief Verify `remote init` seeds built-in skills and generates a random ACCESS_KEY.
     #
    def test_remote_init_creates_config_and_seeds_skills(self):
        rc, out = self._capture(remote_cli.main, ["init"])
        self.assertEqual(rc, 0)
        self.assertIn("Generated initial remote config", out)
        self.assertTrue((self.ws_dir / "llm" / "skill" / "sample_skill.md").is_file())
        self.assertTrue(self.cfg_file.is_file())

        cfg_text = self.cfg_file.read_text(encoding="utf-8")
        match = re.search(r"^ACCESS_KEY\s*=\s*(dr-[0-9a-f]{32})$", cfg_text, re.MULTILINE)
        self.assertIsNotNone(match)
        generated_key = match.group(1)
        self.assertIn(generated_key, out)

        # Second init preserves existing config
        rc2, out2 = self._capture(remote_cli.main, ["init"])
        self.assertEqual(rc2, 0)
        self.assertIn("Remote config already exists", out2)
        self.assertEqual(self.cfg_file.read_text(encoding="utf-8"), cfg_text)
    # End-def

    ##
     # @brief Verify `start`, `status`, `restart`, and `stop` lifecycle with a real background process.
     #
    def test_remote_start_status_restart_stop_lifecycle(self):
        real_popen = subprocess.Popen

        def _mock_daemon_popen(cmd, **kwargs):
            self.assertIs(kwargs.get("start_new_session"), True)
            dummy_code = (
                "import signal, sys, time\n"
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
                "print('daemon-ready', flush=True)\n"
                "time.sleep(60)\n"
            )
            return real_popen([sys.executable, "-c", dummy_code], **kwargs)
        # End-def

        with (
            patch.object(remote_cli, "STARTUP_WAIT_SEC", 0.2),
            patch.object(remote_cli, "STOP_POLL_SEC", 0.05),
            patch.object(remote_cli.subprocess, "Popen", side_effect=_mock_daemon_popen),
        ):
            # 1. Start daemon
            rc, out = self._capture(remote_cli.main, ["start"])
            self.assertEqual(rc, 0)
            self.assertIn("Dandelion Remote started", out)
            self.assertTrue(self.pid_file.is_file())
            pid1 = int(self.pid_file.read_text(encoding="utf-8").strip())
            self.assertTrue(remote_cli._is_pid_alive(pid1))

            # Starting again while running is a no-op returning 0
            rc_dup, out_dup = self._capture(remote_cli.main, ["start"])
            self.assertEqual(rc_dup, 0)
            self.assertIn("already running", out_dup)

            # 2. Check status
            rc_status, status_out = self._capture(remote_cli.main, ["status"])
            self.assertEqual(rc_status, 0)
            self.assertIn(f"RUNNING (PID: {pid1})", status_out)

            # 3. Restart daemon
            rc_restart, _ = self._capture(remote_cli.main, ["restart"])
            self.assertEqual(rc_restart, 0)
            pid2 = int(self.pid_file.read_text(encoding="utf-8").strip())
            self.assertNotEqual(pid2, pid1)
            self.assertFalse(remote_cli._is_pid_alive(pid1))
            self.assertTrue(remote_cli._is_pid_alive(pid2))

            # 4. Stop daemon
            rc_stop, stop_out = self._capture(remote_cli.main, ["stop"])
            self.assertEqual(rc_stop, 0)
            self.assertIn("Dandelion Remote stopped.", stop_out)
            self.assertFalse(self.pid_file.exists())
            self.assertFalse(remote_cli._is_pid_alive(pid2))
        # End-with
    # End-def

    ##
     # @brief Verify `start` returns 1 and prints log tail when child exits early.
     #
    def test_remote_start_failure_prints_log_tail(self):
        real_popen = subprocess.Popen

        def _failing_popen(cmd, **kwargs):
            fail_code = "import sys; print('FATAL: bad config', flush=True); sys.exit(2)"
            return real_popen([sys.executable, "-c", fail_code], **kwargs)
        # End-def

        with (
            patch.object(remote_cli, "STARTUP_WAIT_SEC", 0.2),
            patch.object(remote_cli.subprocess, "Popen", side_effect=_failing_popen),
        ):
            rc, out = self._capture(remote_cli.main, ["start"])
            self.assertEqual(rc, 1)
            self.assertIn("failed to start", out)
            self.assertIn("FATAL: bad config", out)
            self.assertFalse(self.pid_file.exists())
        # End-with
    # End-def

    ##
     # @brief Verify `stop` cleans up a stale PID file when process is not running.
     #
    def test_remote_stop_removes_stale_pid_file(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text("999999\n", encoding="utf-8")

        rc, out = self._capture(remote_cli.main, ["stop"])
        self.assertEqual(rc, 0)
        self.assertIn("not running", out)
        self.assertFalse(self.pid_file.exists())
    # End-def

    ##
     # @brief Verify `logs`, `fg`, `serve`, and unknown command handling.
     #
    def test_remote_logs_fg_serve_and_usage(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.write_text("line-1\nline-2\n", encoding="utf-8")

        rc_logs, out_logs = self._capture(remote_cli.cmd_logs, follow=False)
        self.assertEqual(rc_logs, 0)
        self.assertIn("line-1\nline-2\n", out_logs)

        called = []
        with patch("src.remote.service.main", side_effect=lambda argv=None: called.append(list(argv or []))):
            orig_cwd = os.getcwd()
            try:
                rc_fg, _ = self._capture(remote_cli.main, ["fg", "--port", "9000"])
                self.assertEqual(rc_fg, 0)
            finally:
                os.chdir(orig_cwd)
            # End-try
            self.assertEqual(called[-1], ["serve", "--config", str(self.cfg_file), "--port", "9000"])

            rc_serve, _ = self._capture(remote_cli.main, ["serve", "--config", "custom.cfg"])
            self.assertEqual(rc_serve, 0)
            self.assertEqual(called[-1], ["serve", "--config", "custom.cfg"])
        # End-with

        rc_unk, out_unk = self._capture(remote_cli.main, ["unknown"])
        self.assertEqual(rc_unk, 1)
        self.assertIn("Usage: python main.py remote", out_unk)
    # End-def
# End-class


if __name__ == "__main__":
    unittest.main()
# End-if
