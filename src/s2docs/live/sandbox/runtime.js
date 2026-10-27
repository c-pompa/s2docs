// hermes_sandbox: runtime half of s2docs live actions (cs_script point_script), GENERATED into
// maps/scripts/hermes_sandbox.js by s2docs.live.sandbox_map — edit the sources, not the output.
// Streamed onto stock de_dust2 with `spawn_group_load hermes_sandbox`. Driven over netcon (sv_cheats 1):
//   hs_ping                                              -> [hs] pong <alive>
//   hs_spawn <id> <count> <formation> <spacing> <follow> -> [hs] spawned <k> <id> ... | [hs] error <why>
//   hs_clear <id|all>                                    -> [hs] cleared <k>
//   hs_model <models/....vmdl> [scale]                   -> [hs] precaching … then [hs] model … spawned name=…
// In-game chat lines starting with !h are echoed as [hs] chat <slot> <text> for the s2docs listener (P5).
import { Instance } from "cs_script/point_script";

/*FORMATION*/

const HEIGHT = /*HEIGHTS*/{};
/** @type {{id: string, ents: any[], follow: boolean, formation: string, spacing: number}[]} */
const groups = [];

function human() {
  for (const c of Instance.GetAllPlayerControllers()) {
    if (c && !c.IsBot()) { const p = c.GetPlayerPawn(); if (p && p.IsValid()) return p; }
  }
  return undefined;
}

// ---- the api object a bundled user module gets (s2docs.live.sandbox_user) -----------------------
// A user module never touches Instance: it is handed this object and nothing else. Chat subscribers are
// kept here and called from the runtime's ONE OnPlayerChat registration below — a second registration
// would overwrite the first, which is why the module cannot register its own.
/** @type {((event: any) => void)[]} */
const userChat = [];

function hsUserText(e) {
  if (e && typeof e === "object" && "message" in e) return String(e.message);
  return String(e);
}

function hsUserApi(tag, nonce) {
  return {
    tag,
    nonce,
    msg(text) { Instance.Msg("[user:" + tag + "] " + String(text)); },
    command(name, fn) {
      const n = String(name == null ? "" : name);
      if (!/^[a-z][a-z0-9_]{0,31}$/.test(n) || typeof fn !== "function") {
        Instance.Msg("[user:" + tag + "] error command needs (name, fn): " + n);
        return false;
      }
      Instance.RegisterCheatCommand("hu_" + n, (args) => {
        try { fn(String(args == null ? "" : args)); }
        catch (e) { Instance.Msg("[user:" + tag + "] error " + hsUserText(e)); }
      });
      return true;
    },
    onChat(fn) {
      if (typeof fn !== "function") { Instance.Msg("[user:" + tag + "] error onChat needs a function"); return false; }
      userChat.push(fn);
      return true;
    },
  };
}

function place(group, pawn, yawOverride) {
  const o = pawn.GetAbsOrigin(), yaw = yawOverride ?? pawn.GetEyeAngles().yaw;
  const pts = positions([o.x, o.y, o.z], yaw, group.ents.length, group.formation, group.spacing, HEIGHT[group.id] ?? 32);
  group.ents.forEach((e, i) => { if (e && e.IsValid()) e.Teleport({ position: { x: pts[i][0], y: pts[i][1], z: pts[i][2] } }); });
  return pts;
}

// Followers walk the player's PATH (trailPush/trailPoint/trailHeading, inlined from formation.mjs). Placing
// them from the VIEW yaw turned the line with the camera, so it was never visible in first person (live,
// pomps-pc 2026-10-25). A camera turn adds no breadcrumb, so turning around now shows them.
/** @type {number[][]} */
const trail = [];

function follow(group, pawn) {
  if (group.formation === "line" && trail.length >= 2) {
    group.ents.forEach((e, i) => {
      const p = trailPoint(trail, group.spacing * (i + 1));
      if (e && e.IsValid() && p) e.Teleport({ position: { x: p[0], y: p[1], z: p[2] } });
    });
    return;
  }
  place(group, pawn, trailHeading(trail) ?? undefined);
}

Instance.RegisterCheatCommand("hs_ping", () => {
  const alive = groups.reduce((n, g) => n + g.ents.filter(e => e && e.IsValid()).length, 0);
  Instance.Msg(`[hs] pong ${alive}`);
});

Instance.RegisterCheatCommand("hs_spawn", (args) => {
  const [id, countS, formation, spacingS, followS] = String(args).trim().split(/\s+/);
  /** @type {any} */
  const tpl = Instance.FindEntityByName(`hs_tpl_${id}`);
  const pawn = human();
  if (!tpl) { Instance.Msg(`[hs] error no template hs_tpl_${id}`); return; }
  if (!pawn) { Instance.Msg("[hs] error no human player"); return; }
  const count = Math.max(1, Math.min(16, Number(countS) || 1));
  /** @type {{id: string, ents: any[], follow: boolean, formation: string, spacing: number}} */
  const group = { id, ents: [], follow: followS === "1", formation: formation || "line", spacing: Number(spacingS) || 48 };
  for (let i = 0; i < count; i++) {
    const spawned = tpl.ForceSpawn(pawn.GetAbsOrigin(), { pitch: 0, yaw: pawn.GetEyeAngles().yaw, roll: 0 });
    if (spawned && spawned.length) group.ents.push(spawned[0]);
  }
  const pts = place(group, pawn);
  groups.push(group);
  Instance.Msg(`[hs] spawned ${group.ents.length} ${id} at ${pts.map(p => p.join(",")).join(";")}`);
});

Instance.RegisterCheatCommand("hs_clear", (args) => {
  const which = String(args).trim() || "all";
  let n = 0;
  for (let i = groups.length - 1; i >= 0; i--) {
    if (which !== "all" && groups[i].id !== which) continue;
    for (const e of groups[i].ents) if (e && e.IsValid()) { e.Remove(); n++; }
    groups.splice(i, 1);
  }
  Instance.Msg(`[hs] cleared ${n}`);
});

// hs_model <models/....vmdl> [scale] -- experimental runtime precache, then the generic template + SetModel.
/** @type {{vmdl: string, scale: number, pre: any, asked: number}[]} */
const pendingModels = [];
Instance.RegisterCheatCommand("hs_model", (args) => {
  const [vmdl, scaleS] = String(args).trim().split(/\s+/);
  if (!/^models\/[a-z0-9_\/]+\.vmdl$/.test(vmdl || "")) { Instance.Msg("[hs] error bad model path"); return; }
  const pre = Instance.Precache({ resources: [vmdl] });
  if (!pre) { Instance.Msg(`[hs] error precache refused ${vmdl}`); return; }
  pendingModels.push({ vmdl, scale: Number(scaleS) || 1, pre, asked: Instance.GetGameTime() });
  Instance.Msg(`[hs] precaching ${vmdl}`);
});

function pumpModels() {
  for (let i = pendingModels.length - 1; i >= 0; i--) {
    const p = pendingModels[i];
    if (p.pre.IsLoadedForAll()) {
      /** @type {any} */
      const tpl = Instance.FindEntityByName("hs_tpl_prop");
      const pawn = human();
      /** @type {any} */
      const ent = tpl && pawn ? (tpl.ForceSpawn(pawn.GetAbsOrigin(), { pitch: 0, yaw: pawn.GetEyeAngles().yaw, roll: 0 }) || [])[0] : undefined;
      if (ent) {
        ent.SetModel(p.vmdl);
        ent.SetModelScale(p.scale);
        /** @type {{id: string, ents: any[], follow: boolean, formation: string, spacing: number}} */
        // spacing 160 -> grid puts it 320 u ahead: at 64 (128 ahead) a runtime-loaded bus spawned on top of
        // the player, who was clipped inside it (live, pomps-pc 2026-10-25). Models of unknown size go far out.
        const g = { id: "prop", ents: [ent], follow: false, formation: "grid", spacing: 160 };
        place(g, pawn);
        groups.push(g);
        Instance.Msg(`[hs] model ${p.vmdl} spawned name=${ent.GetModelName()}`);
      } else {
        Instance.Msg(`[hs] error model ${p.vmdl} loaded but no template/player`);
      }
      pendingModels.splice(i, 1);
    } else if (Instance.GetGameTime() - p.asked > 20) {
      Instance.Msg(`[hs] error precache timeout ${p.vmdl} server=${p.pre.IsServerLoaded()}`);
      pendingModels.splice(i, 1);
    }
  }
}

Instance.OnPlayerChat((event) => {
  const text = String(event.text || "");
  if (/^!h(ermes)?\s/i.test(text)) Instance.Msg(`[hs] chat ${event.player ? event.player.GetPlayerSlot() : -1} ${text.replace(/^!h(ermes)?\s+/i, "")}`);
  // ... and the one fan-out a bundled user module's api.onChat lands in.
  for (const fn of userChat) {
    try { fn({ text, player: event.player }); }
    catch (e) { Instance.Msg(`[user] error ${hsUserText(e)}`); }
  }
});

Instance.SetThink(() => {
  pumpModels();
  const pawn = human();
  if (pawn) {
    const o = pawn.GetAbsOrigin();
    trailPush(trail, [o.x, o.y, o.z]);
    for (const g of groups) if (g.follow) follow(g, pawn);
  }
  Instance.SetNextThink(Instance.GetGameTime() + 0.1);
});
Instance.OnActivate(() => Instance.SetNextThink(Instance.GetGameTime()));

/*USER*/
