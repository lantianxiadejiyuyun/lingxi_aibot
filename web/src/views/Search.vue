<script setup lang="ts">
import { ref } from 'vue'
import { Search, ArrowUpRight, LoaderCircle } from '@lucide/vue'
import { api } from '../lib/api'
import { label } from '../lib/navigation'
import PageHeading from '../components/PageHeading.vue'
import EmptyState from '../components/EmptyState.vue'
const query = ref(''), busy = ref(false), searched = ref(false), error = ref('')
const items = ref<{ kind: string; id: string; title: string; text: string }[]>([])
async function search() { if (!query.value.trim() || busy.value) return; busy.value = true; error.value = ''; try { items.value = (await api(`/search?q=${encodeURIComponent(query.value)}`)).items; searched.value = true } catch (e) { error.value = e instanceof Error ? e.message : '搜索失败' } finally { busy.value = false } }
function target(kind: string) { return kind === 'memory' || kind === 'memories' ? '/memories' : ['message','chat','conversation'].includes(kind) ? '/chat' : '/' + kind }
</script>
<template><PageHeading title="找回那个想法" description="搜索笔记、对话和记忆，重新连接那些有价值的片段。" eyebrow="NOTHING GOOD GETS LOST" /><form class="large-search" @submit.prevent="search"><Search :size="22" /><input v-model="query" type="search" aria-label="搜索关键词" placeholder="想找点什么？" maxlength="200" autofocus /><button class="button" :disabled="busy || !query.trim()"><LoaderCircle v-if="busy" class="spin" :size="16" />搜索</button></form><p v-if="error" class="form-error" role="alert">{{ error }}</p><div v-if="items.length" class="search-results"><article v-for="(item,i) in items" :key="i" class="panel"><span class="tag neutral">{{ label(item.kind) }}</span><h3>{{ item.title || '一段值得回看的内容' }}</h3><p>{{ item.text }}</p><RouterLink class="text-link" :to="target(item.kind)">前往查看<ArrowUpRight :size="15" /></RouterLink></article></div><EmptyState v-else :title="searched ? '暂时没有找到匹配的内容' : '让散落的想法，重新相遇'" :description="searched ? '试试更短的关键词，或换个说法。' : '输入一个关键词，看看灵犀为你记下了什么。'" /></template>
