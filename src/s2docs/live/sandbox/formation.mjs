// Mirror of s2docs.live.formation (same vectors, tests/fixtures/live/formation_vectors.json).
// sandbox_map.render_script inlines this file into maps/scripts/hermes_sandbox.js, so it must stay a
// plain function with no imports: cs_script runs one file, and `export` is not allowed there.
export function positions(origin, yawDeg, count, formation, spacing, height) {
  const [ox, oy, oz] = origin
  const yaw = (yawDeg * Math.PI) / 180
  const fx = Math.cos(yaw), fy = Math.sin(yaw), lx = -fy, ly = fx
  const out = []
  if (formation === 'line') for (let i = 1; i <= count; i++) out.push([ox - fx * spacing * i, oy - fy * spacing * i, oz])
  else if (formation === 'stack') { const bx = ox + fx * spacing * 2, by = oy + fy * spacing * 2; for (let i = 0; i < count; i++) out.push([bx, by, oz + height * i]) }
  else if (formation === 'circle') { const r = Math.max(spacing, (spacing * count) / (2 * Math.PI)); for (let i = 0; i < count; i++) { const a = (2 * Math.PI * i) / count; out.push([ox + r * Math.cos(a), oy + r * Math.sin(a), oz]) } }
  else if (formation === 'grid') { const side = Math.ceil(Math.sqrt(count)); for (let i = 0; i < count; i++) { const row = Math.floor(i / side), col = i % side, d = spacing * (2 + row), lat = spacing * ((side - 1) / 2 - col); out.push([ox + fx * d + lx * lat, oy + fy * d + ly * lat, oz]) } }
  else throw new Error(`unknown formation ${formation}`)
  return out.map(p => p.map(c => Math.round(c * 10) / 10))
}

// Following = walking the player's PATH. A follow group placed from the VIEW yaw turns with the camera and
// is never visible in first person (live on pomps-pc 2026-10-25), so the runtime keeps a breadcrumb trail of
// the player's feet and followers stand on it. `trail` is an array of [x, y, z], oldest first.
export function trailPush(trail, p, step = 8, max = 512) {
  const last = trail[trail.length - 1]
  if (!last || Math.hypot(p[0] - last[0], p[1] - last[1]) >= step) {
    trail.push([p[0], p[1], p[2]])
    if (trail.length > max) trail.splice(0, trail.length - max)
  }
  return trail
}

// The point `dist` units back along the trail from its newest breadcrumb (the oldest one when the trail is
// shorter than that), or null for an empty trail.
export function trailPoint(trail, dist) {
  if (!trail.length) return null
  let left = dist
  for (let i = trail.length - 1; i > 0; i--) {
    const a = trail[i], b = trail[i - 1]
    const seg = Math.hypot(a[0] - b[0], a[1] - b[1])
    if (seg >= left && seg > 0) {
      const t = left / seg
      return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]
    }
    left -= seg
  }
  return [...trail[0]]
}

// Direction of travel in degrees (yaw convention: 0 = +x, 90 = +y) over the last `back` units, or null
// before the player has moved.
export function trailHeading(trail, back = 64) {
  if (trail.length < 2) return null
  const a = trail[trail.length - 1], b = trailPoint(trail, back)
  if (!b || Math.hypot(a[0] - b[0], a[1] - b[1]) < 1) return null
  return (Math.atan2(a[1] - b[1], a[0] - b[0]) * 180) / Math.PI
}
