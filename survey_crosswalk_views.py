#!/usr/bin/env python3
"""Render keyboard-like static-camera variants for one CARLA crosswalk."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import carla
from PIL import Image as PILImage
from PIL import ImageDraw

from survey_crosswalks import capture_camera, split_crosswalks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2000, type=int)
    parser.add_argument("--crosswalk-id", default=6, type=int)
    parser.add_argument("--output-dir", default="/data/crosswalk-view-survey", type=Path)
    parser.add_argument("--width", default=704, type=int)
    parser.add_argument("--height", default=480, type=int)
    parser.add_argument("--fov", default=70.0, type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    polygons = split_crosswalks(list(world.get_map().get_crosswalks()))
    polygon = polygons[args.crosswalk_id]

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
    long_x = (end.x - start.x) / length
    long_y = (end.y - start.y) / length
    side_x, side_y = -long_y, long_x

    # Positive/negative side samples both sides of the road. An along-axis offset
    # makes the crosswalk slightly oblique instead of mechanically symmetrical.
    specifications = [
        ("south-left", -5.0, -22.0, 12.0),
        ("south-soft", -2.5, -22.0, 12.0),
        ("south-right", 5.0, -22.0, 12.0),
        ("south-high", 4.0, -26.0, 16.0),
        ("north-left", -5.0, 22.0, 12.0),
        ("north-soft", 2.5, 22.0, 12.0),
        ("north-right", 5.0, 22.0, 12.0),
        ("north-high", -4.0, 26.0, 16.0),
    ]

    blueprint = world.get_blueprint_library().find("sensor.camera.rgb")
    blueprint.set_attribute("image_size_x", str(args.width))
    blueprint.set_attribute("image_size_y", str(args.height))
    blueprint.set_attribute("fov", str(args.fov))
    blueprint.set_attribute("sensor_tick", "0.0")

    sheet = PILImage.new("RGB", (args.width * 4, (args.height + 50) * 2), "#08111b")
    draw = ImageDraw.Draw(sheet)
    variants = []

    for index, (name, along, side, height) in enumerate(specifications):
        camera_x = center_x + long_x * along + side_x * side
        camera_y = center_y + long_y * along + side_y * side
        target_z = 0.6
        delta_x = center_x - camera_x
        delta_y = center_y - camera_y
        yaw = math.degrees(math.atan2(delta_y, delta_x))
        pitch = math.degrees(
            math.atan2(target_z - height, math.hypot(delta_x, delta_y))
        )
        transform = carla.Transform(
            carla.Location(x=camera_x, y=camera_y, z=height),
            carla.Rotation(pitch=pitch, yaw=yaw),
        )
        image = capture_camera(
            world, blueprint, transform, args.width, args.height
        )
        image.save(args.output_dir / f"{index:02d}-{name}.jpg", "JPEG", quality=88)

        variant = {
            "camera": {
                "pitch": round(pitch, 3),
                "roll": 0.0,
                "x": round(camera_x, 3),
                "y": round(camera_y, 3),
                "yaw": round(yaw, 3),
                "z": height,
            },
            "crosswalk_center": {
                "x": round(center_x, 3),
                "y": round(center_y, 3),
                "z": 0.0,
            },
            "fov": args.fov,
            "id": index,
            "name": name,
        }
        variants.append(variant)

        column = index % 4
        row = index // 4
        left = column * args.width
        top = row * (args.height + 50)
        sheet.paste(image, (left, top + 50))
        label = (
            f"#{index} {name}  xyz=({camera_x:.1f}, {camera_y:.1f}, {height:.0f})  "
            f"yaw={yaw:.1f} pitch={pitch:.1f}"
        )
        draw.text((left + 10, top + 16), label, fill="#edf7ff")
        print(label, flush=True)

    sheet_path = args.output_dir / "contact-sheet.jpg"
    sheet.save(sheet_path, "JPEG", quality=88)
    (args.output_dir / "variants.json").write_text(
        json.dumps(
            {
                "crosswalk_id": args.crosswalk_id,
                "map": world.get_map().name,
                "variants": variants,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"Rendered {len(variants)} variants to {sheet_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
