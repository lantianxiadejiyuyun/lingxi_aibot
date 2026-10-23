import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as Lark from '@larksuiteoapi/node-sdk'
import { Spool, normalize, SDK_VERSION } from './bridge.mjs'
const message = (text = '你好') => ({ sender: { sender_type: 'user', sender_id: { open_id: 'ou_owner' } }, message: { message_id: 'om_1', chat_id: 'oc_1', chat_type: 'p2p', message_type: 'text', content: JSON.stringify({ text }), create_time: String(Date.now()) } })
test('only private human messages are normalized; images are explicitly unsupported', () => {
  const d = message()
  assert.equal(normalize(d, 'key').text, '你好')
  d.message.chat_type = 'group'; assert.equal(normalize(d, 'key'), null)
  d.message.chat_type = 'p2p'; d.sender.sender_type = 'app'; assert.equal(normalize(d, 'key'), null)
  d.sender.sender_type = 'user'; d.message.content = 'bad'; assert.equal(normalize(d, 'key'), null)
  d.message.message_type = 'image'; assert.equal(normalize(d, 'key').text, '')
  assert.equal(normalize(message('鱼'.repeat(11000)), 'key'), null)
})
test('official SDK dispatches its real event envelope and propagates durable-write failures', async () => {
  let received
  const dispatcher = new Lark.EventDispatcher({ logger: Object.fromEntries(['error','warn','info','debug','trace'].map(k=>[k,()=>{}])) }).register({ 'im.message.receive_v1': async d => { received = normalize(d, 'key') } })
  const envelope = { schema: '2.0', header: { event_id: 'event', event_type: 'im.message.receive_v1', create_time: String(Date.now()), app_id: 'cli_0123456789abcdef', tenant_key: 'test' }, event: message() }
  await dispatcher.invoke(envelope, { needCheck: false })
  assert.equal(received.text, '你好')
  dispatcher.register({ 'im.message.receive_v1': async () => { throw new Error('disk-full') } })
  await assert.rejects(dispatcher.invoke(envelope, { needCheck: false }), /disk-full/)
  const client = new Lark.WSClient({ appId: 'cli_0123456789abcdef', appSecret: 'test' })
  assert.equal(client.getConnectionStatus().state, 'idle')
  client.close({ force: true }); assert.equal(SDK_VERSION, '1.76.0')
})
test('spool survives restart, deduplicates and retains failed delivery until committed', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'lingxi-feishu-'))
  try {
    let spool = new Spool(dir); await spool.init()
    const one = normalize(message(), 'a'.repeat(64))
    await spool.put(one); await spool.put(one); assert.equal((await spool.files()).length, 1)
    spool = new Spool(dir); await spool.init()
    await assert.rejects(spool.flush(async () => { throw new Error('offline') }))
    assert.equal((await spool.files()).length, 1)
    let sent = 0
    await spool.flush(async v => { assert.deepEqual(v, one); sent++ })
    await spool.flush(async () => { sent++ }); assert.equal(sent, 1)
    assert.equal((await spool.files()).length, 0)
  } finally { await rm(dir, { recursive: true, force: true }) }
})
test('spool delivers oldest message first and preserves the original configuration revision', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'lingxi-feishu-'))
  try {
    const spool = new Spool(dir); await spool.init()
    const one = normalize(message(), 'old-key')
    await spool.put({ ...one, message_id: 'new', created_at: 20 })
    await spool.put({ ...one, message_id: 'old', created_at: 10 })
    const sent = []
    await spool.flush(async v => { assert.equal(v.config_key, 'old-key'); sent.push(v.message_id) })
    assert.deepEqual(sent, ['old','new'])
  } finally { await rm(dir, { recursive: true, force: true }) }
})
