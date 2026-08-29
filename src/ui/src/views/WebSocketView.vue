<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useVirtualList } from '../composables/useVirtualList'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Flow } from '../api/client'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import HexView from '../components/HexView.vue'
import RawView from '../components/RawView.vue'

const capture = useCaptureStore()
const flowsStore = useFlowsStore()
const router = useRouter()
const { t } = useI18n()

// 性能优化：flows 列表只做顶层替换（无 .push 单条），用 shallowRef 避免对每条 flow 深度代理
const flows = shallowRef<Flow[]>([])
const selectedId = ref<number | null>(null)
const selectedHex = ref('')
const selectedRaw = ref('')
const activeDetailTab = ref<'hex' | 'raw'>('hex')
const hexField = ref<'raw_data' | 'request_body' | 'response_body'>('raw_data')
// 当前已加载的最大 flow id，用于增量轮询
const maxFlowId = ref(0)

// 筛选（前端 AND 过滤，多选支持通配符）
const filterHosts = ref<string[]>([])
const filterDirs = ref<string[]>([])  // 'WS-SEND' / 'WS-RECV'
// 专注（前端 OR 过滤）
const focusHosts = ref<string[]>([])
const focusDirs = ref<string[]>([])

let pollTimer: number | null = null

const selectedFlow = computed(() => flows.value.find(f => f.id === selectedId.value) || null)

// 已出现的 host 列表（供筛选/专注 host 选择）
const knownHosts = computed(() => {
  const set = new Set<string>()
  flows.value.forEach((f: any) => { if (f.host) set.add(f.host) })
  return [...set].sort()
})

const hasActiveFilters = computed(() =>
  filterHosts.value.length > 0 || filterDirs.value.length > 0
)
const focusEnabled = computed(() =>
  focusHosts.value.length > 0 || focusDirs.value.length > 0
)

function resetFilters() {
  filterHosts.value = []
  filterDirs.value = []
}

function resetFocus() {
  focusHosts.value = []
  focusDirs.value = []
}

// host 通配符匹配（* → .*, ? → .）
const _hostRegexCache = new Map<string, RegExp | null>()
function compileHostRegex(pattern: string): RegExp | null {
  let r = _hostRegexCache.get(pattern)
  if (r !== undefined) return r
  try {
    const regexStr = '^' + pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.') + '$'
    r = new RegExp(regexStr, 'i')
  } catch {
    r = null
  }
  _hostRegexCache.set(pattern, r)
  return r
}
function matchHostWildcard(text: string, pattern: string): boolean {
  if (!pattern) return false
  const r = compileHostRegex(pattern)
  if (r) return r.test(text)
  return text.toLowerCase().includes(pattern.toLowerCase())
}

// ---------- 统一悬浮窗管理（筛选/专注 互斥） ----------
const activePopup = ref<'filter' | 'focus' | null>(null)
const popupPos = ref({ left: 10, top: 46 })

function togglePopup(name: 'filter' | 'focus') {
  if (activePopup.value === name) {
    closePopup()
  } else {
    activePopup.value = name
    popupPos.value = { left: 10, top: 46 }
  }
}

function closePopup() {
  activePopup.value = null
}

// ---------- 悬浮窗拖动 ----------
let dragStartX = 0, dragStartY = 0, popupStartLeft = 0, popupStartTop = 0
let isDraggingPopup = false
function onPopupHeaderDown(e: MouseEvent) {
  isDraggingPopup = true
  dragStartX = e.clientX
  dragStartY = e.clientY
  popupStartLeft = popupPos.value.left
  popupStartTop = popupPos.value.top
  window.addEventListener('mousemove', onPopupDragMove)
  window.addEventListener('mouseup', onPopupDragUp)
}
function onPopupDragMove(e: MouseEvent) {
  if (!isDraggingPopup) return
  // clamp 到视口范围内，确保悬浮窗 header 始终可见可拖回
  const maxLeft = Math.max(0, window.innerWidth - 120)
  const maxTop = Math.max(0, window.innerHeight - 60)
  const left = Math.min(Math.max(0, popupStartLeft + (e.clientX - dragStartX)), maxLeft)
  const top = Math.min(Math.max(0, popupStartTop + (e.clientY - dragStartY)), maxTop)
  popupPos.value = { left, top }
}
function onPopupDragUp() {
  isDraggingPopup = false
  window.removeEventListener('mousemove', onPopupDragMove)
  window.removeEventListener('mouseup', onPopupDragUp)
}

// ---------- 分隔线拖拽 ----------
const leftRatio = ref(0.4)
const dragging = ref(false)
const wsBodyRef = ref<HTMLElement | null>(null)
function onSplitDown(e: MouseEvent) {
  e.preventDefault()
  dragging.value = true
  window.addEventListener('mousemove', onSplitMove)
  window.addEventListener('mouseup', onSplitUp)
}
function onSplitMove(e: MouseEvent) {
  const el = wsBodyRef.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  const r = (e.clientX - rect.left) / rect.width
  leftRatio.value = Math.min(0.7, Math.max(0.25, r))
}
function onSplitUp() {
  dragging.value = false
  window.removeEventListener('mousemove', onSplitMove)
  window.removeEventListener('mouseup', onSplitUp)
}

// ---------- 多选模式 ----------
const multiSelectMode = ref(false)
const selectedFlowIds = ref<Set<number>>(new Set())

function toggleMultiSelect() {
  multiSelectMode.value = !multiSelectMode.value
  if (!multiSelectMode.value) {
    selectedFlowIds.value.clear()
    showDeleteConfirm.value = false
  }
}

function toggleFlowCheck(flowId: number) {
  if (selectedFlowIds.value.has(flowId)) {
    selectedFlowIds.value.delete(flowId)
  } else {
    selectedFlowIds.value.add(flowId)
  }
  selectedFlowIds.value = new Set(selectedFlowIds.value)
}

function selectAllFlows() {
  selectedFlowIds.value = new Set(displayFlows.value.map((f: any) => f.id))
}

function clearSelection() {
  selectedFlowIds.value = new Set()
}

const selectedCount = computed(() => selectedFlowIds.value.size)

// ---------- 多选悬浮工具栏 ----------
const floatBarVisible = ref(true)
let floatBarTimer: number | null = null

function hideFloatBar() {
  if (!multiSelectMode.value) return
  if (showDeleteConfirm.value) return
  floatBarVisible.value = false
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  floatBarTimer = window.setTimeout(() => {
    if (multiSelectMode.value) floatBarVisible.value = true
  }, 1000)
}

function showFloatBarNow() {
  if (!multiSelectMode.value) return
  floatBarVisible.value = true
  if (floatBarTimer !== null) {
    clearTimeout(floatBarTimer)
    floatBarTimer = null
  }
}

watch(multiSelectMode, (v) => {
  if (v) showFloatBarNow()
})

// 批量删除（带子悬浮窗确认）
const showDeleteConfirm = ref(false)
function batchDeleteFlows() {
  if (!selectedFlowIds.value.size) return
  showDeleteConfirm.value = true
  showFloatBarNow()
}
async function confirmDeleteFlows() {
  const ids = [...selectedFlowIds.value]
  if (!ids.length) { showDeleteConfirm.value = false; return }
  try {
    await api.batchDeleteFlows(ids)
    ElMessage.success(t('ws.deletedNFlows', { n: ids.length }))
    flows.value = flows.value.filter(x => !ids.includes(x.id))
    if (selectedId.value !== null && ids.includes(selectedId.value)) {
      selectedId.value = null
      selectedHex.value = ''
    }
    selectedFlowIds.value.clear()
    multiSelectMode.value = false
    showDeleteConfirm.value = false
  } catch (e: any) {
    ElMessage.error(t('ws.deleteFailed', { msg: e?.message || e }))
    showDeleteConfirm.value = false
  }
}
function cancelDeleteFlows() {
  showDeleteConfirm.value = false
}

// 顶部忽略下拉：按 Host（基于当前选中流量）
async function ignoreHost() {
  const f = selectedFlow.value
  if (!f || !f.host) {
    ElMessage.warning(t('ws.noHostInfo'))
    return
  }
  try {
    await api.ignoreHost(f.host)
    ElMessage.success(t('ws.ignoredHost', { host: f.host }))
  } catch (e: any) {
    ElMessage.error(t('ws.ignoreFailed', { msg: e?.message || e }))
  }
}

const displayFlows = computed(() => {
  const src = flows.value
  const hasFilter = hasActiveFilters.value
  const focusOn = focusEnabled.value
  if (!hasFilter && !focusOn) return src

  const fHosts = filterHosts.value
  const fDirs = filterDirs.value
  const foHosts = focusHosts.value
  const foDirs = focusDirs.value

  const result: Flow[] = []
  for (const f of src) {
    // 筛选（AND 语义）
    if (hasFilter) {
      if (fHosts.length > 0 && !fHosts.some(p => matchHostWildcard(f.host || '', p))) continue
      if (fDirs.length > 0 && !fDirs.includes(f.method || '')) continue
    }
    // 专注（OR 语义）
    if (focusOn) {
      let focusMatch = false
      if (foHosts.length > 0 && foHosts.some(p => matchHostWildcard(f.host || '', p))) focusMatch = true
      else if (foDirs.length > 0 && foDirs.includes(f.method || '')) focusMatch = true
      if (!focusMatch) continue
    }
    result.push(f)
  }
  return result
})

// ---------- 列表 grid 模板（多选时插入复选框列） ----------
const gridCols = computed(() => {
  if (multiSelectMode.value) return '32px 50px 70px 1fr 1.5fr 70px 70px'
  return '50px 70px 1fr 1.5fr 70px 70px'
})

// 自动滚动
const bodyRef = ref<HTMLElement | null>(null)
// P1 虚拟滚动：固定行高 28px，窗口化渲染
const { onScroll: onVScroll, visibleItems, topPad, bottomPad } = useVirtualList<any>({
  containerRef: bodyRef,
  items: () => displayFlows.value,
  itemHeight: 28,
})
let scrollPauseTimer: number | null = null
let programmaticScroll = false

function onBodyScroll(e: Event) {
  onVScroll(e)
  if (programmaticScroll) {
    programmaticScroll = false
    return
  }
  // 多选模式滚动时隐藏工具栏
  if (multiSelectMode.value) hideFloatBar()
  if (!flowsStore.autoScroll) return
  flowsStore.autoScrollPaused = true
  if (scrollPauseTimer !== null) clearTimeout(scrollPauseTimer)
  scrollPauseTimer = window.setTimeout(() => {
    if (!flowsStore.autoScroll) {
      flowsStore.autoScrollPaused = false
      return
    }
    flowsStore.autoScrollPaused = false
    const el = bodyRef.value
    if (el) {
      programmaticScroll = true
      el.scrollTo({ top: 0, behavior: 'smooth' })
    }
  }, flowsStore.autoScrollDelay * 1000)
}

watch(
  () => flows.value.length,
  async () => {
    if (!flowsStore.autoScroll || flowsStore.autoScrollPaused) return
    await nextTick()
    const el = bodyRef.value
    if (el) {
      programmaticScroll = true
      el.scrollTo({ top: 0, behavior: 'smooth' })
    }
  }
)

// 右键菜单
const ctxMenu = ref<{ visible: boolean; x: number; y: number; flow: Flow | null }>({
  visible: false, x: 0, y: 0, flow: null,
})
const ctxMenuRef = ref<HTMLElement | null>(null)
const showCopySubmenu = ref(false)
const showIgnoreSubmenu = ref(false)

function onSubmenuEnter(type: 'copy' | 'ignore') {
  if (type === 'copy') { showCopySubmenu.value = true; showIgnoreSubmenu.value = false }
  else { showCopySubmenu.value = false; showIgnoreSubmenu.value = true }
}
function onSubmenuLeave() {
  showCopySubmenu.value = false; showIgnoreSubmenu.value = false
}

const COPY_FIELDS = computed(() => [
  { key: 'id', label: 'ID', field: 'id' },
  { key: 'method', label: t('ws.direction'), field: 'method' },
  { key: 'host', label: 'Host', field: 'host' },
  { key: 'path', label: 'Path', field: 'path' },
  { key: 'process', label: t('ws.process'), field: 'process_name' },
  { key: 'pid', label: 'PID', field: 'pid' },
  { key: 'size', label: t('ws.size'), field: 'size' },
  { key: 'time', label: t('ws.time'), field: 'timestamp' },
])

function buildWsCurl(f: any): string {
  const url = `ws://${f.host}${f.path}`
  let curl = `curl --include --no-buffer \\\n  --upgrade \\\n  -H 'Upgrade: websocket' \\\n  -H 'Connection: Upgrade' \\\n  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \\\n  -H 'Sec-WebSocket-Version: 13' \\\n  '${url}'`
  return curl
}

function onContextMenu(e: MouseEvent, flow: Flow) {
  e.preventDefault()
  selectFlow(flow)
  ctxMenu.value = { visible: true, x: e.clientX, y: e.clientY, flow }
  nextTick(() => {
    const el = ctxMenuRef.value
    if (!el) return
    const rect = el.getBoundingClientRect()
    const vw = window.innerWidth
    const vh = window.innerHeight
    let x = e.clientX
    let y = e.clientY
    if (x + rect.width > vw - 4) x = Math.max(4, vw - rect.width - 4)
    if (y + rect.height > vh - 4) y = Math.max(4, vh - rect.height - 4)
    ctxMenu.value = { ...ctxMenu.value, x, y }
  })
}

function closeCtxMenu() {
  ctxMenu.value.visible = false
}

function getFieldValue(f: any, field: string): string {
  if (field === '_curl') return buildWsCurl(f)
  if (field === '_url') return `ws://${f.host}${f.path}`
  const v = f[field]
  return v === null || v === undefined ? '' : String(v)
}

function copyField(field: string) {
  const f = ctxMenu.value.flow
  if (!f) return
  const text = getFieldValue(f, field)
  navigator.clipboard.writeText(text).catch(() => {})
  ElMessage.success(t('ws.copied', { text: text.length > 40 ? text.slice(0, 40) + '...' : text }))
  closeCtxMenu()
}

function ctxCopyUrl() {
  const f = ctxMenu.value.flow
  if (!f) return
  const url = `ws://${f.host}${f.path}`
  navigator.clipboard.writeText(url).catch(() => {})
  ElMessage.success(t('ws.copied', { text: url.length > 40 ? url.slice(0, 40) + '...' : url }))
  closeCtxMenu()
}

function ctxCopyCurl() {
  const f = ctxMenu.value.flow
  if (!f) return
  const curl = buildWsCurl(f)
  navigator.clipboard.writeText(curl).catch(() => {})
  ElMessage.success(t('ws.copied', { text: 'cURL' }))
  closeCtxMenu()
}

function ctxCopyRequest() {
  const f = ctxMenu.value.flow
  if (!f) return
  const body = f.request_body || ''
  navigator.clipboard.writeText(body).catch(() => {})
  ElMessage.success(t('ws.copied', { text: body.length > 40 ? body.slice(0, 40) + '...' : body }))
  closeCtxMenu()
}

function ctxCopyResponse() {
  const f = ctxMenu.value.flow
  if (!f) return
  const body = f.response_body || ''
  navigator.clipboard.writeText(body).catch(() => {})
  ElMessage.success(t('ws.copied', { text: body.length > 40 ? body.slice(0, 40) + '...' : body }))
  closeCtxMenu()
}

async function ctxIgnoreProcess() {
  const f = ctxMenu.value.flow
  if (!f || !f.process_name) {
    ElMessage.warning(t('ws.noProcessInfo'))
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreProcess({ pid: f.pid, name: f.process_name })
    ElMessage.success(t('ws.ignoredProcess', { name: f.process_name }))
  } catch (e: any) {
    ElMessage.error(t('ws.ignoreFailed', { msg: e?.message || e }))
  }
  closeCtxMenu()
}

async function ctxIgnorePid() {
  const f = ctxMenu.value.flow
  if (!f || !f.pid) {
    ElMessage.warning(t('ws.noPidInfo'))
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreProcess({ pid: f.pid, name: f.process_name || '' })
    ElMessage.success(t('ws.ignoredPid', { pid: f.pid }))
  } catch (e: any) {
    ElMessage.error(t('ws.ignoreFailed', { msg: e?.message || e }))
  }
  closeCtxMenu()
}

async function ctxIgnoreHost() {
  const f = ctxMenu.value.flow
  if (!f || !f.host) {
    ElMessage.warning(t('ws.noHostInfo'))
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreHost(f.host)
    ElMessage.success(t('ws.ignoredHost', { host: f.host }))
  } catch (e: any) {
    ElMessage.error(t('ws.ignoreFailed', { msg: e?.message || e }))
  }
  closeCtxMenu()
}

// 双击跳转抓包页
function onFlowDblClick(f: Flow) {
  flowsStore.selectFlow(f)
  router.push('/capture')
}

// 删除流量
async function ctxDelete() {
  const f = ctxMenu.value.flow
  if (!f) return
  try {
    await ElMessageBox.confirm(t('ws.deleteConfirm', { id: f.id }), t('ws.delete'), {
      confirmButtonText: t('ws.delete'), cancelButtonText: t('ws.cancel'), type: 'warning',
    })
  } catch {
    closeCtxMenu()
    return
  }
  try {
    await api.deleteFlow(f.id)
    flows.value = flows.value.filter(x => x.id !== f.id)
    if (selectedId.value === f.id) {
      selectedId.value = null
      selectedHex.value = ''
    }
    ElMessage.success(t('ws.deleted'))
  } catch (e: any) {
    ElMessage.error(t('ws.deleteFailed', { msg: e?.message || e }))
  }
  closeCtxMenu()
}

function onGlobalClick() {
  if (ctxMenu.value.visible) closeCtxMenu()
}

// 后端流量被清空时（抓包页点清空），本页本地列表/最大 id 也需重置，
// 否则下方增量轮询用旧 maxFlowId 的 since_id 可能把已删旧包重新拉回
function onFlowsCleared() {
  flows.value = []
  maxFlowId.value = 0
}

async function loadFlows() {
  let sid = capture.status.session_id
  if (!sid) {
    // 等待 capture.status 初始化（最多 2s）
    for (let i = 0; i < 20 && !sid; i++) {
      await new Promise(r => setTimeout(r, 100))
      sid = capture.status.session_id
    }
  }
  if (!sid) {
    try {
      const sessions: any = await api.getSessions()
      const list = sessions?.sessions || sessions || []
      if (Array.isArray(list) && list.length) {
        sid = list[0].id
      }
    } catch { /* ignore */ }
  }
  if (!sid) return
  try {
    const res = await api.getFlows(sid, { limit: 500, protocol: 'ws' })
    const list = (res.flows || []).sort((a, b) => b.id - a.id)
    flows.value = list
    // 列表已降序，第一条就是最大 id
    maxFlowId.value = list[0]?.id || 0
  } catch { /* ignore */ }
}

// 增量轮询：用 since_id 拉取 maxFlowId 之后的 WS 新流量，插入列表顶部
async function pollNewFlows() {
  if (!maxFlowId.value) {
    // 无历史，先全量加载
    await loadFlows()
    return
  }
  let sid = capture.status.session_id
  if (!sid) {
    try {
      const sessions: any = await api.getSessions()
      const list = sessions?.sessions || sessions || []
      if (Array.isArray(list) && list.length) {
        sid = list[0].id
      }
    } catch { /* ignore */ }
  }
  if (!sid) return
  try {
    const res = await api.getFlows(sid, { since_id: maxFlowId.value, limit: 200, protocol: 'ws' })
    const list: Flow[] = res.flows || []
    if (!list.length) return
    // 去重：过滤掉列表中已存在的 id
    const existIds = new Set(flows.value.map(f => f.id))
    const deduped = list.filter(f => !existIds.has(f.id))
    if (!deduped.length) {
      const newMax = list[0]?.id || 0
      if (newMax > maxFlowId.value) maxFlowId.value = newMax
      return
    }
    // 后端返回按 id DESC（最大 id 在前），直接插入顶部保持降序
    deduped.sort((a, b) => b.id - a.id)
    flows.value = [...deduped, ...flows.value]
    const newMax = deduped[0].id
    if (newMax > maxFlowId.value) maxFlowId.value = newMax
  } catch { /* ignore */ }
}

// selectFlow 竞态保护：用户快速点击不同 flow 时，旧请求可能晚于新请求完成，
// 导致 selectedHex/selectedRaw 显示旧 flow 的内容但 selectedId 是新 flow（错位）
// 修复审计 7.1：用版本号校验，仅最新请求的结果会被采用
let selectFlowToken = 0
async function selectFlow(f: Flow) {
  const token = ++selectFlowToken
  selectedId.value = f.id
  selectedHex.value = ''
  selectedRaw.value = ''
  activeDetailTab.value = 'hex'
  try {
    // 并行拉取 hex 和完整 flow（用于 Raw 显示）
    const [hexRes, fullFlow] = await Promise.all([
      api.getFlowHex(f.id, { field: hexField.value, length: 8192 }),
      api.getFlow(f.id),
    ])
    // 版本校验：若期间用户又点击了其他 flow，丢弃本次结果
    if (token !== selectFlowToken) return
    selectedHex.value = hexRes.hex
    // Raw：WS-SEND 用 request_body，WS-RECV 用 response_body
    const body = f.method === 'WS-SEND' ? fullFlow.request_body : fullFlow.response_body
    selectedRaw.value = body || ''
  } catch { /* ignore */ }
}

async function refreshHex() {
  if (!selectedId.value) return
  try {
    const r = await api.getFlowHex(selectedId.value, { field: hexField.value, length: 8192 })
    selectedHex.value = r.hex
  } catch { /* ignore */ }
}

function formatSize(n: number | null): string {
  if (n === null) return ''
  if (n < 1024) return n + ' B'
  return (n / 1024).toFixed(1) + ' KB'
}

function formatTime(ts: string): string {
  if (!ts) return ''
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
}

function dirLabel(method: string): string {
  if (method === 'WS-SEND') return '↑ SEND'
  if (method === 'WS-RECV') return '↓ RECV'
  return method
}

function dirColor(method: string): string {
  if (method === 'WS-SEND') return 'var(--on-redirect)'
  if (method === 'WS-RECV') return 'var(--on-ok)'
  return 'var(--on-text-muted)'
}

onMounted(() => {
  loadFlows()
  // 增量轮询：用 since_id 只拉新流量，降低流量大时的开销
  // 性能修复：页面不可见（切到其他标签/最小化）时跳过轮询，避免后台无谓请求
  pollTimer = window.setInterval(() => {
    if (document.hidden) return
    pollNewFlows()
  }, 3000)
  document.addEventListener('click', onGlobalClick)
  window.addEventListener('telnix:flows-cache-cleared', onFlowsCleared)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (scrollPauseTimer !== null) clearTimeout(scrollPauseTimer)
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  document.removeEventListener('click', onGlobalClick)
  window.removeEventListener('telnix:flows-cache-cleared', onFlowsCleared)
  window.removeEventListener('mousemove', onPopupDragMove)
  window.removeEventListener('mouseup', onPopupDragUp)
  // 重置拖动状态，避免组件销毁时正在拖动导致状态不一致
  isDraggingPopup = false
})
</script>

<template>
  <div class="ws-view-page full flex flex-col">
    <!-- 顶部工具栏 -->
    <div class="ws-toolbar">
      <span class="ws-title no-select">
        <el-icon><Connection /></el-icon>&nbsp;{{ t('ws.title') }}
      </span>
      <div class="flex-1"></div>
      <span class="text-dim mono" style="font-size: 11px">
        {{ t('ws.aggregateHint') }}
      </span>
    </div>

    <!-- 过滤栏（图标按钮风格，对齐抓包页 FlowList） -->
    <div class="filter-bar">
      <el-button size="small" :type="hasActiveFilters ? 'primary' : 'default'" @click="togglePopup('filter')">
        <el-icon><Filter /></el-icon>&nbsp;{{ t('ws.filter') }}
        <span v-if="hasActiveFilters" class="filter-badge"></span>
      </el-button>
      <el-dropdown size="small" :disabled="!selectedFlow" @command="(c: string) => { c === 'host' && ignoreHost() }">
        <el-button size="small" :disabled="!selectedFlow">
          <el-icon><RemoveFilled /></el-icon>&nbsp;{{ t('ws.ignore') }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="host" :disabled="!selectedFlow?.host">{{ t('ws.byHost') }}</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <!-- 专注模式：点击打开悬浮窗 -->
      <el-tooltip :content="focusEnabled ? t('ws.focusActiveHint') : t('ws.focusModeHint')" placement="bottom">
        <el-button
          size="small"
          :type="focusEnabled ? 'success' : 'default'"
          @click="togglePopup('focus')"
        >
          <el-icon><Aim /></el-icon>&nbsp;{{ t('ws.focus') }}
          <span v-if="focusEnabled" class="filter-badge"></span>
        </el-button>
      </el-tooltip>
      <el-tooltip :content="flowsStore.autoScroll ? (flowsStore.autoScrollPaused ? t('ws.autoScrollPaused', { n: flowsStore.autoScrollDelay }) : t('ws.autoScrollOn')) : t('ws.autoScrollOff')" placement="bottom">
        <el-button size="small" :type="flowsStore.autoScroll ? (flowsStore.autoScrollPaused ? 'warning' : 'primary') : 'default'" circle @click="flowsStore.autoScroll = !flowsStore.autoScroll">
          <el-icon><Bottom /></el-icon>
        </el-button>
      </el-tooltip>
      <el-tooltip :content="multiSelectMode ? t('ws.exitMultiSelect') : t('ws.multiSelectMode')" placement="bottom">
        <el-button
          size="small"
          :type="multiSelectMode ? 'warning' : 'default'"
          circle
          @click="toggleMultiSelect"
        >
          <el-icon><CircleCheck /></el-icon>
        </el-button>
      </el-tooltip>
    </div>

    <!-- 统一悬浮窗（筛选 / 专注 互斥） -->
    <transition name="popup-fade">
      <div
        v-if="activePopup"
        class="popup-shell"
        :style="{ left: popupPos.left + 'px', top: popupPos.top + 'px' }"
        @click.stop
      >
        <div class="popup-header" @mousedown="onPopupHeaderDown">
          <span class="popup-title">{{ activePopup === 'filter' ? t('ws.filter') : t('ws.focus') }}</span>
          <el-icon class="popup-close" @click="closePopup"><Close /></el-icon>
        </div>
        <div class="popup-body">
          <!-- 筛选 -->
          <template v-if="activePopup === 'filter'">
            <div class="fp-row">
              <label class="fp-label">Host</label>
              <el-select
                v-model="filterHosts"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                :placeholder="t('ws.filterHostPlaceholder')"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option v-for="h in knownHosts" :key="h" :label="h" :value="h" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">{{ t('ws.direction') }}</label>
              <el-select
                v-model="filterDirs"
                multiple
                :reserve-keyword="false"
                :placeholder="t('ws.selectDirPlaceholder')"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option :label="t('ws.sendDir')" value="WS-SEND" />
                <el-option :label="t('ws.recvDir')" value="WS-RECV" />
              </el-select>
            </div>
            <div class="fp-tip text-dim">{{ t('ws.filterAndTip') }}</div>
            <div class="fp-actions">
              <el-button size="small" @click="resetFilters">{{ t('ws.reset') }}</el-button>
              <el-button size="small" type="primary" @click="closePopup">{{ t('ws.done') }}</el-button>
            </div>
          </template>
          <!-- 专注 -->
          <template v-if="activePopup === 'focus'">
            <div class="fp-row">
              <label class="fp-label">Host</label>
              <el-select
                v-model="focusHosts"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                :placeholder="t('ws.focusHostPlaceholder')"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option v-for="h in knownHosts" :key="h" :label="h" :value="h" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">{{ t('ws.direction') }}</label>
              <el-select
                v-model="focusDirs"
                multiple
                :reserve-keyword="false"
                :placeholder="t('ws.selectDirOrPlaceholder')"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option :label="t('ws.sendDir')" value="WS-SEND" />
                <el-option :label="t('ws.recvDir')" value="WS-RECV" />
              </el-select>
            </div>
            <div class="fp-tip text-dim">{{ t('ws.focusOrTip') }}</div>
            <div class="fp-actions">
              <el-button size="small" @click="resetFocus">{{ t('ws.reset') }}</el-button>
              <el-button size="small" type="primary" @click="closePopup">{{ t('ws.done') }}</el-button>
            </div>
          </template>
        </div>
      </div>
    </transition>

    <!-- 主体：左列表 + 右详情 + 可拖拽分隔线 -->
    <div class="ws-body flex-1 flex overflow-hidden" ref="wsBodyRef">
      <div class="ws-list-pane" :style="{ width: `${leftRatio * 100}%` }">
        <div class="wl-head mono no-select" :style="{ gridTemplateColumns: gridCols }">
          <div v-if="multiSelectMode" class="wl-check"></div>
          <div class="wl-id">#</div>
          <div class="wl-dir no-select">{{ t('ws.direction') }}</div>
          <div class="wl-host no-select">Host</div>
          <div class="wl-path no-select">Path</div>
          <div class="wl-size no-select">{{ t('ws.size') }}</div>
          <div class="wl-time no-select">{{ t('ws.time') }}</div>
        </div>
        <div ref="bodyRef" class="wl-body flex-1 overflow-auto" @scroll="onBodyScroll">
          <div :style="{ height: topPad + 'px' }"></div>
          <div
            v-for="f in visibleItems"
            :key="f.id"
            class="wl-row mono"
            :class="{ selected: selectedId === f.id, checked: selectedFlowIds.has(f.id) }"
            :style="{ gridTemplateColumns: gridCols }"
            :data-flow-id="f.id"
            @click="multiSelectMode ? toggleFlowCheck(f.id) : selectFlow(f)"
            @dblclick="onFlowDblClick(f)"
            @contextmenu="onContextMenu($event, f)"
          >
            <div v-if="multiSelectMode" class="wl-check" @click.stop="toggleFlowCheck(f.id)">
              <el-checkbox :model-value="selectedFlowIds.has(f.id)" size="small" />
            </div>
            <div class="wl-id text-dim">{{ f.id }}</div>
            <div class="wl-dir" :style="{ color: dirColor(f.method) }">{{ dirLabel(f.method) }}</div>
            <div class="wl-host text-muted">{{ f.host || '-' }}</div>
            <div class="wl-path text-muted wl-path-url">{{ f.path || '-' }}</div>
            <div class="wl-size text-muted">{{ formatSize(f.size) }}</div>
            <div class="wl-time text-muted">{{ formatTime(f.timestamp) }}</div>
          </div>
          <div :style="{ height: bottomPad + 'px' }"></div>
          <div v-if="!displayFlows.length" class="empty-text text-dim">{{ t('ws.noMessages') }}</div>
        </div>

        <!-- 多选模式悬浮工具栏 -->
        <transition name="float-bar">
          <div
            v-if="multiSelectMode && floatBarVisible"
            class="multi-float-bar"
            @mouseenter="showFloatBarNow"
          >
            <span class="mfb-count no-select">{{ t('ws.selectedN', { n: selectedCount }) }}</span>
            <el-button size="small" @click="selectAllFlows">{{ t('ws.selectAll') }}</el-button>
            <el-button size="small" @click="clearSelection" :disabled="!selectedCount">{{ t('ws.clear') }}</el-button>
            <el-button
              size="small"
              type="danger"
              :disabled="!selectedCount"
              @click="batchDeleteFlows"
            >
              <el-icon><Delete /></el-icon>&nbsp;{{ t('ws.delete') }}
            </el-button>
            <!-- 子悬浮窗：删除确认 -->
            <transition name="popup-fade">
              <div
                v-if="showDeleteConfirm"
                class="delete-confirm-pop"
                @click.stop
                @mouseenter="showFloatBarNow"
              >
                <div class="dc-text">{{ t('ws.deleteConfirmFlows', { n: selectedCount }) }}</div>
                <div class="dc-actions">
                  <el-button size="small" @click="cancelDeleteFlows">{{ t('ws.cancel') }}</el-button>
                  <el-button size="small" type="danger" @click="confirmDeleteFlows">{{ t('ws.delete') }}</el-button>
                </div>
              </div>
            </transition>
          </div>
        </transition>
      </div>
      <div class="ws-splitter" :class="{ active: dragging }" @mousedown="onSplitDown"></div>
      <div class="ws-detail-pane" :style="{ width: `${(1 - leftRatio) * 100}%` }">
        <div v-if="!selectedFlow" class="empty-detail text-dim">
          <el-icon :size="36"><Document /></el-icon>
          <div style="margin-top: 10px">{{ t('ws.selectToViewHex') }}</div>
        </div>
        <template v-else>
          <div class="detail-head">
            <span class="dir-badge" :style="{ color: dirColor(selectedFlow.method), borderColor: dirColor(selectedFlow.method) }">
              {{ dirLabel(selectedFlow.method) }}
            </span>
            <span class="mono text-muted">#{{ selectedFlow.id }}</span>
            <span class="text-muted detail-url">{{ selectedFlow.host }}{{ selectedFlow.path }}</span>
            <span class="text-muted" v-if="selectedFlow.process_name">{{ selectedFlow.process_name }}</span>
            <div class="flex-1"></div>
          </div>
          <el-tabs v-model="activeDetailTab" class="ws-detail-tabs">
            <el-tab-pane label="Hex" name="hex" lazy>
              <HexView :data="selectedHex" />
            </el-tab-pane>
            <el-tab-pane label="Raw" name="raw" lazy>
              <RawView :flow="selectedFlow" type="ws" />
            </el-tab-pane>
          </el-tabs>
        </template>
      </div>
    </div>

    <!-- 右键菜单 -->
    <teleport to="body">
      <div
        v-if="ctxMenu.visible"
        ref="ctxMenuRef"
        class="ctx-menu"
        :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
        @click.stop
      >
        <div class="ctx-item" @click="onFlowDblClick(ctxMenu.flow!)"><el-icon><Aim /></el-icon>&nbsp;{{ t('ws.viewInCapture') }}</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxCopyUrl"><el-icon><Link /></el-icon>&nbsp;{{ t('ws.copyUrl') }}</div>
        <div class="ctx-item" @click="ctxCopyCurl"><el-icon><DocumentCopy /></el-icon>&nbsp;{{ t('ws.copyCurl') }}</div>
        <div class="ctx-item" @click="ctxCopyRequest"><el-icon><Top /></el-icon>&nbsp;{{ t('ws.copyRequest') }}</div>
        <div class="ctx-item" @click="ctxCopyResponse"><el-icon><Bottom /></el-icon>&nbsp;{{ t('ws.copyResponse') }}</div>
        <div class="ctx-sep"></div>
        <!-- 忽略：hover 子菜单 -->
        <div class="ctx-item ctx-submenu" @mouseenter="onSubmenuEnter('ignore')" @mouseleave="onSubmenuLeave">
          <el-icon><Filter /></el-icon>&nbsp;{{ t('ws.ignore') }}
          <el-icon class="ctx-arrow"><ArrowRight /></el-icon>
          <div class="ctx-submenu-panel" :class="{ visible: showIgnoreSubmenu }">
            <div class="ctx-item" @click="ctxIgnoreProcess"><el-icon><Link /></el-icon>&nbsp;{{ t('ws.ignoreByProcess') }}</div>
            <div class="ctx-item" @click="ctxIgnorePid"><el-icon><Link /></el-icon>&nbsp;{{ t('ws.ignoreByPid') }}</div>
            <div class="ctx-item" @click="ctxIgnoreHost"><el-icon><Link /></el-icon>&nbsp;{{ t('ws.ignoreByHost') }}</div>
          </div>
        </div>
        <div class="ctx-sep"></div>
        <!-- 复制：hover 子菜单 -->
        <div class="ctx-item ctx-submenu" @mouseenter="onSubmenuEnter('copy')" @mouseleave="onSubmenuLeave">
          <el-icon><CopyDocument /></el-icon>&nbsp;{{ t('ws.copy') }}
          <el-icon class="ctx-arrow"><ArrowRight /></el-icon>
          <div class="ctx-submenu-panel" :class="{ visible: showCopySubmenu }">
            <div v-for="item in COPY_FIELDS" :key="item.key" class="ctx-item" @click="copyField(item.field)">{{ item.label }}</div>
          </div>
        </div>
        <div class="ctx-sep"></div>
        <div class="ctx-item ctx-danger" @click="ctxDelete"><el-icon><Delete /></el-icon>&nbsp;{{ t('ws.delete') }}</div>
      </div>
    </teleport>
  </div>
</template>

<style scoped>
.ws-view-page { background: var(--on-bg); position: relative; }
.ws-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.ws-title { font-weight: 600; font-size: 13px; display: flex; align-items: center; }
.ws-status-tags { display: flex; gap: 6px; align-items: center; }
.ws-body { min-height: 0; }
.ws-list-pane {
  display: flex; flex-direction: column;
  border-right: 1px solid var(--on-border-light); min-width: 0; overflow: hidden;
}
.wl-head, .wl-row {
  display: grid;
  gap: 4px; padding: 0 10px; align-items: center;
}
.wl-head {
  height: 30px; font-size: 11px; color: var(--on-text-muted); font-weight: 600;
  border-bottom: 1px solid var(--on-border); background: var(--on-bg);
  position: sticky; top: 0; z-index: 1;
  user-select: none;
}
.wl-row {
  height: 28px; font-size: 12px; cursor: pointer;
  border-bottom: 1px solid var(--on-border-light);
  user-select: none;
}
.wl-row:hover { background: var(--on-bg-hover); }
.wl-row.selected { background: var(--on-accent-glow); border-left: 2px solid var(--on-accent); }
.wl-row.checked { background: rgba(45, 212, 191, 0.08); }
.wl-row > div { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* WebSocket path 列：特长 URL 单行显示，hover 显示横向滚动条 */
.wl-path-url {
  overflow-x: auto !important;
  overflow-y: hidden !important;
  text-overflow: clip !important;
  scrollbar-width: none;
  min-width: 0; /* 修复 grid 布局中长 URL 撑破 1.5fr 列的问题 */
}
.wl-path-url::-webkit-scrollbar { height: 0; }
.wl-path-url:hover::-webkit-scrollbar { height: 4px; }
.wl-path-url:hover::-webkit-scrollbar-thumb { background: var(--on-border-light, rgba(128,128,128,.3)); border-radius: 2px; }
.wl-path-url:hover::-webkit-scrollbar-track { background: transparent; }
.wl-check { display: flex; align-items: center; justify-content: center; }
.empty-text { text-align: center; padding: 30px; color: var(--on-text-dim); }
.ws-detail-pane { display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.ws-splitter {
  width: 4px; background: var(--on-border-light); cursor: col-resize; flex-shrink: 0;
  transition: background 0.15s;
}
.ws-splitter:hover, .ws-splitter.active { background: var(--on-accent); }
.empty-detail {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}
.detail-head {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  font-size: 13px;
}
/* 详情区 URL：单行 + hover 滚动条 */
.detail-url {
  white-space: nowrap;
  overflow-x: auto; overflow-y: hidden;
  min-width: 60px; flex: 1 1 auto;
  scrollbar-width: none;
}
.detail-url::-webkit-scrollbar { height: 0; }
.detail-url:hover::-webkit-scrollbar { height: 4px; }
.detail-url:hover::-webkit-scrollbar-thumb { background: var(--on-border-light, rgba(128,128,128,.3)); border-radius: 2px; }
.detail-url:hover::-webkit-scrollbar-track { background: transparent; }
.dir-badge {
  font-family: var(--on-font-mono); font-weight: 700; font-size: 11px;
  padding: 2px 6px; border-radius: var(--on-radius-sm); border: 1px solid;
  white-space: nowrap; flex-shrink: 0;
}
.hex-container { flex: 1; min-height: 0; overflow: hidden; }
.ws-detail-tabs { padding: 0 10px; display: flex; flex-direction: column; height: calc(100% - 42px); }
.ws-detail-tabs :deep(.el-tabs__content) { flex: 1; overflow: hidden; }
.ws-detail-tabs :deep(.el-tab-pane) { height: 100%; }
.ws-raw-view { height: 100%; padding: 8px; }
.ws-raw-view pre {
  margin: 0; white-space: pre-wrap; word-break: break-all;
  font-size: 12.5px; line-height: 1.6; color: var(--on-text);
}

.filter-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.filter-badge {
  display: inline-block;
  width: 6px; height: 6px;
  border-radius: var(--on-radius-full);
  background: var(--on-accent);
  margin-left: 2px;
  vertical-align: middle;
}

/* 统一悬浮窗 */
/* UX 修复：min-width 用 min() 钳位并限制 max-width，窄窗口下不溢出视口 */
.popup-shell {
  position: absolute;
  z-index: 30;
  min-width: min(340px, calc(100vw - 20px));
  max-width: calc(100vw - 20px);
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border);
  border-radius: var(--on-radius-lg);
  box-shadow: var(--on-shadow-lg);
  font-size: 12.5px;
  overflow: hidden;
}
.popup-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 12px;
  cursor: move;
  background: var(--on-bg-hover);
  border-bottom: 1px solid var(--on-border-light);
  user-select: none;
}
.popup-title { font-weight: 600; font-size: 12.5px; }
.popup-close { cursor: pointer; opacity: .6; }
.popup-close:hover { opacity: 1; color: var(--el-color-danger); }
.popup-body { padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
.popup-fade-enter-active, .popup-fade-leave-active {
  transition: opacity .15s ease, transform .15s ease;
}
.popup-fade-enter-from, .popup-fade-leave-to {
  opacity: 0; transform: translateY(-6px) scale(.98);
}
.fp-row { display: flex; align-items: flex-start; gap: 10px; }
.fp-label {
  width: 48px; color: var(--on-text-muted); font-size: 12px;
  flex-shrink: 0;
  line-height: 28px;
}
.fp-tip {
  font-size: 11px;
  line-height: 1.4;
  padding: 2px 0;
}
.fp-actions {
  display: flex; justify-content: flex-end; gap: 8px;
  padding-top: 4px;
  border-top: 1px solid var(--on-border-light);
}
/* 竖向 tag：每行一个，最多 5 行，超过滚动 */
.vertical-tags { position: relative; }
.vertical-tags :deep(.el-select__tags) {
  flex-direction: column;
  align-items: stretch;
  max-height: 140px;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 2px 0;
  scrollbar-width: thin;
}
.vertical-tags :deep(.el-select__tags::-webkit-scrollbar) { width: 6px; }
.vertical-tags :deep(.el-select__tags::-webkit-scrollbar-thumb) {
  background: var(--on-border);
  border-radius: var(--on-radius-sm);
}
.vertical-tags :deep(.el-select__tags .el-tag) {
  width: 100%;
  justify-content: space-between;
  margin: 1px 0;
  flex-shrink: 0;
}
.vertical-tags :deep(.el-select__tags .el-tag .el-select__tags-text) {
  max-width: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vertical-tags :deep(.el-select__tags .el-tag .el-tag__close) { flex-shrink: 0; }

/* 多选模式悬浮工具栏 */
.multi-float-bar {
  position: absolute;
  top: 38px;
  right: 12px;
  z-index: 20;
  display: flex; align-items: center; gap: 6px;
  padding: 6px 10px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border);
  border-radius: var(--on-radius-lg);
  box-shadow: var(--on-shadow-md);
  font-size: 12px;
}
.mfb-count {
  color: var(--on-accent);
  font-weight: 600;
  padding-right: 4px;
}
.float-bar-enter-active, .float-bar-leave-active {
  transition: opacity .15s ease, transform .15s ease;
}
.float-bar-enter-from, .float-bar-leave-to {
  opacity: 0; transform: translateY(-6px);
}
/* 删除确认子悬浮窗 */
.delete-confirm-pop {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 6px;
  padding: 10px 12px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border);
  border-radius: var(--on-radius-lg);
  box-shadow: var(--on-shadow-md);
  min-width: 220px;
  z-index: 21;
}
.dc-text { font-size: 12px; margin-bottom: 8px; }
.dc-actions { display: flex; justify-content: flex-end; gap: 8px; }

/* 右键菜单 */
.ctx-menu {
  position: fixed; z-index: 9999;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border);
  border-radius: var(--on-radius-md);
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  font-size: 12.5px; min-width: 140px;
  padding: 4px 0;
}
.ctx-item {
  padding: 6px 14px; cursor: pointer; display: flex; align-items: center;
  color: var(--on-text);
}
.ctx-item:hover { background: var(--on-bg-hover); }
.ctx-danger { color: var(--on-error); }
.ctx-danger:hover { background: rgba(248,81,73,0.12); }
.ctx-sep { height: 1px; background: var(--on-border-light); margin: 4px 0; }
.ctx-submenu { position: relative; }
.ctx-arrow { margin-left: auto; font-size: 10px; color: var(--on-text-dim); }
.ctx-submenu-panel {
  display: none; position: absolute; left: 100%; top: 0;
  min-width: 120px; background: var(--on-bg-elevated);
  border: 1px solid var(--on-border); border-radius: var(--on-radius-md);
  box-shadow: 0 4px 16px rgba(0,0,0,0.4); padding: 4px 0;
}
.ctx-submenu:hover .ctx-submenu-panel, .ctx-submenu-panel.visible { display: block; }
</style>
