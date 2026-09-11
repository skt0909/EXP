"""Helpers for simulation Celery/Redis verification."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess


@dataclass(frozen=True)
class ContainerCommand:
    description: str
    command: list[str]


def real_worker_commands() -> list[ContainerCommand]:
    compose = ["docker", "compose", "-f", "simulation/docker-compose.simulation.yml"]
    return [
        ContainerCommand("start postgres, redis, worker, and beat", compose + ["up", "--build"]),
        ContainerCommand("stop simulation stack", compose + ["down", "--volumes"]),
    ]


def docker_compose_available() -> bool:
    try:
        subprocess.run(["docker", "compose", "version"], check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True
