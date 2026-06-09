/**
 * HomeHint — the empty-transcript home screen (item 12; Ink's `helpHint.tsx`).
 * Shown when there are no messages yet: the brand line, a few common commands,
 * and the key input tips. Replaced by the transcript as soon as a turn lands.
 * Fully themed; decorative, so `selectable={false}` (item 4).
 */
import { For } from 'solid-js'

import { useTheme } from './theme.tsx'

const COMMANDS: ReadonlyArray<readonly [string, string]> = [
  ['/help', 'list all commands'],
  ['/model', 'switch model'],
  ['/sessions', 'resume a session'],
  ['/skills', 'browse skills'],
  ['/agents', 'live delegation trace'],
  ['/clear', 'clear the transcript']
]

export function HomeHint() {
  const theme = useTheme()
  return (
    <box style={{ flexDirection: 'column', flexShrink: 0, paddingLeft: 1, marginTop: 1 }}>
      <text selectable={false}>
        <span style={{ fg: theme().color.accent }}>{theme().brand.icon} </span>
        <span style={{ fg: theme().color.primary }}>
          <b>{theme().brand.name}</b>
        </span>
      </text>
      <text selectable={false}>
        <span style={{ fg: theme().color.muted }}>{theme().brand.welcome}</span>
      </text>
      <box style={{ flexDirection: 'column', marginTop: 1 }}>
        <For each={COMMANDS}>
          {([cmd, desc]) => (
            <text selectable={false}>
              <span style={{ fg: theme().color.accent }}>{cmd.padEnd(12)}</span>
              <span style={{ fg: theme().color.muted }}>{desc}</span>
            </text>
          )}
        </For>
      </box>
      <box style={{ marginTop: 1 }}>
        <text selectable={false}>
          <span style={{ fg: theme().color.muted }}>
            Type to chat · ↑↓ history · @file to mention · Ctrl+C to stop/quit
          </span>
        </text>
      </box>
    </box>
  )
}
