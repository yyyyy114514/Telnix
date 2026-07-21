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
const DEFAULT_NAV = [
  { path: '/capture', label: '抓包', icon: 'Aim' },
  { path: '/analyze', label: '全局分析', icon: 'DataAnalysis' },
  { path: '/auto-reply', label: '自动修改', icon: 'SetUp' },
  { path: '/send', label: '发包', icon: 'Promotion' },
  { path: '/clash', label: 'Clash', icon: 'ClashIcon' },
  { path: '/ai', label: 'AI 分析', icon: 'MagicStick' },
  { path: '/codec', label: '编解码', icon: 'Key' },
  { path: '/search', label: '搜索', icon: 'Search' },
  { path: '/raw', label: 'TCP/UDP', icon: 'Connection' },
  { path: '/logs', label: '日志', icon: 'Document' },
  { path: '/settings', label: '设置', icon: 'Setting' },
]
const NAV_ORDER_KEY = 'opennet_nav_order'

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
  // 监听设置页的 Clash 开关事件，实时刷新侧边栏
  window.addEventListener('opennet-clash-toggle', loadClashEnabled)
})

onUnmounted(() => {
  window.removeEventListener('opennet-clash-toggle', loadClashEnabled)
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
      '将退出 OpenNet（关闭前后端 + 清系统代理）。确认退出？',
      '退出 OpenNet',
      { confirmButtonText: '退出', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await api.quitService()
    ElMessage.info('OpenNet 正在退出...')
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
        <div class="brand-logo">ON</div>
        <div class="brand-text">
          <div class="brand-name">OpenNet</div>
          <div class="brand-sub">抓包工具</div>
        </div>
      </div>
      <nav class="nav">
        <div
          v-for="item in visibleNavItems"
          :key="item.path"
          class="nav-item"
          :class="{ active: activePath === item.path, dragging: dragPath === item.path }"
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
                <el-icon><SwitchButton /></el-icon>&nbsp;退出 OpenNet
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
        <span class="sb-item text-dim">OpenNet · 系统全局代理 + SSL bump</span>
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
  border-right: 1px solid var(--on-border);
  display: flex; flex-direction: column;
}
.brand {
  display: flex; align-items: center; gap: 10px;
  padding: 16px 14px; border-bottom: 1px solid var(--on-border-light);
}
.brand-logo {
  width: 34px; height: 34px; border-radius: 7px;
  background: linear-gradient(135deg, var(--on-accent), var(--on-accent-dim));
  color: #001b18; font-weight: 800; font-size: 14px;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 0 16px var(--on-accent-glow);
}
.brand-name { font-size: 15px; font-weight: 700; color: var(--on-text); letter-spacing: .3px; }
.brand-sub { font-size: 11px; color: var(--on-text-dim); }

.nav { flex: 1; padding: 10px 8px; display: flex; flex-direction: column; gap: 2px; }
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border-radius: 6px; cursor: pointer;
  color: var(--on-text-muted); font-size: 13.5px;
  transition: all .15s;
}
.nav-item:hover { background: var(--on-bg-hover); color: var(--on-text); }
.nav-item.active {
  background: var(--on-accent-glow); color: var(--on-accent);
  box-shadow: inset 2px 0 0 var(--on-accent);
}
.nav-item.dragging { opacity: 0.4; }
.nav-item[draggable="true"] { cursor: grab; }
.nav-item[draggable="true"]:active { cursor: grabbing; }
.nav-icon { font-size: 17px; }

.sidebar-footer { padding: 10px 14px; border-top: 1px solid var(--on-border-light); }
.proxy-port { font-size: 11px; }

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
