"""Structured logging helpers for simulation runs."""

from __future__ import annotations

import json
import logging
from uuid import uuid4


logger = logging.getLogger("simulation")


def correlation_id() -> str:
    return f"sim-{uuid4().hex}"


def log_event(message: str, correlation_id: str, **fields) -> None:
    payload = {"message": message, "correlation_id": correlation_id, **fields}
    logger.info(json.dumps(payload, sort_keys=True, default=str))
