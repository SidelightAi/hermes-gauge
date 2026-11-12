// hermes-gauge — desktop half: composer chip + detail pane
import { jsx } from 'react/jsx-runtime'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { atom, useValue } from '@hermes/plugin-sdk'

// Composer surface is overflow:hidden and pinned to the bottom of the window,
// so a panel positioned inside the chip would be clipped or leave the screen.
// popover=auto lifts this node into the top layer (not a portal) and dismisses
// it on an outside press or Escape. JS then pins it to the chip.
const POPOVER_OK = typeof HTMLElement !== 'undefined' && typeof HTMLElement.prototype.showPopover === 'function'

function ago(seconds) {
  const hours = Math.max(1, Math.round((seconds || 0) / 3600))
  if (hours < 48) return hours + ' h ago'
  return Math.round(hours / 24) + ' d ago'
}

function chipLabel(d) {
  if (!d || d.state === 'NO DATA') return 'gauge · no chat yet'
  const note = d.last_compaction
  if (d.state === 'SUMMARIZED') return (note && note.automatic ? 'auto-summary' : 'summarized') + ' · measuring'
  if (d.pct == null) return 'gauge · offline'
  const idle = d.idle || d.age_s > 3600 ? ' · idle' : ''
  // A summary Hermes ran on its own is the one thing the percentage cannot explain.
  const badge = note && note.recent ? (note.automatic ? ' · auto-summary' : ' · summarized') : ''
  return Math.round(d.pct) + '% ' + (d.state || '') + badge + idle
}

// "Context summarized automatically at 03:05 — 45% → 31%". Empty when there is nothing to say.
function compactionLine(c) {
  if (!c || typeof c.at !== 'number') return ''
  const when = formatClock(c.at * 1000).slice(0, 5)
  const lead = (c.automatic ? 'Context summarized automatically at ' : 'Context summarized at ') + when
  const before = typeof c.before_pct === 'number'
    ? formatPct(c.before_pct)
    : (typeof c.before_tokens === 'number' ? formatCompact(c.before_tokens) + ' tok' : null)
  if (before == null) return lead
  const after = typeof c.after_pct === 'number'
    ? (c.recent ? '' : 'now ') + formatPct(c.after_pct)
    : 'measuring…'
  return lead + ' — ' + before + ' → ' + after
}

function compactionNote(d) {
  const line = compactionLine(d && d.last_compaction)
  if (!line) return null
  return jsx('p', {
    key: 'compaction',
    style: {
      margin: '0 0 8px',
      color: 'var(--ui-text-secondary, var(--ui-text-quaternary))',
      font: '12px/1.4 -apple-system, system-ui, sans-serif'
    },
    children: line
  })
}

const BASIS_LABELS = {
  provider: 'provider token count',
  transcript: 'full transcript',
  estimate: 'estimate from replies'
}

function chipTitle(d) {
  if (!d || d.state === 'NO DATA') return 'Send your first message. The gauge fills in after that.'
  if (d.state === 'SUMMARIZED') return (d.name || 'This conversation') + ' was just summarized. Measuring again.'
  if (d.pct == null) return 'Context Gauge is offline.'
  const name = d.name || 'This conversation'
  const used = d.age_s > 3600 ? ', last used ' + ago(d.age_s) : ''
  const step = d.next ? ' — ' + d.next : ''
  const summary = d.last_compaction && d.last_compaction.recent ? '. ' + compactionLine(d.last_compaction) : ''
  return name + ' is ' + Math.round(d.pct) + '% full' + used + step + summary
}

const pctAtom = atom({ pct: null, state: '…', color: 'var(--ui-text-quaternary)', chars_in: 0, window_chars: 0, model: '', tools_chars: 0, skills_chars: 0, conversation_chars: 0, turns: 0 })
const openAtom = atom(null)
const errAtom = atom('')
const historyAtom = atom([])
let samples = []

function chipStyle(d, hot) {
  const unknown = d.pct == null
  return {
    appearance: 'none',
    WebkitAppearance: 'none',
    background: hot ? 'var(--chrome-action-hover, rgba(255,255,255,0.08))' : 'transparent',
    border: 'none',
    borderRadius: 6,
    cursor: 'pointer',
    color: unknown || d.idle || d.age_s > 3600 ? 'var(--ui-text-quaternary)' : d.color,
    opacity: (unknown || d.idle || d.age_s > 3600) && !hot ? 0.55 : 1,
    font: '600 12px/1 -apple-system, system-ui, sans-serif',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    padding: '2px 8px',
    height: 'var(--composer-control-size, 28px)',
    maxWidth: '100%',
    whiteSpace: 'nowrap'
  }
}

// /check returns one row, so the panel remembers the three readings before the current one.
function noteHistory(data) {
  const entry = {
    pct: data.pct,
    state: data.state || '',
    color: data.color || 'var(--ui-text-quaternary)',
    at: Date.now()
  }
  const last = samples[samples.length - 1]
  if (last && last.state === entry.state) return
  samples = samples.concat([entry]).slice(-4)
  historyAtom.set(samples.slice(0, -1).slice(-3).reverse())
}

async function refresh(ctx) {
  try {
    const sessionId = ctx && (ctx.sessionId || ctx.session_id || (ctx.session && ctx.session.id))
    const path = sessionId ? '/check?session_id=' + encodeURIComponent(sessionId) : '/check'
    const data = await ctx.rest(path, { method: 'GET' })
    errAtom.set('')
    if (data && (data.state || data.pct != null)) {
      pctAtom.set(data)
      if (typeof data.pct === 'number') noteHistory(data)
    }
  } catch (e) { errAtom.set(String(e && e.message || e).slice(0, 240)) }
}

function ChipMark({ d }) {
  if (d.pct == null) {
    return jsx('span', {
      'aria-hidden': true,
      style: {
        width: 14, height: 14, borderRadius: '50%',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        border: '1px solid var(--ui-text-quaternary)',
        color: 'var(--ui-text-quaternary)',
        font: '700 10px/1 -apple-system, system-ui, sans-serif',
        flexShrink: 0
      },
      children: '?'
    })
  }
  return jsx('span', {
    'aria-hidden': true,
    style: {
      width: 10, height: 10, borderRadius: '50%', flexShrink: 0,
      border: `2px solid ${d.color}`,
      borderTopColor: 'transparent', display: 'inline-block'
    }
  })
}

function samePlace(a, b) {
  if (!a || !b) return false
  return a.top === b.top && a.left === b.left
}

function measure(panel, anchor) {
  if (!panel || !anchor || typeof anchor.getBoundingClientRect !== 'function') return null
  const margin = 8
  const gap = 6
  const anchorRect = anchor.getBoundingClientRect()
  const width = panel.offsetWidth || 268
  const height = panel.offsetHeight || panel.scrollHeight || 180
  if (!width) return null
  let left = anchorRect.right - width
  if (left < margin) left = anchorRect.left
  if (left + width > window.innerWidth - margin) left = window.innerWidth - width - margin
  if (left < margin) left = margin
  const below = anchorRect.bottom + gap
  const above = anchorRect.top - height - gap
  const belowFits = below + height <= window.innerHeight - margin
  let top = below
  if (!belowFits && above >= margin) top = above
  else if (!belowFits) top = Math.max(margin, window.innerHeight - height - margin)
  return { top: Math.round(top), left: Math.round(left) }
}

function panelStyle(place, wide) {
  const anchored = !!(place && typeof place.top === 'number')
  return {
    position: 'fixed',
    margin: 0,
    top: anchored ? place.top + 'px' : '0px',
    left: anchored ? place.left + 'px' : '0px',
    bottom: 'auto',
    right: 'auto',
    width: wide ? 340 : 268,
    maxWidth: 'calc(100vw - 16px)',
    maxHeight: '70vh',
    overflow: 'auto',
    boxSizing: 'border-box',
    padding: '12px 12px 10px',
    borderRadius: 8,
    border: '1px solid var(--ui-stroke-secondary, rgba(255,255,255,0.12))',
    background: 'var(--ui-bg-elevated, #1c1e22)',
    color: 'var(--ui-text-primary, #eceef1)',
    boxShadow: '0 8px 24px rgba(0, 0, 0, 0.35)',
    zIndex: 1000,
    visibility: anchored ? 'visible' : 'hidden'
  }
}

function formatCount(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return '—'
  return n.toLocaleString()
}

function formatClock(at) {
  const date = new Date(at)
  if (isNaN(date.getTime())) return ''
  const h = String(date.getHours()).padStart(2, '0')
  const m = String(date.getMinutes()).padStart(2, '0')
  const s = String(date.getSeconds()).padStart(2, '0')
  return h + ':' + m + ':' + s
}

function formatCompact(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n < 0) return '—'
  if (n < 1000) return String(Math.round(n))
  if (n < 1000000) return String(Math.round(n / 1000)) + 'K'
  const millions = n / 1000000
  const digits = millions >= 10 ? 0 : 2
  return millions.toFixed(digits).replace(/\.?0+$/, '') + 'M'
}

function formatPct(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  const rounded = Math.round(n * 10) / 10
  if (Math.abs(rounded - Math.round(rounded)) < 1e-9) return Math.round(rounded) + '%'
  return rounded.toFixed(1) + '%'
}

function formatFactor(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return ''
  if (Math.abs(n - Math.round(n)) < 1e-9) return String(Math.round(n))
  return n.toFixed(2)
}

// Older /check payloads omit effective_window_tokens. The chip, ring, and
// window row then stay on pct and window_chars.
function hasEffective(d) {
  return !!(d && typeof d.effective_window_tokens === 'number' && typeof d.tokens_in === 'number')
}

function accountingLine(d) {
  if (!hasEffective(d)) return ''
  const nominal = typeof d.pct_nominal === 'number' ? d.pct_nominal : null
  const effective = typeof d.pct_effective === 'number' ? d.pct_effective : d.pct
  return formatCompact(d.tokens_in) + ' tok — ' + formatPct(nominal) + ' nominal · ' + formatPct(effective) + ' effective (useful window ~' + formatCompact(d.effective_window_tokens) + ')'
}

function familyLine(d) {
  if (!d) return ''
  const family = d.model_family ? String(d.model_family) : ''
  const factor = formatFactor(d.degradation_factor)
  if (family && factor) return 'family ' + family + ' · factor ' + factor
  if (family) return 'family ' + family
  if (factor) return 'factor ' + factor
  return ''
}

function accountingBlock(d) {
  const line = accountingLine(d)
  if (!line) return null
  const family = familyLine(d)
  const children = [
    jsx('p', {
      key: 'line',
      style: {
        margin: 0,
        color: 'var(--ui-text-secondary, var(--ui-text-quaternary))',
        font: '11px/1.45 Menlo, ui-monospace, monospace'
      },
      children: line
    })
  ]
  if (family) {
    children.push(jsx('p', {
      key: 'family',
      style: {
        margin: '4px 0 0',
        color: 'var(--ui-text-quaternary)',
        font: '11px/1.4 Menlo, ui-monospace, monospace'
      },
      children: family
    }))
  }
  return jsx('div', { key: 'account', style: { marginBottom: 8 }, children: children })
}

function GaugeRing({ pct, color }) {
  const size = 52
  const stroke = 4
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const known = typeof pct === 'number' && isFinite(pct)
  const clamped = known ? Math.max(0, Math.min(pct, 100)) : 0
  const dash = c * (clamped / 100)
  const nodes = [
    jsx('circle', {
      key: 'track',
      cx: size / 2,
      cy: size / 2,
      r: r,
      fill: 'none',
      stroke: 'var(--ui-stroke-secondary, rgba(255,255,255,0.14))',
      strokeWidth: stroke
    })
  ]
  if (known && clamped > 0) {
    nodes.push(jsx('circle', {
      key: 'value',
      cx: size / 2,
      cy: size / 2,
      r: r,
      fill: 'none',
      stroke: color,
      strokeWidth: stroke,
      strokeLinecap: 'round',
      strokeDasharray: clamped >= 100 ? `${c} 0` : `${dash} ${c}`,
      transform: `rotate(-90 ${size / 2} ${size / 2})`
    }))
  }
  if (!known) {
    nodes.push(jsx('text', {
      key: 'q',
      x: '50%',
      y: '50%',
      dominantBaseline: 'central',
      textAnchor: 'middle',
      fill: 'var(--ui-text-quaternary)',
      fontSize: 16,
      fontWeight: 700,
      fontFamily: '-apple-system, system-ui, sans-serif',
      children: '?'
    }))
  }
  return jsx('svg', {
    width: size,
    height: size,
    viewBox: `0 0 ${size} ${size}`,
    'aria-hidden': true,
    style: { display: 'block', flexShrink: 0 },
    children: nodes
  })
}

function metaRow(key, label, value) {
  return jsx('div', {
    key: key,
    style: {
      display: 'flex',
      justifyContent: 'space-between',
      gap: 12,
      padding: '5px 0',
      borderTop: '1px solid var(--ui-stroke-secondary, rgba(255,255,255,0.08))',
      font: '12px/1.4 Menlo, ui-monospace, monospace'
    },
    children: [
      jsx('span', { key: 'k', style: { color: 'var(--ui-text-quaternary)' }, children: label }),
      jsx('span', { key: 'v', style: { textAlign: 'right', overflowWrap: 'anywhere' }, children: value })
    ]
  })
}

function HistoryList({ rows }) {
  if (!rows || !rows.length) return null
  return jsx('div', {
    style: { marginTop: 8 },
    children: [
      jsx('div', {
        key: 'label',
        style: {
          marginBottom: 2,
          color: 'var(--ui-text-quaternary)',
          font: '11px/1.3 -apple-system, system-ui, sans-serif'
        },
        children: 'State changes'
      })
    ].concat(rows.map((row, index) => jsx('div', {
      key: String(row.at) + '-' + index,
      style: {
        display: 'flex',
        justifyContent: 'space-between',
        gap: 8,
        padding: '2px 0',
        font: '12px/1.4 Menlo, ui-monospace, monospace'
      },
      children: [
        jsx('span', { key: 'p', style: { color: row.color }, children: `${row.pct}% ${row.state}` }),
        jsx('span', { key: 't', style: { color: 'var(--ui-text-quaternary)' }, children: formatClock(row.at) })
      ]
    })))
  })
}

function PanelBody({ d, history }) {
  const err = useValue(errAtom)
  const quiet = {
    margin: 0,
    color: 'var(--ui-text-secondary, var(--ui-text-quaternary))',
    font: '12px/1.45 -apple-system, system-ui, sans-serif'
  }
  if (d.pct == null) {
    return jsx('div', {
      style: { display: 'flex', gap: 12, alignItems: 'flex-start' },
      children: [
        jsx(GaugeRing, { key: 'ring', pct: null, color: 'var(--ui-text-quaternary)' }),
        jsx('div', {
          key: 'copy',
          children: [
            jsx('p', {
              key: 'text',
              style: quiet,
              children: d.state === 'SUMMARIZED'
                ? 'Just summarized — measuring again. The old percentage is gone until the next measurement.'
                : (d.hint || 'Send your first message. The gauge fills in after that.')
            }),
            d.state === 'SUMMARIZED' && compactionNote(d)
              ? jsx('div', { key: 'note', style: { marginTop: 6 }, children: compactionNote(d) })
              : null
          ]
        })
      ]
        .concat(err ? [jsx('p', {
          key: 'err',
          style: { margin: '8px 0 0', font: '11px/1.4 Menlo, ui-monospace, monospace', color: '#ff8a80', wordBreak: 'break-all' },
          children: 'Diag: ' + err
        })] : [])
    })
  }
  return jsx('div', {
    children: [
      jsx('div', {
        key: 'hero',
        style: { display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10 },
        children: [
          jsx(GaugeRing, { key: 'ring', pct: d.pct, color: d.color }),
          jsx('div', {
            key: 'num',
            children: [
              jsx('div', {
                key: 'pct',
                style: { font: '700 22px/1 Menlo, ui-monospace, monospace', color: d.color },
                children: Math.round(d.pct) + '% full'
              }),
              jsx('div', {
                key: 'who',
                style: { marginTop: 4, color: 'var(--ui-text-secondary, var(--ui-text-quaternary))', font: '12px/1.35 -apple-system, system-ui, sans-serif' },
                children: (d.name || 'This conversation') + (d.age_s > 3600 ? ' · last used ' + ago(d.age_s) : '')
              })
            ]
          })
        ]
      }),
      d.room ? jsx('p', { key: 'room', style: { margin: '0 0 6px', font: '13px/1.4 -apple-system, system-ui, sans-serif' }, children: d.room }) : null,
      d.next ? jsx('p', { key: 'next', style: { margin: '0 0 8px', font: '12px/1.4 -apple-system, system-ui, sans-serif', color: 'var(--ui-text-secondary, var(--ui-text-quaternary))' }, children: d.next }) : null,
      compactionNote(d),
      accountingBlock(d),
      jsx('details', {
        key: 'how',
        style: { marginTop: 8 },
        children: [
          jsx('summary', { key: 'sum', style: { cursor: 'pointer', font: '12px/1.4 -apple-system, system-ui, sans-serif' }, children: 'How this is calculated' }),
          metaRow('nominal', 'advertised window', formatCount(d.window_tokens != null ? d.window_tokens : d.window_chars)),
          metaRow('effective', 'reliable window', formatCount(d.effective_window_tokens)),
          metaRow('family', 'model family', (d.model_family || '—') + (d.degradation_factor != null ? ' × ' + d.degradation_factor : '')),
          metaRow('basis', 'measured from', BASIS_LABELS[d.basis] || '—'),
          metaRow('session', 'conversation', d.session_id_full || d.session_id || '—'),
          jsx('p', {
            key: 'same',
            style: { margin: '6px 0 0', color: 'var(--ui-text-quaternary)', font: '11px/1.4 -apple-system, system-ui, sans-serif' },
            children: 'The gauge_check tool reads this same record for this conversation. If the two disagree, they are looking at different conversations, or one of them has not refreshed yet.'
          })
        ]
      }),
      jsx(HistoryList, { key: 'history', rows: history })
    ]
  })
}

// ---- v0.5: audit, incident report, settings ------------------------------------
// The default view stays the gauge. Audit and Settings are two more tabs in the
// same popover. Both talk to the plugin's own backend (`/audit`, `/config`,
// `/report/incidents`); Settings saves through the same file and the same
// validator as `/gauge config set` on Telegram.

const viewAtom = atom('gauge')
const SANS = '-apple-system, system-ui, sans-serif'
const MONO = 'Menlo, ui-monospace, monospace'
const MUTED = 'var(--ui-text-quaternary)'
const SOFT = 'var(--ui-text-secondary, var(--ui-text-quaternary))'
const RULE = '1px solid var(--ui-stroke-secondary, rgba(255,255,255,0.08))'
const NARROW = 268
const WIDE = 340
const VIEWS = [['gauge', 'Now'], ['audit', 'History'], ['settings', 'Settings']]
const PART_ORDER = ['conversation', 'tools', 'system_prompt', 'skills']
const PART_NAMES = { conversation: 'conversation', tools: 'tools', system_prompt: 'system', skills: 'skills' }
const PART_COLORS = { conversation: '#6ea8fe', tools: '#c9a0ff', system_prompt: '#ffb74d', skills: '#00e676' }

function failure(e) {
  return String(e && e.message || e).slice(0, 200)
}

// The incident page follows the saved language, then the system language, else English.
function reportLang() {
  const saved = typeof pctAtom.get === 'function' ? pctAtom.get() : null
  if (saved && (saved.language === 'fr' || saved.language === 'en')) return saved.language
  const lang = typeof navigator !== 'undefined' && navigator.language ? String(navigator.language).toLowerCase() : 'en'
  return lang.indexOf('fr') === 0 ? 'fr' : 'en'
}

function plural(n, one, many) {
  return n + ' ' + (n === 1 ? one : many)
}

function Tabs({ view }) {
  return jsx('div', {
    role: 'tablist',
    'aria-label': 'Context Gauge views',
    style: { display: 'flex', gap: 4, marginBottom: 10 },
    children: VIEWS.map(pair => {
      const id = pair[0]
      const on = view === id
      return jsx('button', {
        key: id,
        type: 'button',
        role: 'tab',
        'aria-selected': on,
        onClick: () => viewAtom.set(id),
        style: {
          appearance: 'none', WebkitAppearance: 'none', cursor: 'pointer', flex: 1,
          padding: '4px 6px', borderRadius: 6,
          border: '1px solid ' + (on ? 'var(--ui-stroke-primary, rgba(255,255,255,0.28))' : 'transparent'),
          background: on ? 'var(--chrome-action-hover, rgba(255,255,255,0.08))' : 'transparent',
          color: on ? 'var(--ui-text-primary, #eceef1)' : MUTED,
          font: '600 11px/1.2 ' + SANS
        },
        children: pair[1]
      })
    })
  })
}

function ActionButton({ label, onClick, disabled, primary }) {
  return jsx('button', {
    type: 'button',
    onClick: onClick,
    disabled: !!disabled,
    style: {
      appearance: 'none', WebkitAppearance: 'none',
      cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.55 : 1,
      padding: '5px 10px', borderRadius: 6,
      border: '1px solid var(--ui-stroke-secondary, rgba(255,255,255,0.18))',
      background: primary ? 'var(--chrome-action-hover, rgba(255,255,255,0.1))' : 'transparent',
      color: 'var(--ui-text-primary, #eceef1)',
      font: '600 11px/1.2 ' + SANS
    },
    children: label
  })
}

function Note({ text, bad }) {
  if (!text) return null
  return jsx('p', {
    style: {
      margin: '6px 0 0',
      font: '11px/1.4 ' + MONO,
      color: bad ? '#ff8a80' : SOFT,
      wordBreak: 'break-word'
    },
    children: text
  })
}

function CompositionBar({ comp }) {
  if (!comp) return null
  const parts = PART_ORDER.filter(key => comp[key] && comp[key].share > 0)
  if (!parts.length) return null
  const summary = parts.map(key => PART_NAMES[key] + ' ' + formatPct(comp[key].share)).join(' · ')
  return jsx('div', {
    style: { marginTop: 5 },
    children: [
      jsx('div', {
        key: 'bar',
        role: 'img',
        'aria-label': summary,
        style: { display: 'flex', height: 6, borderRadius: 3, overflow: 'hidden', background: 'var(--ui-stroke-secondary, rgba(255,255,255,0.1))' },
        children: parts.map(key => jsx('span', { key: key, style: { width: comp[key].share + '%', background: PART_COLORS[key] } }))
      }),
      jsx('div', { key: 'legend', style: { marginTop: 3, color: MUTED, font: '10px/1.4 ' + MONO }, children: summary })
    ]
  })
}

function peakText(peak) {
  if (!peak) return '—'
  if (typeof peak.fill_percent === 'number') return (peak.inferred ? '≈' : '') + formatPct(peak.fill_percent) + ' ' + peak.status
  return formatCompact(peak.estimated_tokens) + ' tok'
}

function AuditRow({ s }) {
  const now = s.current && typeof s.current.fill_percent === 'number' ? formatPct(s.current.fill_percent) : '—'
  const comp = s.compactions || { count: 0 }
  const tail = plural(comp.count, 'compaction', 'compactions') + (s.resets ? ' · ' + plural(s.resets, 'reset', 'resets') : '')
  return jsx('div', {
    style: { padding: '7px 0', borderTop: RULE },
    children: [
      jsx('div', {
        key: 'name',
        style: { display: 'flex', justifyContent: 'space-between', gap: 8, font: '600 12px/1.3 ' + SANS },
        children: [
          jsx('span', { key: 'n', title: s.name, style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: s.name }),
          jsx('span', { key: 's', style: { color: MUTED, font: '10px/1.3 ' + MONO, flexShrink: 0 }, children: s.state || '' })
        ]
      }),
      jsx('div', { key: 'now', style: { marginTop: 2, color: SOFT, font: '11px/1.45 ' + MONO }, children: 'now ' + now + ' · peak ' + peakText(s.peak) }),
      jsx('div', { key: 'comp', style: { color: MUTED, font: '11px/1.45 ' + MONO }, children: tail }),
      jsx(CompositionBar, { key: 'mix', comp: s.composition })
    ]
  })
}

// POST writes the page on the machine that runs Hermes and answers with its path.
// A bare path is what the desktop opens in the default browser; a file:// URL
// would only be revealed in the file manager.
async function openIncidentReport(ctx, say) {
  say('Building the report…', false)
  try {
    const out = await ctx.rest('/report/incidents', { method: 'POST', body: { lang: reportLang() } })
    if (!out || out.ok === false || !out.path) {
      say((out && (out.error || out.write_error)) || 'The report could not be written.', true)
      return
    }
    const os = ctx.os || {}
    if (os.openExternal && await os.openExternal(out.path)) { say('Opened in your browser.', false); return }
    if (os.revealPath && await os.revealPath(out.path)) { say('Shown in your file manager.', false); return }
    say('Saved at ' + out.path, false)
  } catch (e) { say(failure(e), true) }
}

async function copyIncidentBlock(ctx, say) {
  try {
    const out = await ctx.rest('/report/incidents?lang=' + reportLang(), { method: 'GET' })
    if (!out || out.ok === false || typeof out.llm !== 'string') {
      say((out && out.error) || 'No report to copy.', true)
      return
    }
    const os = ctx.os || {}
    const copied = os.writeClipboard ? await os.writeClipboard(out.llm) : false
    say(copied ? 'Copied. Paste it into any assistant for a fix plan.' : 'The clipboard is not available here.', !copied)
  } catch (e) { say(failure(e), true) }
}

function AuditView({ ctx, open }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState({ text: '', bad: false })
  const say = (text, bad) => setNote({ text: text, bad: !!bad })

  useEffect(() => {
    if (!open) return undefined
    let live = true
    ctx.rest('/audit?limit=6&lang=en', { method: 'GET' }).then(out => {
      if (!live) return
      if (out && out.ok === false) { setError(out.error || 'The audit failed.'); return }
      setError('')
      setData(out)
    }).catch(e => { if (live) setError(failure(e)) })
    return () => { live = false }
  }, [open])

  const quiet = { margin: 0, color: SOFT, font: '12px/1.45 ' + SANS }
  const body = []
  if (error) body.push(jsx('p', { key: 'err', style: { ...quiet, color: '#ff8a80' }, children: error }))
  else if (!data) body.push(jsx('p', { key: 'wait', style: quiet, children: 'Reading the history…' }))
  else if (!data.session_count) body.push(jsx('p', { key: 'none', style: quiet, children: 'No session recorded yet.' }))
  else {
    const totals = data.totals || {}
    const top = totals.highest_peak
    body.push(jsx('div', {
      key: 'head',
      style: { marginBottom: 6, font: '11px/1.5 ' + MONO, color: SOFT },
      children: [
        jsx('div', { key: 'counts', children: plural(totals.compactions || 0, 'compaction', 'compactions') + ' · ' + plural(totals.resets || 0, 'reset', 'resets') + ' · ' + plural(data.session_count, 'session', 'sessions') }),
        top ? jsx('div', { key: 'top', children: 'highest peak: ' + top.name + ' ' + (typeof top.fill_percent === 'number' ? formatPct(top.fill_percent) : formatCompact(top.estimated_tokens) + ' tok') }) : null,
        totals.composition ? jsx('div', { key: 'where', style: { marginTop: 6, color: MUTED }, children: 'where the context goes' }) : null,
        totals.composition ? jsx(CompositionBar, { key: 'mix', comp: totals.composition }) : null
      ]
    }))
    ;(data.sessions || []).forEach(s => body.push(jsx(AuditRow, { key: s.session_id, s: s })))
    if (data.shown < data.session_count) {
      body.push(jsx('p', { key: 'more', style: { ...quiet, color: MUTED, marginTop: 6 }, children: '+' + (data.session_count - data.shown) + ' more — /gauge audit on Telegram or the incident report lists them all.' }))
    }
  }
  return jsx('div', {
    children: body.concat([
      jsx('div', {
        key: 'actions',
        style: { display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 10 },
        children: [
          jsx(ActionButton, { key: 'open', label: 'Open incident report', primary: true, onClick: () => openIncidentReport(ctx, say) }),
          jsx(ActionButton, { key: 'copy', label: 'Copy a summary for an AI chat', onClick: () => copyIncidentBlock(ctx, say) })
        ]
      }),
      jsx(Note, { key: 'note', text: note.text, bad: note.bad }),
      jsx('p', { key: 'foot', style: { margin: '8px 0 0', color: MUTED, font: '10px/1.4 ' + SANS }, children: 'A compaction resets the live counter; this view and the incident report keep the past peaks.' })
    ])
  })
}

// --- Settings ---------------------------------------------------------------------
// Form values live as strings or booleans while the user types. Only what changed
// is sent, and the server's answer is the truth: the form reloads from it.

function toDraft(field) {
  const v = field.value
  if (field.type === 'ratio') return String(Math.round(Number(v) * 1000) / 10)
  if (field.type === 'bool') return !!v
  if (field.type === 'statuses') return Array.isArray(v) ? v.slice() : []
  return v == null ? '' : String(v)
}

function toPayload(field, draft) {
  if (field.type === 'bool') return !!draft
  if (field.type === 'statuses') return Array.isArray(draft) ? draft.slice() : []
  const text = String(draft == null ? '' : draft).trim()
  if (field.type === 'route') return text
  const n = Number(text)
  if (text === '' || !Number.isFinite(n)) return text
  return field.type === 'ratio' ? Math.round(n * 10) / 1000 : n
}

function changedValues(fields, drafts) {
  const out = {}
  fields.forEach(field => {
    const next = toPayload(field, drafts[field.key])
    const now = toPayload(field, toDraft(field))
    if (JSON.stringify(next) !== JSON.stringify(now)) out[field.key] = next
  })
  return out
}

function draftsFrom(fields) {
  const out = {}
  fields.forEach(field => { out[field.key] = toDraft(field) })
  return out
}

function routeKind(text) {
  if (text === 'session') return 'session'
  if (text === 'home') return 'home'
  return 'other'
}

const inputStyle = {
  boxSizing: 'border-box', width: '100%', padding: '4px 6px', borderRadius: 5,
  border: RULE, background: 'var(--ui-bg-input, rgba(255,255,255,0.05))',
  color: 'var(--ui-text-primary, #eceef1)', font: '12px/1.3 ' + MONO
}

function FieldControl({ field, value, onChange }) {
  const id = 'hgf-' + field.key.replace(/[^a-z_]/g, '-')
  if (field.type === 'bool') {
    return jsx('input', { id: id, type: 'checkbox', checked: !!value, onChange: e => onChange(e.target.checked) })
  }
  if (field.type === 'statuses') {
    return jsx('span', {
      style: { display: 'inline-flex', gap: 10 },
      children: (field.choices || ['WATCH', 'FULL']).map(choice => jsx('label', {
        key: choice,
        style: { display: 'inline-flex', gap: 4, alignItems: 'center', font: '12px/1.3 ' + MONO },
        children: [
          jsx('input', {
            key: 'box', type: 'checkbox', checked: value.indexOf(choice) >= 0,
            onChange: e => onChange(e.target.checked ? value.concat([choice]) : value.filter(c => c !== choice))
          }),
          choice
        ]
      }))
    })
  }
  if (field.type === 'route') {
    const kind = routeKind(String(value))
    return jsx('span', {
      style: { display: 'flex', flexDirection: 'column', gap: 4 },
      children: [
        jsx('select', {
          key: 'kind', id: id, value: kind, style: inputStyle,
          onChange: e => onChange(e.target.value === 'other' ? 'custom:' : e.target.value),
          children: [
            jsx('option', { key: 's', value: 'session', children: 'this conversation' }),
            jsx('option', { key: 'h', value: 'home', children: 'home — the home channel' }),
            jsx('option', { key: 'o', value: 'other', children: 'another chat…' })
          ]
        }),
        kind === 'other'
          ? jsx('input', {
            key: 'text', type: 'text', value: String(value), style: inputStyle,
            placeholder: 'custom:telegram:-1001234567890',
            spellCheck: false, autoCapitalize: 'off',
            onChange: e => onChange(e.target.value)
          })
          : null
      ]
    })
  }
  const percent = field.type === 'ratio'
  return jsx('span', {
    style: { display: 'inline-flex', alignItems: 'center', gap: 4 },
    children: [
      jsx('input', {
        key: 'in', id: id, type: 'number', inputMode: 'decimal', value: String(value),
        min: percent ? Math.round(field.min * 100) : field.min,
        max: percent ? Math.round(field.max * 100) : field.max,
        step: percent ? 1 : 'any',
        style: { ...inputStyle, width: 96 },
        onChange: e => onChange(e.target.value)
      }),
      percent ? jsx('span', { key: 'pc', style: { color: MUTED, font: '12px/1 ' + MONO }, children: '%' }) : null
    ]
  })
}

function SettingsView({ ctx, open }) {
  const [fields, setFields] = useState(null)
  const [drafts, setDrafts] = useState({})
  const [errors, setErrors] = useState({})
  const [status, setStatus] = useState({ text: '', bad: false })
  const [busy, setBusy] = useState(false)
  const [where, setWhere] = useState('')
  const [profile, setProfile] = useState('default')

  function adopt(snapshot) {
    setFields(snapshot.fields)
    setDrafts(draftsFrom(snapshot.fields))
    setWhere(snapshot.path || '')
    setProfile(snapshot.profile || 'default')
  }

  useEffect(() => {
    if (!open) return undefined
    let live = true
    ctx.rest('/config', { method: 'GET' }).then(out => {
      if (!live) return
      if (!out || out.ok === false || !Array.isArray(out.fields)) {
        setStatus({ text: (out && out.error) || 'The settings could not be read.', bad: true })
        return
      }
      setErrors({})
      setStatus({ text: '', bad: false })
      adopt(out)
    }).catch(e => { if (live) setStatus({ text: failure(e), bad: true }) })
    return () => { live = false }
  }, [open])

  async function save() {
    const values = changedValues(fields, drafts)
    if (!Object.keys(values).length) { setStatus({ text: 'Nothing to save.', bad: false }); return }
    setBusy(true)
    try {
      const out = await ctx.rest('/config', { method: 'POST', body: { values: values } })
      if (out && out.ok === true) {
        setErrors({})
        adopt(out)
        setStatus({ text: 'Saved: ' + (out.changed || Object.keys(values)).join(', ') + '. Applies to /gauge and to the alerts right away.', bad: false })
        refresh(ctx)
      } else {
        setErrors((out && out.errors) || {})
        setStatus({ text: (out && out.error) || 'Not saved. Fix what is highlighted.', bad: true })
      }
    } catch (e) { setStatus({ text: failure(e), bad: true }) }
    setBusy(false)
  }

  if (!fields) {
    return jsx('p', {
      style: { margin: 0, color: status.bad ? '#ff8a80' : SOFT, font: '12px/1.45 ' + SANS },
      children: status.text || 'Reading the settings…'
    })
  }
  function groupRows(title, group) {
    const items = fields.filter(field => (field.group || 'advanced') === group).map(fieldRow)
    if (!items.length) return []
    return [jsx('p', {
      key: 'g-' + group,
      style: { margin: '8px 0 0', font: '700 11px/1.3 ' + SANS, letterSpacing: '0.04em', color: MUTED },
      children: title
    })].concat(items)
  }
  function fieldRow(field) {
    const error = errors[field.key]
    return jsx('div', {
      key: field.key,
      style: { padding: '7px 0', borderTop: RULE },
      children: [
        jsx('label', {
          key: 'label',
          htmlFor: 'hgf-' + field.key.replace(/[^a-z_]/g, '-'),
          style: { display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 4, font: '600 12px/1.3 ' + SANS },
          children: [
            jsx('span', { key: 'l', children: field.label }),
            jsx('span', { key: 's', style: { color: MUTED, font: '10px/1.3 ' + MONO }, children: field.source === 'file' ? '' : 'default' })
          ]
        }),
        jsx(FieldControl, {
          key: 'control', field: field, value: drafts[field.key],
          onChange: next => setDrafts(prev => ({ ...prev, [field.key]: next }))
        }),
        jsx('p', { key: 'help', style: { margin: '3px 0 0', color: MUTED, font: '12px/1.4 ' + SANS }, children: field.help }),
        error ? jsx('p', { key: 'err', style: { margin: '3px 0 0', color: '#ff8a80', font: '11px/1.4 ' + MONO }, children: error.message || String(error) }) : null
      ]
    })
  }
  const rows = groupRows('Basic', 'basic').concat(groupRows('Advanced', 'advanced'))
  return jsx('div', {
    children: rows.concat([
      jsx('div', {
        key: 'actions',
        style: { display: 'flex', gap: 6, marginTop: 8 },
        children: [
          jsx(ActionButton, { key: 'save', label: busy ? 'Saving…' : 'Save', primary: true, disabled: busy, onClick: save }),
          jsx(ActionButton, { key: 'undo', label: 'Undo edits', disabled: busy, onClick: () => { setDrafts(draftsFrom(fields)); setErrors({}); setStatus({ text: '', bad: false }) } })
        ]
      }),
      jsx(Note, { key: 'status', text: status.text, bad: status.bad }),
      where ? jsx('p', { key: 'where', style: { margin: '8px 0 0', color: MUTED, font: '12px/1.4 ' + MONO, wordBreak: 'break-all' }, children: (profile || 'default') + ' · ' + where }) : null
    ])
  })
}

function Chip({ ctx }) {
  const d = useValue(pctAtom)
  const history = useValue(historyAtom)
  const openId = useValue(openAtom)
  const view = useValue(viewAtom)
  const popId = 'hg' + useId().replace(/:/g, '')
  const open = openId === popId
  const [hover, setHover] = useState(false)
  const [place, setPlace] = useState(null)
  const buttonRef = useRef(null)
  const panelRef = useRef(null)
  const wrapRef = useRef(null)

  useLayoutEffect(() => {
    if (!open) return undefined
    function placePanel() {
      const next = measure(panelRef.current, buttonRef.current)
      if (!next) return
      setPlace(prev => samePlace(prev, next) ? prev : next)
    }
    placePanel()
    window.addEventListener('resize', placePanel)
    window.addEventListener('scroll', placePanel, true)
    return () => {
      window.removeEventListener('resize', placePanel)
      window.removeEventListener('scroll', placePanel, true)
    }
  }, [open, view, d.pct, d.state, d.session_id, d.tokens_in, d.effective_window_tokens, d.model_family, history])

  useEffect(() => {
    if (!open) return undefined
    function onKey(event) {
      if (event.key !== 'Escape') return
      const panel = panelRef.current
      if (panel && typeof panel.hidePopover === 'function' && panel.matches(':popover-open')) {
        panel.hidePopover()
        return
      }
      if (openAtom.get() === popId) openAtom.set(null)
    }
    function onPointer(event) {
      if (POPOVER_OK) return
      const root = wrapRef.current
      if (root && event.target && root.contains(event.target)) return
      openAtom.set(null)
      setPlace(null)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onPointer)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onPointer)
    }
  }, [open, popId])

  function onBeforeToggle(event) {
    if (event.newState === 'open' && event.currentTarget) event.currentTarget.style.visibility = 'hidden'
  }

  function onToggle(event) {
    if (event.newState === 'open') {
      const next = measure(event.currentTarget, buttonRef.current)
      if (next) setPlace(next)
      openAtom.set(popId)
      return
    }
    setPlace(null)
    if (openAtom.get() === popId) openAtom.set(null)
  }

  function onClick() {
    refresh(ctx)
    if (POPOVER_OK) return
    openAtom.set(open ? null : popId)
    if (open) setPlace(null)
  }

  function onClose(event) {
    event.preventDefault()
    event.stopPropagation()
    const panel = panelRef.current
    if (panel && typeof panel.hidePopover === 'function' && panel.matches(':popover-open')) {
      panel.hidePopover()
      return
    }
    openAtom.set(null)
    setPlace(null)
  }

  const buttonProps = {
    ref: buttonRef,
    type: 'button',
    title: chipTitle(d),
    'aria-haspopup': 'dialog',
    'aria-expanded': open,
    'aria-controls': popId,
    onClick: onClick,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: chipStyle(d, hover || open)
  }
  if (POPOVER_OK) {
    buttonProps.popoverTarget = popId
    buttonProps.popoverTargetAction = 'toggle'
  }

  const panelProps = {
    ref: panelRef,
    id: popId,
    role: 'dialog',
    'aria-label': 'Context Gauge',
    onBeforeToggle: onBeforeToggle,
    onToggle: onToggle,
    onPointerDown: (event) => event.stopPropagation(),
    style: panelStyle(place, view !== 'gauge'),
    children: [
      jsx('div', {
        key: 'head',
        style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: 10 },
        children: [
          jsx('span', {
            key: 'title',
            style: { font: '600 12px/1.2 -apple-system, system-ui, sans-serif' },
            children: 'Context Gauge v0.6.2'
          }),
          jsx('button', {
            key: 'close',
            type: 'button',
            'aria-label': 'Close',
            onClick: onClose,
            style: {
              appearance: 'none', WebkitAppearance: 'none',
              background: 'transparent', border: 'none',
              color: 'var(--ui-text-quaternary)', cursor: 'pointer',
              borderRadius: 4, width: 22, height: 22, padding: 0,
              font: '16px/1 -apple-system, system-ui, sans-serif'
            },
            children: '×'
          })
        ]
      }),
      jsx(Tabs, { key: 'tabs', view }),
      view === 'audit' ? jsx(AuditView, { key: 'audit', ctx, open })
        : view === 'settings' ? jsx(SettingsView, { key: 'settings', ctx, open })
          : jsx(PanelBody, { key: 'body', d, history })
    ]
  }
  if (POPOVER_OK) panelProps.popover = 'auto'

  return jsx('div', {
    ref: wrapRef,
    style: { position: 'relative', display: 'inline-flex', maxWidth: '100%', minWidth: 0 },
    children: [
      jsx('button', {
        key: 'chip',
        ...buttonProps,
        children: [
          jsx(ChipMark, { key: 'mark', d }),
          jsx('span', { key: 'pct', children: chipLabel(d) }),
          jsx('span', {
            key: 'caret',
            'aria-hidden': true,
            style: {
              fontSize: 9, lineHeight: 1, opacity: 0.55, flexShrink: 0,
              transform: open ? 'rotate(180deg)' : 'none', display: 'inline-block'
            },
            children: '▾'
          })
        ]
      }),
      jsx('div', { key: 'panel', ...panelProps })
    ]
  })
}

function Pane({ ctx }) {
  const d = useValue(pctAtom)
  const bar = d.pct == null ? 0 : Math.min(d.pct, 100)
  const row = (k, v) => jsx('div', {
    style: { display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--ui-stroke-secondary)', font: '12px/1.4 Menlo, monospace' },
    children: [jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: k }), jsx('span', { children: v })]
  })
  return jsx('div', {
    style: { padding: '14px', overflowY: 'auto', height: '100%' },
    children: [
      jsx('div', { key: 'h', style: { font: '700 15px/1.3 -apple-system', marginBottom: '10px', color: 'var(--ui-accent)' }, children: 'Context Gauge' }),
      jsx('div', { key: 'big', style: { font: `800 40px/1 Menlo`, color: d.color, marginBottom: '10px' }, children: d.pct == null ? '—' : `${d.pct}%` }),
      jsx('div', { key: 'barbg', style: { height: '10px', borderRadius: '6px', background: 'var(--ui-stroke-secondary)', overflow: 'hidden', marginBottom: '14px' },
        children: jsx('div', { style: { width: `${bar}%`, height: '100%', background: d.color, transition: 'width .4s' } }) }),
      d.state === 'SUMMARIZED'
        ? jsx('p', { key: 'measuring', style: { margin: '0 0 8px', color: 'var(--ui-text-secondary, var(--ui-text-quaternary))', font: '12px/1.4 -apple-system, system-ui, sans-serif' }, children: 'Just summarized — measuring again.' })
        : null,
      compactionNote(d),
      accountingBlock(d),
      jsx('div', { key: 'rows', children: [
        row('model', d.model || '—'),
        row('turns', String(d.turns)),
        hasEffective(d)
          ? row('used', `${Number(d.tokens_in).toLocaleString()} / ${Number(d.effective_window_tokens).toLocaleString()} tok`)
          : row('used', `${d.chars_in.toLocaleString()} / ${d.window_chars.toLocaleString()} chars`),
        row('tools', `${d.tools_chars.toLocaleString()} chars`),
        row('skills', `${d.skills_chars.toLocaleString()} chars`),
        row('conversation', `${d.conversation_chars.toLocaleString()} chars`)
      ] }),
      jsx('div', { key: 'foot', style: { marginTop: '10px', color: 'var(--ui-text-quaternary)', font: '11px/1.4 Menlo' }, children: 'hermes-gauge · refreshes every 5s' })
    ]
  })
}

export default {
  id: 'hermes-gauge',
  name: 'Context Gauge',
  register(ctx) {
    ctx.register({
      id: 'gauge-chip',
      area: 'composer.actions',
      order: 0,
      render: () => jsx(Chip, { ctx })
    })
    ctx.register({
      id: 'gauge-pane',
      area: 'panes',
      title: 'Context Gauge',
      data: { placement: 'right', width: '280px' },
      render: () => jsx(Pane, { ctx })
    })
    refresh(ctx)
    setInterval(() => refresh(ctx), 5000)
  }
}
