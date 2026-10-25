"""The closed vocabulary a user may ask the live game for. Never raw console text."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Team = Literal["t", "ct", "spectator"]
Formation = Literal["line", "circle", "stack", "grid"]
SPAWN_ID = r"^[a-z][a-z0-9_]{0,47}$"


class BotsIntent(BaseModel):
    action: Literal["bots"] = "bots"
    team_size: int | None = Field(
        default=None, ge=0, le=10, description="Players per side INCLUDING the user when `me` is t/ct ('5v5' = 5)"
    )
    t: int | None = Field(default=None, ge=0, le=10, description="Exact T bot count (instead of team_size)")
    ct: int | None = Field(default=None, ge=0, le=10, description="Exact CT bot count (instead of team_size)")
    me: Team | None = Field(default=None, description="Move the user to this side; omit to leave them")
    restart: bool = Field(default=True, description="End warmup and restart the round after the change")

    @model_validator(mode="after")
    def _one_way(self) -> BotsIntent:
        if self.team_size is None and self.t is None and self.ct is None:
            raise ValueError("give team_size, or t and/or ct")
        if self.team_size is not None and (self.t is not None or self.ct is not None):
            raise ValueError("team_size and t/ct are exclusive")
        return self

    def counts(self) -> tuple[int, int]:
        """(T bots, CT bots) to add."""
        if self.team_size is None:
            return self.t or 0, self.ct or 0
        t = self.team_size - (1 if self.me == "t" else 0)
        ct = self.team_size - (1 if self.me == "ct" else 0)
        return max(t, 0), max(ct, 0)


class SpawnIntent(BaseModel):
    action: Literal["spawn"] = "spawn"
    what: str = Field(pattern=SPAWN_ID, description="A spawnable id from GET /api/live/spawnables")
    count: int = Field(default=1, ge=1, le=16)
    formation: Formation = "line"
    spacing: float = Field(default=48.0, ge=8.0, le=512.0, description="Units between items")
    follow: bool = Field(default=False, description="Keep the formation trailing the user (needs the sandbox)")


class ClearIntent(BaseModel):
    action: Literal["clear"] = "clear"
    what: str | None = Field(default=None, pattern=SPAWN_ID, description="One spawnable id; omit = all spawned")


Intent = Annotated[BotsIntent | SpawnIntent | ClearIntent, Field(discriminator="action")]


class IntentEnvelope(BaseModel):
    intents: list[Intent] = Field(min_length=1, max_length=8)
    say: str | None = Field(default=None, max_length=120, description="Optional one-line reply shown in game chat")
