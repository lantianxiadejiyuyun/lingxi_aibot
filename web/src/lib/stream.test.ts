import { describe, expect, it } from 'vitest'
import { readEvents, type StreamEvent } from './stream'
import { renderMarkdown } from './markdown'

function bytes(text: string) {
  const encoded = new TextEncoder().encode(text)
  return new ReadableStream<Uint8Array>({ start(controller) { for (const byte of encoded) controller.enqueue(new Uint8Array([byte])); controller.close() } })
}
describe('chat stream', () => {
  it('preserves UTF-8 across chunk boundaries and accepts CRLF and heartbeat frames', async () => {
    const events: StreamEvent[] = []
    await readEvents(bytes(': keepalive\r\n\r\ndata: {"type":"delta","data":"你好🌿"}\r\n\r\ndata: {"type":"done","data":{}}\n\n'), e => events.push(e))
    expect(events).toEqual([{ type: 'delta', data: '你好🌿' }, { type: 'done', data: {} }])
  })
  it('does not treat a broken connection as a completed answer', async () => {
    const events: StreamEvent[] = []
    await expect(readEvents(bytes('data: {"type":"delta","data":"part"}\n\n'), e => events.push(e))).rejects.toThrow('连接已中断')
    expect(events[0].data).toBe('part')
  })
})
describe('model output', () => {
  it('renders markdown while suppressing script markup, unsafe URLs, and tracking images', () => {
    const html = renderMarkdown('## 标题\n\n<script>alert(1)</script>\n\n[click](javascript:alert(1))\n\n![pixel](https://tracker.test/pixel)\n\n**重点**')
    expect(html).toContain('<h2>标题</h2>')
    expect(html).toContain('<strong>重点</strong>')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('href="javascript:')
    expect(html).not.toContain('<img')
  })
})
