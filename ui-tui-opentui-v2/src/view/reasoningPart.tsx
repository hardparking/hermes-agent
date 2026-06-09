/**
 * ReasoningPart — the model's thinking trace, collapsible (item 6; opencode's
 * ReasoningPart/ReasoningHeader). Auto-EXPANDED while the turn streams (so you
 * watch it think), then COLLAPSES to a one-line `▶ Thought: <title>` once the
 * turn settles. Click the header to override either way.
 *
 *   ▼ Thinking: <title>        ← live (streaming), body shown
 *   ▶ Thought: <title>         ← settled (collapsed), click to reopen
 *   │ <reasoning markdown>     ← dim body in a left-bordered block
 *
 * Title is the model's leading `**bold**` line when present (opencode's
 * reasoningSummary). Dim throughout — it's secondary to the answer.
 */
import { createMemo, createSignal, Show } from 'solid-js'

import { Markdown } from './markdown.tsx'
import { useTheme } from './theme.tsx'

const GUTTER = 2

/** Split a leading `**Title**\n\n body` into {title, body} (opencode reasoningSummary). */
function reasoningSummary(text: string): { title?: string; body: string } {
  const s = (text ?? '').replace('[REDACTED]', '').trim()
  const m = s.match(/^\*\*([^*\n]+)\*\*(?:\r?\n\r?\n|$)/)
  const title = m?.[1]?.trim()
  if (!title) return { body: s }
  return { title, body: s.slice(m![0].length).trimStart() }
}

export function ReasoningPart(props: { text: string; streaming?: boolean }) {
  const theme = useTheme()
  const [override, setOverride] = createSignal<boolean | undefined>(undefined)
  // live → expanded so you see it think; settled → collapsed. Click overrides.
  const expanded = () => override() ?? !!props.streaming
  const summary = createMemo(() => reasoningSummary(props.text))
  const label = () => (props.streaming ? 'Thinking' : 'Thought')

  return (
    <Show when={summary().body || summary().title}>
      <box style={{ flexDirection: 'column', flexShrink: 0 }}>
        <box
          style={{ flexDirection: 'row', flexShrink: 0 }}
          onMouseDown={() => setOverride(e => !(e ?? !!props.streaming))}
        >
          <box style={{ flexShrink: 0, width: GUTTER }}>
            <text selectable={false}>
              <span style={{ fg: theme().color.muted }}>{expanded() ? '▼' : '▶'}</span>
            </text>
          </box>
          <text>
            {/* label color matches tool headers (label, not warn) so reasoning + tool
                read as one family of collapsible asides — warn is for warnings (item 8). */}
            <span style={{ fg: theme().color.label }}>{label()}</span>
            <Show when={summary().title}>
              <span style={{ fg: theme().color.muted }}>{`: ${summary().title}`}</span>
            </Show>
          </text>
        </box>
        <Show when={expanded() && summary().body}>
          <box
            style={{ flexDirection: 'column', flexGrow: 1, minWidth: 0, marginLeft: GUTTER, paddingLeft: 1 }}
            border={['left']}
            borderColor={theme().color.border}
          >
            <Markdown text={summary().body} streaming={props.streaming ?? false} fg={theme().color.muted} />
          </box>
        </Show>
      </box>
    </Show>
  )
}
