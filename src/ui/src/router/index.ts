import { createRouter, createWebHashHistory } from 'vue-router'
// 静态导入所有页面，避免懒加载 chunk 导致点击卡顿
import CaptureView from '../views/CaptureView.vue'
import AutoReplyView from '../views/AutoReplyView.vue'
import AIView from '../views/AIView.vue'
import SettingsView from '../views/SettingsView.vue'
import LogView from '../views/LogView.vue'
import CodecView from '../views/CodecView.vue'
import AnalyzeView from '../views/AnalyzeView.vue'
import RawCaptureView from '../views/RawCaptureView.vue'
import SearchView from '../views/SearchView.vue'
import SendView from '../views/SendView.vue'
import ClashView from '../views/ClashView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/capture' },
    { path: '/capture', name: 'capture', component: CaptureView },
    { path: '/analyze', name: 'analyze', component: AnalyzeView },
    { path: '/auto-reply', name: 'auto-reply', component: AutoReplyView },
    { path: '/ai', name: 'ai', component: AIView },
    { path: '/settings', name: 'settings', component: SettingsView },
    { path: '/logs', name: 'logs', component: LogView },
    { path: '/codec', name: 'codec', component: CodecView },
    { path: '/raw', name: 'raw', component: RawCaptureView },
    { path: '/search', name: 'search', component: SearchView },
    { path: '/send', name: 'send', component: SendView },
    { path: '/clash', name: 'clash', component: ClashView },
  ],
})

export default router
