export interface StreamEvent { type: string; data: any }
export async function readEvents(stream: ReadableStream<Uint8Array>, onEvent: (event: StreamEvent) => void) {
  const reader = stream.getReader(), decoder = new TextDecoder()
  let buffer = '', done = false
  try {
    while (true) {
      const chunk = await reader.read()
      buffer += decoder.decode(chunk.value, { stream: !chunk.done })
      if (buffer.length > 5_000_000) throw Error('消息超过接收限制，请重新打开会话')
      let match: RegExpExecArray | null
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, match.index)
        buffer = buffer.slice(match.index + match[0].length)
        const raw = frame.split(/\r?\n/).filter(l => l.startsWith('data:')).map(l => l.slice(5).trimStart()).join('\n')
        if (!raw) continue
        const event = JSON.parse(raw) as StreamEvent
        if (event.type === 'done') done = true
        onEvent(event)
      }
      if (chunk.done) { if (!done) throw Error('连接已中断，重新打开会话可查看已保存的内容'); return }
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
}
