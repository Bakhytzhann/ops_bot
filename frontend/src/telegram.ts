interface TelegramWebApp {
  initData: string
  ready: () => void
  expand: () => void
  isVersionAtLeast?: (version: string) => boolean
  setHeaderColor?: (color: string) => void
  setBackgroundColor?: (color: string) => void
  openLink: (url: string) => void
  BackButton?: {
    show: () => void
    hide: () => void
    onClick: (handler: () => void) => void
    offClick: (handler: () => void) => void
  }
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp }
  }
}

export function telegramLaunch() {
  const bridge = window.Telegram?.WebApp
  if (!bridge?.initData) return null

  const backButton = bridge.isVersionAtLeast?.('6.1') ? bridge.BackButton : undefined
  return {
    headers: { Authorization: `tma ${bridge.initData}` },
    ready() {
      bridge.ready()
      bridge.expand()
      if (bridge.isVersionAtLeast?.('6.9')) {
        bridge.setHeaderColor?.('#090c10')
        bridge.setBackgroundColor?.('#090c10')
      }
    },
    openLink: (url: string) => bridge.openLink(url),
    bindBack: backButton && ((handler: () => void) => {
      backButton.onClick(handler)
      backButton.show()
      return () => {
        backButton.offClick(handler)
        backButton.hide()
      }
    }),
  }
}
