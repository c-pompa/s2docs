"""Shared fixtures: a miniature GameTracking checkout written to tmp_path.

Kept tiny but structurally faithful to the real dumps so tests run offline in
milliseconds. `make_tree(build=..., extra=...)` lets tests create a second
build to exercise replacement, pruning and diffs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from s2docs.config import GAMES
from s2docs.db.connection import Database
from s2docs.ingest.loader import load_build

CONVARS = """\
mp_roundtime 5 (min: 0.1, max: 60, gamedll notify replicated release)
\tHow many minutes each round takes.

hostname "" (release)
\tHostname for server.

sv_cheats false (notify replicated release)
\tAllow cheats on server

sv_multiline 1 (gamedll cheat)
\tFirst line.
\tSecond line.

nodesc 0 (developmentonly)
\t<no description>
"""

COMMANDS = """\
bot_kick (gamedll release)
\tKick bots.

+lookatweapon (clientdll release)
\t<no description>
"""

SERVER_FGD = """\
// Dumped by https://github.com/ValveResourceFormat/DumpSource2

// CEntityInstance
@BaseClass = root
[
\tinput Kill(void)
\tinput AddOutput(string) // PVAL_STRING param
\tinput GetName(void) : "Get the name of the entity." // returns PVAL_ENTITY_NAME retval, Pulse node
]

// CBaseEntity
@BaseClass base(root) = CBaseEntity
[
\ttargetname(string) // FIELD_STRING m_iszName
\tMoveType(intchoices) : "MoveType" : : "" = // FIELD_UINT8 m_MoveType (MoveType_t)
\t[
\t\t0 : "MOVETYPE_NONE"
\t\t2 : "MOVETYPE_WALK"
\t]
\toutput OnUser1(string)
\t// component CBodyComponent
\t\t// m_pSceneNode (CGameSceneNode)
\t\t\torigin(vector) // FIELD_POSITION_VECTOR m_vecAbsOrigin
]

// CBaseTrigger
@PointClass base(CBaseEntity) = trigger
[
\tinput Enable(void)
\tinput Kill(void) : "Overridden kill"
\toutput OnStartTouch(void)
]

// CTriggerMultiple
@PointClass base(trigger) = trigger_multiple
[
\twait(float) // FIELD_FLOAT32 m_flWait
\toutput OnTrigger(void)
]
"""

CLIENT_FGD = """\
// C_BaseEntity
@PointClass = C_BaseEntity
[
\tinput GetAbsOrigin(void) : "The entity origin (absolute)"
]
"""

SCHEMA_BASE = """\
// MNetworkVarNames
class CBaseEntity : public CEntityInstance
{
\t// MNetworkEnable
\tint32 m_iHealth;
\tCUtlVector< thinkfunc_t > m_aThinkFunctions;
\tfloat32 m_flMax; // = 1
\tVector m_vecArr[3];
};
"""

SCHEMA_PAWN = """\
class CCSPlayerPawn : public CBaseEntity
{
\tuint16 m_nCharacterDefIndex;
};
"""

SCHEMA_ENUM = """\
enum CFuncMover::FollowStrategy_t : uint32_t
{
\tFOLLOW_FORWARD = 0,
\tFOLLOW_REVERSE = 1,
};
"""

STEAM_INF = """\
ClientVersion={build}
ServerVersion={build}
PatchVersion=1.0.{build}
ProductName=cs2
appID=730
SourceRevision=123
VersionDate=Oct 08 2026
VersionTime=14:54:05
"""

CORE_EVENTS = """\
"CoreEvents"
{
\t"player_death"\t\t// a player died
\t{
\t\t"userid"\t"short"\t// user ID who died
\t}
\t"server_spawn"\t\t// send once a server starts
\t{
\t\t"hostname"\t"string"\t// public host name
\t}
}
"""

MOD_EVENTS = """\
"ModEvents"
{
\t"player_death"
\t{
\t\t"userid"\t"player_controller"\t// player who died
\t\t"attacker"\t"player_controller"\t// player who killed
\t\t"headshot"\t"bool"
\t}
\t"round_start"
\t{}
}
"""

PULSE = """\
// scope_name = server.dll

// fingerprint = 1
CBaseTrigger_API
{
\t// m_FriendlyName = "Enable"
\t// hidden_in_tool
\tinput Enable(PVAL_EHANDLE:trigger _Target "trigger");

\t// m_FriendlyName = "On Trigger"
\toutput OnTrigger(PVAL_EHANDLE:trigger_multiple _Target "trigger_multiple");
}
"""

DTS = """\
/**
 * Module docs.
 */
declare module "cs_script/point_script"
{
    export const Instance: Domain;

    /**
     * The top level API.
     */
    class Domain {
        /** Log a message to the console. */
        Msg(text: any): void;
        /** Called when a player jumps off the ground. */
        OnPlayerJump(callback: (event: { player: CSPlayerPawn }) => void): void;
        /**
         * Trace a point along a line.
         * Multi-line doc.
         */
        TraceLine(trace: {
            start: Vector,
            end: Vector
        }): TraceResult;
        /** @deprecated This method will be removed in a future update */
        OnReload(callback: (memory: any) => void): void;
    }

    type Vector = { x: number, y: number, z: number };

    export enum CSRoundEndReason {
        TARGET_BOMBED = 1,
        TERRORISTS_WIN,
    }

    interface TraceResult {
        end: Vector;
        hitEntity?: Entity
    }

    export class Entity {
        /** Get the entity name. */
        GetEntityName(): string;
    }

    export class CSPlayerPawn extends Entity {
        /** Current health. */
        GetHealth(): number;
    }
}
"""

TSCONFIG = '{ "compilerOptions": { "target": "es2022" } }\n'


def write_tree(root: Path, *, build: int = 1000, extra_convar: str | None = None) -> Path:
    files = {
        "DumpSource2/convars.txt": CONVARS
        + (f"\n{extra_convar} 1 (release)\n\tAdded later.\n" if extra_convar else ""),
        "DumpSource2/commands.txt": COMMANDS,
        "DumpSource2/entities/server.fgd": SERVER_FGD,
        "DumpSource2/entities/client.fgd": CLIENT_FGD,
        "DumpSource2/schemas/server/CBaseEntity.h": SCHEMA_BASE,
        "DumpSource2/schemas/server/CCSPlayerPawn.h": SCHEMA_PAWN,
        "DumpSource2/schemas/server/CFuncMover__FollowStrategy_t.h": SCHEMA_ENUM,
        "DumpSource2/module_metadata/server/pulse_bindings.txt": PULSE,
        "game/csgo/steam.inf": STEAM_INF.format(build=build),
        "game/core/pak01_dir/resource/core.gameevents": CORE_EVENTS,
        "game/csgo/pak01_dir/resource/mod.gameevents": MOD_EVENTS,
        "content/csgo_addons/cs_script_demo/maps/scripts/point_script.d.ts": DTS,
        "content/csgo_addons/cs_script_demo/maps/scripts/tsconfig.json": TSCONFIG,
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """A database with one ingested cs2 build (1000)."""
    database = Database(tmp_path / "test.db")
    tree = write_tree(tmp_path / "src")
    with database.writer() as conn:
        load_build(conn, GAMES["cs2"], tree)
    return database


@pytest.fixture
def repo(db: Database):
    from s2docs.db.repository import Repository

    with db.reader() as conn:
        yield Repository(conn)
