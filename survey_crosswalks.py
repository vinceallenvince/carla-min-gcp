#!/usr/bin/env python3
"""Render an elevated traffic-camera survey of every crosswalk in the CARLA map."""

from __future__ import annotations

import argparse
import io
import json
import math
import threading
from pathlib import Path
from typing import Any

import carla
from PIL import Image as PILImage
from PIL import ImageDraw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2000, type=int)
    parser.add_argument("--output-dir", default="/data/crosswalk-survey", type=Path)
    parser.add_argument("--width", default=704, type=int)
    parser.add_argument("--height", default=480, type=int)
    return parser.parse_args()


def split_crosswalks(points: list[carla.Location]) -> list[list[carla.Location]]:
    polygons: list[list[carla.Location]] = []
    current: list[carla.Location] = []
    for point in points:
        if not current:
            current = [point]
            continue
        current.append(point)
        if len(current) >= 5 and point.distance(current[0]) < 0.1:
            polygons.append(current[:-1])
            current = []
    if current:
        raise RuntimeError("Crosswalk geometry ended with an incomplete polygon")
    return polygons


def crosswalk_geometry(
    crosswalk_id: int, polygon: list[carla.Location]
) -> dict[str, Any]:
    center_x = sum(point.x for point in polygon) / len(polygon)
    center_y = sum(point.y for point in polygon) / len(polygon)
    edges = [
        (
            math.hypot(
                polygon[(index + 1) % len(polygon)].x - point.x,
                polygon[(index + 1) % len(polygon)].y - point.y,
            ),
            point,
            polygon[(index + 1) % len(polygon)],
        )
        for index, point in enumerate(polygon)
    ]
    length, start, end = max(edges, key=lambda edge: edge[0])
    width = min(edge[0] for edge in edges)
    long_x = (end.x - start.x) / length
    long_y = (end.y - start.y) / length
    side_x, side_y = -long_y, long_x

    # A high corner-mounted camera looking diagonally along the crosswalk.
    camera_x = center_x - long_x * 18.0 + side_x * 10.0
    camera_y = center_y - long_y * 18.0 + side_y * 10.0
    camera_z = 18.0
    target_z = 0.8
    delta_x = center_x - camera_x
    delta_y = center_y - camera_y
    horizontal_distance = math.hypot(delta_x, delta_y)
    yaw = math.degrees(math.atan2(delta_y, delta_x))
    pitch = math.degrees(math.atan2(target_z - camera_z, horizontal_distance))

    return {
        "camera": {
            "pitch": round(pitch, 3),
            "roll": 0.0,
            "x": round(camera_x, 3),
            "y": round(camera_y, 3),
            "yaw": round(yaw, 3),
            "z": camera_z,
        },
        "center": {"x": round(center_x, 3), "y": round(center_y, 3), "z": 0.0},
        "id": crosswalk_id,
        "length_m": round(length, 2),
        "polygon": [
            {"x": round(point.x, 3), "y": round(point.y, 3), "z": round(point.z, 3)}
            for point in polygon
        ],
        "width_m": round(width, 2),
    }


def capture_camera(
    world: carla.World,
    blueprint: carla.ActorBlueprint,
    transform: carla.Transform,
    width: int,
    height: int,
    timeout: float = 20.0,
) -> PILImage.Image:
    captured: list[bytes] = []
    ready = threading.Event()

    def receive(image: carla.Image) -> None:
        if captured:
            return
        captured.append(bytes(image.raw_data))
        ready.set()

    camera = world.spawn_actor(blueprint, transform)
    try:
        camera.listen(receive)
        if not ready.wait(timeout):
            raise RuntimeError(f"Camera at {transform} produced no frame")
    finally:
        camera.stop()
        camera.destroy()

    return PILImage.frombytes(
        "RGBA",
        (width, height),
        captured[0],
        "raw",
        "BGRA",
    ).convert("RGB")


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    polygons = split_crosswalks(list(world.get_map().get_crosswalks()))
    candidates = [
        crosswalk_geometry(crosswalk_id, polygon)
        for crosswalk_id, polygon in enumerate(polygons)
    ]

    blueprint = world.get_blueprint_library().find("sensor.camera.rgb")
    blueprint.set_attribute("image_size_x", str(args.width))
    blueprint.set_attribute("image_size_y", str(args.height))
    blueprint.set_attribute("fov", "90")
    blueprint.set_attribute("sensor_tick", "0.0")

    contact_sheet = PILImage.new(
        "RGB", (args.width * 4, (args.height + 44) * 4), "#08111b"
    )
    draw = ImageDraw.Draw(contact_sheet)

    for index, candidate in enumerate(candidates):
        camera = candidate["camera"]
        transform = carla.Transform(
            carla.Location(x=camera["x"], y=camera["y"], z=camera["z"]),
            carla.Rotation(
                pitch=camera["pitch"], yaw=camera["yaw"], roll=camera["roll"]
            ),
        )
        image = capture_camera(
            world, blueprint, transform, args.width, args.height
        )
        image_path = args.output_dir / f"crosswalk-{index:02d}.jpg"
        image.save(image_path, format="JPEG", quality=88)

        column = index % 4
        row = index // 4
        left = column * args.width
        top = row * (args.height + 44)
        contact_sheet.paste(image, (left, top + 44))
        center = candidate["center"]
        label = (
            f"#{index}  center=({center['x']:.1f}, {center['y']:.1f})  "
            f"{candidate['length_m']:.1f}m x {candidate['width_m']:.1f}m"
        )
        draw.text((left + 10, top + 13), label, fill="#edf7ff")
        print(label, flush=True)

    contact_sheet_path = args.output_dir / "contact-sheet.jpg"
    contact_sheet.save(contact_sheet_path, format="JPEG", quality=88)
    (args.output_dir / "crosswalks.json").write_text(
        json.dumps(
            {"candidates": candidates, "map": world.get_map().name},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"Rendered {len(candidates)} crosswalks to {contact_sheet_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
