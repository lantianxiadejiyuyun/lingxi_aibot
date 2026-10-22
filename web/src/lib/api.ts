import { reactive } from 'vue'
import type { User } from '../types'

export const session = reactive({ token: sessionStorage.getItem('lingxi_token') || '', user: null as User | null })
export function setSession(token: string, user: User | null) {
  session.token = token
  session.user = user
  if (token) sessionStorage.setItem('lingxi_token', token)
  else sessionStorage.removeItem('lingxi_token')
}
export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v2${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...options.headers },
  }).catch((error: unknown) => {
    if (error instanceof Error && error.name === 'AbortError') throw error
    throw new Error('暂时无法连接服务，请检查网络或稍后重试。')
  })
  const value = await response.json().catch(() => ({ error: `请求失败 (${response.status})` }))
  if (response.status === 401 && path !== '/login') setSession('', null)
  if (!response.ok || value.ok === false) throw new Error(value.error || `请求失败 (${response.status})`)
  return value.data as T
}
export function authHeaders(): Record<string, string> { return session.token ? { Authorization: `Bearer ${session.token}` } : {} }
export const post = <T = any>(path: string, body = {}) => api<T>(path, { method: 'POST', body: JSON.stringify(body) })
export const put = <T = any>(path: string, body: unknown) => api<T>(path, { method: 'PUT', body: JSON.stringify(body) })
export const patch = <T = any>(path: string, body: unknown) => api<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
export const remove = (path: string) => api(path, { method: 'DELETE' })
export function requestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
}
