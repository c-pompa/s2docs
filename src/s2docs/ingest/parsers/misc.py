"""Small parsers: steam.inf build info."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class BuildInfo:
    build_number: int
    patch_version: str | None
    source_revision: str | None
    version_date: str | None


def parse_steam_inf(text: str) -> BuildInfo:
    kv: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            kv[key.strip()] = value.strip()
    build = kv.get("ServerVersion") or kv.get("ClientVersion")
    if not build:
        raise ValueError("steam.inf missing ClientVersion/ServerVersion")
    date = " ".join(x for x in (kv.get("VersionDate"), kv.get("VersionTime")) if x) or None
    return BuildInfo(int(build), kv.get("PatchVersion"), kv.get("SourceRevision"), date)
