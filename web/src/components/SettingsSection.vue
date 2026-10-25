<script setup lang="ts">
import { ref, watch } from 'vue'
const props=defineProps<{title:string;description:string;initialOpen?:boolean}>()
const opened=ref(!!props.initialOpen),visited=ref(!!props.initialOpen)
watch(()=>props.initialOpen,v=>{opened.value=!!v;if(v)visited.value=true})
function toggle(e:Event){opened.value=(e.target as HTMLDetailsElement).open;if(opened.value)visited.value=true}
</script>
<template><details class="settings-section" :open="opened" @toggle="toggle"><summary><strong>{{title}}</strong><span>{{description}}</span></summary><div v-if="visited" class="settings-section-content"><slot /></div></details></template>
