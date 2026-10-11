"""HTTP API routes. Thin layer: validate params, call Repository, return models."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from s2docs import models as m
from s2docs.build.validate_cfg import validate_cfg
from s2docs.build.validate_script import validate_cs_script
from s2docs.config import GAMES
from s2docs.db.connection import Database
from s2docs.db.repository import Repository
from s2docs.prompts import PROMPTS, render_prompt

router = APIRouter(prefix="/api")

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_repo(db: Annotated[Database, Depends(get_db)]) -> Iterator[Repository]:
    with db.reader() as conn:
        yield Repository(conn)


def valid_game(game: Annotated[str, Path(description=f"One of: {', '.join(GAMES)}")]) -> str:
    if game not in GAMES:
        raise HTTPException(404, f"unknown game {game!r}; known: {', '.join(GAMES)}")
    return game


Repo = Annotated[Repository, Depends(get_repo)]
Game = Annotated[str, Depends(valid_game)]


# ---------------------------------------------------------------- meta
@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/stats", response_model=m.Stats)
def stats(repo: Repo):
    return repo.stats()


@router.get("/games", response_model=list[m.GameInfo])
def games(repo: Repo):
    return repo.games()


@router.get("/games/{game}/builds", response_model=list[m.BuildInfo])
def builds(game: Game, repo: Repo):
    return repo.builds(game)


@router.get("/search", response_model=m.Page[m.SearchHit])
def search(
    repo: Repo,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    game: str | None = None,
    kind: Annotated[list[str] | None, Query()] = None,
    limit: Limit = 20,
    offset: Offset = 0,
):
    if game is not None:
        valid_game(game)
    return repo.search(q, game=game, kinds=kind, limit=limit, offset=offset)


# ------------------------------------------------------------- convars
@router.get("/games/{game}/convars", response_model=m.Page[m.ConVar])
def list_convars(
    game: Game,
    repo: Repo,
    q: str | None = None,
    kind: Literal["convar", "command"] | None = None,
    flag: str | None = None,
    build: int | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return repo.list_convars(game, q=q, kind=kind, flag=flag, build=build, limit=limit, offset=offset)


@router.get("/games/{game}/convar-flags")
def convar_flags(game: Game, repo: Repo) -> list[dict[str, str | int]]:
    return [{"flag": f, "count": n} for f, n in repo.convar_flags(game)]


@router.get("/games/{game}/convars/{name}", response_model=m.ConVar)
def get_convar(game: Game, name: str, repo: Repo):
    return repo.get_convar(game, name)


# ------------------------------------------------------------ entities
@router.get("/games/{game}/entities", response_model=m.Page[m.EntitySummary])
def list_entities(
    game: Game,
    repo: Repo,
    q: str | None = None,
    side: Literal["server", "client"] | None = "server",
    io: Annotated[str | None, Query(description="Only entities declaring this input/output")] = None,
    include_base: bool = False,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return repo.list_entities(game, q=q, side=side, io=io, include_base=include_base, limit=limit, offset=offset)


@router.get("/games/{game}/entities/{side}/{classname}", response_model=m.Entity)
def get_entity(game: Game, side: Literal["server", "client"], classname: str, repo: Repo, inherited: bool = True):
    return repo.get_entity(game, classname, side=side, inherited=inherited)


# -------------------------------------------------------------- schema
@router.get("/games/{game}/schema", response_model=m.Page[m.SchemaClassSummary])
def list_schema(
    game: Game,
    repo: Repo,
    q: str | None = None,
    module: str | None = None,
    kind: Literal["class", "enum"] | None = None,
    field: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return repo.list_schema(game, q=q, module=module, kind=kind, field=field, limit=limit, offset=offset)


@router.get("/games/{game}/schema-modules")
def schema_modules(game: Game, repo: Repo) -> list[dict[str, str | int]]:
    return [{"module": mod, "count": n} for mod, n in repo.schema_modules(game)]


@router.get("/games/{game}/schema/{name}", response_model=m.SchemaClass)
def get_schema_class(game: Game, name: str, repo: Repo, module: str | None = None, inherited: bool = True):
    return repo.get_schema_class(game, name, module=module, inherited=inherited)


# -------------------------------------------------------------- events
@router.get("/games/{game}/events", response_model=m.Page[m.GameEvent])
def list_events(game: Game, repo: Repo, q: str | None = None, limit: Limit = 100, offset: Offset = 0):
    return repo.list_events(game, q=q, limit=limit, offset=offset)


@router.get("/games/{game}/events/{name}", response_model=m.GameEvent)
def get_event(game: Game, name: str, repo: Repo):
    return repo.get_event(game, name)


# ---------------------------------------------------------- script api
@router.get("/games/{game}/script-api/types", response_model=list[m.ScriptSymbol])
def script_types(game: Game, repo: Repo):
    return repo.list_script_types(game)


@router.get("/games/{game}/script-api/types/{name}", response_model=m.ScriptType)
def script_type(game: Game, name: str, repo: Repo, inherited: bool = True):
    return repo.get_script_type(game, name, inherited=inherited)


@router.get("/games/{game}/script-api/symbols/{symbol}", response_model=list[m.ScriptSymbol])
def script_symbols(game: Game, symbol: str, repo: Repo):
    return repo.find_script_symbols(game, symbol)


@router.get("/games/{game}/script-api/files/{filename}", response_class=PlainTextResponse)
def script_file(game: Game, filename: Literal["point_script.d.ts", "tsconfig.json"], repo: Repo):
    return PlainTextResponse(
        repo.raw_file(game, f"cs_script/{filename}"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------ examples
@router.get("/games/{game}/examples", response_model=list[m.Example])
def list_examples(game: Game, repo: Repo, api: str | None = None):
    return repo.list_examples(game, api=api)


@router.get("/games/{game}/examples/{name}", response_model=m.Example)
def get_example(game: Game, name: str, repo: Repo, api: str = "cs_script"):
    return repo.get_example(game, name, api=api)


# ---------------------------------------------------------------- docs
@router.get("/docs", response_model=m.Page[m.Doc])
def list_docs(repo: Repo, game: str | None = None, q: str | None = None, limit: Limit = 50, offset: Offset = 0):
    if game is not None:
        valid_game(game)
    return repo.list_docs(game=game, q=q, limit=limit, offset=offset)


@router.get("/doc", response_model=m.Doc)
def get_doc(repo: Repo, key: Annotated[str, Query(description="Doc key (from search) or URL")]):
    return repo.get_doc(key)


# --------------------------------------------------------------- pulse
@router.get("/games/{game}/pulse", response_model=m.Page[m.PulseBinding])
def list_pulse(
    game: Game,
    repo: Repo,
    q: str | None = None,
    library: str | None = None,
    kind: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return repo.list_pulse(game, q=q, library=library, kind=kind, limit=limit, offset=offset)


@router.get("/games/{game}/pulse/{binding_id}", response_model=m.PulseBinding)
def get_pulse(game: Game, binding_id: int, repo: Repo):
    return repo.get_pulse(game, binding_id)


# ---------------------------------------------------------------- diff
@router.get("/games/{game}/diff", response_model=m.BuildDiff)
def diff(
    game: Game,
    repo: Repo,
    from_build: Annotated[int | None, Query(alias="from")] = None,
    to_build: Annotated[int | None, Query(alias="to")] = None,
):
    return repo.diff_builds(game, from_build, to_build)


# ----------------------------------------------------------- validation
class ScriptIn(BaseModel):
    code: str = Field(max_length=2_000_000)
    game: str = "cs2"
    filename: str = "script.js"


class CfgIn(BaseModel):
    text: str = Field(max_length=2_000_000)
    game: str = "cs2"
    context: Literal["server", "client", "any"] = "server"


@router.post("/validate/cs-script", response_model=m.ScriptValidation)
def validate_script(body: ScriptIn, repo: Repo):
    valid_game(body.game)
    return validate_cs_script(repo, body.code, game=body.game, filename=body.filename)


@router.post("/validate/cfg", response_model=m.CfgValidation)
def validate_config(body: CfgIn, repo: Repo):
    valid_game(body.game)
    return validate_cfg(repo, body.text, game=body.game, context=body.context)


# ------------------------------------------------------------- prompts
class PromptArgOut(BaseModel):
    name: str
    description: str
    required: bool


class PromptOut(BaseModel):
    name: str
    title: str
    description: str
    arguments: list[PromptArgOut]


class RenderedPrompt(BaseModel):
    name: str
    text: str


@router.get("/prompts", response_model=list[PromptOut])
def list_prompts():
    return [
        PromptOut(
            name=p.name,
            title=p.title,
            description=p.description,
            arguments=[PromptArgOut(name=a.name, description=a.description, required=a.required) for a in p.arguments],
        )
        for p in PROMPTS.values()
    ]


@router.post("/prompts/{name}/render", response_model=RenderedPrompt)
def render(name: str, repo: Repo, arguments: Annotated[dict[str, str], Body(embed=True)]):
    if name not in PROMPTS:
        raise HTTPException(404, f"unknown prompt {name!r}")
    return RenderedPrompt(name=name, text=render_prompt(repo, name, arguments))
