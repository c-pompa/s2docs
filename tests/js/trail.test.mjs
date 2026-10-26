// Following = walking the player's PATH, not hanging off the camera. A follow group placed from the VIEW
// yaw turns with the camera and is never visible in first person (live on pomps-pc 2026-10-25: user saw
// nothing behind them, but a line in third person). These are the pure helpers the runtime uses.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { trailPush, trailPoint, trailHeading } from '../../src/s2docs/live/sandbox/formation.mjs'

const walk = (from, to, step = 4) => {
  const t = []
  const n = Math.round(Math.hypot(to[0] - from[0], to[1] - from[1]) / step)
  for (let i = 0; i <= n; i++) trailPush(t, [from[0] + ((to[0] - from[0]) * i) / n, from[1] + ((to[1] - from[1]) * i) / n, 0])
  return t
}
const near = (a, b, tol = 0.6) => a.forEach((c, k) => assert.ok(Math.abs(c - b[k]) < tol, `${a} vs ${b}`))

test('trailPush keeps one breadcrumb per 8 units moved and caps the length', () => {
  const t = []
  trailPush(t, [0, 0, 0]); trailPush(t, [3, 0, 0]); trailPush(t, [7.9, 0, 0])
  assert.equal(t.length, 1, 'standing still or jittering adds nothing')
  trailPush(t, [8, 0, 0])
  assert.equal(t.length, 2)
  const long = []
  for (let i = 0; i < 2000; i++) trailPush(long, [i * 8, 0, 0], 8, 512)
  assert.equal(long.length, 512)
  assert.deepEqual(long[511], [15992, 0, 0])
})

test('trailPoint walks back along the path, around corners', () => {
  const t = walk([0, 0], [200, 0])
  near(trailPoint(t, 48), [152, 0, 0])
  // turn the corner: 200 along +x, then 96 along +y (8-unit breadcrumbs land exactly on 96); 150 back from
  // the end is 96 down the +y leg, then 54 back along +x
  const corner = walk([0, 0], [200, 0]).concat(walk([200, 0], [200, 96]).slice(1))
  near(trailPoint(corner, 150), [146, 0, 0], 0.6)
})

test('trailPoint past the start returns the oldest breadcrumb, an empty trail returns null', () => {
  const t = walk([0, 0], [40, 0])
  near(trailPoint(t, 1000), [0, 0, 0])
  assert.equal(trailPoint([], 10), null)
})

test('trailHeading is the direction of travel, null before the player moves', () => {
  near([trailHeading(walk([0, 0], [200, 0]))], [0], 0.5)
  near([trailHeading(walk([0, 0], [0, 200]))], [90], 0.5)
  near([trailHeading(walk([0, 0], [-200, 0]))], [180], 0.5)
  assert.equal(trailHeading([[0, 0, 0]]), null)
})

test('followers do not depend on the view: the same trail gives the same points however the camera turns', () => {
  const t = walk([0, 0], [300, 0])
  const before = [1, 2, 3, 4].map(i => trailPoint(t, 48 * i))
  // a camera turn adds no breadcrumb (no movement), so nothing can move
  trailPush(t, [...t[t.length - 1]])
  assert.deepEqual([1, 2, 3, 4].map(i => trailPoint(t, 48 * i)), before)
})
