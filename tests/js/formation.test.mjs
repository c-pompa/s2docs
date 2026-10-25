// The cs_script copy of formation must agree with the Python one on the shared vectors, or a sandbox
// spawn and a console spawn put things in different places.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { positions } from '../../src/s2docs/live/sandbox/formation.mjs'

const vectors = JSON.parse(readFileSync(new URL('../fixtures/live/formation_vectors.json', import.meta.url)))
for (const v of vectors) {
  test(`${v.formation} ${v.count} yaw ${v.yaw}`, () => {
    const got = positions(v.origin, v.yaw, v.count, v.formation, v.spacing, v.height)
    assert.equal(got.length, v.expect.length)
    got.forEach((p, i) => p.forEach((c, k) => assert.ok(Math.abs(c - v.expect[i][k]) < 0.05, `${p} vs ${v.expect[i]}`)))
  })
}
