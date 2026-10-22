import { post, put, requestId } from './api'
import { choice, field, localDate, notify, openForm, today, type Field } from './ui'
import { label } from './navigation'
import type { Entry } from '../types'

export function editRecord(kind: string, saved: () => Promise<unknown>, record?: Entry) {
  const data = record?.data || {}
  const fields: Field[] = [{ ...field('title', '标题', record?.title || '', 'text', true), wide: true }]
  if (kind === 'tasks') fields.push(choice('priority', '优先级', [['1', '低优先级'], ['2', '普通'], ['3', '高优先级']], data.priority || 2), field('due_at', '截止时间', localDate(record?.due_at), 'datetime-local'), field('project', '所属项目', data.project || ''))
  if (kind === 'events') fields.push(field('start_at', '开始时间', localDate(data.start_at), 'datetime-local', true), field('end_at', '结束时间', localDate(data.end_at), 'datetime-local', true), field('location', '地点', data.location || ''))
  if (kind === 'expenses') fields.push({ ...field('amount', '金额（元）', data.amount_cents ? (data.amount_cents / 100).toFixed(2) : '', 'number', true), min: 0.01, max: 1000000000, step: '0.01' }, field('date', '消费日期', data.date || today(), 'date', true), field('category', '分类', data.category || ''), field('payment_method', '支付方式', data.payment_method || ''))
  if (kind === 'fitness') fields.push(field('date', '训练日期', data.date || today(), 'date', true), { ...field('minutes', '时长（分钟）', data.minutes || '', 'number'), min: 0 }, field('calories', '消耗（千卡）', data.calories || '', 'number'), field('workout_type', '训练类型', data.workout_type || ''))
  if (kind === 'trips') fields.push(field('start_date', '出发日期', data.start_date || today(), 'date', true), field('end_date', '返回日期', data.end_date || today(), 'date', true), field('companions', '同行人', data.companions || ''))
  if (kind === 'pages') fields.push(choice('public', '可见范围', [['false', '仅自己可见'], ['true', '通过链接公开']], !!data.public))
  if (kind === 'bookmarks') fields.push({ ...field('url', '链接地址', data.url || '', 'url', true), wide: true })
  fields.push(field('content', kind === 'pages' ? 'HTML 内容' : '内容与备注', record?.content || '', 'textarea'))
  if (['tasks', 'notes', 'bookmarks'].includes(kind)) fields.push({ ...field('tags', '标签', (data.tags || []).join(', ')), wide: true, hint: '使用英文逗号分隔，例如：生活, 工作, 灵感' })
  if (kind === 'skills') fields.push({ ...field('steps', '执行步骤', JSON.stringify(data.steps || [], null, 2), 'textarea', true), hint: 'JSON 数组，每项包含 tool 和 arguments；使用允许的内置工具。' }, choice('enabled', '启用状态', [['false', '停用'], ['true', '已审阅并启用']], !!data.enabled))
  openForm({ title: record ? '编辑' + label(kind) : '新建' + label(kind), description: ['tasks', 'events'].includes(kind) ? '时间按当前浏览器的本地时区保存。' : '把值得记录的事，留在这里。', fields, save: async v => {
    const updated = { ...data }
    for (const f of fields) {
      if (['title', 'content', 'due_at', 'amount', 'tags', 'steps'].includes(f.key)) continue
      if (!v[f.key]) { delete updated[f.key]; continue }
      updated[f.key] = f.type === 'datetime-local' ? Math.floor(new Date(v[f.key]).getTime() / 1000) : f.type === 'number' || f.key === 'priority' ? Number(v[f.key]) : ['enabled', 'public'].includes(f.key) ? v[f.key] === 'true' : v[f.key]
    }
    if (kind === 'expenses') updated.amount_cents = Math.round(Number(v.amount) * 100)
    if ('tags' in v) updated.tags = v.tags.split(',').map(t => t.trim()).filter(Boolean)
    if (kind === 'skills') updated.steps = JSON.parse(v.steps)
    const body = { title: v.title, content: v.content, data: updated, status: record?.status || 'open', revision: record?.revision ?? null, due_at: kind === 'tasks' ? (v.due_at ? Math.floor(new Date(v.due_at).getTime() / 1000) : null) : record?.due_at ?? null }
    if (record) await put(`/records/${kind}/${record.id}`, body)
    else await post(`/records/${kind}`, body)
    notify('已保存到你的空间'); await saved()
  } })
}
export function generateImage() {
  openForm({ title: '把想象变成画面', description: '描述画面、风格和细节，灵犀会在后台完成创作。', fields: [field('prompt', '画面描述', '', 'textarea', true)], submit: '开始生成', save: async v => {
    await post('/jobs', { action: 'image', params: { prompt: v.prompt }, request_id: requestId() }); notify('图片任务已提交，可在任务中心查看进度')
  } })
}
