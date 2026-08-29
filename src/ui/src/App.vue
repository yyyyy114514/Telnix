<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useCaptureStore } from './stores/capture'
import { useFlowsStore } from './stores/flows'
import { api } from './api/client'
import { loadPlatformCapabilities } from './stores/platform'
import CertBanner from './components/CertBanner.vue'
import WinDivertWarningDialog from './components/WinDivertWarningDialog.vue'
import { syncPrefs } from './stores/prefs'

const { t, locale } = useI18n()
const capture = useCaptureStore()
const flows = useFlowsStore()
const route = useRoute()
const router = useRouter()

// 所有页面常驻侧边栏（取消高级功能开关，避免懒加载卡顿）
// 顺序可拖动排序，持久化到 localStorage
// cat：分类色变量名（用于导航活动态左侧色条 + 图标着色，让侧边栏不单调）
const DEFAULT_NAV = computed(() => [
  { path: '/capture',     label: t('nav.capture'),       icon: 'Aim',           cat: 'capture' },
  { path: '/analyze',     label: t('nav.analyze'),       icon: 'DataAnalysis',  cat: 'analyze' },
  { path: '/auto-reply',  label: t('nav.autoReply'),     icon: 'SetUp',         cat: 'auto' },
  { path: '/send',        label: t('nav.send'),          icon: 'Promotion',     cat: 'send' },
  { path: '/clash',       label: t('nav.clash'),         icon: 'ClashIcon',     cat: 'clash' },
  // CoolUI 已下线（文件保留，入口移除）
  { path: '/ai',          label: t('nav.ai'),            icon: 'MagicStick',    cat: 'ai' },
  // 延迟规则已移至设置页入口
  // { path: '/delay',     label: t('nav.delay'),         icon: 'Clock',         cat: 'delay' },
  { path: '/search',      label: t('nav.search'),        icon: 'Search',        cat: 'search' },
  { path: '/raw',         label: t('nav.raw'),           icon: 'Connection',    cat: 'raw' },
  { path: '/dns-hijack',  label: t('nav.dnsHijack'),     icon: 'Histogram',     cat: 'dns' },
  { path: '/ws',          label: t('nav.ws'),            icon: 'ChatLineRound', cat: 'ws' },
  { path: '/workflow',    label: t('nav.workflow'),      icon: 'SetUp',         cat: 'auto' },
  { path: '/logs',        label: t('nav.logs'),          icon: 'Document',      cat: 'log' },
  { path: '/tools',       label: t('nav.tools'),         icon: 'ToolsIcon',     cat: 'tools' },
  { path: '/settings',    label: t('nav.settings'),      icon: 'Setting',       cat: 'settings' },
])
const NAV_ORDER_KEY = 'telnix_nav_order'

function loadNavItems() {
  // 按 localStorage 保存的顺序排列；新出现的项按 DEFAULT_NAV 中的相对位置插入
  // 包 try-catch：localStorage 被篡改为非法 JSON 时回退到默认顺序，避免 App 崩溃白屏
  let savedOrder: string[] = []
  try {
    savedOrder = JSON.parse(localStorage.getItem(NAV_ORDER_KEY) || '[]')
  } catch {
    savedOrder = []
  }
  if (!Array.isArray(savedOrder) || !savedOrder.length) return [...DEFAULT_NAV.value]
  const byPath: Record<string, typeof DEFAULT_NAV.value[number]> = {}
  for (const it of DEFAULT_NAV.value) byPath[it.path] = it
  const result: typeof DEFAULT_NAV.value = []
  for (const p of savedOrder) {
    if (byPath[p]) {
      result.push(byPath[p])
      delete byPath[p]
    }
  }
  // 剩余未保存顺序的新项：按 DEFAULT_NAV 中的位置，插入到「前一项」之后
  // 这样新增页面不会粗暴追加到末尾，而是出现在合理位置
  let orderChanged = false
  for (const it of DEFAULT_NAV.value) {
    if (byPath[it.path]) {
      const defaultIdx = DEFAULT_NAV.value.findIndex(d => d.path === it.path)
      let inserted = false
      for (let i = defaultIdx - 1; i >= 0; i--) {
        const prevPath = DEFAULT_NAV.value[i].path
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
  // 不在 dragover 中持久化（会高频触发 API 调用导致卡死），改在 dragend 中保存
}

function onNavDragEnd() {
  persistNavOrder()
  dragPath.value = null
}

const activePath = computed(() => route.path)

function go(path: string) {
  router.push(path)
}

const captureStateText = computed(() =>
  capture.status.capturing ? t('app.capturing') : t('app.stopped')
)

const certText = computed(() =>
  capture.status.cert_installed ? t('app.certInstalled') : t('app.certNotInstalled')
)

// 页面可见性变化：不可见时暂停 capture 轮询，可见时恢复
function onVisibilityChange() {
  if (document.hidden) {
    capture.stopPolling()
  } else {
    capture.startPolling()
  }
}

onMounted(() => {
  capture.startPolling()
  loadClashEnabled()
  // 预加载平台能力（含 is_admin），供设置页等场景直接使用
  loadPlatformCapabilities()
  // 从后端 settings.json 应用主题（localStorage 为空时以 settings.json 为准）
  applyThemeFromSettings()
  // 监听设置页的 Clash 开关事件，实时刷新侧边栏
  window.addEventListener('telnix-clash-toggle', loadClashEnabled)
  // 页面不可见时暂停 capture 轮询，可见时恢复，节省后台资源/电池
  document.addEventListener('visibilitychange', onVisibilityChange)
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
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

// 路由切换时刷新 Clash 启用状态（设置页可能切换了开关）
watch(() => route.path, () => {
  loadClashEnabled()
})

// 语言切换时重新加载导航项（navItems 存的是静态 label 快照，需重新从 DEFAULT_NAV 取最新翻译）
watch(locale, () => {
  const order = navItems.value.map(i => i.path)
  const byPath: Record<string, typeof DEFAULT_NAV.value[number]> = {}
  for (const it of DEFAULT_NAV.value) byPath[it.path] = it
  navItems.value = order.map(p => byPath[p]).filter(Boolean)
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
  // UX 修复：重启中禁止重复触发
  if (restarting.value) return
  try {
    await ElMessageBox.confirm(
      t('app.restartConfirm'),
      t('app.restartTitle'),
      { confirmButtonText: t('common.restart'), cancelButtonText: t('common.cancel'), type: 'warning' }
    )
  } catch {
    return
  }
  restarting.value = true
  const msg = ElMessage({
    message: t('app.restartingMsg'),
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
    ElMessage.error(t('app.restartFailed') + (e?.message || e))
  }
}

async function onQuit() {
  try {
    await ElMessageBox.confirm(
      t('app.quitConfirm'),
      t('app.quitTitle'),
      { confirmButtonText: t('common.quit'), cancelButtonText: t('common.cancel'), type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await api.quitService()
    ElMessage.info(t('app.quitting'))
  } catch (e: any) {
    ElMessage.error(t('app.quitFailed') + (e?.message || e))
  }
}

async function onClearProxy() {
  try {
    await api.clearProxy()
    ElMessage.success(t('app.proxyOff'))
    await capture.fetchStatus()
  } catch (e: any) {
    ElMessage.error(t('app.operationFailed') + (e?.message || e))
  }
}

async function onEnableProxy() {
  try {
    await api.enableProxy()
    ElMessage.success(t('app.proxyOn'))
    await capture.fetchStatus()
  } catch (e: any) {
    ElMessage.error(t('app.operationFailed') + (e?.message || e))
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
      t('app.proxyLostMsg'),
      t('app.proxyLostTitle'),
      {
        confirmButtonText: t('app.proxyLostReopen'),
        cancelButtonText: t('app.proxyLostSkip'),
        type: 'warning',
        closeOnClickModal: false,
        closeOnPressEscape: false,
      },
    )
    // 用户同意重新开启
    try {
      await api.enableProxy()
      ElMessage.success(t('app.proxyReopened'))
    } catch (e: any) {
      ElMessage.error(t('app.proxyReopenFailed') + (e?.message || e))
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

// ---------- 端口冲突提示 ----------
// 后端启动时若手动设置的端口被占用，会自动切换到随机端口并通过 /status 返回 port_conflict。
// 这里轮询检测到 port_conflict 后弹窗提示用户，仅弹一次。
// 用 sessionStorage 持久化防抖标志，避免页面刷新后重复弹窗。
// 后端重启（started_at 变化）时重置标志，允许下次启动再次提示。
function _portConflictKey(startedAt: number | undefined): string {
  return `telnix_port_conflict_shown_${startedAt ?? 0}`
}
watch(() => capture.status.port_conflict, (conflict) => {
  if (!conflict) return
  const startedAt = capture.status.started_at
  // sessionStorage 持久化：同一次后端运行期间（started_at 不变）只弹一次
  if (sessionStorage.getItem(_portConflictKey(startedAt))) return
  sessionStorage.setItem(_portConflictKey(startedAt), '1')
  const parts: string[] = []
  if (conflict.api) {
    parts.push(t('settings.portConflictApi', { old: conflict.api.old, new: conflict.api.new }))
  }
  if (conflict.proxy) {
    parts.push(t('settings.portConflictProxy', { old: conflict.proxy.old, new: conflict.proxy.new }))
  }
  ElMessageBox.alert(
    t('settings.portConflictMsg', { details: parts.join('\n') }),
    t('settings.portConflictTitle'),
    { confirmButtonText: t('settings.gotIt'), type: 'warning' },
  ).catch(() => { /* 用户关闭弹窗，忽略 */ })
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
          <div class="brand-name">{{ t('app.brandName') }}</div>
          <div class="brand-sub">{{ t('app.brandSub') }}</div>
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
        <div class="proxy-port text-dim mono"><span class="proxy-port-label">{{ t('app.proxyPort') }} </span>:{{ capture.status.proxy_port }}</div>
        <el-dropdown split-button type="primary" size="small" :disabled="restarting" @click="onRestart" @command="onSystemCmd" class="restart-btn">
          <el-icon><Refresh /></el-icon><span class="restart-text">&nbsp;{{ restarting ? t('app.restarting') : t('app.restartService') }}</span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="quit">
                <el-icon><SwitchButton /></el-icon>&nbsp;{{ t('app.quitTelnix') }}
              </el-dropdown-item>
              <el-dropdown-item command="toggle-proxy" divided>
                <el-icon><component :is="systemProxyOn ? 'CircleClose' : 'Connection'" /></el-icon>&nbsp;{{ systemProxyOn ? t('app.proxyToggleOff') : t('app.proxyToggleOn') }}
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
        <router-view v-slot="{ Component, route }">
          <keep-alive :include="['CaptureView', 'AnalyzeView', 'AutoReplyView', 'SearchView', 'ToolsView']">
            <component :is="Component" :key="route.path" />
          </keep-alive>
        </router-view>
      </div>
      <!-- WinDivert 风险提示全局对话框（首次启用相关功能时弹出） -->
      <WinDivertWarningDialog />
      <!-- 状态栏 -->
      <footer class="status-bar mono">
        <span class="sb-item">
          <span class="sb-dot" :class="{ on: capture.status.capturing }"></span>
          {{ captureStateText }}
        </span>
        <span class="sb-sep">|</span>
        <span class="sb-item">{{ t('app.session') }} #{{ capture.status.session_id || '-' }}</span>
        <span class="sb-sep">|</span>
        <span class="sb-item">{{ t('app.flows') }} {{ flows.total }}</span>
        <span class="sb-sep">|</span>
        <span class="sb-item" :class="{ warn: !capture.status.cert_installed }">
          <el-icon><component :is="capture.status.cert_installed ? 'CircleCheck' : 'WarningFilled'" /></el-icon>
          {{ certText }}
        </span>
        <div class="flex-1"></div>
        <a class="sb-item text-dim" href="https://github.com/yyyyy114514/Telnix" target="_blank" rel="noopener" style="display: inline-flex; align-items: center; gap: 5px; text-decoration: none; color: inherit;">
          <svg viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" width="14" height="14" style="fill: currentColor; flex-shrink: 0;">
            <path d="M512 42.666667A464.64 464.64 0 0 0 42.666667 502.186667 460.373333 460.373333 0 0 0 363.52 938.666667c23.466667 4.266667 32-9.813333 32-22.186667v-78.08c-130.56 27.733333-158.293333-61.44-158.293333-61.44a122.026667 122.026667 0 0 0-52.053334-67.413333c-42.666667-28.16 3.413333-27.733333 3.413334-27.733334a98.56 98.56 0 0 1 71.68 47.36 101.12 101.12 0 0 0 136.533333 37.973334 99.413333 99.413333 0 0 1 29.866667-61.44c-104.106667-11.52-213.333333-50.773333-213.333334-226.986667a177.066667 177.066667 0 0 1 47.36-124.16 161.28 161.28 0 0 1 4.693334-121.173333s39.68-12.373333 128 46.933333a455.68 455.68 0 0 1 234.666666 0c89.6-59.306667 128-46.933333 128-46.933333a161.28 161.28 0 0 1 4.693334 121.173333A177.066667 177.066667 0 0 1 810.666667 477.866667c0 176.64-110.08 215.466667-213.333334 226.986666a106.666667 106.666667 0 0 1 32 85.333334v125.866666c0 14.933333 8.533333 26.88 32 22.186667A460.8 460.8 0 0 0 981.333333 502.186667 464.64 464.64 0 0 0 512 42.666667" fill="currentColor"></path>
          </svg>
          <span>Github</span>
        </a>
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
  min-width: 0;
}
/* 窄窗口响应式：收缩为图标 only，避免导航项文字被裁剪 */
@media (max-width: 1100px) {
  .sidebar { width: 64px; }
  .brand { padding: 16px 0; justify-content: center; }
  .brand-text { display: none; }
  .nav-item { justify-content: center; padding: 9px 0; }
  .nav-label { display: none; }
  .proxy-port { justify-content: center; font-size: 10px; }
  .proxy-port-label { display: none; }
  .sidebar-footer { padding: 10px 6px; }
  .restart-btn :deep(.el-button) { padding-left: 8px; padding-right: 8px; }
  .restart-text { display: none; }
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
.nav-cat-sitemap    { --on-cat-color: var(--on-cat-sitemap, var(--on-accent));    --on-cat-glow: var(--on-accent-glow); }
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
.nav-cat-tools      { --on-cat-color: var(--on-cat-tools);      --on-cat-glow: var(--on-purple-glow); }
.nav-cat-settings   { --on-cat-color: var(--on-cat-settings);   --on-cat-glow: var(--on-bg-hover); }
.nav-cat-cookies    { --on-cat-color: var(--on-cat-cookies);    --on-cat-glow: var(--on-amber-glow); }

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
/* UX 修复：窄窗口下溢出隐藏 + 装饰性文本隐藏，避免状态项挤压换行 */
.status-bar {
  display: flex; align-items: center; gap: 10px;
  padding: 5px 14px; height: 28px;
  background: var(--on-bg-statusbar); border-top: 1px solid var(--on-border);
  font-size: 11.5px; color: var(--on-text-muted);
  overflow: hidden; white-space: nowrap;
}
.sb-item { display: flex; align-items: center; gap: 5px; flex-shrink: 0; }
@media (max-width: 980px) {
  .status-bar .sb-item.text-dim { display: none; }
}
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
