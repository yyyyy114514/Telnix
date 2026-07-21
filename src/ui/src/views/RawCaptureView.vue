<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type RawStatus, type Flow } from '../api/client'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import HexView from '../components/HexView.vue'

const capture = useCaptureStore()
const flowsStore = useFlowsStore()
const router = useRouter()

const rawStatus = ref<RawStatus>({ running: false, is_admin: false, pydivert_installed: false })
const filterStr = ref('tcp or udp')
const pidFilter = ref('')
const portFilter = ref('')
// 协议筛选（前端过滤，只含 tcp/udp，不含 http）
const protoFilter = ref<string[]>([])
const flows = ref<Flow[]>([])
const selectedId = ref<number | null>(null)
const selectedHex = ref('')
const hexField = ref<'raw_data' | 'request_body' | 'response_body'>('raw_data')
// 安装 pydivert 状态
const installingPydivert = ref(false)
// 管理员重启中
const restartingAsAdmin = ref(false)

let pollTimer: number | null = null

const selectedFlow = computed(() => flows.value.find(f => f.id === selectedId.value) || null)

// ---------- 筛选状态（用于 badge 提示）----------
const hasActiveFilters = computed(() => {
  return pidFilter.value.trim() !== '' ||
    portFilter.value.trim() !== '' ||
    protoFilter.value.length > 0
})

function resetFilters() {
  pidFilter.value = ''
  portFilter.value = ''
  protoFilter.value = []
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

// ---------- 专注模式（前端 OR 语义，不调后端 api.setFocus） ----------
const focusPids = ref<number[]>([])
const focusProcessSelect = ref<number[]>([])
const focusProcessName = ref('')
const focusPorts = ref<number[]>([])
const focusProtocols = ref<string[]>([])
const focusEnabled = computed(() =>
  focusPids.value.length > 0 ||
  focusPorts.value.length > 0 ||
  focusProtocols.value.length > 0 ||
  focusProcessName.value.trim() !== ''
)

// 专注 PID 多选选项：从当前 flows 的 pid 去重
const focusPidOptions = computed(() => {
  const set = new Map<number, string>()
  for (const f of flows.value) {
    if (f.pid && f.pid > 0 && !set.has(f.pid)) {
      set.set(f.pid, f.process_name || `PID ${f.pid}`)
    }
  }
  return [...set.entries()].map(([pid, name]) => ({ pid, name }))
})

// 专注端口多选选项：从当前 flows 的 src_port + dst_port 去重
const focusPortOptions = computed(() => {
  const set = new Set<number>()
  for (const f of flows.value) {
    if (f.src_port) set.add(f.src_port)
    if (f.dst_port) set.add(f.dst_port)
  }
  return [...set].sort((a, b) => a - b)
})

function onFocusPidsChange(vals: number[]) {
  focusProcessSelect.value = vals
  focusPids.value = vals
}

function onFocusByName() {
  // 进程名只在前端做匹配（不调用后端）
  const name = focusProcessName.value.trim()
  if (!name) return
  ElMessage.success(`专注进程名：${name}（前端匹配）`)
}

function resetFocus() {
  focusPids.value = []
  focusProcessSelect.value = []
  focusProcessName.value = ''
  focusPorts.value = []
  focusProtocols.value = []
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

// 批量忽略 PID：取选中流量的所有非空 PID 去重，逐个调用后端
async function batchIgnorePid() {
  const ids = [...selectedFlowIds.value]
  if (!ids.length) return
  const pidSet = new Set<number>()
  for (const f of flows.value) {
    if (ids.includes(f.id) && f.pid && f.pid > 0) pidSet.add(f.pid)
  }
  if (!pidSet.size) {
    ElMessage.warning('选中流量无 PID 信息')
    return
  }
  try {
    for (const pid of pidSet) {
      await api.ignoreProcess({ pid, name: `PID ${pid}` })
    }
    ElMessage.success(`已忽略 ${pidSet.size} 个 PID`)
    selectedFlowIds.value.clear()
    multiSelectMode.value = false
  } catch (e: any) {
    ElMessage.error('忽略失败：' + (e?.message || e))
  }
}

// 批量忽略进程名：取选中流量的所有非空 process_name 去重
async function batchIgnoreProcess() {
  const ids = [...selectedFlowIds.value]
  if (!ids.length) return
  const nameSet = new Set<string>()
  for (const f of flows.value) {
    if (ids.includes(f.id) && f.process_name) nameSet.add(f.process_name)
  }
  if (!nameSet.size) {
    ElMessage.warning('选中流量无进程名')
    return
  }
  try {
    for (const name of nameSet) {
      await api.ignoreProcess({ pid: null, name })
    }
    ElMessage.success(`已忽略 ${nameSet.size} 个进程`)
    selectedFlowIds.value.clear()
    multiSelectMode.value = false
  } catch (e: any) {
    ElMessage.error('忽略失败：' + (e?.message || e))
  }
}

// ---------- 显示流量（筛选 AND + 专注 OR） ----------
const displayFlows = computed(() => {
  const src = flows.value
  const hasFilter = hasActiveFilters.value
  const focusOn = focusEnabled.value
  if (!hasFilter && !focusOn) return src

  // 解析 PID/端口 字符串
  const filterPidSet = new Set<number>()
  if (pidFilter.value.trim()) {
    for (const s of pidFilter.value.split(',')) {
      const n = parseInt(s.trim())
      if (!isNaN(n)) filterPidSet.add(n)
    }
  }
  const filterPortSet = new Set<number>()
  if (portFilter.value.trim()) {
    for (const s of portFilter.value.split(',')) {
      const n = parseInt(s.trim())
      if (!isNaN(n)) filterPortSet.add(n)
    }
  }
  const filterProtos = protoFilter.value
  const fPids = focusPids.value
  const fPorts = focusPorts.value
  const fProtos = focusProtocols.value
  const fName = focusProcessName.value.trim().toLowerCase()

  const result: Flow[] = []
  for (const f of src) {
    // 筛选（AND 语义）
    if (hasFilter) {
      if (filterPidSet.size > 0 && !(f.pid && filterPidSet.has(f.pid))) continue
      if (filterPortSet.size > 0) {
        const matches = (f.src_port && filterPortSet.has(f.src_port)) ||
                        (f.dst_port && filterPortSet.has(f.dst_port))
        if (!matches) continue
      }
      if (filterProtos.length > 0) {
        const proto = (f.protocol || '').toLowerCase()
        if (!filterProtos.includes(proto)) continue
      }
    }
    // 专注（OR 语义）
    if (focusOn) {
      let focusMatch = false
      if (fPids.length > 0 && f.pid && fPids.includes(f.pid)) focusMatch = true
      else if (fPorts.length > 0) {
        if ((f.src_port && fPorts.includes(f.src_port)) ||
            (f.dst_port && fPorts.includes(f.dst_port))) focusMatch = true
      }
      else if (fProtos.length > 0) {
        const proto = (f.protocol || '').toLowerCase()
        if (fProtos.includes(proto)) focusMatch = true
      }
      else if (fName) {
        const pname = (f.process_name || '').toLowerCase()
        if (pname && pname.includes(fName)) focusMatch = true
      }
      if (!focusMatch) continue
    }
    result.push(f)
  }
  return result
})

// ---------- 列表 grid 模板（多选时插入复选框列） ----------
const gridCols = computed(() => {
  if (multiSelectMode.value) return '32px 50px 60px 1fr 100px 70px 70px'
  return '50px 60px 1fr 100px 70px 70px'
})

// ---------- 自动滚动（复用 flows store，与抓包页/设置同步）----------
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

// flows 数量变化时自动滚到顶部（新流量在顶部）
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

// ---------- 右键菜单 ----------
const ctxMenu = ref<{ visible: boolean; x: number; y: number; flow: Flow | null }>({
  visible: false, x: 0, y: 0, flow: null,
})
const ctxMenuRef = ref<HTMLElement | null>(null)

// 复制项（TCP/UDP 适用：端口、进程、协议、ID）
const COPY_FIELDS = [
  { key: 'id', label: 'ID', field: 'id' },
  { key: 'protocol', label: '协议', field: 'protocol' },
  { key: 'ports', label: '端口', field: 'ports' },
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
  if (field === 'ports') return `${f.src_port || '?'}→${f.dst_port || '?'}`
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

// 双击：跳转抓包页查看（类似全局分析）
function onFlowDblClick(f: Flow) {
  flowsStore.selectFlow(f)
  router.push('/capture')
}

// 忽略进程
async function ctxIgnorePid() {
  const f = ctxMenu.value.flow
  if (!f || !f.pid) {
    ElMessage.warning('该流量无 PID 信息')
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreProcess({ pid: f.pid, name: f.process_name || `PID ${f.pid}` })
    ElMessage.success(`已忽略 PID ${f.pid}`)
  } catch (e: any) {
    ElMessage.error('忽略失败：' + (e?.message || e))
  }
  closeCtxMenu()
}

async function ctxIgnoreProcess() {
  const f = ctxMenu.value.flow
  if (!f || !f.process_name) {
    ElMessage.warning('该流量无进程名')
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreProcess({ pid: null, name: f.process_name })
    ElMessage.success(`已忽略进程 ${f.process_name}`)
  } catch (e: any) {
    ElMessage.error('忽略失败：' + (e?.message || e))
  }
  closeCtxMenu()
}

// 删除流量
async function ctxDelete() {
  const f = ctxMenu.value.flow
  if (!f) return
  try {
    await ElMessageBox.confirm(`确定删除该数据包 #${f.id}？`, '删除', {
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

// 点击空白关闭菜单
function onGlobalClick() {
  if (ctxMenu.value.visible) closeCtxMenu()
}

async function loadStatus() {
  try {
    rawStatus.value = await api.rawStatus()
  } catch { /* ignore */ }
}

async function loadFlows() {
  // 优先用当前 session_id；无则尝试拉最近会话的流量（raw 抓包可能独立创建会话）
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
    const res = await api.getFlows(sid, { limit: 200, protocol: 'tcp' })
    const res2 = await api.getFlows(sid, { limit: 200, protocol: 'udp' })
    // 合并 tcp+udp，按 id 倒序（DNS 协议选项已移除，后端解析代码保留）
    const all = [...(res.flows || []), ...(res2.flows || [])]
    all.sort((a, b) => b.id - a.id)
    flows.value = all.slice(0, 200)
  } catch { /* ignore */ }
}

async function onToggle() {
  try {
    if (rawStatus.value.running) {
      await api.rawStop()
      ElMessage.success('TCP/UDP 抓包已停止')
    } else {
      if (!rawStatus.value.is_admin) {
        ElMessage.warning('需要管理员权限，请点击「管理员重启」')
        return
      }
      if (!rawStatus.value.pydivert_installed) {
        ElMessage.warning('pydivert 未安装，请点击「安装 pydivert」')
        return
      }
      const body: any = { filter_str: filterStr.value }
      if (pidFilter.value.trim()) {
        body.pid_filter = pidFilter.value.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n))
      }
      if (portFilter.value.trim()) {
        body.port_filter = portFilter.value.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n))
      }
      await api.rawStart(body)
      ElMessage.success('TCP/UDP 抓包已启动')
      // 启动后立即拉一次状态（更新 capture.status.session_id）再拉流量
      await capture.fetchStatus()
      await loadFlows()
    }
    await loadStatus()
  } catch (e: any) {
    ElMessage.error('操作失败：' + (e?.message || e))
  }
}

// 以管理员身份重启 OpenNet
async function restartAsAdmin() {
  restartingAsAdmin.value = true
  try {
    await api.restartAsAdmin()
    ElMessage.success('正在以管理员身份重启，请稍候...')
  } catch (e: any) {
    ElMessage.error('重启失败：' + (e?.message || e))
    restartingAsAdmin.value = false
  }
}

// 安装 pydivert
async function installPydivert() {
  installingPydivert.value = true
  try {
    const r: any = await api.installPydivert()
    ElMessage.success('pydivert 安装成功')
    await loadStatus()
  } catch (e: any) {
    ElMessage.error('安装失败：' + (e?.message || e))
  } finally {
    installingPydivert.value = false
  }
}

// 顶部忽略下拉：按 PID / 按进程名（基于当前选中流量）
async function ignorePid() {
  const f = selectedFlow.value
  if (!f || !f.pid) {
    ElMessage.warning('该流量无 PID 信息')
    return
  }
  try {
    await api.ignoreProcess({ pid: f.pid, name: f.process_name || `PID ${f.pid}` })
    ElMessage.success(`已忽略 PID ${f.pid}`)
  } catch (e: any) {
    ElMessage.error('忽略失败：' + (e?.message || e))
  }
}

async function ignoreProcess() {
  const f = selectedFlow.value
  if (!f || !f.process_name) {
    ElMessage.warning('该流量无进程名')
    return
  }
  try {
    await api.ignoreProcess({ pid: null, name: f.process_name })
    ElMessage.success(`已忽略进程 ${f.process_name}`)
  } catch (e: any) {
    ElMessage.error('忽略失败：' + (e?.message || e))
  }
}

async function selectFlow(f: Flow) {
  selectedId.value = f.id
  selectedHex.value = ''
  try {
    const r = await api.getFlowHex(f.id, { field: hexField.value, length: 4096 })
    selectedHex.value = r.hex
  } catch { /* ignore */ }
}

async function refreshHex() {
  if (!selectedId.value) return
  try {
    const r = await api.getFlowHex(selectedId.value, { field: hexField.value, length: 4096 })
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

onMounted(() => {
  loadStatus()
  loadFlows()
  pollTimer = window.setInterval(() => {
    loadStatus()
    if (rawStatus.value.running) loadFlows()
  }, 2000)
  document.addEventListener('click', onGlobalClick)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  document.removeEventListener('click', onGlobalClick)
  window.removeEventListener('mousemove', onPopupDragMove)
  window.removeEventListener('mouseup', onPopupDragUp)
})
</script>

<template>
  <div class="raw-view-page full flex flex-col">
    <!-- 顶部状态 + 控制 -->
    <div class="raw-toolbar">
      <el-button
        :type="rawStatus.running ? 'danger' : 'primary'"
        size="small"
        :disabled="!rawStatus.running && (!rawStatus.is_admin || !rawStatus.pydivert_installed)"
        @click="onToggle"
      >
        <el-icon><component :is="rawStatus.running ? 'VideoPause' : 'VideoPlay'" /></el-icon>
        &nbsp;{{ rawStatus.running ? '停止抓包' : '开始抓包' }}
      </el-button>
      <div class="raw-status-tags">
        <el-tag size="small" :type="rawStatus.is_admin ? 'success' : 'danger'">
          {{ rawStatus.is_admin ? '管理员' : '非管理员' }}
        </el-tag>
        <el-tag size="small" :type="rawStatus.pydivert_installed ? 'success' : 'warning'">
          {{ rawStatus.pydivert_installed ? 'pydivert 已装' : 'pydivert 未装' }}
        </el-tag>
        <el-tag v-if="rawStatus.running" size="small" type="success">抓包中</el-tag>
        <!-- 非管理员：提供管理员重启按钮 -->
        <el-button
          v-if="!rawStatus.is_admin"
          size="small"
          type="warning"
          :loading="restartingAsAdmin"
          @click="restartAsAdmin"
        >
          <el-icon><Key /></el-icon>&nbsp;管理员重启
        </el-button>
        <!-- pydivert 未装：提供安装按钮 -->
        <el-button
          v-if="!rawStatus.pydivert_installed"
          size="small"
          type="primary"
          :loading="installingPydivert"
          @click="installPydivert"
        >
          <el-icon><Download /></el-icon>&nbsp;安装 pydivert
        </el-button>
      </div>
      <div class="flex-1"></div>
      <span class="text-dim mono" style="font-size: 11px">
        WinDivert 网络层抓包 · 需管理员权限
      </span>
    </div>

    <!-- 非管理员警告条 -->
    <div v-if="!rawStatus.is_admin" class="raw-warn-bar">
      <el-icon><WarningFilled /></el-icon>
      <span>TCP/UDP 抓包需要管理员权限。当前非管理员运行，点击右侧「管理员重启」以管理员身份重启 OpenNet。</span>
    </div>
    <!-- pydivert 未装警告条 -->
    <div v-else-if="!rawStatus.pydivert_installed" class="raw-warn-bar">
      <el-icon><WarningFilled /></el-icon>
      <span>未安装 pydivert 驱动。点击右侧「安装 pydivert」自动安装。</span>
    </div>

    <!-- 过滤栏（图标按钮风格，参照 FlowList） -->
    <div class="filter-bar">
      <el-button size="small" :type="hasActiveFilters ? 'primary' : 'default'" @click="togglePopup('filter')">
        <el-icon><Filter /></el-icon>&nbsp;筛选
        <span v-if="hasActiveFilters" class="filter-badge"></span>
      </el-button>
      <el-dropdown size="small" :disabled="!selectedFlow" @command="(c: string) => { c === 'pid' && ignorePid(); c === 'process' && ignoreProcess() }">
        <el-button size="small" :disabled="!selectedFlow">
          <el-icon><RemoveFilled /></el-icon>&nbsp;忽略<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="pid" :disabled="!(selectedFlow?.pid && selectedFlow.pid > 0)">按 PID</el-dropdown-item>
            <el-dropdown-item command="process" :disabled="!selectedFlow?.process_name">按进程名</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <!-- 专注模式：点击打开悬浮窗 -->
      <el-tooltip :content="focusEnabled ? '专注中（点击配置/清空条件）' : '专注模式（点击配置条件）'" placement="top">
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
              <label class="fp-label">BPF</label>
              <el-input v-model="filterStr" size="small" style="width: 360px" placeholder="BPF 过滤表达式（如 tcp or udp）" />
            </div>
            <div class="fp-row">
              <label class="fp-label">PID</label>
              <el-input v-model="pidFilter" size="small" style="width: 360px" placeholder="PID 过滤（逗号分隔）" />
            </div>
            <div class="fp-row">
              <label class="fp-label">端口</label>
              <el-input v-model="portFilter" size="small" style="width: 360px" placeholder="端口过滤（逗号分隔）" />
            </div>
            <div class="fp-row">
              <label class="fp-label">协议</label>
              <el-select
                v-model="protoFilter"
                multiple
                filterable
                :reserve-keyword="false"
                placeholder="选择协议（可多选）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option label="TCP" value="tcp" />
                <el-option label="UDP" value="udp" />
              </el-select>
            </div>
            <div class="fp-tip text-dim">BPF / PID / 端口 在「启动抓包」后生效；协议为前端过滤。</div>
            <div class="fp-actions">
              <el-button size="small" @click="resetFilters">重置</el-button>
              <el-button size="small" type="primary" @click="closePopup">完成</el-button>
            </div>
          </template>
          <!-- 专注 -->
          <template v-if="activePopup === 'focus'">
            <div class="fp-row">
              <label class="fp-label">PID</label>
              <el-select
                v-model="focusProcessSelect"
                multiple
                filterable
                :reserve-keyword="false"
                placeholder="按 PID 选择进程（多选）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onFocusPidsChange"
              >
                <el-option v-for="p in focusPidOptions" :key="p.pid" :label="p.name + ' (' + p.pid + ')'" :value="p.pid" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">进程名</label>
              <el-input v-model="focusProcessName" placeholder="如 chrome.exe（前端模糊匹配）" size="small" clearable style="width: 240px"
                @keyup.enter="onFocusByName" />
              <el-button size="small" @click="onFocusByName">应用</el-button>
            </div>
            <div class="fp-row">
              <label class="fp-label">端口</label>
              <el-select
                v-model="focusPorts"
                multiple
                filterable
                :reserve-keyword="false"
                placeholder="选择端口（多选，OR 匹配）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option v-for="p in focusPortOptions" :key="p" :label="String(p)" :value="p" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">协议</label>
              <el-select
                v-model="focusProtocols"
                multiple
                filterable
                :reserve-keyword="false"
                placeholder="选择协议（多选，OR 匹配）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option label="TCP" value="tcp" />
                <el-option label="UDP" value="udp" />
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
    <div class="raw-body flex-1 flex overflow-hidden">
      <div class="raw-list-pane">
        <div class="rl-head mono" :style="{ gridTemplateColumns: gridCols }">
          <div v-if="multiSelectMode" class="rl-check"></div>
          <div class="rl-id">#</div>
          <div class="rl-proto">协议</div>
          <div class="rl-ports">端口</div>
          <div class="rl-pid">进程</div>
          <div class="rl-size">大小</div>
          <div class="rl-time">时间</div>
        </div>
        <div ref="bodyRef" class="rl-body flex-1 overflow-auto" @scroll="onBodyScroll">
          <div
            v-for="f in displayFlows"
            :key="f.id"
            class="rl-row mono"
            :class="{ selected: selectedId === f.id, checked: selectedFlowIds.has(f.id) }"
            :style="{ gridTemplateColumns: gridCols }"
            :data-flow-id="f.id"
            @click="multiSelectMode ? toggleFlowCheck(f.id) : selectFlow(f)"
            @dblclick="onFlowDblClick(f)"
            @contextmenu="onContextMenu($event, f)"
          >
            <div v-if="multiSelectMode" class="rl-check" @click.stop="toggleFlowCheck(f.id)">
              <el-checkbox :model-value="selectedFlowIds.has(f.id)" size="small" />
            </div>
            <div class="rl-id text-dim">{{ f.id }}</div>
            <div class="rl-proto">
              <el-tag size="small" :type="f.protocol === 'tcp' ? 'primary' : 'warning'">{{ f.protocol }}</el-tag>
            </div>
            <div class="rl-ports text-muted">{{ f.src_port }}→{{ f.dst_port }}</div>
            <div class="rl-pid text-muted">{{ f.process_name || f.pid || '-' }}</div>
            <div class="rl-size text-muted">{{ formatSize(f.size) }}</div>
            <div class="rl-time text-muted">{{ formatTime(f.timestamp) }}</div>
          </div>
          <div v-if="!flows.length" class="empty-text text-dim">暂无 TCP/UDP 数据包</div>
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
              type="warning"
              :disabled="!selectedCount"
              @click="batchIgnorePid"
            >
              <el-icon><RemoveFilled /></el-icon>&nbsp;忽略 PID
            </el-button>
            <el-button
              size="small"
              type="warning"
              :disabled="!selectedCount"
              @click="batchIgnoreProcess"
            >
              <el-icon><RemoveFilled /></el-icon>&nbsp;忽略进程
            </el-button>
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
      <div class="raw-detail-pane">
        <div v-if="!selectedFlow" class="empty-detail text-dim">
          <el-icon :size="36"><Document /></el-icon>
          <div style="margin-top: 10px">选择左侧数据包查看 Hex</div>
        </div>
        <template v-else>
          <div class="detail-head">
            <span class="mono">#{{ selectedFlow.id }} {{ selectedFlow.protocol?.toUpperCase() }}</span>
            <span class="text-muted">{{ selectedFlow.src_port }} → {{ selectedFlow.dst_port }}</span>
            <span class="text-muted">{{ selectedFlow.process_name || 'pid:' + selectedFlow.pid }}</span>
            <div class="flex-1"></div>
            <el-select v-model="hexField" size="small" style="width: 140px" @change="refreshHex">
              <el-option label="原始数据" value="raw_data" />
              <el-option label="请求体" value="request_body" />
              <el-option label="响应体" value="response_body" />
            </el-select>
          </div>
          <div class="hex-container">
            <HexView :data="selectedHex" />
          </div>
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
        <!-- 忽略 -->
        <div class="ctx-item" @click="ctxIgnorePid"><el-icon><RemoveFilled /></el-icon>&nbsp;按 PID 忽略</div>
        <div class="ctx-item" @click="ctxIgnoreProcess"><el-icon><RemoveFilled /></el-icon>&nbsp;按进程名忽略</div>
        <div class="ctx-sep"></div>
        <!-- 复制 -->
        <div class="ctx-item ctx-submenu">
          <el-icon><CopyDocument /></el-icon>&nbsp;复制
          <el-icon class="ctx-arrow"><ArrowRight /></el-icon>
        </div>
        <div class="ctx-submenu-panel">
          <div v-for="item in COPY_FIELDS" :key="item.key" class="ctx-item" @click="copyField(item.field)">{{ item.label }}</div>
        </div>
        <div class="ctx-sep"></div>
        <div class="ctx-item ctx-danger" @click="ctxDelete"><el-icon><Delete /></el-icon>&nbsp;删除流量</div>
      </div>
    </teleport>
  </div>
</template>

<style scoped>
.raw-view-page { background: var(--on-bg); position: relative; }
.raw-toolbar {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.raw-status-tags { display: flex; gap: 6px; align-items: center; }
.raw-warn-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 14px;
  background: var(--on-accent-glow);
  border-bottom: 1px solid var(--on-border-light);
  color: var(--on-warn); font-size: 12.5px;
}
.raw-body { min-height: 0; }
.raw-list-pane {
  width: 50%; display: flex; flex-direction: column;
  border-right: 1px solid var(--on-border-light); min-width: 0;
  position: relative;
}
.rl-head, .rl-row {
  display: grid;
  gap: 4px; padding: 0 10px; align-items: center;
}
.rl-head {
  height: 30px; font-size: 11px; color: var(--on-text-muted); font-weight: 600;
  border-bottom: 1px solid var(--on-border); background: var(--on-bg);
  position: sticky; top: 0; z-index: 1;
  user-select: none;
}
.rl-row {
  height: 28px; font-size: 12px; cursor: pointer;
  border-bottom: 1px solid var(--on-border-light);
  user-select: none;
}
.rl-row:hover { background: var(--on-bg-hover); }
.rl-row.selected { background: var(--on-accent-glow); border-left: 2px solid var(--on-accent); }
.rl-row.checked { background: rgba(45, 212, 191, 0.08); }
.rl-row > div { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rl-check { display: flex; align-items: center; justify-content: center; }
.empty-text { text-align: center; padding: 30px; }
.raw-detail-pane { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.empty-detail {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}
.detail-head {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  font-size: 13px;
}
.hex-container {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* 过滤栏（图标按钮风格，参照 FlowList） */
.filter-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.filter-badge {
  display: inline-block;
  width: 6px; height: 6px;
  border-radius: 50%;
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
  border-radius: 8px;
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
.fp-row {
  display: flex; align-items: flex-start; gap: 10px;
}
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
.vertical-tags {
  position: relative;
}
.vertical-tags :deep(.el-select__tags) {
  flex-direction: column;
  align-items: stretch;
  max-height: 140px;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 2px 0;
  scrollbar-width: thin;
}
.vertical-tags :deep(.el-select__tags::-webkit-scrollbar) {
  width: 6px;
}
.vertical-tags :deep(.el-select__tags::-webkit-scrollbar-thumb) {
  background: var(--on-border);
  border-radius: 3px;
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
.vertical-tags :deep(.el-select__tags .el-tag .el-tag__close) {
  flex-shrink: 0;
}

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
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.45);
  font-size: 12px;
}
.mfb-count {
  color: var(--on-accent, #2dd4bf);
  font-weight: 600;
  padding-right: 4px;
}
.float-bar-enter-active, .float-bar-leave-active {
  transition: opacity .18s ease, transform .18s ease;
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
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  min-width: 220px;
  z-index: 21;
}
.dc-text { font-size: 12px; margin-bottom: 8px; }
.dc-actions { display: flex; justify-content: flex-end; gap: 8px; }

/* 右键菜单样式 */
.ctx-menu {
  position: fixed; z-index: 9999;
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-border, #333344);
  border-radius: 6px;
  padding: 4px 0;
  min-width: 160px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  font-size: 13px;
}
.ctx-item {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 14px; cursor: pointer;
  color: var(--on-text, #ccc);
}
.ctx-item:hover { background: var(--on-bg-hover, #2a2a3e); color: var(--on-accent, #2dd4bf); }
.ctx-item.ctx-danger:hover { color: var(--el-color-danger, #f56c6c); }
.ctx-sep { height: 1px; background: var(--on-border, #333344); margin: 4px 0; }
.ctx-submenu { position: relative; }
.ctx-arrow { margin-left: auto; font-size: 10px; opacity: 0.6; }
.ctx-submenu-panel {
  display: none;
  position: absolute; left: 100%; top: 0;
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-border, #333344);
  border-radius: 6px;
  padding: 4px 0;
  min-width: 140px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
}
.ctx-submenu:hover .ctx-submenu-panel { display: block; }
</style>
