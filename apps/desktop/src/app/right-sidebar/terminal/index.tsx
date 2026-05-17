import '@xterm/xterm/css/xterm.css'

import { Button } from '@/components/ui/button'
import { Loader } from '@/components/ui/loader'

import { SidebarPanelLabel } from '../../shell/sidebar-label'

import { addSelectionShortcutLabel } from './selection'
import { useTerminalSession } from './use-terminal-session'

interface TerminalTabProps {
  cwd: string
  onAddSelectionToChat: (text: string, label?: string) => void
}

export function TerminalTab({ cwd, onAddSelectionToChat }: TerminalTabProps) {
  const { addSelectionToChat, hostRef, selection, selectionStyle, shellName, status } = useTerminalSession({
    cwd,
    onAddSelectionToChat
  })

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div className="flex h-7 shrink-0 items-center px-3">
        <SidebarPanelLabel>{shellName}</SidebarPanelLabel>
      </div>
      <div className="relative min-h-0 flex-1 px-2 pb-2">
        {status === 'starting' && (
          <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center">
            <Loader
              className="size-8 text-(--ui-text-tertiary)"
              pathSteps={180}
              strokeScale={0.68}
              type="spiral-search"
            />
          </div>
        )}
        {selection.trim() && (
          <div className="absolute z-50 flex items-center gap-1" style={selectionStyle ?? { right: 12, top: 8 }}>
            <Button
              className="h-6 rounded-md px-2 text-[0.68rem] shadow-md backdrop-blur-md"
              onClick={event => event.preventDefault()}
              onMouseDown={event => {
                event.preventDefault()
                event.stopPropagation()
                addSelectionToChat()
              }}
              type="button"
              variant="secondary"
            >
              Add to chat
              <span className="ml-1 text-[0.6rem] text-(--ui-text-tertiary)">{addSelectionShortcutLabel()}</span>
            </Button>
          </div>
        )}
        <div
          className="h-full min-h-0 overflow-hidden px-1 py-1 text-(--ui-text-secondary) [&_.xterm]:h-full [&_.xterm-screen]:bg-transparent! [&_.xterm-viewport]:bg-transparent!"
          ref={hostRef}
        />
      </div>
    </div>
  )
}
