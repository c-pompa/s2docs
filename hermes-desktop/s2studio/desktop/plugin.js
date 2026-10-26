/**
 * s2studio — Hermes Desktop page "Source 2 Studio".
 * Catalog: stock spawnables + promoted s2docs assets. Runs: s2docs jobs with events and screenshots.
 * Live: a sentence -> plan preview -> Run in CS2, plus the host's live status and recent actions.
 * Backend: ../dashboard/plugin_api.py at /api/plugins/s2studio/, reached only through ctx.rest.
 *
 * Nothing here writes console text: the sentence goes to the intent model, s2docs validates the
 * envelope and compiles the allowlisted lines, and "verified" is only ever a readback result.
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'
import { host, haptic, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, useQuery, useQueryClient, Badge, Button, Textarea } from '@hermes/plugin-sdk'

const ID = 's2studio'
const PATH = '/s2studio'
const HOST_KEY = 'host'
const EXAMPLES = [
  'give me 4 chickens in a straight line following me',
  'provide bots for 5v5 including myself on T',
  'stack 3 cars on top of each other in front of me',
  'remove everything you spawned'
]
// Badge variants that exist in the SDK: default, muted, success, warn, destructive, outline, solid
const STATUS_TONE = { verified: 'success', passed: 'success', sent: 'muted', inconclusive: 'muted', running: 'default', queued: 'muted', refused: 'warn', failed: 'destructive', error: 'destructive', cancelled: 'muted' }
let api = null

const muted = text => jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: text })
const chip = (text, tone) => jsx(Badge, { variant: STATUS_TONE[tone] || 'outline', children: text })
const Section = ({ title, children }) => jsxs('section', { className: 'space-y-2 rounded-md border border-(--ui-stroke-secondary) p-3', children: [
  jsx('h3', { className: 'text-sm font-semibold', children: title }), children
] })

function CatalogTab({ onSpawn }) {
  const q = useQuery({ queryKey: [ID, 'catalog'], queryFn: () => api.rest('/catalog'), refetchInterval: 60_000 })
  if (q.isLoading) return muted('Loading catalog…')
  if (q.isError) return muted(`Catalog unavailable: ${q.error?.message || 'the s2docs server did not answer'}`)
  const { spawnables = [], assets = [] } = q.data || {}
  return jsxs('div', { className: 'space-y-3', children: [
    jsx(Section, { title: `Stock spawnables (${spawnables.length})`, children: jsx('div', { className: 'grid grid-cols-3 gap-2', children:
      spawnables.map(s => jsxs('div', { key: s.id, className: 'rounded border border-(--ui-stroke-secondary) p-2 text-xs', children: [
        jsx('div', { className: 'font-medium', children: s.label }),
        muted(`${s.id} · ${s.console_ok ? 'console or sandbox' : 'needs sandbox'}`),
        jsx(Button, { size: 'xs', variant: 'outline', className: 'mt-1', onClick: () => onSpawn(`spawn one ${s.label.toLowerCase()} in front of me`), children: 'Spawn in CS2' })
      ] }))
    }) }),
    jsx(Section, { title: `Your models (${assets.length})`, children: assets.length
      ? jsx('div', { className: 'grid grid-cols-3 gap-2', children: assets.map(a => jsxs('div', { key: a.id, className: 'rounded border border-(--ui-stroke-secondary) p-2 text-xs', children: [
        jsx('div', { className: 'font-medium', children: a.name }),
        muted(`${a.kind} · ${a.slug} · ${a.licence}`),
        a.prompt ? muted(`"${a.prompt}"`) : null,
        a.frame_job_id ? jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => host.navigate(`${PATH}?run=${a.frame_job_id}`), children: 'Proof run' }) : null
      ] })) })
      : muted('Nothing promoted yet. A model enters the catalog after a succeeded build AND an in-game frame with a non-fail verdict.') })
  ] })
}

function RunDetail({ id }) {
  const q = useQuery({ queryKey: [ID, 'run', id], queryFn: () => api.rest(`/runs/${id}`), refetchInterval: 15_000 })
  const [shots, setShots] = useState({})
  const [why, setWhy] = useState(null)
  if (q.isError) return muted(`Run ${id} unavailable: ${q.error?.message || 'the s2docs server did not answer'}`)
  if (!q.data) return muted('Loading run…')
  const { job, screenshots = [] } = q.data
  const load = async s => {
    try {
      const out = await api.rest(`/artifact/${id}/${encodeURIComponent(s.name)}`)
      setShots(p => ({ ...p, [s.name]: out.data_url }))
    } catch (e) { setWhy(String(e?.message || e)) }
  }
  return jsxs('div', { className: 'space-y-2', children: [
    jsxs('div', { className: 'flex items-center gap-2', children: [chip(job.status, job.status), jsx('span', { className: 'font-medium', children: job.title || job.id }),
      job.web_url ? jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => api.os.openExternal(job.web_url), children: 'Open in s2docs' }) : null] }),
    job.params?.purpose ? muted(`Purpose: ${job.params.purpose}`) : null,
    job.params?.expected ? muted(`Expected: ${job.params.expected}`) : null,
    job.summary?.what ? muted(`Close-out: ${job.summary.what}`) : null,
    jsx('div', { className: 'max-h-64 overflow-y-auto text-xs font-mono', children: (job.events || []).map((e, i) => jsx('div', { key: i, children: `${e.created_at?.slice(11, 19) || ''} ${e.level} ${e.phase || ''} ${e.message || ''}` })) }),
    jsx('div', { className: 'grid grid-cols-3 gap-2', children: screenshots.map(s => jsxs('div', { key: s.name, className: 'text-xs', children: [
      shots[s.name]
        ? jsx('img', { src: shots[s.name], alt: s.name, className: 'w-full rounded' })
        : jsx(Button, { size: 'xs', variant: 'outline', onClick: () => load(s), children: `Show ${s.step || ''} ${s.reason || ''}` }),
      muted(`${s.name}${s.verdict ? ` · ${s.verdict}` : ''}`)
    ] })) }),
    why ? muted(why) : null
  ] })
}

function RunsTab({ initial }) {
  const [open, setOpen] = useState(initial || null)
  const q = useQuery({ queryKey: [ID, 'runs'], queryFn: () => api.rest('/runs?limit=30'), refetchInterval: 30_000 })
  if (q.isError) return muted('Runs unavailable: the s2docs server did not answer.')
  return jsxs('div', { className: 'grid grid-cols-[22rem_1fr] gap-3', children: [
    jsx('div', { className: 'space-y-1', children: (q.data || []).map(j => jsxs('button', { key: j.id, className: 'flex w-full items-center gap-2 rounded px-2 py-1 text-left text-xs hover:bg-(--ui-fill-secondary)', onClick: () => setOpen(j.id), children: [
      chip(j.status, j.status), jsx('span', { className: 'truncate', children: j.title || j.id })] })) }),
    open ? jsx(RunDetail, { id: open }) : muted('Pick a run to see its steps, verdicts and screenshots.')
  ] })
}

function LiveTab({ draft, setDraft }) {
  const qc = useQueryClient()
  const [hostId, setHostId] = useState(() => api.storage.get(HOST_KEY, 'pomps-pc'))
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState(null)
  const [note, setNote] = useState(null)
  const status = useQuery({ queryKey: [ID, 'status', hostId], queryFn: () => api.rest(`/live/status?host_id=${encodeURIComponent(hostId)}`, { timeoutMs: 25_000 }), refetchInterval: 20_000, retry: 0 })
  const actions = useQuery({ queryKey: [ID, 'actions', hostId], queryFn: () => api.rest(`/live/actions?host_id=${encodeURIComponent(hostId)}`), refetchInterval: 10_000 })
  const pick = h => { setHostId(h); api.storage.set(HOST_KEY, h) }
  const plan = async () => {
    setBusy(true); setNote(null); setPreview(null)
    try {
      const out = await api.rest('/intent', { method: 'POST', body: { text: draft, host_id: hostId }, timeoutMs: 90_000 })
      if (out.unsupported) setNote({ bad: true, text: `Can't do that yet: ${out.unsupported}` })
      else setPreview(out)
    } catch (e) { setNote({ bad: true, text: String(e?.message || e) }) } finally { setBusy(false) }
  }
  const run = async () => {
    setBusy(true); haptic('tap')
    try {
      const out = await api.rest('/act', { method: 'POST', body: { host_id: hostId, envelope: preview.envelope, text: draft }, timeoutMs: 120_000 })
      setNote({ bad: out.status !== 'verified' && out.status !== 'sent', text: `${out.status}: ${out.detail || ''}` })
      qc.invalidateQueries({ queryKey: [ID, 'actions', hostId] })
    } catch (e) { setNote({ bad: true, text: String(e?.message || e) }) } finally { setBusy(false) }
  }
  const st = status.data
  return jsxs('div', { className: 'space-y-3', children: [
    jsxs('div', { className: 'flex flex-wrap items-center gap-2 text-xs', children: [
      jsx('span', { children: 'Host' }),
      ...['pomps-pc', 'desktop-pc'].map(h => jsx(Button, { key: h, size: 'xs', variant: h === hostId ? 'outline' : 'ghost', onClick: () => pick(h), children: h })),
      st ? chip(st.reachable ? 'CS2 up' : 'CS2 not reachable', st.reachable ? 'verified' : 'failed') : status.isError ? chip('no netcon address / server error', 'failed') : chip('checking…', 'sent'),
      st?.map ? chip(`map ${st.map}`, 'sent') : null,
      st?.cs2_build ? chip(`CS2 ${st.cs2_build}`, 'sent') : null,
      st ? chip(st.sandbox ? 'sandbox loaded' : 'no sandbox', st.sandbox ? 'verified' : 'refused') : null
    ] }),
    status.isError ? muted(String(status.error?.message || status.error)) : null,
    jsx(Textarea, { value: draft, rows: 3, placeholder: 'What should happen in the game?', onChange: e => setDraft(e.target.value) }),
    jsx('div', { className: 'flex flex-wrap gap-1', children: EXAMPLES.map(x => jsx(Button, { key: x, size: 'xs', variant: 'ghost', onClick: () => setDraft(x), children: x })) }),
    jsxs('div', { className: 'flex gap-2', children: [
      jsx(Button, { disabled: busy || !draft.trim(), onClick: plan, children: busy && !preview ? 'Planning…' : 'Plan' }),
      jsx(Button, { variant: 'outline', disabled: busy || !preview || !st?.reachable, onClick: run, children: busy && preview ? 'Running…' : 'Run in CS2' })
    ] }),
    preview ? jsx(Section, { title: 'Plan', children: jsxs('div', { className: 'space-y-1 text-xs', children: [
      ...(preview.plan.summary || []).map((s, i) => jsx('div', { key: `s${i}`, children: s })),
      preview.plan.needs_sandbox && !st?.sandbox ? muted('Needs the sandbox: it will be refused until the sandbox is loaded (P4).') : null,
      ...(preview.plan.warnings || []).map((w, i) => muted(`warning: ${w}`)),
      jsx('pre', { className: 'whitespace-pre-wrap font-mono text-(--ui-text-quaternary)', children: preview.plan.lines.join('\n') })
    ] }) }) : null,
    note ? jsx('div', { className: 'text-xs', style: { color: note.bad ? 'var(--ui-danger, #c62828)' : 'var(--ui-success, #2e7d32)' }, children: note.text }) : null,
    jsx(Section, { title: 'Recent actions', children: jsx('div', { className: 'space-y-1 text-xs', children: (actions.data || []).map(a => jsxs('div', { key: a.id, className: 'flex gap-2', children: [
      chip(a.status, a.status), jsx('span', { className: 'text-(--ui-text-quaternary)', children: a.created_at?.slice(11, 19) }),
      jsx('span', { className: 'truncate', children: a.text || a.detail })] })) }) })
  ] })
}

function Page() {
  const search = (window.location.hash.split('?')[1]) || window.location.search.slice(1)
  const params = new URLSearchParams(search || '')
  const [tab, setTab] = useState(params.get('run') ? 'runs' : 'live')
  const [draft, setDraft] = useState('')
  const spawn = text => { setDraft(text); setTab('live') }
  return jsxs('div', { className: 'mx-auto max-w-6xl space-y-3 p-4', children: [
    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx('h2', { className: 'mr-4 text-lg font-semibold', children: 'Source 2 Studio' }),
      ...[['live', 'Live'], ['catalog', 'Catalog'], ['runs', 'Test runs']].map(([k, label]) => jsx(Button, { key: k, size: 'sm', variant: tab === k ? 'outline' : 'ghost', onClick: () => setTab(k), children: label }))
    ] }),
    tab === 'live' ? jsx(LiveTab, { draft, setDraft }) : tab === 'catalog' ? jsx(CatalogTab, { onSpawn: spawn }) : jsx(RunsTab, { initial: params.get('run') })
  ] })
}

export default {
  id: ID, // must match the folder name
  name: 'Source 2 Studio',
  description: 'The s2docs catalog, test runs with screenshots, and live CS2 actions from a sentence.',
  register(ctx) {
    api = ctx
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx('div', { className: 'h-full overflow-y-auto', children: jsx(Page, {}) }) })
    ctx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, order: 62, data: { codicon: 'game', label: 'Source 2 Studio', path: PATH } })
    ctx.register({ id: 'open', area: PALETTE_AREA, data: { id: `${ID}.open`, label: 'Source 2 Studio: open', keywords: ['cs2', 'source2', 'catalog', 'bots', 'chickens', 's2docs'], run: () => { haptic('tap'); host.navigate(PATH) } } })
  }
}
