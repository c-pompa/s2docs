"""Pydantic models shared by the HTTP API, the MCP server and prompts."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

SearchKind = Literal[
    "convar",
    "command",
    "entity",
    "schema_class",
    "schema_enum",
    "event",
    "script",
    "pulse",
    "doc",
]
SEARCH_KINDS: tuple[str, ...] = SearchKind.__args__  # type: ignore[attr-defined]


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class GameInfo(BaseModel):
    id: str
    name: str
    app_id: int | None
    mod_dir: str | None
    current_build: int | None = None
    patch_version: str | None = None
    version_date: str | None = None
    script_api: str | None = None


class BuildInfo(BaseModel):
    build_number: int
    patch_version: str | None
    source_revision: str | None
    version_date: str | None
    source_commit: str | None
    ingested_at: str
    is_current: bool


class SearchHit(BaseModel):
    kind: str
    game: str
    key: str = Field(description="Lookup key for the detail endpoint/tool of this kind")
    name: str
    title: str
    snippet: str | None = None
    score: float


class ConVar(BaseModel):
    name: str
    kind: Literal["convar", "command"]
    default_value: str | None = None
    min_value: str | None = None
    max_value: str | None = None
    flags: list[str] = []
    description: str | None = None


class EntityMember(BaseModel):
    kind: Literal["keyvalue", "input", "output"]
    name: str
    type: str | None = None
    description: str | None = None
    notes: str | None = None
    component: str | None = None
    choices: list[tuple[str, str]] | None = None
    defined_in: str = Field(description="Entity class that declares this member")


class EntitySummary(BaseModel):
    classname: str
    side: str
    class_type: str
    cpp_class: str | None = None
    description: str | None = None
    bases: list[str] = []


class Entity(EntitySummary):
    inheritance: list[str] = Field(default=[], description="Ancestor chain, nearest first")
    members: list[EntityMember] = []


class SchemaField(BaseModel):
    name: str
    type: str | None = None
    value: str | None = Field(default=None, description="Enum value, or field default")
    metadata: list[str] = []
    defined_in: str


class SchemaClassSummary(BaseModel):
    name: str
    module: str
    kind: Literal["class", "enum"]
    bases: list[str] = []
    underlying_type: str | None = None
    field_count: int = 0


class SchemaClass(SchemaClassSummary):
    metadata: list[str] = []
    inheritance: list[str] = []
    derived: list[str] = Field(default=[], description="Direct subclasses")
    fields: list[SchemaField] = []


class GameEventField(BaseModel):
    name: str
    type: str
    description: str | None = None


class GameEvent(BaseModel):
    name: str
    source_file: str
    description: str | None = None
    fields: list[GameEventField] = []
    overrides: list[str] = Field(default=[], description="Lower-precedence files that also define this event")


class ScriptSymbol(BaseModel):
    api: str
    name: str
    qualified_name: str
    parent: str | None = None
    kind: str
    signature: str
    doc: str | None = None
    extends: str | None = None
    deprecated: bool = False


class ScriptType(ScriptSymbol):
    members: list[ScriptSymbol] = []
    inherited_from: list[str] = Field(default=[], description="Ancestor types whose members are also available")


class PulseBinding(BaseModel):
    id: int
    module: str
    library: str
    kind: str
    name: str
    signature: str
    friendly_name: str | None = None
    description: str | None = None
    metadata: dict = {}


class DiffEntry(BaseModel):
    name: str
    change: Literal["added", "removed", "changed"]
    before: str | None = None
    after: str | None = None


class BuildDiff(BaseModel):
    game: str
    from_build: int
    to_build: int
    sections: dict[str, list[DiffEntry]]


class Stats(BaseModel):
    schema_version: int
    games: list[GameInfo]
    counts: dict[str, dict[str, int]]
