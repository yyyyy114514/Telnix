<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useVirtualList } from '../composables/useVirtualList'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type RawStatus, type Flow } from '../api/client'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import HexView from '../components/HexView.vue'
import ProtocolView from '../components/ProtocolView.vue'

const { t } = useI18n()
const capture = useCaptureStore()
const flowsStore = useFlowsStore()
const router = useRouter()

const rawStatus = ref<RawStatus>({ running: false, is_admin: false, pydivert_installed: true, backend: 'windivert', supported: true })
const filterStr = ref('tcp or udp')
const pidFilter = ref('')
const portFilter = ref('')
// 协议筛选（前端过滤，只含 tcp/udp，不含 http）
const protoFilter = ref<string[]>([])
// 性能优化：flows 列表只做顶层替换（无 .push 单条），用 shallowRef 避免对每条 flow 深度代理
const flows = shallowRef<Flow[]>([])
const selectedId = ref<number | null>(null)
const selectedHex = ref('')
const hexField = ref<'raw_data' | 'request_body' | 'response_body'>('raw_data')
// 详情区 tab：'hex' 显示十六进制，'protocol' 显示协议深度解析
const detailTab = ref<'hex' | 'protocol'>('hex')
// 当前已加载的最大 flow id，用于增量轮询
const maxFlowId = ref(0)
// 管理员重启中
const restartingAsAdmin = ref(false)

let pollTimer: number | null = null

const selectedFlow = computed(() => flows.value.find(f => f.id === selectedId.value) || null)

// 抓包后端友好显示名
const backendDisplayName = computed(() => {
  const b = rawStatus.value.backend
  if (b === 'windivert') return 'WinDivert'
  if (b === 'af_packet') return 'AF_PACKET'
  if (b === 'bpf') return 'BPF'
  return 'TCP/UDP'
})

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
  ElMessage.success(t('raw.focusProcessNameSuccess', { name }))
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
    ElMessage.success(t('raw.deletedCount', { count: ids.length }))
    flows.value = flows.value.filter(x => !ids.includes(x.id))
    if (selectedId.value !== null && ids.includes(selectedId.value)) {
      selectedId.value = null
      selectedHex.value = ''
    }
    selectedFlowIds.value.clear()
    multiSelectMode.value = false
    showDeleteConfirm.value = false
  } catch (e: any) {
    ElMessage.error(t('raw.deleteFailed') + (e?.message || e))
    showDeleteConfirm.value = false
  }
}
function cancelDeleteFlows() {
  showDeleteConfirm.value = false
}

// 批量忽略 PID：取选中流量的所有非空 PID 去重，并发调用后端
// 修复审计 5.1：原代码串行 await，50 个 PID 会产生 50 次串行请求，耗时数秒
async function batchIgnorePid() {
  const ids = [...selectedFlowIds.value]
  if (!ids.length) return
  const pidSet = new Set<number>()
  for (const f of flows.value) {
    if (ids.includes(f.id) && f.pid && f.pid > 0) pidSet.add(f.pid)
  }
  if (!pidSet.size) {
    ElMessage.warning(t('raw.noPidInSelection'))
    return
  }
  try {
    // 并发发起所有忽略请求，Promise.all 任一失败则进入 catch
    // 用 allSettled 可部分成功，但 ignoreProcess 失败罕见且需用户感知，用 all 即可
    const tasks = [...pidSet].map(pid =>
      api.ignoreProcess({ pid, name: `PID ${pid}` })
    )
    await Promise.all(tasks)
    ElMessage.success(t('raw.ignoredPidCount', { count: pidSet.size }))
    selectedFlowIds.value.clear()
    multiSelectMode.value = false
  } catch (e: any) {
    ElMessage.error(t('raw.ignoreFailed') + (e?.message || e))
  }
}

// 批量忽略进程名：取选中流量的所有非空 process_name 去重，并发调用后端
async function batchIgnoreProcess() {
  const ids = [...selectedFlowIds.value]
  if (!ids.length) return
  const nameSet = new Set<string>()
  for (const f of flows.value) {
    if (ids.includes(f.id) && f.process_name) nameSet.add(f.process_name)
  }
  if (!nameSet.size) {
    ElMessage.warning(t('raw.noProcessInSelection'))
    return
  }
  try {
    const tasks = [...nameSet].map(name =>
      api.ignoreProcess({ pid: null, name })
    )
    await Promise.all(tasks)
    ElMessage.success(t('raw.ignoredProcessCount', { count: nameSet.size }))
    selectedFlowIds.value.clear()
    multiSelectMode.value = false
  } catch (e: any) {
    ElMessage.error(t('raw.ignoreFailed') + (e?.message || e))
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
  { key: 'id', label: 'raw.fieldId', field: 'id' },
  { key: 'protocol', label: 'raw.protocol', field: 'protocol' },
  { key: 'ports', label: 'raw.port', field: 'ports' },
  { key: 'process', label: 'raw.process', field: 'process_name' },
  { key: 'pid', label: 'raw.pid', field: 'pid' },
  { key: 'size', label: 'raw.size', field: 'size' },
  { key: 'time', label: 'raw.time', field: 'timestamp' },
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
  ElMessage.success(t('raw.copiedText', { text: text.length > 40 ? text.slice(0, 40) + '...' : text }))
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
    ElMessage.warning(t('raw.noPidInfo'))
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreProcess({ pid: f.pid, name: f.process_name || `PID ${f.pid}` })
    ElMessage.success(t('raw.ignoredPid', { pid: f.pid }))
  } catch (e: any) {
    ElMessage.error(t('raw.ignoreFailed') + (e?.message || e))
  }
  closeCtxMenu()
}

async function ctxIgnoreProcess() {
  const f = ctxMenu.value.flow
  if (!f || !f.process_name) {
    ElMessage.warning(t('raw.noProcessName'))
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreProcess({ pid: null, name: f.process_name })
    ElMessage.success(t('raw.ignoredProcess', { name: f.process_name }))
  } catch (e: any) {
    ElMessage.error(t('raw.ignoreFailed') + (e?.message || e))
  }
  closeCtxMenu()
}

// 删除流量
async function ctxDelete() {
  const f = ctxMenu.value.flow
  if (!f) return
  try {
    await ElMessageBox.confirm(t('raw.deleteConfirmMsg', { id: f.id }), t('raw.delete'), {
      confirmButtonText: t('raw.delete'), cancelButtonText: t('raw.cancel'), type: 'warning',
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
    ElMessage.success(t('raw.deleted'))
  } catch (e: any) {
    ElMessage.error(t('raw.deleteFailed') + (e?.message || e))
  }
  closeCtxMenu()
}

// 点击空白关闭菜单
function onGlobalClick() {
  if (ctxMenu.value.visible) closeCtxMenu()
}

// 后端流量被清空时（抓包页点清空），本页本地列表/最大 id 也需重置，
// 否则下方增量轮询用旧 maxFlowId 的 since_id 可能把已删旧包重新拉回
function onFlowsCleared() {
  flows.value = []
  maxFlowId.value = 0
}

// UX5 修复：初始加载流量时显示 loading 态，避免空白列表让用户无法区分
// "加载中"与"确实无包"
const loading = ref(false)

async function loadStatus() {
  try {
    rawStatus.value = await api.rawStatus()
  } catch { /* ignore */ }
}

async function loadFlows() {
  loading.value = true
  try {
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
    const res = await api.getFlows(sid, { limit: 200, protocol: 'tcp' })
    const res2 = await api.getFlows(sid, { limit: 200, protocol: 'udp' })
    // 合并 tcp+udp，按 id 倒序（DNS 协议选项已移除，后端解析代码保留）
    const all = [...(res.flows || []), ...(res2.flows || [])]
    all.sort((a, b) => b.id - a.id)
    flows.value = all.slice(0, 200)
    // 列表已降序，第一条就是最大 id
    maxFlowId.value = flows.value[0]?.id || 0
  } catch { /* ignore */ }
  finally {
    loading.value = false
  }
}

// 增量轮询：用 since_id 分别拉取 tcp/udp 新流量，合并去重后插入顶部
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
    const [resTcp, resUdp] = await Promise.all([
      api.getFlows(sid, { since_id: maxFlowId.value, limit: 200, protocol: 'tcp' }),
      api.getFlows(sid, { since_id: maxFlowId.value, limit: 200, protocol: 'udp' }),
    ])
    const list: Flow[] = [...(resTcp.flows || []), ...(resUdp.flows || [])]
    if (!list.length) return
    // 去重：过滤掉列表中已存在的 id
    const existIds = new Set(flows.value.map(f => f.id))
    const deduped = list.filter(f => !existIds.has(f.id))
    if (!deduped.length) {
      const newMax = list.reduce((m, f) => f.id > m ? f.id : m, 0)
      if (newMax > maxFlowId.value) maxFlowId.value = newMax
      return
    }
    // 后端返回按 id DESC，合并后再排一次保证降序
    deduped.sort((a, b) => b.id - a.id)
    flows.value = [...deduped, ...flows.value]
    const newMax = deduped[0].id
    if (newMax > maxFlowId.value) maxFlowId.value = newMax
  } catch { /* ignore */ }
}

async function onToggle() {
  try {
    if (rawStatus.value.running) {
      await api.rawStop()
      ElMessage.success(t('raw.captureStopped'))
    } else {
      if (!rawStatus.value.is_admin) {
        ElMessage.warning(t('raw.needAdminRestart'))
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
      ElMessage.success(t('raw.captureStarted'))
      // 启动后立即拉一次状态（更新 capture.status.session_id）再拉流量
      await capture.fetchStatus()
      await loadFlows()
    }
    await loadStatus()
  } catch (e: any) {
    ElMessage.error(t('raw.operationFailed') + (e?.message || e))
  }
}

// 以管理员身份重启 Telnix
async function restartAsAdmin() {
  restartingAsAdmin.value = true
  try {
    await api.restartAsAdmin()
    ElMessage.success(t('raw.restartingAsAdmin'))
  } catch (e: any) {
    ElMessage.error(t('raw.restartFailed') + (e?.message || e))
    restartingAsAdmin.value = false
  }
}

// 顶部忽略下拉：按 PID / 按进程名（基于当前选中流量）
async function ignorePid() {
  const f = selectedFlow.value
  if (!f || !f.pid) {
    ElMessage.warning(t('raw.noPidInfo'))
    return
  }
  try {
    await api.ignoreProcess({ pid: f.pid, name: f.process_name || `PID ${f.pid}` })
    ElMessage.success(t('raw.ignoredPid', { pid: f.pid }))
  } catch (e: any) {
    ElMessage.error(t('raw.ignoreFailed') + (e?.message || e))
  }
}

async function ignoreProcess() {
  const f = selectedFlow.value
  if (!f || !f.process_name) {
    ElMessage.warning(t('raw.noProcessName'))
    return
  }
  try {
    await api.ignoreProcess({ pid: null, name: f.process_name })
    ElMessage.success(t('raw.ignoredProcess', { name: f.process_name }))
  } catch (e: any) {
    ElMessage.error(t('raw.ignoreFailed') + (e?.message || e))
  }
}

// selectFlow 竞态保护：用户快速点击不同 flow 时，旧请求可能晚于新请求完成，
// 导致 selectedHex 显示旧 flow 的内容但 selectedId 是新 flow（错位）
// 修复审计 7.1：用版本号校验，仅最新请求的结果会被采用
let selectFlowToken = 0
async function selectFlow(f: Flow) {
  const token = ++selectFlowToken
  selectedId.value = f.id
  selectedHex.value = ''
  try {
    const r = await api.getFlowHex(f.id, { field: hexField.value, length: 4096 })
    // 版本校验：若期间用户又点击了其他 flow，丢弃本次结果
    if (token !== selectFlowToken) return
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
  // 增量轮询：用 since_id 只拉新流量，降低流量大时的开销
  pollTimer = window.setInterval(() => {
    loadStatus()
    if (rawStatus.value.running) pollNewFlows()
  }, 2000)
  document.addEventListener('click', onGlobalClick)
  window.addEventListener('telnix:flows-cache-cleared', onFlowsCleared)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  document.removeEventListener('click', onGlobalClick)
  window.removeEventListener('telnix:flows-cache-cleared', onFlowsCleared)
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
        :disabled="!rawStatus.running && !rawStatus.is_admin"
        @click="onToggle"
      >
        <el-icon><component :is="rawStatus.running ? 'VideoPause' : 'VideoPlay'" /></el-icon>
        &nbsp;{{ rawStatus.running ? t('raw.stopCapture') : t('raw.startCapture') }}
      </el-button>
      <div class="raw-status-tags">
        <el-tag size="small" :type="rawStatus.is_admin ? 'success' : 'danger'">
          {{ rawStatus.is_admin ? (rawStatus.backend === 'windivert' ? t('raw.admin') : 'root') : (rawStatus.backend === 'windivert' ? t('raw.nonAdmin') : t('raw.nonRoot')) }}
        </el-tag>
        <el-tag v-if="rawStatus.backend && rawStatus.backend !== 'none'" size="small" type="info">
          {{ backendDisplayName }}
        </el-tag>
        <el-tag v-if="rawStatus.running" size="small" type="success">{{ t('raw.capturing') }}</el-tag>
        <!-- 非管理员/root：提供提权重启按钮 -->
        <el-button
          v-if="!rawStatus.is_admin"
          size="small"
          type="warning"
          :loading="restartingAsAdmin"
          @click="restartAsAdmin"
        >
          <el-icon><Key /></el-icon>&nbsp;{{ rawStatus.backend === 'windivert' ? t('raw.adminRestart') : t('raw.elevateRestart') }}
        </el-button>
      </div>
      <div class="flex-1"></div>
      <span class="text-dim mono" style="font-size: 11px">
        {{ t('raw.networkCaptureHint', { backend: backendDisplayName, perm: rawStatus.backend === 'windivert' ? t('raw.admin') : 'root' }) }}
      </span>
    </div>

    <!-- 非管理员/root 警告条 -->
    <div v-if="!rawStatus.is_admin" class="raw-warn-bar">
      <el-icon><WarningFilled /></el-icon>
      <span>{{ t('raw.warnBarText', {
        perm: rawStatus.backend === 'windivert' ? t('raw.admin') : 'root',
        restart: rawStatus.backend === 'windivert' ? t('raw.adminRestart') : t('raw.elevateRestart')
      }) }}</span>
    </div>

    <!-- 过滤栏（图标按钮风格，参照 FlowList） -->
    <div class="filter-bar">
      <el-button size="small" :type="hasActiveFilters ? 'primary' : 'default'" @click="togglePopup('filter')">
        <el-icon><Filter /></el-icon>&nbsp;{{ t('raw.filter') }}
        <span v-if="hasActiveFilters" class="filter-badge"></span>
      </el-button>
      <el-dropdown size="small" :disabled="!selectedFlow" @command="(c: string) => { c === 'pid' && ignorePid(); c === 'process' && ignoreProcess() }">
        <el-button size="small" :disabled="!selectedFlow">
          <el-icon><RemoveFilled /></el-icon>&nbsp;{{ t('raw.ignore') }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="pid" :disabled="!(selectedFlow?.pid && selectedFlow.pid > 0)">{{ t('raw.ignoreByPid') }}</el-dropdown-item>
            <el-dropdown-item command="process" :disabled="!selectedFlow?.process_name">{{ t('raw.ignoreByProcess') }}</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <!-- 专注模式：点击打开悬浮窗 -->
      <el-tooltip :content="focusEnabled ? t('raw.focusActive') : t('raw.focusMode')" placement="bottom">
        <el-button
          size="small"
          :type="focusEnabled ? 'success' : 'default'"
          @click="togglePopup('focus')"
        >
          <el-icon><Aim /></el-icon>&nbsp;{{ t('raw.focus') }}
          <span v-if="focusEnabled" class="filter-badge"></span>
        </el-button>
      </el-tooltip>
      <el-tooltip :content="flowsStore.autoScroll ? (flowsStore.autoScrollPaused ? t('raw.autoScrollPaused', { delay: flowsStore.autoScrollDelay }) : t('raw.autoScrollOn')) : t('raw.autoScrollOff')" placement="bottom">
        <el-button size="small" :type="flowsStore.autoScroll ? (flowsStore.autoScrollPaused ? 'warning' : 'primary') : 'default'" circle @click="flowsStore.autoScroll = !flowsStore.autoScroll">
          <el-icon><Bottom /></el-icon>
        </el-button>
      </el-tooltip>
      <el-tooltip :content="multiSelectMode ? t('raw.exitMultiSelect') : t('raw.multiSelectMode')" placement="bottom">
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
          <span class="popup-title">{{ activePopup === 'filter' ? t('raw.popupFilter') : t('raw.popupFocus') }}</span>
          <el-icon class="popup-close" @click="closePopup"><Close /></el-icon>
        </div>
        <div class="popup-body">
          <!-- 筛选 -->
          <template v-if="activePopup === 'filter'">
            <div class="fp-row">
              <label class="fp-label">BPF</label>
              <el-input v-model="filterStr" size="small" style="width: 360px" :placeholder="t('raw.bpfPlaceholder')" />
            </div>
            <!-- BPF 预设按钮：一键填入常用过滤表达式 -->
            <div class="fp-row fp-presets">
              <label class="fp-label">{{ t('raw.presets') }}</label>
              <el-button size="small" @click="filterStr = 'tcp or udp'">{{ t('raw.presetAll') }}</el-button>
              <el-button size="small" @click="filterStr = 'tcp'">{{ t('raw.presetTcpOnly') }}</el-button>
              <el-button size="small" @click="filterStr = 'udp'">{{ t('raw.presetUdpOnly') }}</el-button>
              <el-button size="small" @click="filterStr = 'tcp and (tcp.DstPort == 80 or tcp.SrcPort == 80)'">HTTP 80</el-button>
              <el-button size="small" @click="filterStr = 'tcp and (tcp.DstPort == 443 or tcp.SrcPort == 443)'">HTTPS 443</el-button>
              <el-button size="small" @click="filterStr = 'udp and (udp.DstPort == 53 or udp.SrcPort == 53)'">DNS 53</el-button>
              <el-button size="small" @click="filterStr = 'udp and (udp.DstPort == 123 or udp.SrcPort == 123)'">NTP 123</el-button>
              <el-tooltip :content="t('raw.bpfSyntaxHelp')" placement="bottom">
                <el-icon class="bpf-help"><QuestionFilled /></el-icon>
              </el-tooltip>
            </div>
            <div class="fp-row">
              <label class="fp-label">PID</label>
              <el-input v-model="pidFilter" size="small" style="width: 360px" :placeholder="t('raw.pidFilterPlaceholder')" />
            </div>
            <div class="fp-row">
              <label class="fp-label">{{ t('raw.port') }}</label>
              <el-input v-model="portFilter" size="small" style="width: 360px" :placeholder="t('raw.portFilterPlaceholder')" />
            </div>
            <div class="fp-row">
              <label class="fp-label">{{ t('raw.protocol') }}</label>
              <el-select
                v-model="protoFilter"
                multiple
                filterable
                :reserve-keyword="false"
                :placeholder="t('raw.protoFilterPlaceholder')"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option label="TCP" value="tcp" />
                <el-option label="UDP" value="udp" />
              </el-select>
            </div>
            <div class="fp-tip text-dim">{{ t('raw.filterTip') }}</div>
            <div class="fp-actions">
              <el-button size="small" @click="resetFilters">{{ t('raw.reset') }}</el-button>
              <el-button size="small" type="primary" @click="closePopup">{{ t('raw.done') }}</el-button>
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
                :placeholder="t('raw.focusPidPlaceholder')"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onFocusPidsChange"
              >
                <el-option v-for="p in focusPidOptions" :key="p.pid" :label="p.name + ' (' + p.pid + ')'" :value="p.pid" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">{{ t('raw.processName') }}</label>
              <el-input v-model="focusProcessName" :placeholder="t('raw.focusProcessNamePlaceholder')" size="small" clearable style="width: 240px"
                @keyup.enter="onFocusByName" />
              <el-button size="small" @click="onFocusByName">{{ t('raw.apply') }}</el-button>
            </div>
            <div class="fp-row">
              <label class="fp-label">{{ t('raw.port') }}</label>
              <el-select
                v-model="focusPorts"
                multiple
                filterable
                :reserve-keyword="false"
                :placeholder="t('raw.focusPortPlaceholder')"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option v-for="p in focusPortOptions" :key="p" :label="String(p)" :value="p" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">{{ t('raw.protocol') }}</label>
              <el-select
                v-model="focusProtocols"
                multiple
                filterable
                :reserve-keyword="false"
                :placeholder="t('raw.focusProtoPlaceholder')"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option label="TCP" value="tcp" />
                <el-option label="UDP" value="udp" />
              </el-select>
            </div>
            <div class="fp-tip text-dim">{{ t('raw.focusTip') }}</div>
            <div class="fp-actions">
              <el-button size="small" @click="resetFocus">{{ t('raw.reset') }}</el-button>
              <el-button size="small" type="primary" @click="closePopup">{{ t('raw.done') }}</el-button>
            </div>
          </template>
        </div>
      </div>
    </transition>

    <!-- 主体：左列表 + 右详情 -->
    <div class="raw-body flex-1 flex overflow-hidden">
      <div class="raw-list-pane" v-loading="loading">
        <div class="rl-head mono" :style="{ gridTemplateColumns: gridCols }">
          <div v-if="multiSelectMode" class="rl-check"></div>
          <div class="rl-id">#</div>
          <div class="rl-proto">{{ t('raw.protocol') }}</div>
          <div class="rl-ports">{{ t('raw.port') }}</div>
          <div class="rl-pid">{{ t('raw.process') }}</div>
          <div class="rl-size">{{ t('raw.size') }}</div>
          <div class="rl-time">{{ t('raw.time') }}</div>
        </div>
        <div ref="bodyRef" class="rl-body flex-1 overflow-auto" @scroll="onBodyScroll">
          <div :style="{ height: topPad + 'px' }"></div>
          <div
            v-for="f in visibleItems"
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
          <div :style="{ height: bottomPad + 'px' }"></div>
          <div v-if="!displayFlows.length" class="empty-text text-dim">{{ t('raw.noPackets') }}</div>
        </div>

        <!-- 多选模式悬浮工具栏 -->
        <transition name="float-bar">
          <div
            v-if="multiSelectMode && floatBarVisible"
            class="multi-float-bar"
            @mouseenter="showFloatBarNow"
          >
            <span class="mfb-count">{{ t('raw.selectedCount', { count: selectedCount }) }}</span>
            <el-button size="small" @click="selectAllFlows">{{ t('raw.selectAll') }}</el-button>
            <el-button size="small" @click="clearSelection" :disabled="!selectedCount">{{ t('raw.clearSelection') }}</el-button>
            <el-button
              size="small"
              type="warning"
              :disabled="!selectedCount"
              @click="batchIgnorePid"
            >
              <el-icon><RemoveFilled /></el-icon>&nbsp;{{ t('raw.ignorePid') }}
            </el-button>
            <el-button
              size="small"
              type="warning"
              :disabled="!selectedCount"
              @click="batchIgnoreProcess"
            >
              <el-icon><RemoveFilled /></el-icon>&nbsp;{{ t('raw.ignoreProcess') }}
            </el-button>
            <el-button
              size="small"
              type="danger"
              :disabled="!selectedCount"
              @click="batchDeleteFlows"
            >
              <el-icon><Delete /></el-icon>&nbsp;{{ t('raw.delete') }}
            </el-button>
            <!-- 子悬浮窗：删除确认 -->
            <transition name="popup-fade">
              <div
                v-if="showDeleteConfirm"
                class="delete-confirm-pop"
                @click.stop
                @mouseenter="showFloatBarNow"
              >
                <div class="dc-text">{{ t('raw.confirmDeleteSelected', { count: selectedCount }) }}</div>
                <div class="dc-actions">
                  <el-button size="small" @click="cancelDeleteFlows">{{ t('raw.cancel') }}</el-button>
                  <el-button size="small" type="danger" @click="confirmDeleteFlows">{{ t('raw.delete') }}</el-button>
                </div>
              </div>
            </transition>
          </div>
        </transition>
      </div>
      <div class="raw-detail-pane">
        <div v-if="!selectedFlow" class="empty-detail text-dim">
          <el-icon :size="36"><Document /></el-icon>
          <div style="margin-top: 10px">{{ t('raw.selectToViewHex') }}</div>
        </div>
        <template v-else>
          <div class="detail-head">
            <span class="mono">#{{ selectedFlow.id }} {{ selectedFlow.protocol?.toUpperCase() }}</span>
            <span class="text-muted">{{ selectedFlow.src_port }} → {{ selectedFlow.dst_port }}</span>
            <span class="text-muted">{{ selectedFlow.process_name || 'pid:' + selectedFlow.pid }}</span>
            <div class="flex-1"></div>
            <el-radio-group v-model="detailTab" size="small" class="detail-tab">
              <el-radio-button label="hex">Hex</el-radio-button>
              <el-radio-button label="protocol">{{ t('raw.protocolAnalysis') }}</el-radio-button>
            </el-radio-group>
            <el-select v-model="hexField" size="small" style="width: 140px" @change="refreshHex">
              <el-option :label="t('raw.rawData')" value="raw_data" />
              <el-option :label="t('raw.requestBody')" value="request_body" />
              <el-option :label="t('raw.responseBody')" value="response_body" />
            </el-select>
          </div>
          <div class="hex-container">
            <HexView v-if="detailTab === 'hex'" :data="selectedHex" />
            <ProtocolView v-else :flow-id="selectedFlow.id" :field="hexField" />
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
        <div class="ctx-item" @click="onFlowDblClick(ctxMenu.flow!)"><el-icon><Aim /></el-icon>&nbsp;{{ t('raw.viewInCapturePage') }}</div>
        <!-- 忽略 -->
        <div class="ctx-item" @click="ctxIgnorePid"><el-icon><RemoveFilled /></el-icon>&nbsp;{{ t('raw.ctxIgnoreByPid') }}</div>
        <div class="ctx-item" @click="ctxIgnoreProcess"><el-icon><RemoveFilled /></el-icon>&nbsp;{{ t('raw.ctxIgnoreByProcess') }}</div>
        <div class="ctx-sep"></div>
        <!-- 复制 -->
        <div class="ctx-item ctx-submenu">
          <el-icon><CopyDocument /></el-icon>&nbsp;{{ t('raw.copy') }}
          <el-icon class="ctx-arrow"><ArrowRight /></el-icon>
        </div>
        <div class="ctx-submenu-panel">
          <div v-for="item in COPY_FIELDS" :key="item.key" class="ctx-item" @click="copyField(item.field)">{{ t(item.label) }}</div>
        </div>
        <div class="ctx-sep"></div>
        <div class="ctx-item ctx-danger" @click="ctxDelete"><el-icon><Delete /></el-icon>&nbsp;{{ t('raw.deleteFlow') }}</div>
      </div>
    </teleport>
  </div>
</template>

<style scoped>
.raw-view-page { background: var(--on-bg); position: relative; }
.raw-toolbar {
  display: flex; align-items: center; gap: 8px;
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
.empty-text { text-align: center; padding: 30px; color: var(--on-text-dim); }
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
.fp-row {
  display: flex; align-items: flex-start; gap: 10px;
}
/* BPF 预设按钮行：允许换行 */
.fp-row.fp-presets {
  flex-wrap: wrap;
  gap: 6px;
}
.fp-presets .bpf-help {
  margin-left: 4px;
  color: var(--on-text-dim);
  cursor: help;
  align-self: center;
  font-size: 16px;
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

/* 右键菜单样式 */
.ctx-menu {
  position: fixed; z-index: 9999;
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-border, #333344);
  border-radius: var(--on-radius-md);
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
  border-radius: var(--on-radius-md);
  padding: 4px 0;
  min-width: 140px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
}
.ctx-submenu:hover .ctx-submenu-panel { display: block; }
</style>
