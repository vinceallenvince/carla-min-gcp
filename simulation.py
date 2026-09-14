#!/usr/bin/env python3
"""Run the CARLA traffic scene, attached RGB camera, and browser viewer."""

from __future__ import annotations

import argparse
import io
import json
import math
import queue
import random
import signal
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import carla
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from PIL import Image as PILImage

from hls_adapter import HLSAdapterSupervisor, HLSConfig


ROLE_NAME = "carla-poc-phase5"
CAMERA_ROLE_NAME = "carla-poc-phase5-camera"
HERO_ROLE_NAME = "hero"
WALKER_ROLE_NAME = "carla-poc-crosswalk-pedestrian"
WALKER_CONTROL_SPEED_SCALE = 20.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2000, type=int)
    parser.add_argument("--traffic-manager-port", default=8000, type=int)
    parser.add_argument("--vehicles", default=20, type=int)
    parser.add_argument("--pedestrians", default=8, type=int)
    parser.add_argument(
        "--pedestrian-mode",
        choices=("manual", "ai"),
        default="manual",
    )
    parser.add_argument("--seed", default=20260912, type=int)
    parser.add_argument("--output-dir", default="/data", type=Path)
    parser.add_argument("--http-host", default="127.0.0.1")
    parser.add_argument("--http-port", default=8080, type=int)
    parser.add_argument("--hls-segment-seconds", default=2.0, type=float)
    parser.add_argument("--hls-list-size", default=4, type=int)
    parser.add_argument("--hls-delete-threshold", default=1, type=int)
    parser.add_argument("--hls-restart-delay", default=1.0, type=float)
    parser.add_argument("--hls-stale-after", default=6.0, type=float)
    parser.add_argument("--ffmpeg-binary", default="ffmpeg")
    parser.add_argument("--camera-width", default=352, type=int)
    parser.add_argument("--camera-height", default=240, type=int)
    parser.add_argument("--camera-fps", default=20.0, type=float)
    parser.add_argument("--camera-fov", default=70.0, type=float)
    parser.add_argument("--jpeg-quality", default=75, type=int)
    parser.add_argument("--camera-mode", choices=("static", "vehicle"), default="static")
    parser.add_argument("--crosswalk-id", default=8, type=int)
    parser.add_argument("--static-camera-x", default=-80.066, type=float)
    parser.add_argument("--static-camera-y", default=-60.985, type=float)
    parser.add_argument("--static-camera-z", default=9.6, type=float)
    parser.add_argument("--static-camera-pitch", default=-27.242, type=float)
    parser.add_argument("--static-camera-yaw", default=-8.098, type=float)
    parser.add_argument("--static-camera-roll", default=0.0, type=float)
    args = parser.parse_args()
    if args.camera_width <= 0 or args.camera_height <= 0:
        parser.error("camera dimensions must be positive")
    if args.pedestrians < 0:
        parser.error("pedestrian count cannot be negative")
    if args.camera_fps <= 0:
        parser.error("camera FPS must be positive")
    if min(
        args.hls_segment_seconds,
        args.hls_list_size,
        args.hls_delete_threshold,
        args.hls_restart_delay,
        args.hls_stale_after,
    ) <= 0:
        parser.error("HLS timing and retention settings must be positive")
    if not 1 <= args.camera_fov < 180:
        parser.error("camera FOV must be between 1 and 180 degrees")
    if not 1 <= args.jpeg_quality <= 95:
        parser.error("JPEG quality must be between 1 and 95")
    return args


def connect(args: argparse.Namespace) -> carla.Client:
    last_error: Exception | None = None
    for attempt in range(1, 61):
        try:
            client = carla.Client(args.host, args.port)
            client.set_timeout(10.0)
            client.get_world()
            return client
        except Exception as exc:
            last_error = exc
            print(f"Waiting for CARLA ({attempt}/60): {exc}", flush=True)
            time.sleep(5)
    raise RuntimeError("CARLA did not become ready within five minutes") from last_error


def cleanup_stale_actors(client: carla.Client, world: carla.World) -> None:
    stale_ids = []
    for actor in world.get_actors():
        managed_role = actor.attributes.get("role_name") in {
            ROLE_NAME,
            CAMERA_ROLE_NAME,
            HERO_ROLE_NAME,
            WALKER_ROLE_NAME,
        }
        managed_walker_controller = actor.type_id == "controller.ai.walker"
        if managed_role or managed_walker_controller:
            stale_ids.append(actor.id)
    if stale_ids:
        client.apply_batch([carla.command.DestroyActor(actor_id) for actor_id in stale_ids])
        world.wait_for_tick(10.0)
        print(f"Removed {len(stale_ids)} stale Phase 5 actors", flush=True)


def choose_vehicle_blueprints(world: carla.World) -> list[carla.ActorBlueprint]:
    blueprints = list(world.get_blueprint_library().filter("vehicle.*"))
    four_wheeled = [
        blueprint
        for blueprint in blueprints
        if not blueprint.has_attribute("number_of_wheels")
        or int(blueprint.get_attribute("number_of_wheels")) == 4
    ]
    if not four_wheeled:
        raise RuntimeError("The CARLA world has no usable vehicle blueprints")
    return four_wheeled


def configure_vehicle_blueprint(
    blueprint: carla.ActorBlueprint, rng: random.Random, role_name: str
) -> carla.ActorBlueprint:
    blueprint.set_attribute("role_name", role_name)
    for attribute_name in ("color", "driver_id"):
        if blueprint.has_attribute(attribute_name):
            values = blueprint.get_attribute(attribute_name).recommended_values
            if values:
                blueprint.set_attribute(attribute_name, rng.choice(values))
    return blueprint


def spawn_vehicles(
    world: carla.World,
    traffic_manager_port: int,
    target_count: int,
    rng: random.Random,
) -> list[carla.Vehicle]:
    spawn_points = list(world.get_map().get_spawn_points())
    rng.shuffle(spawn_points)
    blueprints = choose_vehicle_blueprints(world)
    vehicles: list[carla.Vehicle] = []

    for transform in spawn_points:
        role_name = HERO_ROLE_NAME if not vehicles else ROLE_NAME
        blueprint = configure_vehicle_blueprint(
            rng.choice(blueprints), rng, role_name
        )
        actor = world.try_spawn_actor(blueprint, transform)
        if actor is None:
            continue
        actor.set_autopilot(True, traffic_manager_port)
        vehicles.append(actor)
        if len(vehicles) == target_count:
            break

    if len(vehicles) != target_count:
        raise RuntimeError(
            f"Spawned {len(vehicles)} vehicles; {target_count} were required"
        )
    return vehicles


def configure_traffic_manager_vehicle(
    traffic_manager: carla.TrafficManager, vehicle: carla.Vehicle
) -> None:
    light_ignore_percentage = (
        100.0
        if vehicle.attributes.get("role_name") == HERO_ROLE_NAME
        else 0.0
    )
    traffic_manager.ignore_lights_percentage(vehicle, light_ignore_percentage)
    traffic_manager.ignore_signs_percentage(vehicle, 100.0)
    traffic_manager.vehicle_percentage_speed_difference(vehicle, -20.0)
    traffic_manager.auto_lane_change(vehicle, True)


def spawn_replacement_vehicle(
    world: carla.World,
    traffic_manager: carla.TrafficManager,
    traffic_manager_port: int,
    rng: random.Random,
) -> carla.Vehicle:
    occupied = [actor.get_location() for actor in world.get_actors().filter("vehicle.*")]
    spawn_points = list(world.get_map().get_spawn_points())
    rng.shuffle(spawn_points)
    blueprints = choose_vehicle_blueprints(world)

    for transform in spawn_points:
        if any(transform.location.distance(location) < 12.0 for location in occupied):
            continue
        blueprint = configure_vehicle_blueprint(
            rng.choice(blueprints), rng, ROLE_NAME
        )
        actor = world.try_spawn_actor(blueprint, transform)
        if actor is None:
            continue
        actor.set_autopilot(True, traffic_manager_port)
        configure_traffic_manager_vehicle(traffic_manager, actor)
        return actor

    raise RuntimeError("Could not replace a lost Phase 5 vehicle")


def replenish_missing_vehicles(
    world: carla.World,
    vehicles: list[carla.Vehicle],
    traffic_manager: carla.TrafficManager,
    traffic_manager_port: int,
    rng: random.Random,
) -> int:
    replacements = 0
    for index, vehicle in enumerate(list(vehicles)):
        if world.get_actor(vehicle.id) is not None:
            continue
        if index == 0:
            raise RuntimeError("The ego vehicle was lost; rebuilding the full scene")
        replacement = spawn_replacement_vehicle(
            world, traffic_manager, traffic_manager_port, rng
        )
        vehicles[index] = replacement
        replacements += 1
        print(
            f"Replaced lost vehicle {vehicle.id} with {replacement.id}",
            flush=True,
        )
    return replacements


def relocate_stalled_vehicles(
    world: carla.World,
    vehicles: list[carla.Vehicle],
    traffic_manager: carla.TrafficManager,
    traffic_manager_port: int,
    rng: random.Random,
    max_count: int = 5,
) -> int:
    positions: dict[int, carla.Location] = {}
    stalled: list[carla.Vehicle] = []
    for vehicle in vehicles:
        try:
            positions[vehicle.id] = vehicle.get_location()
            velocity = vehicle.get_velocity()
            speed = math.sqrt(velocity.x**2 + velocity.y**2 + velocity.z**2)
            if speed <= 0.5:
                stalled.append(vehicle)
        except RuntimeError:
            continue

    stalled.sort(
        key=lambda vehicle: vehicle.attributes.get("role_name") != HERO_ROLE_NAME
    )
    spawn_points = list(world.get_map().get_spawn_points())
    rng.shuffle(spawn_points)
    relocated = 0

    for vehicle in stalled:
        occupied = [
            location
            for actor_id, location in positions.items()
            if actor_id != vehicle.id
        ]
        transform = next(
            (
                candidate
                for candidate in spawn_points
                if all(candidate.location.distance(location) >= 12.0 for location in occupied)
            ),
            None,
        )
        if transform is None:
            break

        vehicle.set_autopilot(False, traffic_manager_port)
        vehicle.set_transform(transform)
        vehicle.set_target_velocity(carla.Vector3D())
        vehicle.set_target_angular_velocity(carla.Vector3D())
        vehicle.set_autopilot(True, traffic_manager_port)
        configure_traffic_manager_vehicle(traffic_manager, vehicle)
        positions[vehicle.id] = transform.location
        spawn_points.remove(transform)
        relocated += 1
        if relocated == max_count:
            break

    return relocated


def crosswalk_axes(
    world: carla.World, crosswalk_id: int
) -> tuple[carla.Location, float, float, carla.Vector3D, carla.Vector3D]:
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
    if current or not 0 <= crosswalk_id < len(polygons):
        raise RuntimeError(f"Crosswalk {crosswalk_id} is not available")

    polygon = polygons[crosswalk_id]
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
    long_axis = carla.Vector3D(
        x=(end.x - start.x) / length,
        y=(end.y - start.y) / length,
        z=0.0,
    )
    side_axis = carla.Vector3D(x=-long_axis.y, y=long_axis.x, z=0.0)
    return center, length, width, long_axis, side_axis


class CrosswalkWalker:
    def __init__(
        self,
        actor: carla.Walker,
        endpoint_a: carla.Location,
        endpoint_b: carla.Location,
        target_a: bool,
        speed: float,
        ground_z: float,
    ) -> None:
        self.actor = actor
        self.endpoint_a = endpoint_a
        self.endpoint_b = endpoint_b
        self.target_a = target_a
        self.speed = speed
        self.turnaround_count = 0
        self.last_location = self.actor.get_location()
        self.reset_z = ground_z + 1.0
        self.last_update_at = time.monotonic()
        self.last_moved_at = self.last_update_at
        self.last_route_change_at = self.last_update_at
        self.has_moved = False
        self.observed_speed = 0.0
        path_x = self.endpoint_a.x - self.endpoint_b.x
        path_y = self.endpoint_a.y - self.endpoint_b.y
        self.path_length = math.hypot(path_x, path_y)
        self.path_direction_x = path_x / self.path_length
        self.path_direction_y = path_y / self.path_length
        self.path_progress = 0.0
        self.sync_progress(self.last_location)

    @property
    def target(self) -> carla.Location:
        return self.endpoint_a if self.target_a else self.endpoint_b

    def sync_progress(self, location: carla.Location) -> None:
        progress = (
            (location.x - self.endpoint_b.x) * self.path_direction_x
            + (location.y - self.endpoint_b.y) * self.path_direction_y
        )
        self.path_progress = max(0.0, min(self.path_length, progress))

    def update(self) -> Any:
        now = time.monotonic()
        location = self.actor.get_location()
        elapsed = min(max(now - self.last_update_at, 0.001), 0.2)
        displacement = math.hypot(
            location.x - self.last_location.x,
            location.y - self.last_location.y,
        )
        self.observed_speed = displacement / elapsed
        if displacement >= 0.03:
            self.last_moved_at = now
            self.has_moved = True
        self.last_location = location
        self.last_update_at = now

        self.sync_progress(location)
        endpoint_tolerance = 0.35
        endpoint_stall_zone = 7.0
        stalled_near_endpoint = (
            now - self.last_moved_at >= 2.0
            and now - self.last_route_change_at >= 2.0
            and (
                self.path_progress <= endpoint_stall_zone
                or self.path_progress >= self.path_length - endpoint_stall_zone
            )
        )
        if stalled_near_endpoint:
            self.target_a = not self.target_a
            self.turnaround_count += 1
            self.last_route_change_at = now
            self.last_moved_at = now
        elif self.target_a and self.path_progress >= self.path_length - endpoint_tolerance:
            self.target_a = False
            self.turnaround_count += 1
            self.last_route_change_at = now
        elif not self.target_a and self.path_progress <= endpoint_tolerance:
            self.target_a = True
            self.turnaround_count += 1
            self.last_route_change_at = now

        direction_sign = 1.0 if self.target_a else -1.0
        direction_x = self.path_direction_x * direction_sign
        direction_y = self.path_direction_y * direction_sign
        control = carla.WalkerControl()
        control.direction = carla.Vector3D(x=direction_x, y=direction_y, z=0.0)
        # CARLA 0.10's UE5 character movement advances at about 1/20 scale in
        # this runtime. Compensate at the control input while leaving transforms,
        # ground contact, collisions, and gait entirely under native control.
        control.speed = self.speed * WALKER_CONTROL_SPEED_SCALE
        control.jump = False
        return carla.command.ApplyWalkerControl(self.actor, control)

    def destroy(self) -> None:
        try:
            self.actor.destroy()
        except RuntimeError:
            pass

    def is_valid(self, world: carla.World) -> bool:
        try:
            return (
                self.actor.is_alive
                and world.get_actor(self.actor.id) is not None
            )
        except RuntimeError:
            return False


class AIWalker:
    """Observe CARLA's stock walker navigation between crosswalk approaches."""

    def __init__(
        self,
        actor: carla.Walker,
        controller: carla.WalkerAIController,
        endpoint_a: carla.Location,
        endpoint_b: carla.Location,
        target_a: bool,
        speed: float,
    ) -> None:
        self.actor = actor
        self.controller = controller
        self.endpoint_a = endpoint_a
        self.endpoint_b = endpoint_b
        self.target_a = target_a
        self.speed = speed
        self.turnaround_count = 0
        self.last_location = self.actor.get_location()
        self.last_update_at = time.monotonic()
        self.last_moved_at = self.last_update_at
        self.last_route_change_at = self.last_update_at
        self.has_moved = False
        self.observed_speed = 0.0

    @property
    def target(self) -> carla.Location:
        return self.endpoint_a if self.target_a else self.endpoint_b

    def update(self) -> None:
        now = time.monotonic()
        location = self.actor.get_location()
        elapsed = min(max(now - self.last_update_at, 0.001), 0.2)
        displacement = math.hypot(
            location.x - self.last_location.x,
            location.y - self.last_location.y,
        )
        self.observed_speed = displacement / elapsed
        if displacement >= 0.03:
            self.last_moved_at = now
            self.has_moved = True
        self.last_location = location
        self.last_update_at = now

        if (
            location.distance(self.target) <= 2.0
            and now - self.last_route_change_at >= 2.0
        ):
            self.target_a = not self.target_a
            self.controller.go_to_location(self.target)
            self.turnaround_count += 1
            self.last_route_change_at = now

    def destroy(self) -> None:
        try:
            self.controller.stop()
        except RuntimeError:
            pass
        for actor in (self.controller, self.actor):
            try:
                actor.destroy()
            except RuntimeError:
                pass

    def is_valid(self, world: carla.World) -> bool:
        try:
            return (
                self.actor.is_alive
                and self.controller.is_alive
                and world.get_actor(self.actor.id) is not None
                and world.get_actor(self.controller.id) is not None
            )
        except RuntimeError:
            return False


def spawn_crosswalk_walkers(
    world: carla.World,
    count: int,
    crosswalk_id: int,
    rng: random.Random,
) -> list[CrosswalkWalker]:
    if count == 0:
        return []

    center, length, width, long_axis, side_axis = crosswalk_axes(
        world, crosswalk_id
    )
    road_waypoint = world.get_map().get_waypoint(
        center,
        project_to_road=True,
        lane_type=carla.LaneType.Any,
    )
    ground_z = road_waypoint.transform.location.z if road_waypoint else center.z
    blueprints = list(world.get_blueprint_library().filter("walker.pedestrian.*"))
    if not blueprints:
        raise RuntimeError("The CARLA world has no pedestrian blueprints")

    max_lateral = max(0.0, width * 0.32)
    lane_count = min(4, count)
    lateral_slots = [
        0.0
        if lane_count == 1
        else -max_lateral + (2 * max_lateral * index / (lane_count - 1))
        for index in range(lane_count)
    ]
    pending_walkers: list[
        tuple[carla.Walker, carla.Location, carla.Location, bool, float]
    ] = []

    for index in range(count):
        blueprint = rng.choice(blueprints)
        blueprint.set_attribute("role_name", WALKER_ROLE_NAME)
        if blueprint.has_attribute("is_invincible"):
            blueprint.set_attribute("is_invincible", "true")

        phase_count = math.ceil(count / 2)
        phase_fraction = ((index // 2) + 0.35) / phase_count
        target_a = index % 2 == 0
        initial_progress_fraction = (
            phase_fraction if target_a else 1.0 - phase_fraction
        )
        lane_index = index % lane_count
        lateral = lateral_slots[lane_index]
        endpoint_margin = 6.0

        def endpoint(sign: float) -> carla.Location:
            return carla.Location(
                x=center.x
                + long_axis.x * sign * (length / 2 + endpoint_margin)
                + side_axis.x * lateral,
                y=center.y
                + long_axis.y * sign * (length / 2 + endpoint_margin)
                + side_axis.y * lateral,
                z=center.z,
            )

        endpoint_a = endpoint(1.0)
        endpoint_b = endpoint(-1.0)
        actor: carla.Walker | None = None
        for progress_offset in (0.0, 0.07, -0.07, 0.14, -0.14):
            progress = max(
                0.02,
                min(0.98, initial_progress_fraction + progress_offset),
            )
            start = carla.Location(
                x=endpoint_b.x + (endpoint_a.x - endpoint_b.x) * progress,
                y=endpoint_b.y + (endpoint_a.y - endpoint_b.y) * progress,
                z=ground_z + 1.0,
            )
            actor = world.try_spawn_actor(blueprint, carla.Transform(start))
            if actor is not None:
                break
        if actor is None:
            for walker, *_ in pending_walkers:
                walker.destroy()
            raise RuntimeError(
                f"Could not spawn pedestrian {index + 1}/{count} at crosswalk {crosswalk_id}"
            )
        speed = rng.uniform(1.1, 1.65)
        pending_walkers.append(
            (
                actor,
                endpoint_a,
                endpoint_b,
                target_a,
                speed,
            )
        )

    walkers = [
        CrosswalkWalker(
            actor,
            endpoint_a,
            endpoint_b,
            target_a,
            speed,
            ground_z,
        )
        for (
            actor,
            endpoint_a,
            endpoint_b,
            target_a,
            speed,
        ) in pending_walkers
    ]

    return walkers


def sample_crosswalk_approaches(
    world: carla.World,
    crosswalk_id: int,
    sample_count: int = 30000,
) -> tuple[list[carla.Location], list[carla.Location]]:
    center, length, width, long_axis, side_axis = crosswalk_axes(
        world, crosswalk_id
    )
    half_length = length / 2
    side_a: list[carla.Location] = []
    side_b: list[carla.Location] = []
    for _ in range(sample_count):
        location = world.get_random_location_from_navigation()
        if location is None:
            continue
        offset_x = location.x - center.x
        offset_y = location.y - center.y
        longitudinal = offset_x * long_axis.x + offset_y * long_axis.y
        lateral = offset_x * side_axis.x + offset_y * side_axis.y
        approach_distance = abs(longitudinal) - half_length
        if abs(lateral) > width / 2 + 4.0:
            continue
        if not 0.5 <= approach_distance <= 8.0:
            continue
        (side_a if longitudinal > 0 else side_b).append(location)

    def rank(location: carla.Location) -> tuple[float, float]:
        offset_x = location.x - center.x
        offset_y = location.y - center.y
        longitudinal = offset_x * long_axis.x + offset_y * long_axis.y
        lateral = offset_x * side_axis.x + offset_y * side_axis.y
        return abs(lateral), abs(abs(longitudinal) - half_length - 2.0)

    side_a.sort(key=rank)
    side_b.sort(key=rank)
    return side_a, side_b


def select_separated_locations(
    candidates: list[carla.Location],
    count: int,
) -> list[carla.Location]:
    selected: list[carla.Location] = []
    for candidate in candidates:
        if all(candidate.distance(existing) >= 0.9 for existing in selected):
            selected.append(candidate)
        if len(selected) == count:
            break
    return selected


def spawn_ai_crosswalk_walkers(
    world: carla.World,
    count: int,
    crosswalk_id: int,
    rng: random.Random,
) -> list[AIWalker]:
    if count == 0:
        return []

    side_a, side_b = sample_crosswalk_approaches(world, crosswalk_id)
    per_side = math.ceil(count / 2)
    endpoints_a = select_separated_locations(side_a, per_side)
    endpoints_b = select_separated_locations(side_b, per_side)
    if min(len(endpoints_a), len(endpoints_b)) < per_side:
        raise RuntimeError(
            f"Crosswalk {crosswalk_id} has insufficient navigation-mesh approaches: "
            f"side_a={len(endpoints_a)}, side_b={len(endpoints_b)}"
        )

    world.set_pedestrians_cross_factor(1.0)
    walker_blueprints = list(
        world.get_blueprint_library().filter("walker.pedestrian.*")
    )
    controller_blueprint = world.get_blueprint_library().find(
        "controller.ai.walker"
    )
    walkers: list[AIWalker] = []
    occupied_starts: list[carla.Location] = []
    try:
        for index in range(count):
            start_on_a = index % 2 == 0
            slot = index // 2
            start_candidates = side_a if start_on_a else side_b
            endpoint_a = endpoints_a[slot]
            endpoint_b = endpoints_b[slot]
            blueprint = rng.choice(walker_blueprints)
            blueprint.set_attribute("role_name", WALKER_ROLE_NAME)
            if blueprint.has_attribute("is_invincible"):
                blueprint.set_attribute("is_invincible", "true")

            actor: carla.Walker | None = None
            for candidate in start_candidates[:200]:
                if any(
                    candidate.distance(existing) < 0.9
                    for existing in occupied_starts
                ):
                    continue
                start = carla.Location(
                    x=candidate.x,
                    y=candidate.y,
                    z=candidate.z + 0.5,
                )
                actor = world.try_spawn_actor(blueprint, carla.Transform(start))
                if actor is not None:
                    occupied_starts.append(candidate)
                    break
            if actor is None:
                raise RuntimeError(
                    f"Could not spawn AI pedestrian {index + 1}/{count} "
                    f"near crosswalk {crosswalk_id}"
                )

            try:
                controller = world.spawn_actor(
                    controller_blueprint,
                    carla.Transform(),
                    attach_to=actor,
                )
            except Exception:
                actor.destroy()
                raise
            speed = rng.uniform(1.1, 1.65)
            target_a = not start_on_a
            walker = AIWalker(
                actor,
                controller,
                endpoint_a,
                endpoint_b,
                target_a,
                speed,
            )
            walkers.append(walker)
            controller.start()
            controller.set_max_speed(speed)
            controller.go_to_location(walker.target)
    except Exception:
        for walker in walkers:
            walker.destroy()
        raise

    return walkers


def spawn_managed_walkers(
    world: carla.World,
    count: int,
    crosswalk_id: int,
    rng: random.Random,
    pedestrian_mode: str,
) -> list[CrosswalkWalker | AIWalker]:
    if pedestrian_mode == "ai":
        return spawn_ai_crosswalk_walkers(world, count, crosswalk_id, rng)
    return spawn_crosswalk_walkers(world, count, crosswalk_id, rng)


def maintain_crosswalk_walkers(
    world: carla.World,
    walkers: list[CrosswalkWalker | AIWalker],
    target_count: int,
    crosswalk_id: int,
    rng: random.Random,
    pedestrian_mode: str,
) -> tuple[list[CrosswalkWalker | AIWalker], bool]:
    center, length, width, long_axis, side_axis = crosswalk_axes(
        world, crosswalk_id
    )
    maintained: list[CrosswalkWalker | AIWalker] = []
    recovered = False
    for walker in walkers:
        if not walker.is_valid(world):
            walker.destroy()
            recovered = True
            continue
        try:
            if isinstance(walker, AIWalker):
                maintained.append(walker)
                continue
            location = walker.actor.get_location()
            offset_x = location.x - center.x
            offset_y = location.y - center.y
            longitudinal = offset_x * long_axis.x + offset_y * long_axis.y
            lateral = offset_x * side_axis.x + offset_y * side_axis.y
            inside_corridor = (
                abs(longitudinal) <= length / 2 + 8.0
                and abs(lateral) <= width / 2 + 3.0
            )
            if not inside_corridor:
                transform = walker.actor.get_transform()
                transform.location = carla.Location(
                    x=(walker.endpoint_a.x + walker.endpoint_b.x) / 2,
                    y=(walker.endpoint_a.y + walker.endpoint_b.y) / 2,
                    z=walker.reset_z,
                )
                walker.actor.set_transform(transform)
                walker.last_location = transform.location
                walker.sync_progress(transform.location)
                walker.last_update_at = time.monotonic()
                recovered = True
            maintained.append(walker)
        except RuntimeError:
            recovered = True

    maintained = maintained[:target_count]
    missing = target_count - len(maintained)
    if missing > 0:
        print(
            f"Replacing {missing} invalid {pedestrian_mode} pedestrian(s)",
            flush=True,
        )
        try:
            maintained.extend(
                spawn_managed_walkers(
                    world,
                    missing,
                    crosswalk_id,
                    rng,
                    pedestrian_mode,
                )
            )
        except RuntimeError as exc:
            print(f"Pedestrian replacement deferred: {exc}", flush=True)
        recovered = True
    elif recovered:
        print("Returned out-of-corridor pedestrian(s) to the crossing", flush=True)

    return maintained, recovered


def pedestrian_status(
    walkers: list[CrosswalkWalker | AIWalker],
) -> dict[str, Any]:
    moving = 0
    samples = []
    now = time.monotonic()
    for walker in walkers:
        try:
            if not walker.actor.is_alive:
                continue
            location = walker.actor.get_location()
        except RuntimeError:
            continue
        speed = walker.observed_speed
        moving += walker.has_moved and now - walker.last_moved_at <= 10.0
        if len(samples) < 5:
            samples.append(
                {
                    "id": walker.actor.id,
                    "location": {
                        "x": round(location.x, 2),
                        "y": round(location.y, 2),
                        "z": round(location.z, 2),
                    },
                    "speed_mps": round(speed, 2),
                    "turnaround_count": walker.turnaround_count,
                }
            )
    return {
        "moving_pedestrian_count": moving,
        "pedestrian_count": sum(walker.actor.is_alive for walker in walkers),
        "pedestrian_samples": samples,
        "pedestrian_turnaround_count": sum(
            walker.turnaround_count for walker in walkers
        ),
    }


class CameraRecorder:
    """Publish JPEGs without blocking CARLA's sensor callback on encoding or disk I/O."""

    def __init__(self, output_dir: Path, jpeg_quality: int, target_fps: float) -> None:
        self.output_dir = output_dir
        self.jpeg_quality = jpeg_quality
        self.target_fps = target_fps
        self._condition = threading.Condition()
        self._closed = False
        self._pending_frame: tuple[int, int, int, bytes] | None = None
        self._snapshot_queue: queue.Queue[tuple[PILImage.Image, bool] | None] = (
            queue.Queue(maxsize=1)
        )
        self._published_at: deque[float] = deque(maxlen=240)
        self._last_snapshot_at = 0.0
        self.latest_jpeg: bytes | None = None
        self.frame_count = 0
        self.received_frame_count = 0
        self.dropped_frame_count = 0
        self.latest_frame = 0
        self.latest_timestamp = 0.0
        self.latest_width = 0
        self.latest_height = 0
        self._snapshot_thread = threading.Thread(
            target=self._write_snapshots,
            name="carla-camera-snapshots",
            daemon=True,
        )
        self._encoder_thread = threading.Thread(
            target=self._encode_frames,
            name="carla-camera-encoder",
            daemon=True,
        )
        self._snapshot_thread.start()
        self._encoder_thread.start()

    def __call__(self, image: carla.Image) -> None:
        raw_data = bytes(image.raw_data)
        with self._condition:
            if self._closed:
                return
            self.received_frame_count += 1
            if self._pending_frame is not None:
                self.dropped_frame_count += 1
            self._pending_frame = (image.frame, image.width, image.height, raw_data)
            self._condition.notify_all()

    def _encode_frames(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._pending_frame is not None or self._closed
                )
                if self._pending_frame is None:
                    return
                frame_number, width, height, raw_data = self._pending_frame
                self._pending_frame = None

            try:
                rgb = PILImage.frombytes(
                    "RGBA",
                    (width, height),
                    raw_data,
                    "raw",
                    "BGRA",
                ).convert("RGB")
                encoded = io.BytesIO()
                rgb.save(encoded, format="JPEG", quality=self.jpeg_quality)
                jpeg = encoded.getvalue()
                published_at = time.monotonic()

                with self._condition:
                    self.frame_count += 1
                    self.latest_frame = frame_number
                    self.latest_timestamp = time.time()
                    self.latest_jpeg = jpeg
                    self.latest_width = width
                    self.latest_height = height
                    self._published_at.append(published_at)
                    frame_count = self.frame_count
                    self._condition.notify_all()

                if frame_count == 1 or published_at - self._last_snapshot_at >= 5.0:
                    try:
                        self._snapshot_queue.put_nowait((rgb.copy(), frame_count == 1))
                        self._last_snapshot_at = published_at
                    except queue.Full:
                        pass
            except Exception as exc:
                print(f"Camera encoder error: {exc}", flush=True)

    def _write_snapshots(self) -> None:
        while True:
            item = self._snapshot_queue.get()
            if item is None:
                return
            image, is_first = item
            try:
                if is_first:
                    image.save(self.output_dir / "camera-first-frame.png", format="PNG")
                temporary_path = self.output_dir / "camera-latest.tmp.png"
                image.save(temporary_path, format="PNG")
                temporary_path.replace(self.output_dir / "camera-latest.png")
            except Exception as exc:
                print(f"Camera snapshot error: {exc}", flush=True)

    def _measured_fps_locked(self) -> float:
        cutoff = time.monotonic() - 5.0
        while len(self._published_at) > 1 and self._published_at[0] < cutoff:
            self._published_at.popleft()
        if len(self._published_at) < 2:
            return 0.0
        elapsed = self._published_at[-1] - self._published_at[0]
        return (len(self._published_at) - 1) / elapsed if elapsed > 0 else 0.0

    def snapshot(self) -> dict[str, int | float]:
        with self._condition:
            return {
                "dropped_frame_count": self.dropped_frame_count,
                "fps": round(self._measured_fps_locked(), 2),
                "frame_count": self.frame_count,
                "height": self.latest_height,
                "jpeg_bytes": len(self.latest_jpeg) if self.latest_jpeg else 0,
                "latest_frame": self.latest_frame,
                "latest_timestamp": self.latest_timestamp,
                "received_frame_count": self.received_frame_count,
                "target_fps": self.target_fps,
                "width": self.latest_width,
            }

    def wait_for_jpeg(
        self, after_frame: int, timeout: float = 10.0
    ) -> tuple[int, bytes | None, bool]:
        with self._condition:
            self._condition.wait_for(
                lambda: self.latest_frame > after_frame or self._closed,
                timeout=timeout,
            )
            return self.latest_frame, self.latest_jpeg, self._closed

    def current_jpeg(self) -> bytes | None:
        with self._condition:
            return self.latest_jpeg

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._encoder_thread.join(timeout=10)
        self._snapshot_queue.put(None)
        self._snapshot_thread.join(timeout=10)


def create_viewer_app(
    recorder: CameraRecorder,
    status_path: Path,
    hls_adapter: HLSAdapterSupervisor,
) -> FastAPI:
    app = FastAPI(
        title="CARLA on GCP",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    index_html = (Path(__file__).parent / "static" / "index.html").read_text()

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-store"})

    @app.get("/healthz", response_class=JSONResponse)
    def health() -> JSONResponse:
        try:
            scene_status = json.loads(status_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            scene_status = {}
        camera_status = recorder.snapshot()
        hls_status = hls_adapter.health()
        return JSONResponse(
            {
                "camera_dropped_frame_count": camera_status[
                    "dropped_frame_count"
                ],
                "camera_fps": camera_status["fps"],
                "camera_fov": scene_status.get("camera", {}).get("fov"),
                "camera_frame_count": camera_status["frame_count"],
                "camera_height": camera_status["height"],
                "camera_jpeg_bytes": camera_status["jpeg_bytes"],
                "camera_latest_frame": camera_status["latest_frame"],
                "camera_received_frame_count": camera_status[
                    "received_frame_count"
                ],
                "camera_target_fps": camera_status["target_fps"],
                "camera_width": camera_status["width"],
                "camera_mode": scene_status.get("camera", {}).get("mode"),
                "crosswalk_id": scene_status.get("camera", {}).get(
                    "crosswalk_id"
                ),
                "ego_speed_mps": scene_status.get("ego_speed_mps"),
                "map": scene_status.get("map"),
                "moving_pedestrian_count": scene_status.get(
                    "moving_pedestrian_count"
                ),
                "pedestrian_count": scene_status.get("pedestrian_count"),
                "pedestrian_mode": scene_status.get("pedestrian_mode"),
                "state": scene_status.get("state", "starting"),
                "vehicle_count": scene_status.get("vehicle_count"),
                **hls_status,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/snapshot.jpg")
    def snapshot() -> Response:
        jpeg = recorder.current_jpeg()
        if jpeg is None:
            raise HTTPException(status_code=503, detail="Camera frame unavailable")
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/live/playlist.m3u8")
    def hls_playlist() -> FileResponse:
        if not hls_adapter.playlist_path.is_file():
            raise HTTPException(status_code=503, detail="HLS playlist unavailable")
        return FileResponse(
            hls_adapter.playlist_path,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/live/{segment_name}")
    def hls_segment(segment_name: str) -> FileResponse:
        segment_path = hls_adapter.segment_path(segment_name)
        if segment_path is None:
            raise HTTPException(status_code=404, detail="HLS segment not found")
        return FileResponse(
            segment_path,
            media_type="video/mp2t",
            headers={"Cache-Control": "public, max-age=30, immutable"},
        )

    @app.get("/stream.mjpg")
    def stream() -> StreamingResponse:
        def frames():
            last_frame = 0
            while True:
                frame_number, jpeg, closed = recorder.wait_for_jpeg(last_frame)
                if closed:
                    return
                if jpeg is None or frame_number <= last_frame:
                    continue
                last_frame = frame_number
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                    + jpeg
                    + b"\r\n"
                )

        return StreamingResponse(
            frames(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    return app


def start_viewer(
    args: argparse.Namespace,
    recorder: CameraRecorder,
    hls_adapter: HLSAdapterSupervisor,
) -> tuple[uvicorn.Server, threading.Thread]:
    app = create_viewer_app(
        recorder,
        args.output_dir / "status.json",
        hls_adapter,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.http_host,
            port=args.http_port,
            access_log=False,
            log_level="info",
            server_header=False,
        )
    )
    thread = threading.Thread(target=server.run, name="carla-viewer", daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.1)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError(
            f"Viewer failed to listen on {args.http_host}:{args.http_port}"
        )
    print(
        f"Viewer listening on http://{args.http_host}:{args.http_port}", flush=True
    )
    return server, thread


def spawn_camera(
    world: carla.World,
    ego_vehicle: carla.Vehicle,
    recorder: CameraRecorder,
    args: argparse.Namespace,
) -> carla.Sensor:
    blueprint = world.get_blueprint_library().find("sensor.camera.rgb")
    blueprint.set_attribute("image_size_x", str(args.camera_width))
    blueprint.set_attribute("image_size_y", str(args.camera_height))
    blueprint.set_attribute("fov", str(args.camera_fov))
    blueprint.set_attribute("sensor_tick", f"{1.0 / args.camera_fps:.6f}")
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", CAMERA_ROLE_NAME)

    if args.camera_mode == "static":
        transform = carla.Transform(
            carla.Location(
                x=args.static_camera_x,
                y=args.static_camera_y,
                z=args.static_camera_z,
            ),
            carla.Rotation(
                pitch=args.static_camera_pitch,
                yaw=args.static_camera_yaw,
                roll=args.static_camera_roll,
            ),
        )
        camera = world.spawn_actor(blueprint, transform)
    else:
        transform = carla.Transform(
            carla.Location(x=1.5, z=1.7),
            carla.Rotation(pitch=-5.0),
        )
        camera = world.spawn_actor(
            blueprint,
            transform,
            attach_to=ego_vehicle,
            attachment_type=carla.AttachmentType.Rigid,
        )
    camera.listen(recorder)
    return camera


def vehicle_status(vehicles: list[carla.Vehicle]) -> dict[str, Any]:
    ego_speed = 0.0
    moving = 0
    max_speed = 0.0
    samples = []
    unavailable = 0
    for vehicle in vehicles:
        try:
            velocity = vehicle.get_velocity()
            location = vehicle.get_location()
        except RuntimeError as exc:
            unavailable += 1
            print(f"Vehicle {vehicle.id} telemetry unavailable: {exc}", flush=True)
            continue
        speed = math.sqrt(velocity.x**2 + velocity.y**2 + velocity.z**2)
        if vehicle.attributes.get("role_name") == HERO_ROLE_NAME:
            ego_speed = speed
        max_speed = max(max_speed, speed)
        moving += speed > 0.5
        if len(samples) < 5:
            samples.append(
                {
                    "id": vehicle.id,
                    "location": {
                        "x": round(location.x, 2),
                        "y": round(location.y, 2),
                        "z": round(location.z, 2),
                    },
                    "speed_mps": round(speed, 2),
                }
            )
    return {
        "ego_speed_mps": round(ego_speed, 2),
        "max_speed_mps": round(max_speed, 2),
        "moving_vehicle_count": moving,
        "unavailable_vehicle_count": unavailable,
        "vehicle_samples": samples,
    }


def write_status(path: Path, status: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    temporary_path.replace(path)


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    client = connect(args)
    world = client.get_world()
    cleanup_stale_actors(client, world)

    traffic_manager = client.get_trafficmanager(args.traffic_manager_port)
    traffic_manager.set_random_device_seed(args.seed)
    traffic_manager.set_global_distance_to_leading_vehicle(2.5)
    traffic_manager.global_percentage_speed_difference(10.0)
    traffic_manager.set_synchronous_mode(False)
    traffic_manager.set_hybrid_physics_mode(True)
    traffic_manager.set_hybrid_physics_radius(100.0)

    rng = random.Random(args.seed)
    vehicles: list[carla.Vehicle] = []
    walkers: list[CrosswalkWalker | AIWalker] = []
    camera: carla.Sensor | None = None
    viewer_server: uvicorn.Server | None = None
    viewer_thread: threading.Thread | None = None
    recorder = CameraRecorder(args.output_dir, args.jpeg_quality, args.camera_fps)
    hls_adapter = HLSAdapterSupervisor(
        HLSConfig(
            input_url=f"http://127.0.0.1:{args.http_port}/stream.mjpg",
            output_dir=args.output_dir / "hls",
            width=args.camera_width,
            height=args.camera_height,
            fps=args.camera_fps,
            segment_seconds=args.hls_segment_seconds,
            list_size=args.hls_list_size,
            delete_threshold=args.hls_delete_threshold,
            restart_delay=args.hls_restart_delay,
            stale_after=args.hls_stale_after,
            ffmpeg_binary=args.ffmpeg_binary,
        )
    )
    started_at = time.time()
    ego_stalled_since: float | None = None
    relocation_count = 0
    replacement_count = 0
    pedestrian_rebuild_count = 0
    last_scene_update_at = 0.0

    try:
        walkers = spawn_managed_walkers(
            world,
            args.pedestrians,
            args.crosswalk_id,
            rng,
            args.pedestrian_mode,
        )
        vehicles = spawn_vehicles(
            world, args.traffic_manager_port, args.vehicles, rng
        )
        for vehicle in vehicles:
            configure_traffic_manager_vehicle(traffic_manager, vehicle)
        camera = spawn_camera(world, vehicles[0], recorder, args)
        viewer_server, viewer_thread = start_viewer(args, recorder, hls_adapter)
        hls_adapter.start()
        camera_description = (
            f"static over crosswalk {args.crosswalk_id}"
            if args.camera_mode == "static"
            else f"attached to ego vehicle {vehicles[0].id}"
        )
        print(
            f"Spawned {len(vehicles)} autopilot vehicles; "
            f"{len(walkers)} crosswalk pedestrians; "
            f"RGB camera {camera.id} {camera_description}",
            flush=True,
        )

        while not stop_event.is_set():
            if viewer_thread is None or not viewer_thread.is_alive():
                raise RuntimeError("The browser viewer stopped unexpectedly")
            if not hls_adapter.is_alive():
                raise RuntimeError("The HLS supervisor stopped unexpectedly")
            snapshot = world.wait_for_tick(10.0)
            walker_commands = []
            for walker in walkers:
                try:
                    command = walker.update()
                    if command is not None:
                        walker_commands.append(command)
                except RuntimeError:
                    pass
            if walker_commands:
                client.apply_batch(walker_commands)
            loop_now = time.monotonic()
            if loop_now - last_scene_update_at < 0.5:
                continue
            last_scene_update_at = loop_now

            camera_status = recorder.snapshot()
            if time.time() - started_at > 30 and camera_status["frame_count"] == 0:
                raise RuntimeError("The attached RGB camera produced no frames")

            replacement_count += replenish_missing_vehicles(
                world,
                vehicles,
                traffic_manager,
                args.traffic_manager_port,
                rng,
            )
            walkers, rebuilt_walkers = maintain_crosswalk_walkers(
                world,
                walkers,
                args.pedestrians,
                args.crosswalk_id,
                rng,
                args.pedestrian_mode,
            )
            pedestrian_rebuild_count += int(rebuilt_walkers)
            telemetry = vehicle_status(vehicles)
            pedestrian_telemetry = pedestrian_status(walkers)
            now = time.time()
            if telemetry["ego_speed_mps"] < 0.5:
                if ego_stalled_since is None:
                    ego_stalled_since = now
                elif now - ego_stalled_since >= 10:
                    relocated = relocate_stalled_vehicles(
                        world,
                        vehicles,
                        traffic_manager,
                        args.traffic_manager_port,
                        rng,
                    )
                    relocation_count += relocated
                    ego_stalled_since = None
                    print(
                        f"Traffic watchdog relocated {relocated} stalled vehicles",
                        flush=True,
                    )
            else:
                ego_stalled_since = None

            camera_transform = camera.get_transform()
            status = {
                "actor_ids": (
                    [vehicle.id for vehicle in vehicles]
                    + [walker.actor.id for walker in walkers]
                    + [camera.id]
                ),
                "camera": {
                    "actor_id": camera.id,
                    "attached_vehicle_id": (
                        vehicles[0].id if args.camera_mode == "vehicle" else None
                    ),
                    "crosswalk_id": (
                        args.crosswalk_id if args.camera_mode == "static" else None
                    ),
                    "height": args.camera_height,
                    "fov": args.camera_fov,
                    "jpeg_quality": args.jpeg_quality,
                    "mode": args.camera_mode,
                    "transform": {
                        "pitch": round(camera_transform.rotation.pitch, 3),
                        "roll": round(camera_transform.rotation.roll, 3),
                        "x": round(camera_transform.location.x, 3),
                        "y": round(camera_transform.location.y, 3),
                        "yaw": round(camera_transform.rotation.yaw, 3),
                        "z": round(camera_transform.location.z, 3),
                    },
                    "width": args.camera_width,
                    **camera_status,
                },
                "client_version": client.get_client_version(),
                "map": world.get_map().name,
                "requested_vehicle_count": args.vehicles,
                "requested_pedestrian_count": args.pedestrians,
                "pedestrian_mode": args.pedestrian_mode,
                "pedestrian_rebuild_count": pedestrian_rebuild_count,
                "relocation_count": relocation_count,
                "replacement_count": replacement_count,
                "server_version": client.get_server_version(),
                "simulation_frame": snapshot.frame,
                "state": "running",
                "updated_at_unix": time.time(),
                "viewer": {
                    "host": args.http_host,
                    "hls_playlist_path": "/live/playlist.m3u8",
                    "port": args.http_port,
                    "snapshot_path": "/snapshot.jpg",
                    "stream_path": "/stream.mjpg",
                },
                "vehicle_count": len(vehicles),
                "vehicle_ids": [vehicle.id for vehicle in vehicles],
                "pedestrian_ids": [walker.actor.id for walker in walkers],
                **pedestrian_telemetry,
                **telemetry,
            }
            write_status(args.output_dir / "status.json", status)
    finally:
        if camera is not None:
            camera.stop()
        hls_adapter.stop()
        recorder.close()
        if viewer_server is not None:
            viewer_server.should_exit = True
        if viewer_thread is not None:
            viewer_thread.join(timeout=10)
        for walker in walkers:
            walker.destroy()
        actor_ids = [vehicle.id for vehicle in vehicles]
        if camera is not None:
            actor_ids.append(camera.id)
        if actor_ids:
            client.apply_batch(
                [carla.command.DestroyActor(actor_id) for actor_id in actor_ids]
            )
        print(f"Destroyed {len(actor_ids)} Phase 5 actors", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
