import { reactive } from 'vue'
import { session } from './api'

export const notices = reactive<{ id: number; message: string; error: boolean }[]>([])
let sequence = 0
export function notify(message: string, error = false) {
  const id = ++sequence
  notices.push({ id, message, error })
  setTimeout(() => { const i = notices.findIndex(n => n.id === id); if (i >= 0) notices.splice(i, 1) }, 5000)
}
export async function run(action: () => Promise<unknown>) {
  try { return await action() } catch (error) { notify(error instanceof Error ? error.message : '操作失败，请重试', true) }
}
export function date(value?: number | null, short = false) {
  if (!value) return '未设置'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: session.user?.timezone || 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    ...(short ? {} : { hour: '2-digit', minute: '2-digit', hour12: false }),
  }).format(new Date(value * 1000))
}
export function localDate(value?: number | null) {
  if (!value) return ''
  const d = new Date(value * 1000)
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
export function today() { return localDate(Date.now() / 1000).slice(0, 10) }
export function recordBody(r: import('../types').Entry, changes = {}) {
  return { title: r.title, content: r.content, data: r.data, status: r.status, due_at: r.due_at, revision: r.revision, ...changes }
}
export interface Field {
  key: string; label: string; type?: string; value?: string | number | boolean;
  required?: boolean; options?: [string, string][]; hint?: string; wide?: boolean;
  min?: number; max?: number; step?: string; readonly?: boolean;
  presets?: [number, string][];
}
export const field = (key: string, label: string, value: Field['value'] = '', type = 'text', required = false): Field => ({ key, label, value, type, required })
export const choice = (key: string, label: string, options: [string, string][], value: Field['value']): Field => ({ key, label, options, value, type: 'select' })
export interface FormConfig { title: string; description?: string; fields?: Field[]; submit?: string; danger?: boolean; save: (values: Record<string, string>) => Promise<unknown> }
export const modal = reactive({ config: null as FormConfig | null, version: 0 })
export function openForm(config: FormConfig) { modal.version++; modal.config = config }
export function confirmAction(title: string, description: string, save: () => Promise<unknown>) {
  openForm({ title, description, fields: [], submit: '确认', danger: true, save })
}
