import type { ITheme, Terminal } from '@xterm/xterm'
import type { CSSProperties } from 'react'

// Solarized (Ethan Schoonover) for both modes. The accent palette is shared
// — Schoonover's design is that ANSI 0–15 are identical between Light and
// Dark; only fg/bg/cursor swap (`base00/01` vs `base0/1`). We skip both
// Solarized backgrounds (`base3` cream / `base03` slate) and keep the glass
// translucent in either mode.
//
// Heads-up: ANSI 7 (`white` = `base2` #eee8d5) is the lightest cream by
// design — near-invisible against light glass. Bump to `base1` (#93a1a1)
// if anything that emits `\x1b[37m` (e.g. tmux status bars) breaks.
//
// Palette source: altercation/solarized (iTerm2 scheme).
const SOLARIZED_ANSI: ITheme = {
  black: '#073642',
  red: '#dc322f',
  green: '#859900',
  yellow: '#b58900',
  blue: '#268bd2',
  magenta: '#d33682',
  cyan: '#2aa198',
  white: '#eee8d5',
  brightBlack: '#002b36',
  brightRed: '#cb4b16',
  brightGreen: '#586e75',
  brightYellow: '#657b83',
  brightBlue: '#839496',
  brightMagenta: '#6c71c4',
  brightCyan: '#93a1a1',
  brightWhite: '#fdf6e3'
}

const TRANSPARENT_GLASS: ITheme = {
  background: '#00000000',
  selectionBackground: '#8c8c8c33'
}

// The only thing Schoonover swaps between modes: the fg + cursor pair.
const MODE_TONES = {
  light: { foreground: '#657b83', cursor: '#586e75', cursorAccent: '#fdf6e3' }, // base00 / base01 / base3
  dark: { foreground: '#839496', cursor: '#93a1a1', cursorAccent: '#002b36' } //   base0  / base1  / base03
}

export const terminalTheme = (mode: 'light' | 'dark'): ITheme => ({
  ...SOLARIZED_ANSI,
  ...TRANSPARENT_GLASS,
  ...MODE_TONES[mode]
})

export const isMacPlatform = () => navigator.platform.toLowerCase().includes('mac')

export const addSelectionShortcutLabel = () => (isMacPlatform() ? '⌘L' : 'Ctrl+L')

export function isAddSelectionShortcut(event: KeyboardEvent) {
  return isMacPlatform()
    ? event.metaKey && !event.shiftKey && event.key.toLowerCase() === 'l'
    : event.ctrlKey && !event.shiftKey && event.key.toLowerCase() === 'l'
}

function selectionLineCount(text: string) {
  return Math.max(1, text.trim().split(/\r?\n/).length)
}

export function terminalSelectionLabel(term: Terminal, shellName: string, text: string) {
  const position = term.getSelectionPosition()

  if (position) {
    return position.start.y === position.end.y
      ? `${shellName}:${position.start.y}`
      : `${shellName}:${position.start.y}-${position.end.y}`
  }

  const lines = selectionLineCount(text)

  return `${shellName}:${lines} line${lines === 1 ? '' : 's'}`
}

export function terminalSelectionAnchor(host: HTMLDivElement): CSSProperties | null {
  const selectionRects = Array.from(host.querySelectorAll<HTMLElement>('.xterm-selection div'))
    .map(node => node.getBoundingClientRect())
    .filter(rect => rect.width > 0 && rect.height > 0)

  const rect = selectionRects.at(-1)

  if (!rect) {
    return null
  }

  const hostRect = host.getBoundingClientRect()
  const buttonWidth = 128
  const left = Math.min(Math.max(rect.left - hostRect.left, 8), Math.max(8, host.clientWidth - buttonWidth - 8))
  const top = Math.min(Math.max(rect.bottom - hostRect.top + 4, 8), Math.max(8, host.clientHeight - 34))

  return { left, top }
}
