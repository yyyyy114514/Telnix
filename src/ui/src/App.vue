<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useCaptureStore } from './stores/capture'
import { useFlowsStore } from './stores/flows'
import { api } from './api/client'
import CertBanner from './components/CertBanner.vue'
import { syncPrefs } from './stores/prefs'

const capture = useCaptureStore()
const flows = useFlowsStore()
const route = useRoute()
const router = useRouter()

// 所有页面常驻侧边栏（取消高级功能开关，避免懒加载卡顿）
// 顺序可拖动排序，持久化到 localStorage
// cat：分类色变量名（用于导航活动态左侧色条 + 图标着色，让侧边栏不单调）
const DEFAULT_NAV = [
  { path: '/capture',     label: '抓包',       icon: 'Aim',           cat: 'capture' },
  { path: '/analyze',     label: '全局分析',   icon: 'DataAnalysis',  cat: 'analyze' },
  { path: '/auto-reply',  label: '自动修改',   icon: 'SetUp',         cat: 'auto' },
  { path: '/send',        label: '发包',       icon: 'Promotion',     cat: 'send' },
  { path: '/clash',       label: 'Clash',      icon: 'ClashIcon',     cat: 'clash' },
  // CoolUI 已下线（文件保留，入口移除）
  { path: '/ai',          label: 'AI 分析',    icon: 'MagicStick',    cat: 'ai' },
  { path: '/codec',       label: '编解码',     icon: 'Key',           cat: 'codec' },
  { path: '/search',      label: '搜索',       icon: 'Search',        cat: 'search' },
  { path: '/raw',         label: 'TCP/UDP',    icon: 'Connection',    cat: 'raw' },
  { path: '/dns-hijack',  label: 'DNS 劫持',   icon: 'Histogram',     cat: 'dns' },
  { path: '/ws',          label: 'WebSocket',  icon: 'ChatLineRound', cat: 'ws' },
  { path: '/logs',        label: '日志',       icon: 'Document',      cat: 'log' },
  { path: '/settings',    label: '设置',       icon: 'Setting',       cat: 'settings' },
]
const NAV_ORDER_KEY = 'telnix_nav_order'

function loadNavItems() {
  // 按 localStorage 保存的顺序排列；新出现的项按 DEFAULT_NAV 中的相对位置插入
  const savedOrder: string[] = JSON.parse(localStorage.getItem(NAV_ORDER_KEY) || '[]')
  if (!Array.isArray(savedOrder) || !savedOrder.length) return [...DEFAULT_NAV]
  const byPath: Record<string, typeof DEFAULT_NAV[number]> = {}
  for (const it of DEFAULT_NAV) byPath[it.path] = it
  const result: typeof DEFAULT_NAV = []
  for (const p of savedOrder) {
    if (byPath[p]) {
      result.push(byPath[p])
      delete byPath[p]
    }
  }
  // 剩余未保存顺序的新项：按 DEFAULT_NAV 中的位置，插入到「前一项」之后
  // 这样新增页面不会粗暴追加到末尾，而是出现在合理位置
  let orderChanged = false
  for (const it of DEFAULT_NAV) {
    if (byPath[it.path]) {
      const defaultIdx = DEFAULT_NAV.findIndex(d => d.path === it.path)
      let inserted = false
      for (let i = defaultIdx - 1; i >= 0; i--) {
        const prevPath = DEFAULT_NAV[i].path
        const idx = result.findIndex(r => r.path === prevPath)
        if (idx >= 0) {
          result.splice(idx + 1, 0, it)
          inserted = true
          break
        }
      }
      if (!inserted) result.unshift(it)
      orderChanged = true
    }
  }
  // 若顺序有变化（新增项被加入），立即持久化，让新项参与 nav_order
  if (orderChanged) {
    localStorage.setItem(NAV_ORDER_KEY, JSON.stringify(result.map(i => i.path)))
  }
  return result
}

const navItems = ref(loadNavItems())

// Clash 启用状态：未启用时隐藏侧边栏 Clash 入口
const clashEnabled = ref(false)
// 加载期间禁止 watcher 触发跳转（避免初始 false 误触发 /clash → /capture）
let clashLoadingFlag = false
async function loadClashEnabled() {
  clashLoadingFlag = true
  try {
    // 用轻量接口 /clash/enabled（只读 settings.json，不探测 Mihomo），
    // 避免后端刚重启或 Mihomo 不可达时 /clash/status 等 1 秒 is_reachable 超时导致菜单延迟显示
    const r = await api.clashEnabledQuick()
    clashEnabled.value = !!r.enabled
  } catch { /* ignore */ } finally {
    await nextTick()
    clashLoadingFlag = false
  }
}
// 过滤后的导航项（Clash 未启用时隐藏）
const visibleNavItems = computed(() =>
  navItems.value.filter(i => i.path !== '/clash' || clashEnabled.value)
)

function persistNavOrder() {
  localStorage.setItem(NAV_ORDER_KEY, JSON.stringify(navItems.value.map(i => i.path)))
  syncPrefs()
}

// ---------- 拖动排序 ----------
const dragPath = ref<string | null>(null)

function onNavDragStart(path: string) {
  dragPath.value = path
}

function onNavDragOver(e: DragEvent, path: string) {
  // 阻止默认，允许 drop；用鼠标位置决定插入到目标项的前/后
  e.preventDefault()
  if (!dragPath.value || dragPath.value === path) return
  const target = e.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const after = (e.clientY - rect.top) > rect.height / 2
  const fromIdx = navItems.value.findIndex(i => i.path === dragPath.value)
  const toIdx = navItems.value.findIndex(i => i.path === path)
  if (fromIdx < 0 || toIdx < 0) return
  const items = [...navItems.value]
  const [moved] = items.splice(fromIdx, 1)
  // 计算插入位置：after 时插入到 toIdx 之后，但移除后索引可能前移
  let insertAt = after ? toIdx + 1 : toIdx
  if (fromIdx < toIdx && after) insertAt -= 1  // 移除后 toIdx 已前移
  if (fromIdx < toIdx && !after) insertAt -= 1
  items.splice(Math.max(0, Math.min(insertAt, items.length)), 0, moved)
  navItems.value = items
  persistNavOrder()
}

function onNavDragEnd() {
  dragPath.value = null
}

const activePath = computed(() => route.path)

function go(path: string) {
  router.push(path)
}

const captureStateText = computed(() =>
  capture.status.capturing ? '抓取中' : '已停止'
)

const certText = computed(() =>
  capture.status.cert_installed ? '证书已安装' : '证书未安装'
)

onMounted(() => {
  capture.startPolling()
  loadClashEnabled()
  // 从后端 settings.json 应用主题（localStorage 为空时以 settings.json 为准）
  applyThemeFromSettings()
  // 监听设置页的 Clash 开关事件，实时刷新侧边栏
  window.addEventListener('telnix-clash-toggle', loadClashEnabled)
})

// 启动时从 settings.json 同步主题到 localStorage + DOM
// 解决：清浏览器缓存/换设备后 localStorage 丢失，settings.json 的主题不生效
async function applyThemeFromSettings() {
  try {
    const s = await api.getSettings()
    const remoteTheme = s.theme as string | undefined
    const localTheme = localStorage.getItem('telnix_theme')
    // localStorage 无主题时，用 settings.json 的主题（默认 dark）
    const theme = localTheme || remoteTheme || 'dark'
    if (!localTheme) {
      localStorage.setItem('telnix_theme', theme)
    }
    if (theme === 'dark') {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  } catch {
    /* 后端不可达时保留 main.ts 的默认 dark */
  }
}

onUnmounted(() => {
  window.removeEventListener('telnix-clash-toggle', loadClashEnabled)
})

// 路由切换时刷新 Clash 启用状态（设置页可能切换了开关）
watch(() => route.path, () => {
  loadClashEnabled()
})

// Clash 被禁用时，若当前在 /clash 页则跳转到抓包页
// （加载期间 clashLoadingFlag=true 时不跳转，避免初始 false 误触发）
watch(clashEnabled, (enabled) => {
  if (clashLoadingFlag) return
  if (!enabled && route.path === '/clash') {
    router.replace('/capture')
  }
})

// ---------- 系统控制 ----------
const restarting = ref(false)

async function onRestart() {
  try {
    await ElMessageBox.confirm(
      '将重启前后端服务（清代理 + 重启 Python 进程）。重启期间页面会断开，约 3-5 秒后刷新即可。继续？',
      '重启服务',
      { confirmButtonText: '重启', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  restarting.value = true
  const msg = ElMessage({
    message: '正在重启服务，请稍候...',
    type: 'info',
    duration: 0,
  })
  try {
    await api.restartService()
    // 后端 os.execv 后会断连，等 5 秒后自动刷新（确保后端完全就绪，避免 API 调用失败导致状态误判）
    setTimeout(() => {
      window.location.reload()
    }, 5000)
  } catch (e: any) {
    msg?.close()
    restarting.value = false
    ElMessage.error('重启失败：' + (e?.message || e))
  }
}

async function onQuit() {
  try {
    await ElMessageBox.confirm(
      '将退出 Telnix（关闭前后端 + 清系统代理）。确认退出？',
      '退出 Telnix',
      { confirmButtonText: '退出', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await api.quitService()
    ElMessage.info('Telnix 正在退出...')
  } catch (e: any) {
    ElMessage.error('退出失败：' + (e?.message || e))
  }
}

async function onClearProxy() {
  try {
    await api.clearProxy()
    ElMessage.success('系统代理已关闭')
    await capture.fetchStatus()
  } catch (e: any) {
    ElMessage.error('操作失败：' + (e?.message || e))
  }
}

async function onEnableProxy() {
  try {
    await api.enableProxy()
    ElMessage.success('系统代理已开启')
    await capture.fetchStatus()
  } catch (e: any) {
    ElMessage.error('操作失败：' + (e?.message || e))
  }
}

// 系统代理当前是否开启
const systemProxyOn = computed(() => !!capture.status.system_proxy_on)

// 代理丢失弹窗防抖：避免轮询期间重复弹窗
let _proxyLostDialogShown = false
watch(() => !!capture.status.proxy_lost, async (lost) => {
  if (!lost) {
    _proxyLostDialogShown = false
    return
  }
  // 已弹窗或用户主动关闭代理时不弹
  if (_proxyLostDialogShown || !systemProxyOn.value) return
  _proxyLostDialogShown = true
  try {
    await ElMessageBox.confirm(
      '检测到系统代理已被外部关闭，抓包功能可能无法正常工作。\n是否重新开启系统代理？',
      '代理丢失',
      {
        confirmButtonText: '重新开启',
        cancelButtonText: '暂不开启',
        type: 'warning',
        closeOnClickModal: false,
        closeOnPressEscape: false,
      },
    )
    // 用户同意重新开启
    try {
      await api.enableProxy()
      ElMessage.success('系统代理已重新开启')
    } catch (e: any) {
      ElMessage.error('重新开启失败：' + (e?.message || e))
    }
    await capture.fetchStatus()
  } catch {
    // 用户拒绝：调用 clearProxy 同步后端状态，避免重复弹窗
    try {
      await api.clearProxy()
    } catch {
      /* ignore */
    }
    await capture.fetchStatus()
  }
})

function onSystemCmd(cmd: string) {
  if (cmd === 'quit') onQuit()
  else if (cmd === 'toggle-proxy') {
    // 根据当前状态切换开/关
    if (systemProxyOn.value) onClearProxy()
    else onEnableProxy()
  }
}
</script>

<template>
  <div class="app-layout full flex">
    <!-- 侧边栏 -->
    <aside class="sidebar">
      <div class="brand">
        <svg class="brand-logo" viewBox="0 0 1028 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" width="34" height="34">
          <path d="M1006.633925 536.184845l-212.082369-136.555338V197.829919a47.352209 47.352209 0 0 0-21.593841-39.948606L539.230091 7.392806a46.272517 46.272517 0 0 0-50.179973 0L255.322494 157.881313a47.506451 47.506451 0 0 0-21.645255 39.948606v202.005243L21.646284 536.390501a47.403623 47.403623 0 0 0-21.645255 39.948606v248.740485a47.403623 47.403623 0 0 0 21.645255 39.948606l233.67621 150.488507a46.272517 46.272517 0 0 0 50.231387 0L514.140104 881.63489l208.586223 134.34454a46.272517 46.272517 0 0 0 50.231388 0l233.67621-150.488507a47.506451 47.506451 0 0 0 21.645255-40.411331v-249.151796a47.506451 47.506451 0 0 0-21.645255-39.742951zM514.140104 103.074088l186.940968 120.462785v176.092634L514.140104 520.040878 327.147723 399.629507V223.536873z m420.668592 696.247136l-186.992382 120.462785-186.940968-120.462785v-176.04122l203.444833-131.002636 170.488517 109.768693v197.326577z" fill="currentColor" />
        </svg>
        <div class="brand-text">
          <div class="brand-name">Telnix</div>
          <div class="brand-sub">抓包工具</div>
        </div>
      </div>
      <nav class="nav">
        <div
          v-for="item in visibleNavItems"
          :key="item.path"
          class="nav-item"
          :class="[
            `nav-cat-${item.cat}`,
            { active: activePath === item.path, dragging: dragPath === item.path }
          ]"
          draggable="true"
          @click="go(item.path)"
          @dragstart="onNavDragStart(item.path)"
          @dragover="onNavDragOver($event, item.path)"
          @dragend="onNavDragEnd"
        >
          <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
          <span class="nav-label">{{ item.label }}</span>
        </div>
      </nav>
      <div class="sidebar-footer">
        <div class="proxy-port text-dim mono">代理 :{{ capture.status.proxy_port }}</div>
        <el-dropdown split-button type="primary" size="small" @click="onRestart" @command="onSystemCmd" class="restart-btn">
          <el-icon><Refresh /></el-icon>&nbsp;{{ restarting ? '重启中' : '重启服务' }}
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="quit">
                <el-icon><SwitchButton /></el-icon>&nbsp;退出 Telnix
              </el-dropdown-item>
              <el-dropdown-item command="toggle-proxy" divided>
                <el-icon><component :is="systemProxyOn ? 'CircleClose' : 'Connection'" /></el-icon>&nbsp;{{ systemProxyOn ? '关闭系统代理' : '开启系统代理' }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </aside>

    <!-- 右侧主区 -->
    <div class="main flex-1 flex flex-col">
      <CertBanner />
      <div class="content flex-1 overflow-hidden">
        <router-view />
      </div>
      <!-- 状态栏 -->
      <footer class="status-bar mono">
        <span class="sb-item">
          <span class="sb-dot" :class="{ on: capture.status.capturing }"></span>
          {{ captureStateText }}
        </span>
        <span class="sb-sep">|</span>
        <span class="sb-item">会话 #{{ capture.status.session_id || '-' }}</span>
        <span class="sb-sep">|</span>
        <span class="sb-item">流量 {{ flows.total }}</span>
        <span class="sb-sep">|</span>
        <span class="sb-item" :class="{ warn: !capture.status.cert_installed }">
          <el-icon><component :is="capture.status.cert_installed ? 'CircleCheck' : 'WarningFilled'" /></el-icon>
          {{ certText }}
        </span>
        <div class="flex-1"></div>
        <span class="sb-item text-dim">Telnix · 系统全局代理 + SSL bump</span>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.app-layout { height: 100vh; }

/* 侧边栏 */
.sidebar {
  width: 180px; flex-shrink: 0;
  background: var(--on-bg-sidebar);
  background-image: var(--on-gradient-sidebar);
  border-right: 1px solid var(--on-border);
  display: flex; flex-direction: column;
  position: relative;
}
/* 侧边栏右侧细微高光，制造层次 */
.sidebar::after {
  content: ''; position: absolute; top: 0; right: 0; bottom: 0;
  width: 1px;
  background: linear-gradient(180deg, transparent 0%, var(--on-border-light) 20%, var(--on-border-light) 80%, transparent 100%);
  pointer-events: none;
}
.brand {
  display: flex; align-items: center; gap: 10px;
  padding: 16px 14px; border-bottom: 1px solid var(--on-border-light);
  position: relative;
}
.brand::after {
  content: ''; position: absolute; left: 14px; right: 14px; bottom: -1px; height: 1px;
  background: linear-gradient(90deg, transparent 0%, var(--on-accent-glow) 50%, transparent 100%);
}
.brand-logo {
  width: 34px; height: 34px;
  color: var(--on-accent);
  filter: drop-shadow(0 0 8px var(--on-accent-glow));
  flex-shrink: 0;
  animation: brand-glow 3.6s ease-in-out infinite;
}
@keyframes brand-glow {
  0%, 100% { filter: drop-shadow(0 0 6px var(--on-accent-glow)); }
  50% { filter: drop-shadow(0 0 12px var(--on-accent-glow)); }
}
.brand-name {
  font-size: 16px; font-weight: 700;
  background: var(--on-gradient-accent);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; color: transparent;
  letter-spacing: .4px;
}
.brand-sub { font-size: 11px; color: var(--on-text-dim); letter-spacing: .5px; }

.nav { flex: 1; padding: 10px 8px; display: flex; flex-direction: column; gap: 2px; overflow-y: auto; }
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 12px; border-radius: var(--on-radius-md); cursor: pointer;
  color: var(--on-text-muted); font-size: 13.5px;
  transition: all .18s cubic-bezier(0.4, 0, 0.2, 1);
  position: relative;
  border: 1px solid transparent;
}
.nav-item:hover {
  background: var(--on-bg-hover); color: var(--on-text);
  transform: translateX(2px);
}
.nav-item.active {
  color: var(--on-cat-color, var(--on-accent));
  background: var(--on-cat-glow, var(--on-accent-glow));
  box-shadow: inset 3px 0 0 var(--on-cat-color, var(--on-accent)),
              0 1px 3px var(--on-cat-glow, var(--on-accent-glow));
  font-weight: 600;
  border-color: var(--on-cat-glow, var(--on-accent-glow));
}
.nav-item.active .nav-icon {
  color: var(--on-cat-color, var(--on-accent));
  filter: drop-shadow(0 0 4px var(--on-cat-glow, var(--on-accent-glow)));
}
.nav-item.dragging { opacity: 0.4; transform: scale(0.98); }
.nav-item[draggable="true"] { cursor: grab; }
.nav-item[draggable="true"]:active { cursor: grabbing; }
.nav-icon { font-size: 17px; transition: all .18s; }
.nav-item:hover .nav-icon { transform: scale(1.1); }

/* 每个分类的配色（活动态色 + glow） */
.nav-cat-capture    { --on-cat-color: var(--on-cat-capture);    --on-cat-glow: var(--on-accent-glow); }
.nav-cat-analyze    { --on-cat-color: var(--on-cat-analyze);    --on-cat-glow: var(--on-indigo-glow); }
.nav-cat-auto       { --on-cat-color: var(--on-cat-auto);       --on-cat-glow: var(--on-amber-glow); }
.nav-cat-send       { --on-cat-color: var(--on-cat-send);       --on-cat-glow: var(--on-blue-glow); }
.nav-cat-clash      { --on-cat-color: var(--on-cat-clash);      --on-cat-glow: var(--on-rose-glow); }
.nav-cat-cool       { --on-cat-color: var(--on-cat-cool);       --on-cat-glow: var(--on-cyan-glow); }
.nav-cat-ai         { --on-cat-color: var(--on-cat-ai);         --on-cat-glow: var(--on-purple-glow); }
.nav-cat-codec      { --on-cat-color: var(--on-cat-codec);      --on-cat-glow: var(--on-emerald-glow); }
.nav-cat-search     { --on-cat-color: var(--on-cat-search);     --on-cat-glow: var(--on-blue-glow); }
.nav-cat-raw        { --on-cat-color: var(--on-cat-raw);        --on-cat-glow: var(--on-pink-glow); }
.nav-cat-ws         { --on-cat-color: var(--on-cat-ws);         --on-cat-glow: var(--on-accent-glow); }
.nav-cat-log        { --on-cat-color: var(--on-cat-log);        --on-cat-glow: var(--on-bg-hover); }
.nav-cat-settings   { --on-cat-color: var(--on-cat-settings);   --on-cat-glow: var(--on-bg-hover); }

.sidebar-footer {
  padding: 10px 14px; border-top: 1px solid var(--on-border-light);
  display: flex; flex-direction: column; gap: 8px;
}
.proxy-port {
  font-size: 11px; display: flex; align-items: center; gap: 5px;
}
.proxy-port::before {
  content: ''; width: 5px; height: 5px; border-radius: 50%;
  background: var(--on-accent); box-shadow: 0 0 6px var(--on-accent);
}
.restart-btn { width: 100%; }

/* 主区 */
.main { min-width: 0; background: var(--on-bg); }
.content { min-height: 0; }

/* 状态栏 */
.status-bar {
  display: flex; align-items: center; gap: 10px;
  padding: 5px 14px; height: 28px;
  background: var(--on-bg-statusbar); border-top: 1px solid var(--on-border);
  font-size: 11.5px; color: var(--on-text-muted);
}
.sb-item { display: flex; align-items: center; gap: 5px; }
.sb-item.warn { color: var(--on-warn); }
.sb-sep { color: var(--on-border); }
.sb-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--on-pending);
}
.sb-dot.on {
  background: var(--on-ok);
  box-shadow: 0 0 8px var(--on-ok);
  animation: pulse 1.6s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
</style>
