import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'

const source = await readFile(new URL('./bridge.js', import.meta.url), 'utf8')
function setup({ parentOrigin = 'http://nas:18081', hasOpener = true, storageFailure = false } = {}) {
  const listeners = new Map(), sent = [], saved = [], replaced = [], timers = new Map()
  const parent = { postMessage: (data, origin) => sent.push({ data, origin }) }
  const status = { textContent: '' }
  const location = { hash: `#origin=${encodeURIComponent(parentOrigin)}`, pathname: '/webui/lingxi-login/', href: 'http://nas:6099/webui/lingxi-login/', replace: url => replaced.push(url) }
  const browser = {
    opener: hasOpener ? parent : null, location, document: { getElementById: () => status },
    history: { replaceState: (_state, _unused, path) => { location.href = `http://nas:6099${path}`; location.hash = '' } },
    crypto: { getRandomValues: bytes => { bytes.fill(10); return bytes } },
    addEventListener: (name, listener) => listeners.set(name, listener),
    removeEventListener: (name, listener) => { if (listeners.get(name) === listener) listeners.delete(name) },
    setTimeout: (fn, ms) => { timers.set(1, { fn, ms }); return 1 }, clearTimeout: id => timers.delete(id),
    localStorage: { setItem: (key, value) => { if (storageFailure) throw new Error('disabled'); saved.push({ key, value }) } },
  }
  vm.runInNewContext(source, { window: browser, URL, URLSearchParams, Uint8Array })
  const nonce = sent[0]?.data.nonce
  const message = (overrides = {}) => listeners.get('message')?.({ source: parent, origin: parentOrigin, data: { type: 'lingxi-napcat-session', nonce, credential: 'temporary-session' }, ...overrides })
  return { browser, parent, status, listeners, sent, saved, replaced, timers, message, nonce }
}

test('detaches opener and accepts a single matching session without a credential URL or reply', () => {
  const app = setup()
  assert.equal(app.browser.opener, null)
  assert.equal(app.browser.location.hash, '')
  assert.equal(app.sent[0].data.type, 'lingxi-napcat-ready')
  assert.equal(app.sent[0].origin, 'http://nas:18081')
  app.message()
  assert.deepEqual(app.saved, [{ key: 'token', value: 'temporary-session' }])
  assert.deepEqual(app.replaced, ['http://nas:6099/webui/qq_login'])
  assert.equal(app.sent[1].data.type, 'lingxi-napcat-complete')
  assert.equal(JSON.stringify(app.sent).includes('temporary-session'), false)
  assert.equal(app.listeners.size, 0)
  assert.equal(app.timers.size, 0)
  app.message()
  assert.equal(app.saved.length, 1)
})

test('rejects wrong origin, window, nonce, message type, and malformed credentials', () => {
  const app = setup()
  app.message({ origin: 'https://attacker.example' })
  app.message({ source: {} })
  app.message({ data: { type: 'lingxi-napcat-session', nonce: 'wrong', credential: 'bad' } })
  app.message({ data: { type: 'anything', nonce: app.nonce, credential: 'bad' } })
  app.message({ data: { type: 'lingxi-napcat-session', nonce: app.nonce, credential: { token: 'bad' } } })
  assert.equal(app.saved.length, 0)
  assert.equal(app.replaced.length, 0)
  app.message()
  assert.equal(app.saved.length, 1)
})

test('requires a valid HTTP origin and a parent window', () => {
  for (const options of [{ hasOpener: false }, { parentOrigin: 'javascript:alert(1)' }, { parentOrigin: 'http://nas:18081/path' }, { parentOrigin: 'http://name:password@nas:18081' }]) {
    const app = setup(options)
    assert.equal(app.sent.length, 0)
    assert.equal(app.listeners.size, 0)
    assert.match(app.status.textContent, /一键进入/)
  }
})

test('reports storage failure without navigating or exposing the session', () => {
  const app = setup({ storageFailure: true })
  app.message()
  assert.equal(app.replaced.length, 0)
  assert.equal(app.sent[1].data.type, 'lingxi-napcat-error')
  assert.equal(JSON.stringify(app.sent).includes('temporary-session'), false)
  assert.match(app.status.textContent, /无法保存/)
  assert.equal(app.listeners.size, 0)
})

test('expires an unused handoff and ignores later messages', () => {
  const app = setup()
  assert.equal(app.timers.get(1).ms, 30_000)
  app.timers.get(1).fn()
  app.message()
  assert.equal(app.saved.length, 0)
  assert.equal(app.listeners.size, 0)
  assert.match(app.status.textContent, /超时/)
})

test('removes message listeners when the handoff page is left', () => {
  const app = setup()
  app.listeners.get('pagehide')()
  app.message()
  assert.equal(app.saved.length, 0)
  assert.equal(app.listeners.size, 0)
  assert.equal(app.timers.size, 0)
})
