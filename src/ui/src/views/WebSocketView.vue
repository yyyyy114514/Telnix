<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Flow } from '../api/client'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import HexView from '../components/HexView.vue'

const capture = useCaptureStore()
const flowsStore = useFlowsStore()
const router = useRouter()

const flows = ref<Flow[]>([])
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
  popupPos.value = {
    left: popupStartLeft + (e.clientX - dragStartX),
    top: popupStartTop + (e.clientY - dragStartY),
  }
}
function onPopupDragUp() {
  isDraggingPopup = false
  window.removeEventListener('mousemove', onPopupDragMove)
  window.removeEventListener('mouseup', onPopupDragUp)
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
    ElMessage.success(`已删除 ${ids.length} 条`)
    flows.value = flows.value.filter(x => !ids.includes(x.id))
    if (selectedId.value !== null && ids.includes(selectedId.value)) {
      selectedId.value = null
      selectedHex.value = ''
    }
    selectedFlowIds.value.clear()
    multiSelectMode.value = false
    showDeleteConfirm.value = false
  } catch (e: any) {
    ElMessage.error('删除失败：' + (e?.message || e))
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
    ElMessage.warning('该流量无 Host 信息')
    return
  }
  try {
    await api.ignoreHost(f.host)
    ElMessage.success(`已忽略 Host ${f.host}（对新连接生效，已有连接需重启后端）`)
  } catch (e: any) {
    ElMessage.error('忽略失败：' + (e?.message || e))
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
let scrollPauseTimer: number | null = null
let programmaticScroll = false

function onBodyScroll() {
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

const COPY_FIELDS = [
  { key: 'id', label: 'ID', field: 'id' },
  { key: 'method', label: '方向', field: 'method' },
  { key: 'host', label: 'Host', field: 'host' },
  { key: 'path', label: 'Path', field: 'path' },
  { key: 'process', label: '进程', field: 'process_name' },
  { key: 'pid', label: 'PID', field: 'pid' },
  { key: 'size', label: '大小', field: 'size' },
  { key: 'time', label: '时间', field: 'timestamp' },
]

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
  const v = f[field]
  return v === null || v === undefined ? '' : String(v)
}

function copyField(field: string) {
  const f = ctxMenu.value.flow
  if (!f) return
  const text = getFieldValue(f, field)
  navigator.clipboard.writeText(text).catch(() => {})
  ElMessage.success(`已复制：${text.length > 40 ? text.slice(0, 40) + '...' : text}`)
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
    await ElMessageBox.confirm(`确定删除 #${f.id}？`, '删除', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
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
    ElMessage.success('已删除')
  } catch (e: any) {
    ElMessage.error('删除失败：' + (e?.message || e))
  }
  closeCtxMenu()
}

function onGlobalClick() {
  if (ctxMenu.value.visible) closeCtxMenu()
}

async function loadFlows() {
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

async function selectFlow(f: Flow) {
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
  pollTimer = window.setInterval(() => {
    pollNewFlows()
  }, 2000)
  document.addEventListener('click', onGlobalClick)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (scrollPauseTimer !== null) clearTimeout(scrollPauseTimer)
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  document.removeEventListener('click', onGlobalClick)
  window.removeEventListener('mousemove', onPopupDragMove)
  window.removeEventListener('mouseup', onPopupDragUp)
})
</script>

<template>
  <div class="ws-view-page full flex flex-col">
    <!-- 顶部工具栏 -->
    <div class="ws-toolbar">
      <span class="ws-title">
        <el-icon><Connection /></el-icon>&nbsp;WebSocket 消息
      </span>
      <div class="ws-status-tags">
        <el-tag size="small" type="info">{{ flows.length }} 条消息</el-tag>
      </div>
      <div class="flex-1"></div>
      <span class="text-dim mono" style="font-size: 11px">
        WebSocket 帧按 message 聚合记录 · 双击在抓包页查看
      </span>
    </div>

    <!-- 过滤栏（图标按钮风格，对齐抓包页 FlowList） -->
    <div class="filter-bar">
      <el-button size="small" :type="hasActiveFilters ? 'primary' : 'default'" @click="togglePopup('filter')">
        <el-icon><Filter /></el-icon>&nbsp;筛选
        <span v-if="hasActiveFilters" class="filter-badge"></span>
      </el-button>
      <el-dropdown size="small" :disabled="!selectedFlow" @command="(c: string) => { c === 'host' && ignoreHost() }">
        <el-button size="small" :disabled="!selectedFlow">
          <el-icon><RemoveFilled /></el-icon>&nbsp;忽略<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="host" :disabled="!selectedFlow?.host">按 Host</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <!-- 专注模式：点击打开悬浮窗 -->
      <el-tooltip :content="focusEnabled ? '专注中（点击配置/清空条件）' : '专注模式（点击配置条件）'" placement="bottom">
        <el-button
          size="small"
          :type="focusEnabled ? 'success' : 'default'"
          @click="togglePopup('focus')"
        >
          <el-icon><Aim /></el-icon>&nbsp;专注
          <span v-if="focusEnabled" class="filter-badge"></span>
        </el-button>
      </el-tooltip>
      <el-tooltip :content="flowsStore.autoScroll ? (flowsStore.autoScrollPaused ? `自动滚动：暂停中（${flowsStore.autoScrollDelay}s 后恢复）` : '自动滚动：开') : '自动滚动：关'" placement="top">
        <el-button size="small" :type="flowsStore.autoScroll ? (flowsStore.autoScrollPaused ? 'warning' : 'primary') : 'default'" circle @click="flowsStore.autoScroll = !flowsStore.autoScroll">
          <el-icon><Bottom /></el-icon>
        </el-button>
      </el-tooltip>
      <el-tooltip :content="multiSelectMode ? '退出多选' : '多选模式'" placement="top">
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
          <span class="popup-title">{{ activePopup === 'filter' ? '筛选' : '专注' }}</span>
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
                placeholder="输入 host 按 Enter 添加，支持 * ? 通配符"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option v-for="h in knownHosts" :key="h" :label="h" :value="h" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">方向</label>
              <el-select
                v-model="filterDirs"
                multiple
                :reserve-keyword="false"
                placeholder="选择方向（可多选）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option label="↑ 发送 (WS-SEND)" value="WS-SEND" />
                <el-option label="↓ 接收 (WS-RECV)" value="WS-RECV" />
              </el-select>
            </div>
            <div class="fp-tip text-dim">筛选采用 AND 语义：所有条件都需满足。仅前端有效。</div>
            <div class="fp-actions">
              <el-button size="small" @click="resetFilters">重置</el-button>
              <el-button size="small" type="primary" @click="closePopup">完成</el-button>
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
                placeholder="通配符如 *.example.com 或选择已出现 host"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option v-for="h in knownHosts" :key="h" :label="h" :value="h" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">方向</label>
              <el-select
                v-model="focusDirs"
                multiple
                :reserve-keyword="false"
                placeholder="选择方向（多选，OR 匹配）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option label="↑ 发送 (WS-SEND)" value="WS-SEND" />
                <el-option label="↓ 接收 (WS-RECV)" value="WS-RECV" />
              </el-select>
            </div>
            <div class="fp-tip text-dim">专注采用 OR 语义：满足任一条件即显示。仅前端有效。</div>
            <div class="fp-actions">
              <el-button size="small" @click="resetFocus">重置</el-button>
              <el-button size="small" type="primary" @click="closePopup">完成</el-button>
            </div>
          </template>
        </div>
      </div>
    </transition>

    <!-- 主体：左列表 + 右详情 -->
    <div class="ws-body flex-1 flex overflow-hidden">
      <div class="ws-list-pane">
        <div class="wl-head mono" :style="{ gridTemplateColumns: gridCols }">
          <div v-if="multiSelectMode" class="wl-check"></div>
          <div class="wl-id">#</div>
          <div class="wl-dir">方向</div>
          <div class="wl-host">Host</div>
          <div class="wl-path">Path</div>
          <div class="wl-size">大小</div>
          <div class="wl-time">时间</div>
        </div>
        <div ref="bodyRef" class="wl-body flex-1 overflow-auto" @scroll="onBodyScroll">
          <div
            v-for="f in displayFlows"
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
            <div class="wl-path text-muted">{{ f.path || '-' }}</div>
            <div class="wl-size text-muted">{{ formatSize(f.size) }}</div>
            <div class="wl-time text-muted">{{ formatTime(f.timestamp) }}</div>
          </div>
          <div v-if="!flows.length" class="empty-text text-dim">暂无 WebSocket 消息</div>
        </div>

        <!-- 多选模式悬浮工具栏 -->
        <transition name="float-bar">
          <div
            v-if="multiSelectMode && floatBarVisible"
            class="multi-float-bar"
            @mouseenter="showFloatBarNow"
          >
            <span class="mfb-count">已选 {{ selectedCount }}</span>
            <el-button size="small" @click="selectAllFlows">全选</el-button>
            <el-button size="small" @click="clearSelection" :disabled="!selectedCount">清空</el-button>
            <el-button
              size="small"
              type="danger"
              :disabled="!selectedCount"
              @click="batchDeleteFlows"
            >
              <el-icon><Delete /></el-icon>&nbsp;删除
            </el-button>
            <!-- 子悬浮窗：删除确认 -->
            <transition name="popup-fade">
              <div
                v-if="showDeleteConfirm"
                class="delete-confirm-pop"
                @click.stop
                @mouseenter="showFloatBarNow"
              >
                <div class="dc-text">确定删除选中的 {{ selectedCount }} 条流量？</div>
                <div class="dc-actions">
                  <el-button size="small" @click="cancelDeleteFlows">取消</el-button>
                  <el-button size="small" type="danger" @click="confirmDeleteFlows">删除</el-button>
                </div>
              </div>
            </transition>
          </div>
        </transition>
      </div>
      <div class="ws-detail-pane">
        <div v-if="!selectedFlow" class="empty-detail text-dim">
          <el-icon :size="36"><Document /></el-icon>
          <div style="margin-top: 10px">选择左侧消息查看 Hex</div>
        </div>
        <template v-else>
          <div class="detail-head">
            <span class="dir-badge" :style="{ color: dirColor(selectedFlow.method), borderColor: dirColor(selectedFlow.method) }">
              {{ dirLabel(selectedFlow.method) }}
            </span>
            <span class="mono text-muted">#{{ selectedFlow.id }}</span>
            <span class="text-muted">{{ selectedFlow.host }}{{ selectedFlow.path }}</span>
            <span class="text-muted" v-if="selectedFlow.process_name">{{ selectedFlow.process_name }}</span>
            <div class="flex-1"></div>
          </div>
          <el-tabs v-model="activeDetailTab" class="ws-detail-tabs">
            <el-tab-pane label="Hex" name="hex" lazy>
              <HexView :data="selectedHex" />
            </el-tab-pane>
            <el-tab-pane label="Raw" name="raw" lazy>
              <div class="ws-raw-view mono overflow-auto">
                <pre>{{ selectedRaw || '(空)' }}</pre>
              </div>
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
        <div class="ctx-item" @click="onFlowDblClick(ctxMenu.flow!)"><el-icon><Aim /></el-icon>&nbsp;在抓包页查看</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item ctx-submenu">
          <el-icon><CopyDocument /></el-icon>&nbsp;复制
          <el-icon class="ctx-arrow"><ArrowRight /></el-icon>
        </div>
        <div class="ctx-submenu-panel">
          <div v-for="item in COPY_FIELDS" :key="item.key" class="ctx-item" @click="copyField(item.field)">{{ item.label }}</div>
        </div>
        <div class="ctx-sep"></div>
        <div class="ctx-item ctx-danger" @click="ctxDelete"><el-icon><Delete /></el-icon>&nbsp;删除</div>
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
  width: 50%; display: flex; flex-direction: column;
  border-right: 1px solid var(--on-border-light); min-width: 0;
  position: relative;
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
.wl-check { display: flex; align-items: center; justify-content: center; }
.empty-text { text-align: center; padding: 30px; color: var(--on-text-dim); }
.ws-detail-pane { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.empty-detail {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}
.detail-head {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  font-size: 13px;
}
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
  background: var(--on-accent, #2dd4bf);
  margin-left: 2px;
  vertical-align: middle;
}

/* 统一悬浮窗 */
.popup-shell {
  position: absolute;
  z-index: 30;
  min-width: 340px;
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-border, #333344);
  border-radius: var(--on-radius-lg);
  box-shadow: 0 6px 24px rgba(0,0,0,0.5);
  font-size: 12.5px;
  overflow: hidden;
}
.popup-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 12px;
  cursor: move;
  background: var(--on-bg-hover, #252535);
  border-bottom: 1px solid var(--on-border-light);
  user-select: none;
}
.popup-title { font-weight: 600; font-size: 12.5px; }
.popup-close { cursor: pointer; opacity: .6; }
.popup-close:hover { opacity: 1; color: var(--el-color-danger, #f56c6c); }
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
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-border, #333344);
  border-radius: var(--on-radius-lg);
  box-shadow: 0 4px 16px rgba(0,0,0,0.45);
  font-size: 12px;
}
.mfb-count {
  color: var(--on-accent, #2dd4bf);
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
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-border, #333344);
  border-radius: var(--on-radius-lg);
  box-shadow: 0 4px 16px rgba(0,0,0,0.5);
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
.ctx-submenu:hover .ctx-submenu-panel { display: block; }
</style>
