#!/usr/bin/env python3
"""Independently verify the live Phase 5 traffic and RGB-camera scene."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

import carla


ROLE_NAME = "carla-poc-phase5"
CAMERA_ROLE_NAME = "carla-poc-phase5-camera"
HERO_ROLE_NAME = "hero"
WALKER_ROLE_NAME = "carla-poc-crosswalk-pedestrian"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2000, type=int)
    parser.add_argument("--status", default="/data/status.json", type=Path)
    parser.add_argument("--capture", default="/data/camera-latest.png", type=Path)
    parser.add_argument("--timeout", default=120.0, type=float)
    parser.add_argument("--expected-width", default=352, type=int)
    parser.add_argument("--expected-height", default=240, type=int)
    parser.add_argument("--expected-crosswalk-id", default=8, type=int)
    parser.add_argument("--expected-camera-x", default=-80.066, type=float)
    parser.add_argument("--expected-camera-y", default=-60.985, type=float)
    parser.add_argument("--expected-camera-z", default=9.6, type=float)
    parser.add_argument("--expected-camera-pitch", default=-27.242, type=float)
    parser.add_argument("--expected-camera-yaw", default=-8.098, type=float)
    parser.add_argument("--expected-camera-fov", default=70.0, type=float)
    parser.add_argument("--expected-pedestrians", default=8, type=int)
    return parser.parse_args()


def load_status(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        header = path.read_bytes()[:24]
        if path.stat().st_size <= 1_000 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return (int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big"))
    except FileNotFoundError:
        return None


def speed(actor: carla.Actor) -> float:
    try:
        velocity = actor.get_velocity()
        return math.sqrt(velocity.x**2 + velocity.y**2 + velocity.z**2)
    except RuntimeError:
        return 0.0


def static_transform_matches(status: dict[str, Any], args: argparse.Namespace) -> bool:
    camera = status.get("camera", {})
    transform = camera.get("transform", {})
    expected = {
        "pitch": args.expected_camera_pitch,
        "x": args.expected_camera_x,
        "y": args.expected_camera_y,
        "yaw": args.expected_camera_yaw,
        "z": args.expected_camera_z,
    }
    try:
        transform_matches = all(
            abs(float(transform[name]) - value) <= 0.02
            for name, value in expected.items()
        )
        return (
            transform_matches
            and camera.get("crosswalk_id") == args.expected_crosswalk_id
            and abs(float(camera.get("fov")) - args.expected_camera_fov) <= 0.02
        )
    except (KeyError, TypeError, ValueError):
        return False


def main() -> int:
    args = parse_args()
    client = carla.Client(args.host, args.port)
    client.set_timeout(15.0)
    world = client.get_world()
    deadline = time.time() + args.timeout
    last_observation: dict[str, Any] = {}
    previous_pedestrian_positions: dict[int, carla.Location] = {}
    previous_pedestrian_sample_at: float | None = None

    while time.time() < deadline:
        actors = world.get_actors()
        vehicles = [
            actor
            for actor in actors.filter("vehicle.*")
            if actor.attributes.get("role_name") in {ROLE_NAME, HERO_ROLE_NAME}
        ]
        cameras = [
            actor
            for actor in actors.filter("sensor.camera.rgb")
            if actor.attributes.get("role_name") == CAMERA_ROLE_NAME
        ]
        pedestrians = [
            actor
            for actor in actors.filter("walker.pedestrian.*")
            if actor.attributes.get("role_name") == WALKER_ROLE_NAME
        ]
        status = load_status(args.status)
        speeds = [speed(vehicle) for vehicle in vehicles]
        pedestrian_positions = {
            pedestrian.id: pedestrian.get_location() for pedestrian in pedestrians
        }
        sampled_at = time.monotonic()
        sample_elapsed = (
            sampled_at - previous_pedestrian_sample_at
            if previous_pedestrian_sample_at is not None
            else 0.0
        )
        pedestrian_speeds = [
            math.hypot(
                position.x - previous_pedestrian_positions[actor_id].x,
                position.y - previous_pedestrian_positions[actor_id].y,
            )
            / sample_elapsed
            for actor_id, position in pedestrian_positions.items()
            if sample_elapsed > 0 and actor_id in previous_pedestrian_positions
        ]
        previous_pedestrian_positions = pedestrian_positions
        previous_pedestrian_sample_at = sampled_at
        moving_count = sum(value > 0.5 for value in speeds)
        measured_moving_pedestrian_count = sum(
            value > 0.05 for value in pedestrian_speeds
        )
        reported_moving_pedestrian_count = int(
            status.get("moving_pedestrian_count", 0)
        )
        moving_pedestrian_count = max(
            measured_moving_pedestrian_count,
            reported_moving_pedestrian_count,
        )
        pedestrians_in_crosswalk_area = sum(
            -66.0 <= pedestrian.get_location().x <= -59.0
            and -77.0 <= pedestrian.get_location().y <= -50.0
            for pedestrian in pedestrians
        )
        status_age = time.time() - status.get("updated_at_unix", 0)
        camera_frames = status.get("camera", {}).get("frame_count", 0)
        camera_fps = status.get("camera", {}).get("fps", 0.0)
        dimensions = png_dimensions(args.capture)
        vehicle_ids = {vehicle.id for vehicle in vehicles}
        camera_parent_id = (
            cameras[0].parent.id
            if len(cameras) == 1 and cameras[0].parent is not None
            else None
        )
        camera_attached = (
            camera_parent_id in vehicle_ids
            and status.get("camera", {}).get("attached_vehicle_id")
            == camera_parent_id
        )
        camera_mode = status.get("camera", {}).get("mode")
        static_transform_valid = static_transform_matches(status, args)
        camera_static = (
            len(cameras) == 1
            and camera_parent_id is None
            and camera_mode == "static"
            and static_transform_valid
        )
        camera_mount_valid = camera_attached or camera_static
        camera_vehicle_speed = next(
            (
                value
                for vehicle, value in zip(vehicles, speeds)
                if vehicle.id == camera_parent_id
            ),
            0.0,
        )

        last_observation = {
            "camera_attached_to_managed_vehicle": camera_attached,
            "camera_actor_count": len(cameras),
            "camera_capture_bytes": args.capture.stat().st_size
            if args.capture.exists()
            else 0,
            "camera_dimensions": dimensions,
            "camera_frame_count": camera_frames,
            "camera_fps": camera_fps,
            "camera_parent_id": camera_parent_id,
            "camera_static_transform": status.get("camera", {}).get("transform"),
            "camera_static_transform_valid": static_transform_valid,
            "camera_mode": camera_mode,
            "camera_mount_valid": camera_mount_valid,
            "camera_vehicle_speed_mps": round(camera_vehicle_speed, 2),
            "map": world.get_map().name,
            "max_vehicle_speed_mps": round(max(speeds, default=0.0), 2),
            "moving_vehicle_count": moving_count,
            "moving_pedestrian_count": moving_pedestrian_count,
            "measured_moving_pedestrian_count": measured_moving_pedestrian_count,
            "max_pedestrian_speed_mps": round(
                max(pedestrian_speeds, default=0.0), 2
            ),
            "pedestrian_count": len(pedestrians),
            "pedestrians_in_crosswalk_area": pedestrians_in_crosswalk_area,
            "status_age_seconds": round(status_age, 2),
            "vehicle_count": len(vehicles),
        }

        if (
            len(vehicles) == 20
            and len(cameras) == 1
            and camera_mount_valid
            and len(pedestrians) == args.expected_pedestrians
            and moving_pedestrian_count > 0
            and pedestrians_in_crosswalk_area == args.expected_pedestrians
            and (
                (camera_mode == "vehicle" and camera_vehicle_speed > 0.5)
                or (camera_mode == "static" and moving_count > 0)
            )
            and camera_frames >= 10
            and status_age < 10
            and dimensions == (args.expected_width, args.expected_height)
        ):
            print("CARLA Phase 5 scene verification: PASS")
            print(json.dumps(last_observation, indent=2, sort_keys=True))
            return 0
        time.sleep(2)

    print("CARLA Phase 5 scene verification: FAIL")
    print(json.dumps(last_observation, indent=2, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
