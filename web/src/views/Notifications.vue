<script setup lang="ts">
import { ref } from 'vue'
import { Bell, CheckCheck, ChevronLeft, ChevronRight } from '@lucide/vue'
import { api, post } from '../lib/api'
import { date, run } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { Notification } from '../types'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
const offset = ref(0)
const { data, loading, error, load } = useResource(() => api<Notification[]>(`/notifications?limit=30&offset=${offset.value}`), [])
async function read(n: Notification) { await post(`/notifications/${n.id}/read`); await load() }
function paginate(by: number) { offset.value += by * 30; load() }
</script>
<template><PageHeading title="来自灵犀的消息" description="到期的提醒、工作的简报，以及每一件事的回音。" eyebrow="KEEPING YOU IN THE LOOP" /><ResourceState :loading="loading" :error="error" @retry="load"><EmptyState v-if="!data.length" title="暂时没有新的消息" description="重要的事情，灵犀会在这里告诉你。" /><div class="notification-list"><article v-for="n in data" :key="n.id" class="notification-card" :class="{ unread: !n.read_at }"><span class="notification-icon"><Bell :size="20" /></span><div class="grow"><div class="section-heading"><h3>{{ n.title }}</h3><small>{{ date(n.created_at) }}</small></div><p>{{ n.body }}</p><button v-if="!n.read_at" class="text-link" @click="run(() => read(n))"><CheckCheck :size="14" />标为已读</button><small v-else class="muted">已读</small></div></article></div><div v-if="offset || data.length === 30" class="pagination"><button class="button secondary" :disabled="!offset" @click="paginate(-1)"><ChevronLeft :size="16" />上一页</button><button class="button secondary" :disabled="data.length < 30" @click="paginate(1)">下一页<ChevronRight :size="16" /></button></div></ResourceState></template>
