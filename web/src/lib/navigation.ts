import { LayoutDashboard, Sparkles, CheckCheck, CalendarDays, NotebookPen, Wallet, Activity, Plane,
  PanelsTopLeft, Image, Brain, Shapes, Clock3, Bell, FolderOpen, Settings2, Bookmark, UsersRound, Sticker, Heart, TvMinimalPlay, Sprout } from '@lucide/vue'

export const groups = [
  { title: '工作空间', items: [
    { id: 'dashboard', name: '今日总览', icon: LayoutDashboard }, { id: 'chat', name: '灵犀助手', icon: Sparkles },
  ] },
  { title: '陪伴与社交', items: [
    { id: 'companion', name: '主动陪伴', icon: Heart },
    { id: 'persona', name: '人设养成', icon: Sprout },
    { id: 'bilibili', name: 'B 站书房', icon: TvMinimalPlay },
    { id: 'qq', name: 'QQ 群与会话', icon: UsersRound },
    { id: 'stickers', name: '图片与表情', icon: Sticker },
  ] },
  { title: '日常与生活', items: [
    { id: 'tasks', name: '待办事项', icon: CheckCheck }, { id: 'events', name: '日历日程', icon: CalendarDays },
    { id: 'notes', name: '灵感笔记', icon: NotebookPen }, { id: 'expenses', name: '消费记账', icon: Wallet },
    { id: 'fitness', name: '运动记录', icon: Activity }, { id: 'trips', name: '出行计划', icon: Plane },
  ] },
  { title: '灵感与创作', items: [
    { id: 'memories', name: '长期记忆', icon: Brain }, { id: 'pages', name: '网页作品', icon: PanelsTopLeft },
    { id: 'images', name: '图像空间', icon: Image }, { id: 'skills', name: '我的技能', icon: Shapes },
    { id: 'bookmarks', name: '收藏链接', icon: Bookmark },
  ] },
  { title: '管理', items: [
    { id: 'jobs', name: '任务中心', icon: Clock3 }, { id: 'notifications', name: '消息通知', icon: Bell },
    { id: 'storage', name: '文件空间', icon: FolderOpen }, { id: 'settings', name: '设置', icon: Settings2 },
  ] },
]
export const navigation = groups.flatMap(g => g.items)
export const label = (id: string) => navigation.find(n => n.id === id)?.name || '工作空间'
