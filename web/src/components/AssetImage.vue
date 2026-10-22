<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ImageOff } from '@lucide/vue'
import { authHeaders } from '../lib/api'
const props = defineProps<{ id: string; alt: string }>()
const url = ref(''), failed = ref(false), controller = new AbortController()
onMounted(async () => {
  try { const r = await fetch(`/api/v2/assets/${encodeURIComponent(props.id)}`, { headers: authHeaders(), signal: controller.signal }); if (!r.ok) throw Error(); const blob = await r.blob(); if (!controller.signal.aborted) url.value = URL.createObjectURL(blob) }
  catch { if (!controller.signal.aborted) failed.value = true }
})
onBeforeUnmount(() => { controller.abort(); if (url.value) URL.revokeObjectURL(url.value) })
</script>
<template><img v-if="url" :src="url" :alt="alt" class="record-image" /><div v-else class="image-placeholder"><ImageOff v-if="failed" /><span>{{ failed ? '图片暂不可用' : '正在加载图片…' }}</span></div></template>
