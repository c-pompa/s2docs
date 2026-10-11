"""Render each SPA route in headless Chrome and summarise the resulting <main> DOM."""

import html
import re
import subprocess
import sys

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
ROUTES = [
    "cs2/search",
    "cs2/search?q=OnTrigger",
    "cs2/convars/mp_roundtime",
    "cs2/entities/server/trigger_multiple",
    "cs2/schema/CCSPlayerPawn?module=server",
    "cs2/events/player_death",
    "cs2/script",
    "cs2/script/CSPlayerPawn",
    "cs2/pulse?q=Enable",
    "cs2/diff",
    "cs2/prompts",
    "cs2/stats",
    "deadlock/convars?flag=cheat",
    "deadlock/schema/CCitadel_Ability_Baba_BenchRun_VData",
    "cs2/entities/server/trigger_multiple",
    "cs2/entities/server/env_fire",
    "cs2/examples",
    "cs2/examples/trace.js",
    "cs2/docs",
    "cs2/docs?key=s2wiki%3A%2FScripting%2FCounter-Strike%202%2Fgame-modes",
    "cs2/search?q=spawn%20point",
    "cs2/search?q=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E",
]
ROW = re.compile(r'<tr|class="hit"|class="card"')

for route in ROUTES:
    dom = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=5000", "--dump-dom", f"{BASE}/#/{route}"],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    main = re.search(r"<main[^>]*>(.*?)</main>", dom, re.S)
    body = main.group(1) if main else ""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    title = html.unescape(re.sub(r"<[^>]+>", "", h1.group(1))) if h1 else None
    err = re.search(r'class="err">(.*?)<', body)
    print(
        f"{route:55} h1={title!r:48} rows={len(ROW.findall(body)):4} "
        f"err={html.unescape(err.group(1)) if err else None} img_tag={'<img' in body}"
    )
