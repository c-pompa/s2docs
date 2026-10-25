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
