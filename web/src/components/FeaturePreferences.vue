<script setup lang="ts">
import { Image, BookOpen, Settings2 } from '@lucide/vue'
import { api, put } from '../lib/api'
import { choice, field, notify, openForm, run } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { Profile } from '../types'
import ResourceState from './ResourceState.vue'
const {data,loading,error,load}=useResource(async()=>{const [stickers,bili]=await Promise.all([api<Record<string,any>>('/qq/stickers'),api<Record<string,any>>('/bilibili')]);return {stickers,bili}},{stickers:{settings:{}} as Record<string,any>,bili:{config:{}} as Record<string,any>})
async function stickers() {
  const profiles = await api<Profile[]>('/profiles'), c = data.value.stickers.settings
  openForm({ title: '让灵犀挑选喜欢的图片', description: '支持主人私聊和已接入的群。模型看过喜欢才收藏，也可以由主人明确要求；聊天时会按需要从当前会话的收藏中挑一张回复。', fields: [
    choice('enabled', '图片收藏', [['true','开启'], ['false','关闭']], c.enabled),
    choice('auto_review', '收藏方式', [['true','模型挑选 + 主人命令'], ['false','仅主人命令']], c.auto_review),
    {...choice('profile_id', '看图模型', [['','沿用 QQ 对话模型'], ...profiles.map(p => [p.id, p.name] as [string,string])], c.profile_id || ''), wide: true, hint: '用于私聊识图和收藏判断，需支持图片输入。关闭收藏后，私聊仍可看图；GIF 最多识别 10 帧，不足 10 帧读取全部帧。'},
    {...field('taste', '灵犀的选图偏好', c.taste, 'textarea'), hint: '例如：喜欢可爱的小动物、简洁的画面和有趣的聊天表情。'},
    {...field('hourly_limit', '每小时最多判断收藏', c.hourly_limit, 'number', true), min: 1, max: 200, hint: '仅限制自动收藏判断；主人明确命令不占此额度。'},
    {...field('max_items', '每个图库最多收藏张数', c.max_items, 'number', true), min: 1, max: 2000},
    {...field('max_megabytes', '每个图库容量（MB）', c.max_megabytes, 'number', true), min: 1, max: 1024},
  ], save: async v => { await put('/qq/stickers/settings', {enabled: v.enabled === 'true', auto_review: v.auto_review === 'true', profile_id: v.profile_id || null, taste: v.taste, hourly_limit: Number(v.hourly_limit), max_items: Number(v.max_items), max_megabytes: Number(v.max_megabytes)}); notify('收藏偏好已保存'); await load() } })
}
async function bilibili() {
  const profiles = await api<Profile[]>('/profiles'), c = data.value.bili.config
  openForm({ title: '安排一点阅读时间', description: `按 ${data.value.bili.timezone} 运行，保存后从下一次约定时间开始。每次读取字幕会调用你选择的模型。`, fields: [
    choice('enabled', '每天自动发现', [['false', '关闭，仅手动阅读'], ['true', '开启']], c.enabled),
    {...field('daily_at', '每天几点开始', c.daily_at, 'time', true)},
    {...field('keywords', '感兴趣的内容', c.keywords.join('\n'), 'textarea'), hint: '一行一个，或用逗号分隔。最多 8 项，每项 40 字。'},
    {...choice('profile_id', '阅读模型与人设', [['', '沿用默认模型'], ...profiles.map(p => [p.id, p.name] as [string, string])], c.profile_id || ''), wide: true},
    {...field('taste', '想从视频中发现什么', c.taste, 'textarea'), hint: '例如：喜欢真实经验和有新意的观点，留意和日常生活有关的小发现。最多 1000 字。'},
    {...field('daily_limit', '每天最多尝试几条', c.daily_limit, 'number', true), min: 1, max: 20},
    {...field('per_run', '每次最多几条', c.per_run, 'number', true), min: 1, max: 5},
    {...field('recent_days', '发现最近几天的视频', c.recent_days, 'number', true), min: 1, max: 30},
  ], save: async v => {
    await put('/bilibili', { enabled: v.enabled === 'true', daily_at: v.daily_at, keywords: v.keywords.split(/[,，\r\n]+/).map(s => s.trim()).filter(Boolean), daily_limit: Number(v.daily_limit), per_run: Number(v.per_run), recent_days: Number(v.recent_days), profile_id: v.profile_id || null, taste: v.taste })
    notify('阅读安排已保存'); await load()
  } })
}
</script>
<template><ResourceState :loading="loading" :error="error" @retry="load"><div class="integration-grid"><article class="integration-card"><span class="stat-icon lilac"><Image :size="23" /></span><h3>图片与表情</h3><p>看图模型、收藏偏好、判断频率与图库容量。</p><span class="tag neutral">{{data.stickers.settings.enabled?'已开启收藏流程':'收藏流程未开启'}}</span><button class="text-link" @click="run(stickers)"><Settings2 :size="16" />配置图片与收藏</button></article><article class="integration-card"><span class="stat-icon blue"><BookOpen :size="23" /></span><h3>B 站阅读</h3><p>阅读模型、兴趣关键词、每天的阅读时间与上限。</p><span class="tag neutral">{{data.bili.config.enabled?'已安排定时阅读':'仅手动阅读'}}</span><button class="text-link" @click="run(bilibili)"><Settings2 :size="16" />配置阅读安排</button><RouterLink to="/bilibili" class="text-link">管理 B 站登录与阅读记录</RouterLink></article></div></ResourceState></template>
