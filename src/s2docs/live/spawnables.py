"""What can be spawned. Stock entries are fixed here; catalog models join in P6 (resolve())."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Spawnable:
    id: str
    label: str
    template: str  # point_template targetname in maps/hermes_sandbox
    classname: str  # the template child's classname
    model: str | None  # vmdl for prop_dynamic children; None when the class brings its own model
    height: float  # units; 'stack' spacing
    console_ok: bool  # True only after spike S1 proved `ent_create <classname>` RENDERS on retail

    def as_dict(self) -> dict:
        return asdict(self)


# console_ok for chicken comes from spike S1 (Task 0.3): PASS 2026-10-25 on pomps-pc, CS2 1.41.9.2 —
# 0 chickens -> ent_create -> user saw only a chicken -> ent_remove_all ("Removed 1 chickens") -> gone.
# Change it only with a new recorded spike.
STOCK: tuple[Spawnable, ...] = (
    Spawnable("chicken", "Chicken", "hs_tpl_chicken", "chicken", None, 24.0, console_ok=True),
)


def stock() -> dict[str, Spawnable]:
    return {s.id: s for s in STOCK}
