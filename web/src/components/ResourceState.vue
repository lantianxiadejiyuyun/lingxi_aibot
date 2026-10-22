<script setup lang="ts">
import { AlertCircle, LoaderCircle } from '@lucide/vue'
defineProps<{ loading: boolean; error: string }>()
defineEmits<{ retry: [] }>()
</script>
<template>
  <div v-if="error" class="state-panel" role="alert"><AlertCircle /><h3>暂时无法加载</h3><p>{{ error }}</p><button class="button secondary" @click="$emit('retry')">重新加载</button></div>
  <div v-else-if="loading" class="loading-state" role="status"><LoaderCircle class="spin" />正在整理你的空间…</div>
  <slot v-else />
</template>
