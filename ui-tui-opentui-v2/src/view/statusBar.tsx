/**
 * StatusBar — the persistent bottom chrome (spec §3; Ink's `appChrome.tsx`
 * StatusRule, item 14). One themed row pinned below the input zone:
 *
 *   ● model ·effort   ████░░░░ 42%                              ~/dir (branch)
 *
 * Fields are sourced from `store.state.info` (the `session.info` event +
 * session.create/resume result; see store `SessionInfo`). Width-aware (Ink's
 * `statusRuleWidths` progressive disclosure): the context bar drops on narrow
 * terminals and the cwd is left-truncated (`…/tail`) so the row NEVER wraps or
 * clips. Read-only chrome — no input handling here.
 */
import { useTerminalDimensions } from '@opentui/solid'
import { createMemo, Show } from 'solid-js'

import { useTheme } from './theme.tsx'
import type { SessionStore } from '../logic/store.ts'

const HOME = process.env.HOME ?? ''
const CTX_BAR_CELLS = 8

/** `anthropic/claude-opus-4-8` → `claude-opus-4-8`; trims the provider prefix (Ink shortModelLabel). */
function shortModel(model: string): string {
  return model.includes('/') ? (model.split('/').at(-1) ?? model) : model
}

/** Reasoning effort → a compact suffix; hidden for the default/medium effort. */
function effortSuffix(effort: string | undefined, fast: boolean | undefined): string {
  const parts: string[] = []
  if (effort && effort !== 'medium' && effort !== 'default') parts.push(effort)
  if (fast) parts.push('fast')
  return parts.length ? ` ·${parts.join('·')}` : ''
}

/** Abbreviate cwd with `~` for $HOME, then collapse to the last two path segments
 *  (`…/lively-thrush/hermes-agent`) so deep worktree paths stay readable (Ink fmtCwdBranch). */
function shortCwd(cwd: string): string {
  const home = HOME && (cwd === HOME || cwd.startsWith(HOME + '/')) ? '~' + cwd.slice(HOME.length) : cwd
  const segs = home.split('/').filter(Boolean)
  return segs.length <= 3 ? home : '…/' + segs.slice(-2).join('/')
}

/** Keep the TAIL of a string, prefixing with `…` when it must be clipped. */
function truncLeft(s: string, max: number): string {
  if (max <= 1) return s.length > max ? '…' : s
  return s.length <= max ? s : '…' + s.slice(s.length - max + 1)
}

/** A unicode meter: `████░░░░` filled to `pct`% over `width` cells (Ink ctxBar). */
function ctxBar(pct: number, width: number): string {
  const filled = Math.max(0, Math.min(width, Math.round((pct / 100) * width)))
  return '█'.repeat(filled) + '░'.repeat(width - filled)
}

export function StatusBar(props: { store: SessionStore }) {
  const theme = useTheme()
  const dims = useTerminalDimensions()
  const info = () => props.store.state.info

  // Context-bar colour escalates with pressure (Ink ctxBarColor good→warn→bad→critical).
  const ctxColor = (pct: number) =>
    pct >= 92
      ? theme().color.statusCritical
      : pct >= 80
        ? theme().color.statusBad
        : pct >= 60
          ? theme().color.statusWarn
          : theme().color.statusGood

  const dot = () => (info().running ? '◐' : props.store.state.ready ? '●' : '○')
  const dotColor = () =>
    info().running ? theme().color.statusWarn : props.store.state.ready ? theme().color.statusGood : theme().color.muted

  const model = () => (info().model ? shortModel(info().model!) : '')
  const effort = () => effortSuffix(info().effort, info().fast)
  const pct = () => info().contextPercent

  // Progressive disclosure budget (the row is `width - 2` after the box padding).
  // left = dot+space+model+effort ; the context bar shows only when there's room.
  const showBar = createMemo(() => pct() !== undefined && dims().width >= 64)
  const ctxText = () => (showBar() ? `${ctxBar(pct()!, CTX_BAR_CELLS)} ${pct()}%` : '')

  // Right side: cwd (branch), left-truncated to whatever the left side leaves.
  const cwdFull = createMemo(() => {
    const c = info().cwd ? shortCwd(info().cwd!) : ''
    if (!c) return ''
    return info().branch ? `${c} (${info().branch})` : c
  })
  const rightText = createMemo(() => {
    const leftLen = 2 + model().length + effort().length + (showBar() ? ctxText().length + 3 : 0)
    const budget = dims().width - 2 - leftLen - 2 // box padding + a 2-col gap
    return budget > 4 ? truncLeft(cwdFull(), budget) : ''
  })

  return (
    <box
      style={{
        flexShrink: 0,
        flexDirection: 'row',
        backgroundColor: theme().color.statusBg,
        paddingLeft: 1,
        paddingRight: 1
      }}
    >
      {/* left: turn/connection dot + model + effort + context bar */}
      <box style={{ flexShrink: 0, flexDirection: 'row' }}>
        <text selectable={false}>
          <span style={{ fg: dotColor() }}>{dot()}</span>
          <Show when={model()}>
            <span style={{ fg: theme().color.statusFg }}>{` ${model()}`}</span>
            <span style={{ fg: theme().color.muted }}>{effort()}</span>
          </Show>
          <Show when={showBar()}>
            {/* a dim divider segments the bar into scannable fields (item 8) */}
            <span style={{ fg: theme().color.border }}>{'  │  '}</span>
            <span style={{ fg: ctxColor(pct()!) }}>{ctxBar(pct()!, CTX_BAR_CELLS)}</span>
            <span style={{ fg: theme().color.statusFg }}>{` ${pct()}%`}</span>
          </Show>
        </text>
      </box>

      {/* spacer pushes the cwd to the right edge */}
      <box style={{ flexGrow: 1, minWidth: 0 }} />

      {/* right: cwd (branch), pre-truncated so the row never wraps */}
      <Show when={rightText()}>
        <box style={{ flexShrink: 0, flexDirection: 'row' }}>
          <text selectable={false}>
            <span style={{ fg: theme().color.muted }}>{rightText()}</span>
          </text>
        </box>
      </Show>
    </box>
  )
}
