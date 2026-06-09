/**
 * MessageLine — renders one transcript row (spec v4 §2 / §7). An assistant turn
 * is ONE ordered `parts[]` dispatched by `<Switch>`/`<Match>` on `part.type`, so
 * text / reasoning / tool interleave INLINE (the §7 fix for "tools dump below").
 * User/system rows (and settled/resumed assistant rows with no parts) render flat
 * `text`. Fully themed; rich text via <b>/<span>, never an attributes bitmask (§8 #1).
 *
 * Stable `id` per part as the <For> key so a new tool part below a streaming text
 * part doesn't remount it. Native <markdown> for text parts lands in 2b-ii.
 */
import { createMemo, For, Match, Show, Switch } from 'solid-js'

import { segmentMarkdown } from '../logic/mdTable.ts'
import type { Message } from '../logic/store.ts'
import { Markdown } from './markdown.tsx'
import { MdTable } from './mdTable.tsx'
import { ReasoningPart } from './reasoningPart.tsx'
import { useTheme } from './theme.tsx'
import { ToolPart } from './toolPart.tsx'

const GUTTER = 2

/** A text part: native markdown for prose, an aligned grid for GFM tables (item 7). */
function AssistantText(props: { text: string; streaming: boolean }) {
  const segments = createMemo(() => segmentMarkdown(props.text.replace(/^\n+|\n+$/g, '')))
  return (
    <For each={segments()}>
      {seg =>
        seg.kind === 'table' ? (
          <MdTable seg={seg} />
        ) : (
          <Markdown text={seg.text.replace(/^\n+|\n+$/g, '')} streaming={props.streaming} />
        )
      }
    </For>
  )
}

export function MessageLine(props: { message: Message }) {
  const theme = useTheme()
  const m = () => props.message
  const glyph = () => (m().role === 'assistant' ? theme().brand.icon : m().role === 'user' ? theme().brand.prompt : '·')
  const glyphFg = () =>
    m().role === 'assistant' ? theme().color.accent : m().role === 'user' ? theme().color.accent : theme().color.muted
  const hasParts = () => (m().parts?.length ?? 0) > 0

  return (
    <box style={{ flexDirection: 'row', flexShrink: 0, marginTop: m().role === 'user' ? 1 : 0 }}>
      <box style={{ flexShrink: 0, width: GUTTER }}>
        {/* the role glyph is decorative — exclude it from mouse selection (item 4).
            Bold so the user `❯` / assistant `⚕` turn boundaries pop (item 8). */}
        <text selectable={false}>
          <span style={{ fg: glyphFg() }}>
            <b>{glyph()}</b>
          </span>
        </text>
      </box>
      {/* gap owns ALL inter-part spacing (item 5) — uniform 1 line between text /
          reasoning / tool regardless of order or stream timing, so blank lines
          don't pop in and out as parts are created/merged mid-stream. */}
      <box style={{ flexDirection: 'column', flexGrow: 1, minWidth: 0, gap: 1 }}>
        <Show
          when={m().role === 'assistant' && hasParts()}
          fallback={
            // No parts yet: the just-started streaming turn shows ONLY the caret,
            // inline with the glyph (not an empty line + a dangling caret below —
            // item 10 cursor misalignment); a settled row shows its flat text.
            <Show
              when={m().streaming && !hasParts()}
              fallback={
                <text>
                  <span style={{ fg: theme().color.text }}>{m().text}</span>
                </text>
              }
            >
              <text>
                <span style={{ fg: theme().color.muted }}>▍</span>
              </text>
            </Show>
          }
        >
          <For each={m().parts ?? []}>
            {part => (
              <Switch>
                <Match when={part.type === 'tool' && part}>{tool => <ToolPart part={tool()} />}</Match>
                <Match when={part.type === 'reasoning' && part}>
                  {r => <ReasoningPart text={r().text} streaming={m().streaming ?? false} />}
                </Match>
                <Match when={part.type === 'text' && part}>
                  {/* prose via the native renderable; GFM tables as an aligned grid
                      (item 7). Leading/trailing blanks are stripped so the column
                      `gap` is the sole inter-part spacing — no jitter (item 5). */}
                  {t => <AssistantText text={t().text} streaming={m().streaming ?? false} />}
                </Match>
              </Switch>
            )}
          </For>
        </Show>
      </box>
    </box>
  )
}
