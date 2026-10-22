export interface User { id: string; username: string; timezone: string; is_admin: boolean }
export interface Entry {
  id: string; kind: string; title: string; content: string;
  data: Record<string, any>; status: string; due_at: number | null;
  revision: number; created_at: number; updated_at: number;
}
export interface Page<T> { items: T[]; has_more: boolean }
export interface Profile {
  id: string; name: string; protocol: string; base_url: string; model: string;
  api_key: string; context_window: number; max_output: number; system_prompt: string;
}
export interface Conversation { id: string; title: string; profile_id: string | null; updated_at: number }
export interface ChatMessage { id: number; role: string; content: string; tools?: { name: string; result: unknown }[] }
export interface Memory { id: string; content: string; source: string; importance: number; expires_at: number | null }
export interface Job { id: string; action: string; status: string; result: string | null; error: string | null; created_at: number }
export interface Schedule { id: string; name: string; action: string; cron: string; timezone: string; enabled: boolean; next_run_at: number }
export interface Notification { id: string; title: string; body: string; read_at: number | null; created_at: number }
export interface Stats { pending_tasks: number; memories: number; unread_notifications: number; counts: Record<string, number> }
