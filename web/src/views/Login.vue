<script setup lang="ts">
import { ref } from 'vue'
import { ArrowUpRight, LockKeyhole, LoaderCircle } from '@lucide/vue'
import BrandMark from '../components/BrandMark.vue'
import { post, setSession } from '../lib/api'
import type { User } from '../types'
const username = ref(''), password = ref(''), error = ref(''), busy = ref(false)
async function login() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { const result = await post<{ token: string; user: User }>('/login', { username: username.value, password: password.value }); setSession(result.token, result.user) }
  catch (e) { error.value = e instanceof Error ? e.message : '登录失败' }
  finally { busy.value = false }
}
</script>
<template>
  <main class="login-layout">
    <section class="login-story">
      <div class="brand"><BrandMark /><span>灵犀<span class="brand-en">LINGXI</span></span></div>
      <div class="login-copy"><span class="eyebrow">A LITTLE CLARITY, EVERY DAY</span><h1>让想法有归处，<br />让生活有留白。</h1><p>你的计划、灵感和那些值得记住的小事。<br />在这里，慢慢理清。</p></div>
      <div class="login-art" aria-hidden="true"><div class="orbital orbit-one"></div><div class="orbital orbit-two"></div><div class="art-core"><BrandMark /></div><span class="art-label">thoughts, connected.</span></div>
      <div class="login-story-footer"><span>一个属于你的个人空间</span><span>01 / 每一天，都更从容</span></div>
    </section>
    <section class="login-panel"><form @submit.prevent="login"><span class="eyebrow">WELCOME HOME</span><h2>欢迎回到灵犀</h2><p>从这里，继续你的美好日常。</p><label>用户名<input v-model="username" name="username" autocomplete="username" placeholder="输入你的用户名" required autofocus /></label><label>密码<input v-model="password" name="password" type="password" autocomplete="current-password" placeholder="输入登录密码" required /></label><p v-if="error" class="form-error" role="alert">{{ error }}</p><button class="button" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="18" />{{ busy ? '正在进入…' : '进入我的空间' }}<ArrowUpRight v-if="!busy" :size="18" /></button><div class="login-note"><LockKeyhole :size="14" />首次使用，请先初始化管理员账户。</div></form><span class="login-copyright">灵犀 · 让重要的事，有迹可循</span></section>
  </main>
</template>
