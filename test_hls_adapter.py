#!/usr/bin/env python3
"""Unit tests for the HLS process supervisor."""

from __future__ import annotations

import os
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from hls_adapter import HLSAdapterSupervisor, HLSConfig


class HLSAdapterSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.fake_ffmpeg = self.root / "fake-ffmpeg"
        self.fake_ffmpeg.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import signal
                import sys
                import time
                from pathlib import Path

                running = True
                signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__('running', False))
                playlist = Path(sys.argv[-1])
                template = sys.argv[sys.argv.index('-hls_segment_filename') + 1]
                segment = Path(template.replace('%06d', '000000'))
                segment.write_bytes(b'fake transport stream')
                playlist.write_text('#EXTM3U\\n#EXT-X-VERSION:6\\n#EXTINF:2.0,\\n' + segment.name + '\\n')
                while running:
                    time.sleep(0.05)
                """
            )
        )
        self.fake_ffmpeg.chmod(0o755)
        self.output_dir = self.root / "hls"
        self.supervisor = HLSAdapterSupervisor(
            HLSConfig(
                input_url="http://127.0.0.1:8080/stream.mjpg",
                output_dir=self.output_dir,
                restart_delay=0.2,
                stale_after=10,
                ffmpeg_binary=str(self.fake_ffmpeg),
            )
        )

    def tearDown(self) -> None:
        self.supervisor.stop()
        self.temporary_directory.cleanup()

    def wait_for(self, predicate, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail("condition was not satisfied before timeout")

    def test_supervises_and_restarts_encoder(self) -> None:
        command = self.supervisor._command()
        self.assertEqual(command[command.index("-preset") + 1], "ultrafast")
        self.assertEqual(command[command.index("-threads") + 1], "1")
        self.supervisor.start()
        initial = self.wait_for(
            lambda: (health := self.supervisor.health())["hls_ready"] and health
        )
        self.assertEqual(initial["hls_process_state"], "ready")
        self.assertEqual(self.supervisor.playlist_segments(), ["segment-000000.ts"])
        self.assertIsNotNone(self.supervisor.segment_path("segment-000000.ts"))
        self.assertIsNone(self.supervisor.segment_path("../status.json"))

        os.kill(initial["hls_process_pid"], 15)
        self.wait_for(
            lambda: self.supervisor.health()["hls_process_state"] == "backoff"
        )
        recovered = self.wait_for(
            lambda: (
                (health := self.supervisor.health())["hls_ready"]
                and health["hls_process_pid"] != initial["hls_process_pid"]
                and health
            )
        )
        self.assertEqual(recovered["hls_restart_count"], 1)
        self.assertEqual(recovered["hls_last_exit_code"], 0)

    def test_missing_executable_is_reported_and_retried(self) -> None:
        missing = HLSAdapterSupervisor(
            HLSConfig(
                input_url="http://127.0.0.1:8080/stream.mjpg",
                output_dir=self.root / "missing-hls",
                restart_delay=0.2,
                ffmpeg_binary=str(self.root / "does-not-exist"),
            )
        )
        try:
            missing.start()
            health = self.wait_for(
                lambda: (
                    (current := missing.health())["hls_last_error"] and current
                )
            )
            self.assertFalse(health["hls_ready"])
            self.assertEqual(health["hls_process_state"], "backoff")
            self.wait_for(lambda: missing.health()["hls_restart_count"] >= 1)
        finally:
            missing.stop()


if __name__ == "__main__":
    unittest.main()
