#!/usr/bin/env python3
"""Verify rolling HLS output and optional encoder-only recovery."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from verify_viewer import inspect_jpeg


SEGMENT_PATTERN = re.compile(r"segment-\d+\.ts")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--expected-width", default=352, type=int)
    parser.add_argument("--expected-height", default=240, type=int)
    parser.add_argument("--advance-timeout", default=8.0, type=float)
    parser.add_argument("--recovery-timeout", default=20.0, type=float)
    parser.add_argument("--exercise-restart", action="store_true")
    return parser.parse_args()


def get_bytes(url: str, timeout: float = 15.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def get_json(url: str) -> dict[str, Any]:
    return json.loads(get_bytes(url))


def playlist_segments(playlist: bytes) -> list[str]:
    text = playlist.decode("utf-8")
    if "#EXTM3U" not in text or "#EXT-X-ENDLIST" in text:
        raise RuntimeError("HLS playlist is invalid or not live")
    segments = [line.strip() for line in text.splitlines() if line.endswith(".ts")]
    if not segments or any(not SEGMENT_PATTERN.fullmatch(name) for name in segments):
        raise RuntimeError(f"Unexpected HLS segment names: {segments}")
    return segments


def inspect_segment(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError(f"No decodable video stream in {path.name}")
    return int(streams[0]["width"]), int(streams[0]["height"])


def wait_for_playlist_change(base_url: str, initial: bytes, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = get_bytes(f"{base_url}/live/playlist.m3u8")
        except urllib.error.URLError:
            time.sleep(0.25)
            continue
        if current != initial:
            return current
        time.sleep(0.25)
    raise RuntimeError("HLS playlist did not advance")


def wait_for_health(base_url: str, predicate, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_health: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last_health = get_json(f"{base_url}/healthz")
        except urllib.error.URLError:
            time.sleep(0.1)
            continue
        if predicate(last_health):
            return last_health
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for HLS health state; last={last_health}")


def main() -> int:
    args = parse_args()
    expected_dimensions = (args.expected_width, args.expected_height)
    health_before = wait_for_health(
        args.base_url, lambda health: health.get("hls_ready") is True, 20.0
    )
    playlist_before = get_bytes(f"{args.base_url}/live/playlist.m3u8")
    playlist_after = wait_for_playlist_change(
        args.base_url, playlist_before, args.advance_timeout
    )
    segments = playlist_segments(playlist_after)

    decoded: dict[str, tuple[int, int]] = {}
    with tempfile.TemporaryDirectory(prefix="carla-hls-verify-") as temp_dir:
        output_dir = Path(temp_dir)
        for segment in segments:
            segment_bytes = get_bytes(f"{args.base_url}/live/{segment}")
            if not segment_bytes:
                raise RuntimeError(f"HLS segment is empty: {segment}")
            segment_path = output_dir / segment
            segment_path.write_bytes(segment_bytes)
            dimensions = inspect_segment(segment_path)
            if dimensions != expected_dimensions:
                raise RuntimeError(
                    f"Unexpected dimensions for {segment}: {dimensions}"
                )
            decoded[segment] = dimensions

    snapshot = get_bytes(f"{args.base_url}/snapshot.jpg")
    snapshot_format, snapshot_dimensions = inspect_jpeg(snapshot)
    if snapshot_format != "JPEG" or snapshot_dimensions != expected_dimensions:
        raise RuntimeError(
            f"Unexpected snapshot: {snapshot_format} {snapshot_dimensions}"
        )

    maximum_disk_segments = (
        int(health_before["hls_list_size"])
        + int(health_before["hls_delete_threshold"])
    )
    if int(health_before["hls_disk_segment_count"]) > maximum_disk_segments:
        raise RuntimeError("Rolling HLS directory contains too many segments")

    recovery: dict[str, Any] | None = None
    if args.exercise_restart:
        original_pid = int(health_before["hls_process_pid"])
        original_restart_count = int(health_before["hls_restart_count"])
        camera_frame_before = int(health_before["camera_frame_count"])
        os.kill(original_pid, signal.SIGTERM)
        failure = wait_for_health(
            args.base_url,
            lambda health: (
                health.get("hls_ready") is False
                and health.get("hls_process_state") in {"backoff", "starting"}
            ),
            5.0,
        )
        recovered = wait_for_health(
            args.base_url,
            lambda health: (
                health.get("hls_ready") is True
                and health.get("hls_process_pid") != original_pid
                and int(health.get("hls_restart_count", 0)) > original_restart_count
            ),
            args.recovery_timeout,
        )
        if int(recovered["camera_frame_count"]) <= camera_frame_before:
            raise RuntimeError("CARLA camera did not advance during HLS recovery")
        recovered_playlist = get_bytes(f"{args.base_url}/live/playlist.m3u8")
        wait_for_playlist_change(
            args.base_url, recovered_playlist, args.advance_timeout
        )
        recovery = {
            "failure_state": failure["hls_process_state"],
            "new_pid": recovered["hls_process_pid"],
            "old_pid": original_pid,
            "restart_count": recovered["hls_restart_count"],
        }

    result = {
        "decoded_segments": decoded,
        "disk_segment_count": health_before["hls_disk_segment_count"],
        "hls_ready": health_before["hls_ready"],
        "playlist_advanced": True,
        "playlist_segment_count": len(segments),
        "recovery": recovery,
        "snapshot_dimensions": snapshot_dimensions,
    }
    print("CARLA Phase 1 HLS verification: PASS")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
