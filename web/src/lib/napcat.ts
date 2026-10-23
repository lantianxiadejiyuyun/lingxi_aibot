export function handoffNapcat(popup: Window, url: URL, credential: string, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const origin = url.origin
    let nonce = ''
    const cleanup = () => {
      window.clearTimeout(timer)
      window.removeEventListener('message', receive)
      signal.removeEventListener('abort', abort)
    }
    const finish = (error?: Error) => {
      cleanup()
      if (error) reject(error)
      else resolve()
    }
    const abort = () => finish(new Error('已取消 NapCat 登录。'))
    const receive = (event: MessageEvent) => {
      if (event.origin !== origin || event.source !== popup || !event.data || typeof event.data !== 'object') return
      const message = event.data
      if (message.type === 'lingxi-napcat-ready' && !nonce && typeof message.nonce === 'string' && /^[a-f0-9]{32}$/.test(message.nonce)) {
        nonce = message.nonce
        popup.postMessage({ type: 'lingxi-napcat-session', nonce, credential }, origin)
      } else if (nonce && message.nonce === nonce) {
        if (message.type === 'lingxi-napcat-complete') finish()
        else if (message.type === 'lingxi-napcat-error') finish(new Error('NapCat 无法保存登录状态，请允许浏览器存储后重试。'))
      }
    }
    const timer = window.setTimeout(() => finish(new Error('NapCat 登录连接超时，请确认服务可访问后重试。')), 30_000)
    window.addEventListener('message', receive)
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) { abort(); return }
    url.hash = new URLSearchParams({ origin: window.location.origin }).toString()
    try { popup.location.replace(url.href) }
    catch { finish(new Error('无法打开 NapCat，请允许本站打开弹窗后重试。')) }
  })
}
