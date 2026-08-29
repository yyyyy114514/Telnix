import { createRouter, createWebHashHistory } from 'vue-router'
// 默认首页（CaptureView）静态导入，保证首屏立即可见、点击「抓包」零延迟。
import CaptureView from '../views/CaptureView.vue'

// 其余页面懒加载：避免首屏一次性加载全部视图（Settings/Analyze/Raw/AI/WS 等重量级页面
// 体积很大，多数会话根本不会打开）。为消除「点击才下载」的卡顿感，下方在路由就绪后
// 于浏览器空闲时预取这些 chunk（见 prefetchViews）。
const AutoReplyView = () => import('../views/AutoReplyView.vue')
const AIView = () => import('../views/AIView.vue')
const SettingsView = () => import('../views/SettingsView.vue')
const LogView = () => import('../views/LogView.vue')
const AnalyzeView = () => import('../views/AnalyzeView.vue')
const RawCaptureView = () => import('../views/RawCaptureView.vue')
const WebSocketView = () => import('../views/WebSocketView.vue')
const SearchView = () => import('../views/SearchView.vue')
const SendView = () => import('../views/SendView.vue')
const ClashView = () => import('../views/ClashView.vue')
const DnsHijackView = () => import('../views/DnsHijackView.vue')
const CookiesView = () => import('../views/CookiesView.vue')
const SiteMapView = () => import('../views/SiteMapView.vue')
const ToolsView = () => import('../views/ToolsView.vue')
const TimelineView = () => import('../views/TimelineView.vue')
const DelayView = () => import('../views/DelayView.vue')
const RecordView = () => import('../views/RecordView.vue')
const MockView = () => import('../views/MockView.vue')
const WorkflowView = () => import('../views/WorkflowView.vue')

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/capture' },
    { path: '/capture', name: 'capture', component: CaptureView },
    { path: '/analyze', name: 'analyze', component: AnalyzeView },
    { path: '/auto-reply', name: 'auto-reply', component: AutoReplyView },
    { path: '/ai', name: 'ai', component: AIView },
    { path: '/settings', name: 'settings', component: SettingsView },
    { path: '/tools', name: 'tools', component: ToolsView },
    { path: '/logs', name: 'logs', component: LogView },
    { path: '/raw', name: 'raw', component: RawCaptureView },
    { path: '/ws', name: 'ws', component: WebSocketView },
    { path: '/search', name: 'search', component: SearchView },
    { path: '/send', name: 'send', component: SendView },
    { path: '/clash', name: 'clash', component: ClashView },
    // CoolUI 已下线（保留文件，仅注释路由）
    // { path: '/cool', name: 'cool', component: CoolUIView },
    { path: '/dns-hijack', name: 'dns-hijack', component: DnsHijackView },
    { path: '/cookies', name: 'cookies', component: CookiesView },
    { path: '/site-map', name: 'site-map', component: SiteMapView },
    { path: '/timeline', name: 'timeline', component: TimelineView },
    { path: '/delay', name: 'delay', component: DelayView },
    { path: '/record', name: 'record', component: RecordView },
    { path: '/mock', name: 'mock', component: MockView },
    { path: '/workflow', name: 'workflow', component: WorkflowView },
  ],
})

// 空闲预取：首屏渲染完成后，在浏览器空闲时段依次预加载懒页面 chunk，
// 这样用户真正点击侧边栏时 chunk 已在缓存中，无「点击才下载」的卡顿。
const prefetchViews: Array<() => Promise<unknown>> = [
  AnalyzeView, AutoReplyView, RawCaptureView, WebSocketView, SearchView,
  SendView, ClashView, DnsHijackView, CookiesView, SiteMapView, ToolsView,
  TimelineView, DelayView, RecordView, MockView, AIView, SettingsView, LogView,
  WorkflowView,
]

router.isReady().then(() => {
  const ric: (cb: () => void) => void =
    (window as any).requestIdleCallback
      ? (cb) => (window as any).requestIdleCallback(cb, { timeout: 2000 })
      : (cb) => setTimeout(cb, 200)
  let i = 0
  const pump = () => {
    if (i >= prefetchViews.length) return
    const load = prefetchViews[i++]
    try { load() } catch { /* 预取失败忽略，真正导航时会重试 */ }
    ric(pump)
  }
  ric(pump)
})

export default router
