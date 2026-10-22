<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { ImageOff } from '@lucide/vue'
import { authHeaders } from '../lib/api'
const props = defineProps<{ id: string; title: string; original?: boolean }>()
const url = ref(''), failed = ref(false)
let controller: AbortController | undefined
function clear() { controller?.abort(); if (url.value) URL.revokeObjectURL(url.value); url.value = '' }
watch(() => [props.id, props.original], async () => {
  clear(); failed.value = false
  const current = new AbortController(); controller = current
  try {
    const response = await fetch(`/api/v2/qq/stickers/${encodeURIComponent(props.id)}/image?thumbnail=${!props.original}`, { headers: authHeaders(), signal: current.signal })
    if (!response.ok) throw Error('图片不可用')
    const blob = await response.blob()
    if (!current.signal.aborted) url.value = URL.createObjectURL(blob)
  } catch { if (!current.signal.aborted) failed.value = true }
}, { immediate: true })
onBeforeUnmount(clear)
</script>
<template>
  <img v-if="url" :src="url" :alt="title" class="sticker-image" />
  <span v-else class="sticker-placeholder" role="status"><ImageOff v-if="failed" :size="23" /><span>{{ failed ? '图片暂不可用' : '正在加载…' }}</span></span>
</template>
