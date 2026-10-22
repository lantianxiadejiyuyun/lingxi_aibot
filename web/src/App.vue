<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search, Bell, ChevronRight, ChevronDown, Menu, X, LogOut, Check, AlertCircle, Sparkles } from '@lucide/vue'
import { api, post, session, setSession } from './lib/api'
import { groups, label } from './lib/navigation'
import { notices, run } from './lib/ui'
import BrandMark from './components/BrandMark.vue'
import FormModal from './components/FormModal.vue'
import Login from './views/Login.vue'
import type { User } from './types'
const route = useRoute(), router = useRouter()
const ready = ref(false), mobileOpen = ref(false)
const bootError = ref('')
const searchShortcut = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘ K' : 'Ctrl K'
const page = computed(() => route.path.split('/')[1])
const today = new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' })
async function restoreSession() {
  ready.value = false; bootError.value = ''
  if (session.token) {
    try { session.user = await api<User>('/me') }
    catch (e) { if (session.token) bootError.value = e instanceof Error ? e.message : '暂时无法连接服务' }
  }
  ready.value = true
}
onMounted(() => {
  void restoreSession()
  window.addEventListener('keydown', shortcut)
})
onUnmounted(() => window.removeEventListener('keydown', shortcut))
watch(() => route.fullPath, () => { mobileOpen.value = false; document.title = `${page.value === 'search' ? '搜索' : label(page.value)} · 灵犀` }, { immediate: true })
function shortcut(e: KeyboardEvent) { if ((e.metaKey || e.ctrlKey) && e.key === 'k' && session.user) { e.preventDefault(); router.push('/search') } if (e.key === 'Escape') mobileOpen.value = false }
async function logout() { await post('/logout'); setSession('', null) }
</script>
<template>
  <div v-if="!ready" class="boot-screen"><BrandMark /><span>正在打开你的空间…</span></div>
  <div v-else-if="bootError" class="boot-screen boot-error" role="alert"><BrandMark /><h1>空间暂时没有连上</h1><p>{{ bootError }}</p><small>登录信息已保留，连接恢复后可以继续。</small><button class="button" @click="restoreSession">重新连接</button></div>
  <Login v-else-if="!session.user" />
  <div v-else class="workspace-shell" :class="{ 'mobile-open': mobileOpen }">
    <a class="skip-link" href="#main-content" @click.prevent="($refs.mainContent as HTMLElement)?.focus()">跳到主要内容</a>
    <button v-if="mobileOpen" class="sidebar-scrim" aria-label="关闭导航" @click="mobileOpen = false"></button>
    <aside id="workspace-navigation" class="sidebar">
      <RouterLink class="brand" to="/dashboard"><BrandMark /><span>灵犀<span class="brand-en">LINGXI</span></span><span class="version-pill">2.0</span></RouterLink>
      <button class="workspace-select" @click="router.push('/settings')"><span class="workspace-avatar">{{ session.user.username[0]?.toUpperCase() }}</span><span><strong>我的工作空间</strong><small>Personal workspace</small></span><ChevronDown :size="15" /></button>
      <nav aria-label="主导航"><div v-for="group in groups" :key="group.title" class="nav-group"><p class="nav-caption">{{ group.title }}</p><RouterLink v-for="item in group.items" :key="item.id" :to="'/' + item.id" :class="{ 'assistant-link': item.id === 'chat', 'router-link-active': page === item.id }"><component :is="item.icon" :size="18" :stroke-width="1.7" /><span>{{ item.name }}</span><span v-if="item.id === 'chat'" class="ai-badge">AI</span><span class="nav-active-dot"></span></RouterLink></div></nav>
      <div class="sidebar-bottom"><span class="status-dot"></span><span>属于你的私有空间</span><button class="icon-button" title="退出登录" aria-label="退出登录" @click="run(logout)"><LogOut :size="16" /></button></div>
    </aside>
    <div class="main-shell"><header class="topbar"><div class="breadcrumb"><button class="icon-button mobile-menu" :aria-expanded="mobileOpen" aria-controls="workspace-navigation" :aria-label="mobileOpen ? '关闭导航' : '打开导航'" @click="mobileOpen = !mobileOpen"><Menu /></button><span>我的工作空间</span><ChevronRight :size="13" /><strong>{{ page === 'search' ? '搜索' : label(page) }}</strong></div><div class="topbar-actions"><span class="today-label">{{ today }}</span><button class="global-search" aria-label="搜索所有内容" @click="router.push('/search')"><Search :size="16" /><span>搜索</span><kbd>{{ searchShortcut }}</kbd></button><RouterLink to="/notifications" class="icon-button" aria-label="消息通知"><Bell :size="19" /></RouterLink><RouterLink to="/settings?tab=account" class="user-avatar" aria-label="账户设置">{{ session.user.username[0]?.toUpperCase() }}</RouterLink></div></header>
      <main id="main-content" ref="mainContent" tabindex="-1" class="main-content" :class="{ 'chat-content': page === 'chat' }"><RouterView v-slot="{ Component }"><component :is="Component" :key="route.path" /></RouterView></main>
      <footer v-if="page !== 'chat'" class="workspace-footer"><span><Sparkles :size="13" /> 灵犀，陪你理清每一天。</span><span>YOUR PERSONAL SPACE</span></footer>
    </div>
  </div>
  <FormModal />
  <div class="toast-stack" aria-live="polite"><TransitionGroup name="toast"><div v-for="notice in notices" :key="notice.id" class="toast" :class="{ error: notice.error }"><AlertCircle v-if="notice.error" :size="18" /><Check v-else :size="18" /><span>{{ notice.message }}</span><button class="icon-button" aria-label="关闭提示" @click="notices.splice(notices.indexOf(notice), 1)"><X :size="15" /></button></div></TransitionGroup></div>
</template>
