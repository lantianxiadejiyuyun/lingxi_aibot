import * as Lark from '@larksuiteoapi/node-sdk'
import { mkdir, readdir, readFile, rename, unlink, open } from 'node:fs/promises'
import { createHash, randomUUID } from 'node:crypto'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

export const SDK_VERSION = '1.76.0'
const log = (event) => console.log(JSON.stringify({ at: new Date().toISOString(), event }))
// The SDK can log signed WebSocket URLs and HTTP payloads. Never forward raw logs.
const logger = Object.fromEntries(['trace', 'debug', 'info', 'warn', 'error'].map(k => [k, () => {}]))
export function normalize(data, configKey) {
  const m = data?.message, s = data?.sender
  if (!m || m.chat_type !== 'p2p' || s?.sender_type !== 'user') return null
  if (![m.message_id, m.chat_id, s.sender_id?.open_id].every(x => typeof x === 'string' && x.length > 0 && x.length <= 128)) return null
  let text = ''
  if (m.message_type === 'text') {
    try { text = JSON.parse(m.content).text } catch { return null }
    if (typeof text !== 'string' || Buffer.byteLength(text) > 32000) return null
  }
  const created = Number(m.create_time)
  if (!Number.isSafeInteger(created)) return null
  return { config_key: configKey, message_id: m.message_id, sender_id: s.sender_id.open_id,
    chat_id: m.chat_id, chat_type: 'p2p', sender_type: 'user', message_type: m.message_type,
    text, created_at: created }
}
export class Spool {
  constructor(dir) { this.dir = dir }
  async init() { await mkdir(this.dir, { recursive: true, mode: 0o700 }) }
  async files() { return (await readdir(this.dir)).filter(x => /^[0-9a-f]{64}\.json$/.test(x)).sort() }
  async put(event) {
    const key = createHash('sha256').update(event.config_key + ':' + event.message_id).digest('hex')
    const file = join(this.dir, key + '.json')
    try { await readFile(file); return } catch (e) { if (e.code !== 'ENOENT') throw e }
    if ((await this.files()).length >= 1000) throw new Error('spool_full')
    const tmp = file + '.' + randomUUID() + '.tmp'
    const h = await open(tmp, 'wx', 0o600)
    try { await h.writeFile(JSON.stringify(event)); await h.sync() } finally { await h.close() }
    await rename(tmp, file)
    // fsync the directory too, so an acknowledged event survives a host crash.
    if (process.platform !== 'win32') { const d = await open(this.dir, 'r'); try { await d.sync() } finally { await d.close() } }
  }
  async flush(send) {
    const events = await Promise.all((await this.files()).map(async name => ({ name, event: JSON.parse(await readFile(join(this.dir, name), 'utf8')) })))
    events.sort((a, b) => a.event.created_at - b.event.created_at || a.name.localeCompare(b.name))
    for (const { name, event } of events) {
      await send(event) // Delete only after the backend commits acceptance/dedupe/ignore.
      await unlink(join(this.dir, name)).catch(e => { if (e.code !== 'ENOENT') throw e })
    }
  }
}
// The Rust parent owns stdin in embedded mode. Losing the parent closes the pipe,
// so an orphan cannot keep a second WebSocket consumer alive after a restart.
export function observeParent(input, stop) {
  input.once('end', stop)
  input.once('error', stop)
  input.resume()
}
export async function run() {
  const base = (process.env.LINGXI_URL || 'http://app:8000').replace(/\/$/, '')
  const token = process.env.LINGXI_FEISHU_BRIDGE_TOKEN
  if (!token || !/^[0-9a-f]{64}$/.test(token)) throw new Error('bridge_token_missing')
  const spool = new Spool(process.env.LINGXI_FEISHU_SPOOL || '/data/inbox')
  await spool.init()
  const api = async (path, body) => {
    const r = await fetch(base + '/api/v2/feishu/bridge/' + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(5000), redirect: 'error'
    })
    if (!r.ok) throw new Error('backend_' + r.status)
    const v = await r.json()
    if (!v.ok) throw new Error('backend_rejected')
    return v.data
  }
  let ws, currentKey = '', stopped = false, state = 'idle', configuredAt = 0, lastState = ''
  const stopClient = () => { ws?.close({ force: true }); ws = undefined; currentKey = '' }
  const stop = () => { stopped = true; stopClient() }
  process.once('SIGTERM', stop); process.once('SIGINT', stop)
  if (process.env.LINGXI_FEISHU_MANAGED === 'true') observeParent(process.stdin, stop)
  let nextConfig = 0, nextFlush = 0, backoff = 1000
  while (!stopped) {
    if (Date.now() >= nextConfig) {
      try {
        const cfg = await api('config')
        configuredAt = Date.now()
        if (!cfg.enabled) { stopClient(); state = 'disabled' }
        else if (cfg.config_key !== currentKey || ws?.getConnectionStatus().state === 'failed') {
          stopClient()
          currentKey = cfg.config_key
          const key = currentKey // Never stamp old callbacks with new credentials/revision.
          ws = new Lark.WSClient({ appId: cfg.app_id, appSecret: cfg.app_secret,
            autoReconnect: true, logger, loggerLevel: Lark.LoggerLevel.error,
            handshakeTimeoutMs: 10000, wsConfig: { pingTimeout: 20 } })
          await ws.start({ eventDispatcher: new Lark.EventDispatcher({ logger }).register({
            'im.message.receive_v1': async data => {
              if (stopped || key !== currentKey) return
              const event = normalize(data, key)
              if (event) await spool.put(event)
            }
          }) })
          log('sdk_started')
        }
        state = ws?.getConnectionStatus().state || state
        await api('heartbeat', { state, sdk_version: SDK_VERSION, spool_count: (await spool.files()).length })
        const health = await open(process.env.LINGXI_FEISHU_HEALTH || '/tmp/lingxi-feishu-health', 'w', 0o600)
        try { await health.writeFile(String(Date.now())) } finally { await health.close() }
      } catch {
        state = 'backend_unavailable'
        if (Date.now() - configuredAt > 45000) stopClient()
      }
      if (state !== lastState) { log(state); lastState = state }
      nextConfig = Date.now() + 15000
    }
    if (Date.now() >= nextFlush) {
      try { await spool.flush(event => api('inbound', event)); backoff = 1000 }
      catch { backoff = Math.min(backoff * 2, 30000) }
      nextFlush = Date.now() + backoff
    }
    await new Promise(r => setTimeout(r, 250))
  }
  log('stopped')
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  run().catch(() => { log('bridge_failed'); process.exitCode = 1 })
}
