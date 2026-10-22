<script setup lang="ts">
import { Sprout } from '@lucide/vue'
defineProps<{ title: string; description?: string }>()
</script>
<template><div class="empty-state"><div class="empty-icon"><Sprout :size="28" /></div><h3>{{ title }}</h3><p>{{ description || '留一点空间，给接下来的好想法。' }}</p><slot /></div></template>
