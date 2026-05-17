import { atom } from 'nanostores'

export type RightSidebarTabId = 'files' | 'git' | 'terminal' | 'web'

export const $rightSidebarTab = atom<RightSidebarTabId>('files')

export function setRightSidebarTab(tab: RightSidebarTabId) {
  $rightSidebarTab.set(tab)
}
