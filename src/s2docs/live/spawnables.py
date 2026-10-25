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
#
# The vehicles are VPK-listed 2026-10-25 (scripts/vpk_ls.py pak01_dir.vpk). dust2's own hatchback/taxi/flatbed
# ship as 4-part models (body/glass/interior/wheels), so those would spawn as one decapitated piece: these are
# single-model vehicles instead, and `PointTemplate.ForceSpawn hs_tpl_car_*` brings them in as ONE entity.
# console_ok stays False — only the sandbox template precaches them; ent_create rendered nothing on retail.
STOCK: tuple[Spawnable, ...] = (
    Spawnable("chicken", "Chicken", "hs_tpl_chicken", "chicken", None, 24.0, console_ok=True),
    Spawnable("car_wreck", "Dust-car wreck", "hs_tpl_car_wreck", "prop_dynamic",
              "models/props/de_dust/hr_dust/dust_vehicles/dust_car_wreck_body.vmdl",
              64.0,  # UNMEASURED - fix from the Task 4.8 live stack readback
              console_ok=False),
    Spawnable("car_hatch", "Hatchback", "hs_tpl_car_hatch", "prop_dynamic",
              "models/props_vehicles/cara_82hatchback.vmdl",
              64.0,  # UNMEASURED - fix from the Task 4.8 live stack readback
              console_ok=False),
    Spawnable("car_van", "Van", "hs_tpl_car_van", "prop_dynamic",
              "models/props_vehicles/van.vmdl",
              96.0,  # UNMEASURED - fix from the Task 4.8 live stack readback
              console_ok=False),
    # Spike S2 (Task 4.9): the placeholder a runtime-precached model is swapped ONTO (hs_model + SetModel).
    # chicken_roasted.vmdl_c is in pak01_dir.vpk (VPK-listed 2026-10-25) and doubles as the TELL: a roasted
    # chicken where the requested model should be means SetModel did not take.
    Spawnable("prop", "Generic prop (placeholder)", "hs_tpl_prop", "prop_dynamic",
              "models/chicken/chicken_roasted.vmdl", 32.0, console_ok=False),
)


def stock() -> dict[str, Spawnable]:
    return {s.id: s for s in STOCK}
