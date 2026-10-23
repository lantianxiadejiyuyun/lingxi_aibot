/* Receives an ordinary NapCat session; the management key never enters this page. */
function startNapcatHandoff(browser) {
  const parent = browser.opener
  browser.opener = null
  const fragment = new URLSearchParams(browser.location.hash.slice(1))
  browser.history.replaceState(null, '', browser.location.pathname)
  const status = browser.document.getElementById('status')
  const show = text => { if (status) status.textContent = text }
  let expectedOrigin
  try {
    expectedOrigin = new URL(fragment.get('origin'))
    if (!['http:', 'https:'].includes(expectedOrigin.protocol) || expectedOrigin.origin !== fragment.get('origin') || !parent) throw new Error()
  } catch {
    show('请从灵犀的「一键进入 NapCat」按钮打开此页面。')
    return
  }
  const bytes = browser.crypto.getRandomValues(new Uint8Array(16))
  const nonce = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  const origin = expectedOrigin.origin
  let active = true
  const cleanup = () => {
    active = false
    browser.clearTimeout(timer)
    browser.removeEventListener('message', receive)
    browser.removeEventListener('pagehide', cleanup)
  }
  const receive = event => {
    if (!active || event.source !== parent || event.origin !== origin || event.data?.type !== 'lingxi-napcat-session' || event.data.nonce !== nonce) return
    const credential = event.data.credential
    if (typeof credential !== 'string' || !credential || credential.length > 8192) return
    cleanup()
    try {
      browser.localStorage.setItem('token', credential)
      parent.postMessage({ type: 'lingxi-napcat-complete', nonce }, origin)
      browser.location.replace(new URL('../qq_login', browser.location.href).href)
    } catch {
      show('无法保存登录状态，请允许浏览器存储，或使用手动登录。')
      parent.postMessage({ type: 'lingxi-napcat-error', nonce }, origin)
    }
  }
  const timer = browser.setTimeout(() => {
    cleanup()
    show('连接已超时，请返回灵犀重新点击「一键进入 NapCat」。')
  }, 30_000)
  browser.addEventListener('message', receive)
  browser.addEventListener('pagehide', cleanup)
  parent.postMessage({ type: 'lingxi-napcat-ready', nonce }, origin)
}
startNapcatHandoff(window)
