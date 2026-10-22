<script setup lang="ts">
import { Brain, Plus, Sparkles, Trash2, Pin, Clock3 } from '@lucide/vue'
import { api, post, remove, requestId } from '../lib/api'
import { choice, confirmAction, date, field, notify, openForm, run } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { Memory } from '../types'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
const { data, loading, error, load } = useResource(() => api<Memory[]>('/memories'), [])
function add() { openForm({ title: '让灵犀记住一件事', description: '你的偏好、习惯，或一条对你重要的信息。', fields: [field('content', '要记住什么', '', 'textarea', true), choice('importance', '重要程度', [['1','1 · 次要'],['2','2 · 一般'],['3','3 · 普通'],['4','4 · 重要'],['5','5 · 非常重要']], 3), field('expires', '有效期（留空则长期有效）', '', 'datetime-local')], save: async v => { await post('/memories', { content: v.content, importance: Number(v.importance), expires_at: v.expires ? Math.floor(new Date(v.expires).getTime()/1000) : null }); notify('灵犀记住了'); await load() } }) }
function forget(memory: Memory) { confirmAction('忘记这条记忆？', '删除后，自动整理不会再次添加相同的记忆。你仍可以手动重新添加。', async () => { await remove(`/memories/${memory.id}`); await load(); notify('已忘记这条记忆') }) }
async function consolidate() { await post('/jobs', { action: 'consolidate', params: {}, request_id: requestId() }); notify('记忆整理已提交，可在任务中心查看') }
</script>
<template>
  <PageHeading title="那些值得记住的事" description="关于你的偏好、习惯和点滴，让下一次对话更有默契。" eyebrow="A LITTLE MORE UNDERSTANDING"><button class="button secondary" @click="run(consolidate)"><Sparkles :size="16" />整理记忆</button><button class="button" @click="add"><Plus :size="17" />添加记忆</button></PageHeading>
  <div class="memory-banner"><span class="memory-banner-icon"><Brain :size="28" /></span><div><h3>越了解，越懂你。</h3><p>你明确记住的内容优先保留。自动整理只补充新信息，记忆随时可以查看和删除。</p></div><span class="memory-count">{{ data.length }}<small>条记忆</small></span></div>
  <ResourceState :loading="loading" :error="error" @retry="load"><EmptyState v-if="!data.length" title="从一个小小的偏好开始" description="比如：我喜欢简洁的回答，或者周末喜欢去徒步。"><button class="button secondary" @click="add">添加第一条记忆</button></EmptyState><div v-else class="record-grid"><article v-for="m in data" :key="m.id" class="memory-card"><div class="record-meta"><span :class="['tag', m.source === 'manual' ? 'green' : 'lilac']"><Pin v-if="m.source === 'manual'" :size="12" /><Sparkles v-else :size="12" />{{ m.source === 'manual' ? '你希望记住的' : '从对话中了解的' }}</span><span class="importance" :aria-label="'重要度 ' + m.importance + ' / 5'"><i v-for="n in 5" :key="n" :class="{ filled: n <= m.importance }"></i></span></div><p>{{ m.content }}</p><div class="memory-footer"><small><Clock3 :size="12" />{{ m.expires_at ? '有效至 ' + date(m.expires_at) : '长期有效' }}</small><button class="icon-button danger" aria-label="忘记这条记忆" @click="forget(m)"><Trash2 :size="16" /></button></div></article></div></ResourceState>
</template>
