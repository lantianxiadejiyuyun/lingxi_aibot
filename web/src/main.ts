import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import Dashboard from './views/Dashboard.vue'
import Chat from './views/Chat.vue'
import Records from './views/Records.vue'
import Memories from './views/Memories.vue'
import Jobs from './views/Jobs.vue'
import Settings from './views/Settings.vue'
import Notifications from './views/Notifications.vue'
import Storage from './views/Storage.vue'
import Search from './views/Search.vue'
import GroupMemory from './views/GroupMemory.vue'
import GroupConversation from './views/GroupConversation.vue'
import Stickers from './views/Stickers.vue'
import Companion from './views/Companion.vue'
import Bilibili from './views/Bilibili.vue'
import './style.css'
import './styles/refinements.css'
import './styles/theme.css'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' }, { path: '/dashboard', component: Dashboard },
    { path: '/chat', component: Chat }, { path: '/memories', component: Memories },
    { path: '/jobs', component: Jobs }, { path: '/settings', component: Settings },
    { path: '/notifications', component: Notifications }, { path: '/storage', component: Storage },
    { path: '/search', component: Search },
    { path: '/qq', component: GroupMemory },
    { path: '/qq/groups/:group', component: GroupConversation },
    { path: '/stickers', component: Stickers },
    { path: '/companion', component: Companion },
    { path: '/bilibili', component: Bilibili },
    { path: '/:kind(tasks|events|notes|expenses|fitness|trips|pages|images|skills|bookmarks)', component: Records, props: true },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
createApp(App).use(router).mount('#app')
