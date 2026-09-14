#!/usr/bin/env python3
"""Supervise FFmpeg while it converts the local MJPEG feed to rolling HLS."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SEGMENT_NAME_PATTERN = re.compile(r"segment-(\d+)\.ts")


@dataclass(frozen=True)
class HLSConfig:
    input_url: str
    output_dir: Path
    width: int = 352
    height: int = 240
    fps: float = 20.0
    segment_seconds: float = 2.0
    list_size: int = 4
    delete_threshold: int = 1
    restart_delay: float = 1.0
    stale_after: float = 6.0
    ffmpeg_binary: str = "ffmpeg"

    def __post_init__(self) -> None:
        numeric_values = {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "segment_seconds": self.segment_seconds,
            "list_size": self.list_size,
            "delete_threshold": self.delete_threshold,
            "restart_delay": self.restart_delay,
            "stale_after": self.stale_after,
        }
        invalid = [name for name, value in numeric_values.items() if value <= 0]
        if invalid:
            raise ValueError(f"HLS settings must be positive: {', '.join(invalid)}")


class HLSAdapterSupervisor:
    """Keep one FFmpeg HLS encoder alive and report its observable state."""

    def __init__(self, config: HLSConfig) -> None:
        self.config = config
        self.playlist_path = config.output_dir / "playlist.m3u8"
        self.log_path = config.output_dir / "ffmpeg.log"
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_state = "stopped"
        self._restart_count = 0
        self._last_exit_code: int | None = None
        self._last_error: str | None = None
        self._started_once = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            self._remove_hls_artifacts()
            self._stop_event.clear()
            self._process_state = "starting"
            self._thread = threading.Thread(
                target=self._supervise,
                name="carla-hls-supervisor",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
        with self._lock:
            self._process = None
            self._process_state = "stopped"

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def playlist_segments(self) -> list[str]:
        try:
            playlist = self.playlist_path.read_text()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return []
        return [
            line.strip()
            for line in playlist.splitlines()
            if SEGMENT_NAME_PATTERN.fullmatch(line.strip())
        ]

    def segment_path(self, name: str) -> Path | None:
        if not SEGMENT_NAME_PATTERN.fullmatch(name):
            return None
        path = self.config.output_dir / name
        return path if path.is_file() else None

    def health(self) -> dict[str, Any]:
        now = time.time()
        playlist_segments = self.playlist_segments()
        segment_files: list[tuple[Path, float]] = []
        for path in self.config.output_dir.glob("segment-*.ts"):
            try:
                segment_files.append((path, path.stat().st_mtime))
            except FileNotFoundError:
                # FFmpeg may rotate a segment between glob() and stat().
                continue
        latest_file = max(segment_files, key=lambda item: item[1], default=None)
        latest_path = latest_file[0] if latest_file else None
        latest_age = max(0.0, now - latest_file[1]) if latest_file else None
        listed_segments_exist = bool(playlist_segments) and all(
            (self.config.output_dir / name).is_file() for name in playlist_segments
        )
        with self._lock:
            process = self._process
            process_alive = process is not None and process.poll() is None
            process_pid = process.pid if process_alive else None
            process_state = self._process_state
            restart_count = self._restart_count
            last_exit_code = self._last_exit_code
            last_error = self._last_error
        ready = bool(
            process_alive
            and self.playlist_path.is_file()
            and listed_segments_exist
            and latest_age is not None
            and latest_age <= self.config.stale_after
        )
        return {
            "hls_ready": ready,
            "hls_process_state": "ready" if ready else process_state,
            "hls_process_alive": process_alive,
            "hls_process_pid": process_pid,
            "hls_restart_count": restart_count,
            "hls_last_exit_code": last_exit_code,
            "hls_last_error": last_error,
            "hls_latest_segment": latest_path.name if latest_path else None,
            "hls_latest_segment_age_seconds": (
                round(latest_age, 3) if latest_age is not None else None
            ),
            "hls_playlist_segment_count": len(playlist_segments),
            "hls_disk_segment_count": len(segment_files),
            "hls_list_size": self.config.list_size,
            "hls_delete_threshold": self.config.delete_threshold,
            "hls_segment_seconds": self.config.segment_seconds,
        }

    def _remove_hls_artifacts(self) -> None:
        self.playlist_path.unlink(missing_ok=True)
        for pattern in ("segment-*.ts", "*.tmp"):
            for path in self.config.output_dir.glob(pattern):
                path.unlink(missing_ok=True)

    def _command(self) -> list[str]:
        keyframe_interval = max(1, round(self.config.fps * self.config.segment_seconds))
        segment_template = self.config.output_dir / "segment-%06d.ts"
        return [
            self.config.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "2",
            "-i",
            self.config.input_url,
            "-an",
            "-vf",
            f"scale={self.config.width}:{self.config.height}:flags=fast_bilinear",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-threads",
            "1",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            "-r",
            f"{self.config.fps:g}",
            "-g",
            str(keyframe_interval),
            "-keyint_min",
            str(keyframe_interval),
            "-sc_threshold",
            "0",
            "-f",
            "hls",
            "-hls_time",
            f"{self.config.segment_seconds:g}",
            "-hls_list_size",
            str(self.config.list_size),
            "-hls_delete_threshold",
            str(self.config.delete_threshold),
            "-hls_flags",
            "delete_segments+independent_segments+omit_endlist+temp_file",
            "-hls_segment_filename",
            str(segment_template),
            str(self.playlist_path),
        ]

    def _supervise(self) -> None:
        while not self._stop_event.is_set():
            if self._started_once:
                self._remove_hls_artifacts()
                with self._lock:
                    self._restart_count += 1
            self._started_once = True
            try:
                # Retain diagnostics for the current attempt without allowing a
                # retry loop to grow the log indefinitely.
                with self.log_path.open("wb") as log_file:
                    process = subprocess.Popen(
                        self._command(),
                        stdout=subprocess.DEVNULL,
                        stderr=log_file,
                    )
                    with self._lock:
                        self._process = process
                        self._process_state = "running"
                        self._last_error = None
                    while not self._stop_event.wait(0.2):
                        exit_code = process.poll()
                        if exit_code is not None:
                            with self._lock:
                                self._last_exit_code = exit_code
                                self._process_state = "backoff"
                            break
                    else:
                        continue
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._process_state = "backoff"
            finally:
                with self._lock:
                    process = self._process
                    if process is not None and process.poll() is not None:
                        self._last_exit_code = process.returncode
                        self._process = None

            if not self._stop_event.wait(self.config.restart_delay):
                with self._lock:
                    self._process_state = "starting"
