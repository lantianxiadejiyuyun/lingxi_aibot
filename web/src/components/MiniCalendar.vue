<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChevronLeft, ChevronRight } from '@lucide/vue'
const now = new Date(), month = ref(new Date(now.getFullYear(), now.getMonth(), 1))
const cells = computed(() => {
  const first = (month.value.getDay() + 6) % 7
  const days = new Date(month.value.getFullYear(), month.value.getMonth() + 1, 0).getDate()
  return [...Array(first).fill(null), ...Array.from({ length: days }, (_, i) => i + 1)]
})
function move(by: number) { month.value = new Date(month.value.getFullYear(), month.value.getMonth() + by, 1) }
function isToday(day: number) { return day === now.getDate() && month.value.getMonth() === now.getMonth() && month.value.getFullYear() === now.getFullYear() }
</script>
<template><section class="panel calendar-panel"><div class="section-heading"><h2>{{ month.getFullYear() }} 年 {{ month.getMonth() + 1 }} 月</h2><div class="inline-actions"><button class="icon-button" aria-label="上个月" @click="move(-1)"><ChevronLeft :size="17" /></button><button class="icon-button" aria-label="下个月" @click="move(1)"><ChevronRight :size="17" /></button></div></div><div class="calendar-grid"><span v-for="day in ['一', '二', '三', '四', '五', '六', '日']" :key="day" class="weekday">{{ day }}</span><template v-for="(day, i) in cells" :key="i"><RouterLink v-if="day" :to="{ path: '/events', query: { date: `${month.getFullYear()}-${String(month.getMonth()+1).padStart(2,'0')}-${String(day).padStart(2,'0')}` } }" :class="{ today: isToday(day) }" :aria-label="`${month.getMonth()+1}月${day}日的日程`">{{ day }}</RouterLink><span v-else /></template></div><div class="calendar-footer"><span class="status-dot"></span>给重要的事，留一个位置。</div></section></template>
