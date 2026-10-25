"""Where N spawned things go, relative to the user. Mirrored by maps/scripts/hermes_sandbox.js (same vectors)."""

from __future__ import annotations

import math

Vec = tuple[float, float, float]


def positions(origin: Vec, yaw_deg: float, count: int, formation: str, spacing: float, height: float) -> list[Vec]:
    """Source yaw: 0 = +X, 90 = +Y. line trails behind; stack/grid sit in front; circle surrounds."""
    ox, oy, oz = origin
    yaw = math.radians(yaw_deg)
    fx, fy = math.cos(yaw), math.sin(yaw)
    lx, ly = -fy, fx  # the user's left
    out: list[Vec] = []
    if formation == "line":
        out = [(ox - fx * spacing * i, oy - fy * spacing * i, oz) for i in range(1, count + 1)]
    elif formation == "stack":
        bx, by = ox + fx * spacing * 2, oy + fy * spacing * 2
        out = [(bx, by, oz + height * i) for i in range(count)]
    elif formation == "circle":
        r = max(spacing, spacing * count / (2 * math.pi))
        out = [(ox + r * math.cos(2 * math.pi * i / count), oy + r * math.sin(2 * math.pi * i / count), oz)
               for i in range(count)]
    elif formation == "grid":
        side = math.ceil(math.sqrt(count))
        for i in range(count):
            row, col = divmod(i, side)
            d, lat = spacing * (2 + row), spacing * ((side - 1) / 2 - col)
            out.append((ox + fx * d + lx * lat, oy + fy * d + ly * lat, oz))
    else:
        raise ValueError(f"unknown formation {formation!r}")
    return [(round(x, 1), round(y, 1), round(z, 1)) for x, y, z in out]
