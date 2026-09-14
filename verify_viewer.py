#!/usr/bin/env python3
"""Verify the browser page, health endpoint, and a complete MJPEG frame."""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--phase", default="6")
    parser.add_argument("--expected-width", default=352, type=int)
    parser.add_argument("--expected-height", default=240, type=int)
    parser.add_argument("--minimum-fps", default=12.0, type=float)
    parser.add_argument("--sample-seconds", default=3.0, type=float)
    parser.add_argument(
        "--expected-camera-mode",
        choices=("static", "vehicle"),
        default="static",
    )
    parser.add_argument("--expected-crosswalk-id", default=14, type=int)
    parser.add_argument("--expected-pedestrians", default=16, type=int)
    parser.add_argument("--expected-pedestrian-mode", default="ai")
    parser.add_argument(
        "--output", default="/data/viewer-verified-frame.jpg", type=Path
    )
    return parser.parse_args()


def get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=15) as response:
        return response.read()


def get_json(url: str) -> dict[str, Any]:
    return json.loads(get_bytes(url))


def minimum_frames_for_sample(minimum_fps: float, sample_seconds: float) -> int:
    return math.ceil(minimum_fps * sample_seconds)


def read_mjpeg_frame(url: str) -> tuple[bytes, str]:
    with urllib.request.urlopen(url, timeout=20) as response:
        content_type = response.headers.get("Content-Type", "")
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = response.read(4096)
            if not chunk:
                raise RuntimeError("MJPEG stream ended before frame headers")
            buffer += chunk

        raw_headers, frame_data = buffer.split(b"\r\n\r\n", 1)
        headers: dict[str, str] = {}
        for line in raw_headers.split(b"\r\n")[1:]:
            name, value = line.decode("ascii").split(":", 1)
            headers[name.lower()] = value.strip()

        frame_size = int(headers["content-length"])
        while len(frame_data) < frame_size:
            chunk = response.read(frame_size - len(frame_data))
            if not chunk:
                raise RuntimeError("MJPEG stream ended during a JPEG frame")
            frame_data += chunk
        return frame_data[:frame_size], content_type


def inspect_jpeg(jpeg: bytes) -> tuple[str, tuple[int, int]]:
    """Return format and dimensions using only the Python standard library."""
    if not jpeg.startswith(b"\xff\xd8"):
        raise RuntimeError("Frame does not start with a JPEG SOI marker")

    offset = 2
    start_of_frame_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset < len(jpeg):
        if jpeg[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(jpeg) and jpeg[offset] == 0xFF:
            offset += 1
        if offset >= len(jpeg):
            break

        marker = jpeg[offset]
        offset += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(jpeg):
            break
        segment_length = int.from_bytes(jpeg[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(jpeg):
            raise RuntimeError("JPEG contains an invalid marker segment")
        if marker in start_of_frame_markers:
            if segment_length < 7:
                raise RuntimeError("JPEG start-of-frame segment is too short")
            height = int.from_bytes(jpeg[offset + 3 : offset + 5], "big")
            width = int.from_bytes(jpeg[offset + 5 : offset + 7], "big")
            return "JPEG", (width, height)
        offset += segment_length

    raise RuntimeError("JPEG dimensions were not found")


def main() -> int:
    args = parse_args()
    page = get_bytes(f"{args.base_url}/").decode("utf-8")
    if "<title>CARLA on GCP</title>" not in page or 'src="/stream.mjpg"' not in page:
        raise RuntimeError("Viewer page does not contain the expected live stream")

    health_before = get_json(f"{args.base_url}/healthz")
    sample_started = time.monotonic()
    time.sleep(args.sample_seconds)
    health_after = get_json(f"{args.base_url}/healthz")
    sample_elapsed = time.monotonic() - sample_started
    frame_count_delta = (
        health_after["camera_frame_count"] - health_before["camera_frame_count"]
    )
    if frame_count_delta <= 0:
        raise RuntimeError("Camera frame count did not advance")
    observed_fps = frame_count_delta / sample_elapsed
    minimum_frame_count = minimum_frames_for_sample(
        args.minimum_fps, args.sample_seconds
    )
    if frame_count_delta < minimum_frame_count:
        raise RuntimeError(
            f"Camera advanced {frame_count_delta} frames in the "
            f"{args.sample_seconds:.2f}-second sample; {minimum_frame_count} "
            f"frames are required ({observed_fps:.2f} measured fps)"
        )
    if health_after.get("camera_mode") != args.expected_camera_mode:
        raise RuntimeError(
            f"Unexpected camera mode: {health_after.get('camera_mode')}"
        )
    if (
        args.expected_camera_mode == "static"
        and health_after.get("crosswalk_id") != args.expected_crosswalk_id
    ):
        raise RuntimeError(
            f"Unexpected crosswalk: {health_after.get('crosswalk_id')}"
        )
    if health_after.get("pedestrian_count") != args.expected_pedestrians:
        raise RuntimeError(
            f"Unexpected pedestrian count: {health_after.get('pedestrian_count')}"
        )
    if health_after.get("pedestrian_mode") != args.expected_pedestrian_mode:
        raise RuntimeError(
            f"Unexpected pedestrian mode: {health_after.get('pedestrian_mode')}"
        )
    if not health_after.get("moving_pedestrian_count", 0):
        raise RuntimeError("No managed pedestrians are moving")

    jpeg, content_type = read_mjpeg_frame(f"{args.base_url}/stream.mjpg")
    image_format, dimensions = inspect_jpeg(jpeg)

    if "multipart/x-mixed-replace" not in content_type:
        raise RuntimeError(f"Unexpected stream content type: {content_type}")
    expected_dimensions = (args.expected_width, args.expected_height)
    if image_format != "JPEG" or dimensions != expected_dimensions:
        raise RuntimeError(
            f"Unexpected frame: format={image_format}, dimensions={dimensions}"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(jpeg)
    result = {
        "camera_dropped_frame_count": health_after.get(
            "camera_dropped_frame_count"
        ),
        "camera_measured_fps": health_after.get("camera_fps"),
        "camera_mode": health_after.get("camera_mode"),
        "camera_frame_count_after": health_after["camera_frame_count"],
        "camera_frame_count_before": health_before["camera_frame_count"],
        "camera_frame_count_delta": frame_count_delta,
        "camera_observed_fps": round(observed_fps, 2),
        "camera_target_fps": health_after.get("camera_target_fps"),
        "crosswalk_id": health_after.get("crosswalk_id"),
        "jpeg_bytes": len(jpeg),
        "jpeg_dimensions": dimensions,
        "map": health_after.get("map"),
        "mjpeg_content_type": content_type,
        "page": "CARLA on GCP",
        "pedestrian_count": health_after.get("pedestrian_count"),
        "pedestrian_mode": health_after.get("pedestrian_mode"),
        "moving_pedestrian_count": health_after.get(
            "moving_pedestrian_count"
        ),
        "vehicle_count": health_after.get("vehicle_count"),
    }
    print(f"CARLA Phase {args.phase} browser viewer verification: PASS")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
