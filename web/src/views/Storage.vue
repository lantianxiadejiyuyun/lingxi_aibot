<script setup lang="ts">
import { ref } from 'vue'
import { FolderOpen, Folder, File, Plus, ArrowUpLeft, ChevronRight, HardDrive, LoaderCircle } from '@lucide/vue'
import { api, put, requestId, session } from '../lib/api'
import { field, openForm, run } from '../lib/ui'
import { useResource } from '../lib/resource'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
const { data, loading, error, load } = useResource(() => api<{ id: string; name: string }[]>('/storage'), [])
const selected = ref(''), path = ref(''), browsing = ref(false), files = ref<{ name: string; directory: boolean; size: number }[]>([])
let sequence = 0
function add() { openForm({ title: '连接一个存储位置', description: '容器部署时，请先将对应目录挂载到容器。', fields: [field('name', '显示名称', '', 'text', true), field('path', '服务器上的绝对目录', '', 'text', true)], save: async v => { const cfg = await api('/integrations'); cfg.storage = [...(cfg.storage || []), { id: requestId(), ...v }]; await put('/integrations', cfg); await load() } }) }
async function browse(id: string, folder = '') { const current = ++sequence; browsing.value = true; try { const result = await api(`/storage/${id}/files?path=${encodeURIComponent(folder)}`); if (current === sequence) { selected.value = id; path.value = folder; files.value = result.items } } finally { if (current === sequence) browsing.value = false } }
</script>
<template><PageHeading title="你的文件空间" description="浏览已连接的存储，把资料放在触手可及的地方。" eyebrow="EVERYTHING IN ITS PLACE"><button v-if="session.user?.is_admin" class="button" @click="add"><Plus :size="17" />添加存储</button></PageHeading><ResourceState :loading="loading" :error="error" @retry="load"><EmptyState v-if="!data.length" title="给资料，找一个家" description="由管理员添加一个存储目录，即可在这里浏览文件。" /><div class="storage-grid"><button v-for="s in data" :key="s.id" class="storage-card" :class="{ selected: selected === s.id }" @click="run(() => browse(s.id))"><span class="storage-icon"><HardDrive :size="26" /></span><strong>{{ s.name || s.id }}</strong><small>打开目录<ChevronRight :size="15" /></small></button></div><div v-if="browsing" class="loading-state"><LoaderCircle class="spin" />正在打开目录…</div><section v-else-if="selected" class="panel spaced"><div class="section-heading"><h2><FolderOpen :size="19" />{{ path || '根目录' }}</h2><button v-if="path" class="text-link" @click="run(() => browse(selected, path.split('/').slice(0,-1).join('/')))"><ArrowUpLeft :size="16" />上级目录</button></div><EmptyState v-if="!files.length" title="这是一个空目录" /><div v-for="f in files" :key="f.name" class="file-row"><component :is="f.directory ? Folder : File" :size="20" /><button v-if="f.directory" class="text-link grow" @click="run(() => browse(selected, [path, f.name].filter(Boolean).join('/')))">{{ f.name }}</button><span v-else class="grow">{{ f.name }}</span><small>{{ f.directory ? '文件夹' : (f.size / 1024).toFixed(1) + ' KB' }}</small><ChevronRight v-if="f.directory" :size="16" /></div></section></ResourceState></template>
