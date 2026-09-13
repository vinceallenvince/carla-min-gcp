#!/usr/bin/env python3
"""Exercise CARLA's stock walker AI across a selected Town10 crosswalk."""

from __future__ import annotations

import argparse
import json
import math
import random
import time

import carla

from survey_navmesh_crosswalks import Crosswalk, get_crosswalks


ROLE_NAME = "carla-poc-ai-probe"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2000, type=int)
    parser.add_argument("--crosswalk-id", default=14, type=int)
    parser.add_argument("--walkers", default=4, type=int)
    parser.add_argument("--nav-samples", default=30000, type=int)
    parser.add_argument("--duration", default=45.0, type=float)
    parser.add_argument("--seed", default=20260913, type=int)
    return parser.parse_args()


def route_coordinates(
    crosswalk: Crosswalk, location: carla.Location
) -> tuple[float, float]:
    offset_x = location.x - crosswalk.center.x
    offset_y = location.y - crosswalk.center.y
    return (
        offset_x * crosswalk.long_axis.x + offset_y * crosswalk.long_axis.y,
        -offset_x * crosswalk.long_axis.y + offset_y * crosswalk.long_axis.x,
    )


def sample_approaches(
    world: carla.World,
    crosswalk: Crosswalk,
    count: int,
) -> tuple[list[carla.Location], list[carla.Location]]:
    side_a: list[carla.Location] = []
    side_b: list[carla.Location] = []
    half_length = crosswalk.length / 2
    for _ in range(count):
        location = world.get_random_location_from_navigation()
        if location is None:
            continue
        longitudinal, lateral = route_coordinates(crosswalk, location)
        if abs(lateral) > crosswalk.width / 2 + 4.0:
            continue
        approach_distance = abs(longitudinal) - half_length
        if not 0.5 <= approach_distance <= 8.0:
            continue
        (side_a if longitudinal > 0 else side_b).append(location)

    def rank(location: carla.Location) -> tuple[float, float]:
        longitudinal, lateral = route_coordinates(crosswalk, location)
        return abs(lateral), abs(abs(longitudinal) - half_length - 2.0)

    side_a.sort(key=rank)
    side_b.sort(key=rank)
    return side_a, side_b


def choose_separated(
    candidates: list[carla.Location], count: int
) -> list[carla.Location]:
    selected: list[carla.Location] = []
    for candidate in candidates:
        if all(candidate.distance(existing) >= 0.9 for existing in selected):
            selected.append(candidate)
        if len(selected) == count:
            break
    return selected


def main() -> int:
    args = parse_args()
    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    crosswalks = get_crosswalks(world)
    if not 0 <= args.crosswalk_id < len(crosswalks):
        raise RuntimeError(f"Crosswalk {args.crosswalk_id} is unavailable")
    crosswalk = crosswalks[args.crosswalk_id]
    side_a, side_b = sample_approaches(world, crosswalk, args.nav_samples)
    per_side = math.ceil(args.walkers / 2)
    targets_a = choose_separated(list(reversed(side_a)), per_side)
    targets_b = choose_separated(list(reversed(side_b)), per_side)
    if min(len(side_a), len(side_b), len(targets_a), len(targets_b)) < per_side:
        raise RuntimeError(
            f"Insufficient approach points: side_a={len(side_a)}, side_b={len(side_b)}"
        )

    rng = random.Random(args.seed)
    walker_blueprints = list(
        world.get_blueprint_library().filter("walker.pedestrian.*")
    )
    controller_blueprint = world.get_blueprint_library().find(
        "controller.ai.walker"
    )
    spawned: list[tuple[carla.Walker, carla.WalkerAIController, float]] = []
    occupied_starts: list[carla.Location] = []
    try:
        for index in range(args.walkers):
            from_a = index % 2 == 0
            slot = index // 2
            target = targets_b[slot] if from_a else targets_a[slot]
            blueprint = rng.choice(walker_blueprints)
            blueprint.set_attribute("role_name", ROLE_NAME)
            if blueprint.has_attribute("is_invincible"):
                blueprint.set_attribute("is_invincible", "true")
            actor: carla.Walker | None = None
            start: carla.Location | None = None
            candidates = side_a if from_a else side_b
            for candidate in candidates[:100]:
                if any(candidate.distance(existing) < 0.9 for existing in occupied_starts):
                    continue
                spawn_location = carla.Location(
                    x=candidate.x,
                    y=candidate.y,
                    z=candidate.z + 0.5,
                )
                actor = world.try_spawn_actor(
                    blueprint,
                    carla.Transform(spawn_location),
                )
                if actor is not None:
                    start = candidate
                    occupied_starts.append(candidate)
                    break
            if actor is None:
                continue
            assert start is not None
            controller = world.spawn_actor(
                controller_blueprint,
                carla.Transform(),
                attach_to=actor,
            )
            start_longitudinal, _ = route_coordinates(crosswalk, start)
            spawned.append((actor, controller, start_longitudinal))
            controller.start()
            controller.set_max_speed(rng.uniform(1.1, 1.65))
            controller.go_to_location(target)

        if not spawned:
            raise RuntimeError("No AI walkers could be spawned")

        started_at = time.monotonic()
        last_locations = {actor.id: actor.get_location() for actor, _, _ in spawned}
        distances = {actor.id: 0.0 for actor, _, _ in spawned}
        crossed = {actor.id: False for actor, _, _ in spawned}
        maximum_progress = {actor.id: 0.0 for actor, _, _ in spawned}
        while time.monotonic() - started_at < args.duration:
            world.wait_for_tick(10.0)
            for actor, _, start_longitudinal in spawned:
                location = actor.get_location()
                distances[actor.id] += location.distance(last_locations[actor.id])
                last_locations[actor.id] = location
                longitudinal, _ = route_coordinates(crosswalk, location)
                direction = -1.0 if start_longitudinal > 0 else 1.0
                progress = (longitudinal - start_longitudinal) * direction
                maximum_progress[actor.id] = max(maximum_progress[actor.id], progress)
                if longitudinal * start_longitudinal < 0:
                    crossed[actor.id] = True

        result = {
            "map": world.get_map().name,
            "crosswalk_id": args.crosswalk_id,
            "duration_seconds": args.duration,
            "spawned": len(spawned),
            "crossed_center": sum(crossed.values()),
            "walkers": [
                {
                    "id": actor.id,
                    "distance_m": round(distances[actor.id], 2),
                    "maximum_route_progress_m": round(maximum_progress[actor.id], 2),
                    "crossed_center": crossed[actor.id],
                    "final_location": {
                        "x": round(actor.get_location().x, 3),
                        "y": round(actor.get_location().y, 3),
                        "z": round(actor.get_location().z, 3),
                    },
                }
                for actor, _, _ in spawned
            ],
        }
        print(json.dumps(result, indent=2), flush=True)
        return 0 if result["crossed_center"] else 2
    finally:
        for _, controller, _ in spawned:
            try:
                controller.stop()
            except RuntimeError:
                pass
        client.apply_batch(
            [
                carla.command.DestroyActor(target.id)
                for walker, controller, _ in spawned
                for target in (controller, walker)
            ]
        )


if __name__ == "__main__":
    raise SystemExit(main())
