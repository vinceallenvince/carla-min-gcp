#!/usr/bin/env python3
"""Rank Town10 crosswalks by sampled pedestrian-navigation coverage."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import carla


@dataclass
class Crosswalk:
    crosswalk_id: int
    center: carla.Location
    length: float
    width: float
    long_axis: carla.Vector3D


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2000, type=int)
    parser.add_argument("--samples", default=30000, type=int)
    parser.add_argument("--bins", default=10, type=int)
    parser.add_argument("--side-margin", default=5.0, type=float)
    parser.add_argument("--corridor-margin", default=1.5, type=float)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def get_crosswalks(world: carla.World) -> list[Crosswalk]:
    polygons: list[list[carla.Location]] = []
    current: list[carla.Location] = []
    for point in world.get_map().get_crosswalks():
        if not current:
            current = [point]
            continue
        current.append(point)
        if len(current) >= 5 and point.distance(current[0]) < 0.1:
            polygons.append(current[:-1])
            current = []

    crosswalks = []
    for crosswalk_id, polygon in enumerate(polygons):
        center = carla.Location(
            x=sum(point.x for point in polygon) / len(polygon),
            y=sum(point.y for point in polygon) / len(polygon),
            z=sum(point.z for point in polygon) / len(polygon),
        )
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
        crosswalks.append(
            Crosswalk(
                crosswalk_id=crosswalk_id,
                center=center,
                length=length,
                width=width,
                long_axis=carla.Vector3D(
                    x=(end.x - start.x) / length,
                    y=(end.y - start.y) / length,
                    z=0.0,
                ),
            )
        )
    return crosswalks


def main() -> int:
    args = parse_args()
    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    crosswalks = get_crosswalks(world)
    counts = {
        crosswalk.crosswalk_id: {
            "bins": [0] * args.bins,
            "approach_a": 0,
            "approach_b": 0,
            "center": 0,
            "corridor": 0,
        }
        for crosswalk in crosswalks
    }

    valid_samples = 0
    for _ in range(args.samples):
        location = world.get_random_location_from_navigation()
        if location is None:
            continue
        valid_samples += 1
        for crosswalk in crosswalks:
            offset_x = location.x - crosswalk.center.x
            offset_y = location.y - crosswalk.center.y
            longitudinal = (
                offset_x * crosswalk.long_axis.x
                + offset_y * crosswalk.long_axis.y
            )
            lateral = abs(
                -offset_x * crosswalk.long_axis.y
                + offset_y * crosswalk.long_axis.x
            )
            corridor_half_width = crosswalk.width / 2 + args.corridor_margin
            if lateral > corridor_half_width:
                continue
            if abs(longitudinal) > crosswalk.length / 2 + args.side_margin:
                continue

            sample_counts = counts[crosswalk.crosswalk_id]
            sample_counts["corridor"] += 1
            if abs(longitudinal) <= crosswalk.length * 0.1:
                sample_counts["center"] += 1
            if longitudinal < -crosswalk.length / 2:
                sample_counts["approach_b"] += 1
            elif longitudinal > crosswalk.length / 2:
                sample_counts["approach_a"] += 1
            else:
                normalized = (longitudinal + crosswalk.length / 2) / crosswalk.length
                bin_index = min(args.bins - 1, int(normalized * args.bins))
                sample_counts["bins"][bin_index] += 1

    candidates = []
    for crosswalk in crosswalks:
        sample_counts = counts[crosswalk.crosswalk_id]
        covered_bins = sum(value > 0 for value in sample_counts["bins"])
        minimum_bin_samples = min(sample_counts["bins"])
        approaches_present = int(sample_counts["approach_a"] > 0) + int(
            sample_counts["approach_b"] > 0
        )
        candidates.append(
            {
                "id": crosswalk.crosswalk_id,
                "center": {
                    "x": round(crosswalk.center.x, 3),
                    "y": round(crosswalk.center.y, 3),
                    "z": round(crosswalk.center.z, 3),
                },
                "length_m": round(crosswalk.length, 2),
                "width_m": round(crosswalk.width, 2),
                "covered_bins": covered_bins,
                "total_bins": args.bins,
                "minimum_bin_samples": minimum_bin_samples,
                "center_samples": sample_counts["center"],
                "approach_a_samples": sample_counts["approach_a"],
                "approach_b_samples": sample_counts["approach_b"],
                "corridor_samples": sample_counts["corridor"],
                "score": (
                    covered_bins * 10000
                    + approaches_present * 1000
                    + min(sample_counts["approach_a"], sample_counts["approach_b"])
                    + min(sample_counts["center"], 999)
                ),
            }
        )

    candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
    result = {
        "map": world.get_map().name,
        "requested_samples": args.samples,
        "valid_samples": valid_samples,
        "bins": args.bins,
        "candidates": candidates,
    }
    rendered = json.dumps(result, indent=2)
    print(rendered, flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
