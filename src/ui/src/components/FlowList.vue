<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useFlowsStore } from '../stores/flows'
import { useCaptureStore } from '../stores/capture'
import { api, type ProcessInfo } from '../api/client'
import { syncPrefs } from '../stores/prefs'
import { parseFlowFilter, matchFlow, type ParsedFilter } from '../utils/flowfilter'

// 会话列表：表格 + 统一悬浮窗（筛选/专注） + 一键忽略 + 右键菜单
const props = defineProps<{
  processes: ProcessInfo[]
  // 可显示的额外列（status/method/protocol/content_type/size/duration），默认全不显示
  flowColumns?: string[]
  // 多选工具栏悬浮显示延时（秒），用户滚动列表后隐藏，N 秒不操作再显示。默认 1 秒。
  multiSelectBarDelay?: number
  // 自动滚动+多选按钮靠右（仅抓包页启用，其他页保持默认左对齐）
  actionsRight?: boolean
}>()
const emit = defineEmits<{
  'ignore-process': [proc: ProcessInfo]
  'ignore-host': [host: string]
  'replay': [flowId: number]
  'replay-batch': [flowIds: number[]]
  'ai-analyze': [flowId: number]
  'ai-analyze-batch': [flowIds: number[]]
  'view-in-analyze': [flowId: number]
  'flows-changed': []
  // 右键「自动修改」子菜单：mode='request' 基于请求预填，mode='response' 基于响应预填
  'auto-modify': [flow: any, mode: 'request' | 'response']
}>()

const store = useFlowsStore()
const capture = useCaptureStore()

const bodyRef = ref<HTMLElement | null>(null)

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

function batchAIAnalyze() {
  const ids = [...selectedFlowIds.value]
  if (!ids.length) return
  emit('ai-analyze-batch', ids)
  multiSelectMode.value = false
  selectedFlowIds.value.clear()
}

// 批量重放：交给父组件处理（含 QPS 限制和进度提示）
function batchReplay() {
  const ids = [...selectedFlowIds.value]
  if (!ids.length) return
  emit('replay-batch', ids)
}

// 批量删除：显示子悬浮窗确认（不弹页面中间框）
const showDeleteConfirm = ref(false)
function batchDeleteFlows() {
  if (!selectedFlowIds.value.size) return
  showDeleteConfirm.value = true
  // 子悬浮窗显示时阻止工具栏自动隐藏
  showFloatBarNow()
}
async function confirmDeleteFlows() {
  const ids = [...selectedFlowIds.value]
  if (!ids.length) { showDeleteConfirm.value = false; return }
  try {
    await api.batchDeleteFlows(ids)
    ElMessage.success(`已删除 ${ids.length} 条`)
    // 前端直接移除，避免全量重载导致卡顿
    store.removeFlows(ids)
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

async function batchReleaseFlows(action: 'release' | 'drop' = 'release') {
  const ids = [...selectedFlowIds.value]
  if (!ids.length) return
  try {
    const r: any = await api.batchReleaseFlows(ids, action)
    const label = action === 'drop' ? '丢弃' : '放行'
    ElMessage.success(`已${label} ${r.released}/${r.total} 条`)
    // 放行/丢弃后前端移除断点标记（不从列表删除，仅清除断点状态）
    // shallowRef 模式下用 store.patchFlows 原地 mutate + triggerRef
    store.patchFlows(ids, (f) => { f.breakpoint_status = null })
    selectedFlowIds.value.clear()
    multiSelectMode.value = false
  } catch (e: any) {
    const label = action === 'drop' ? '丢弃' : '放行'
    ElMessage.error(`${label}失败：` + (e?.message || e))
  }
}

const hasPendingBreakpoint = computed(() => {
  const ids = selectedFlowIds.value
  return store.flows.some(f => ids.has(f.id) && f.breakpoint_status)
})

// ---------- 多选悬浮工具栏 ----------
// 滚动时隐藏，N 秒不操作再显示。N 由 props.multiSelectBarDelay 控制，默认 1 秒。
const floatBarVisible = ref(true)
let floatBarTimer: number | null = null
const floatBarDelaySec = computed(() => {
  const v = Number(props.multiSelectBarDelay)
  return isFinite(v) && v >= 0 ? v : 1
})

function hideFloatBar() {
  if (!multiSelectMode.value) return
  // 子悬浮窗（删除确认）显示时不隐藏工具栏
  if (showDeleteConfirm.value) return
  floatBarVisible.value = false
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  floatBarTimer = window.setTimeout(() => {
    if (multiSelectMode.value) floatBarVisible.value = true
  }, floatBarDelaySec.value * 1000)
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

// ---------- 统一悬浮窗管理 ----------
// 一次只能开一个悬浮窗（筛选 / 专注 互斥）
const activePopup = ref<'filter' | 'focus' | null>(null)

// ===== Flowfilter DSL =====
// DSL 输入框文本（双向绑定）
const dslQuery = ref('')
// 当前已应用的解析后过滤器
const dslFilter = ref<ParsedFilter>({ raw: '', conditions: [] })
// 解析错误提示
const dslError = ref('')
// 显示语法帮助浮层
const showDslHelp = ref(false)
// 应用 DSL：解析后存到 dslFilter，displayFlows 会重新计算
function applyDsl() {
  const q = dslQuery.value.trim()
  if (!q) {
    dslFilter.value = { raw: '', conditions: [] }
    dslError.value = ''
    return
  }
  const parsed = parseFlowFilter(q)
  if (parsed.error) {
    dslError.value = parsed.error
    ElMessage.warning('DSL 语法错误：' + parsed.error)
    return
  }
  dslError.value = ''
  dslFilter.value = parsed
}
// 清空 DSL（清除按钮触发）
watch(dslQuery, (v) => {
  if (!v) {
    dslFilter.value = { raw: '', conditions: [] }
    dslError.value = ''
  }
})
// 悬浮窗位置（绝对定位，可拖动）
const popupPos = ref({ left: 10, top: 46 })

function togglePopup(name: 'filter' | 'focus') {
  if (activePopup.value === name) {
    closePopup()
  } else {
    // 打开新的，直接关闭旧的
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
onUnmounted(() => {
  window.removeEventListener('mousemove', onPopupDragMove)
  window.removeEventListener('mouseup', onPopupDragUp)
  // 清理自动滚动暂停定时器，避免组件卸载后回调仍触发访问已卸载的 store/refs
  if (scrollPauseTimer !== null) clearTimeout(scrollPauseTimer)
  // 清理多选工具栏显隐定时器
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  // 清理 knownHosts/Methods/StatusCodes debounce 定时器
  if (knownDebounceTimer !== null) clearTimeout(knownDebounceTimer)
})

// ---------- 专注模式 ----------
// 专注自动启用：有任一条件（pids/hosts/methods/status_codes/content_types）非空就开，全空就关
const focusPids = ref<number[]>([])
// 专注 host（通配符列表，前端显示过滤）
const focusHosts = ref<string[]>([])
// 专注进程多选
const focusProcessSelect = ref<number[]>([])
const focusProcessName = ref('')
const focusIncludeChildren = ref(true)
// 专注方法/状态码/Content-Type（多选，跨类 OR 匹配）
const focusMethods = ref<string[]>([])
const focusStatusCodes = ref<string[]>([])
const focusContentTypes = ref<string[]>([])
// 专注协议（http/tcp/udp，OR 匹配；http 同时匹配 http/https）
const focusProtocols = ref<string[]>([])
// focusEnabled 自动计算：有任一专注条件即开
const focusEnabled = computed(() =>
  focusPids.value.length > 0 || focusHosts.value.length > 0 ||
  focusMethods.value.length > 0 || focusStatusCodes.value.length > 0 ||
  focusContentTypes.value.length > 0 || focusProtocols.value.length > 0
)

async function loadFocus() {
  try {
    const r = await api.getFocus()
    focusPids.value = r.pids || []
    focusProcessSelect.value = r.pids || []
    focusHosts.value = r.hosts || []
    focusMethods.value = r.methods || []
    focusStatusCodes.value = (r.status_codes || []).map((x: any) => String(x))
    focusContentTypes.value = r.content_types || []
    focusProtocols.value = r.protocols || (r as any).protocol || []
  } catch { /* ignore */ }
}

// 同步专注状态到后端（enabled 由后端自动判断，这里传 false 占位）
async function syncFocusToBackend() {
  try {
    await api.setFocus({
      enabled: false,
      pids: focusPids.value,
      hosts: focusHosts.value,
      methods: focusMethods.value,
      status_codes: focusStatusCodes.value.map((x: string) => Number(x)).filter((n: number) => !isNaN(n)),
      content_types: focusContentTypes.value,
      protocols: focusProtocols.value,
    } as any)
  } catch { /* ignore */ }
}

async function onFocusPidsChange(vals: number[]) {
  focusProcessSelect.value = vals
  focusPids.value = vals
  await syncFocusToBackend()
}

// host 专注变化时同步后端
async function onFocusHostsChange() {
  onHostSelectChange()
  await syncFocusToBackend()
}

// 方法/状态码/Content-Type 专注变化时同步后端
async function onFocusExtraChange() {
  await syncFocusToBackend()
}

async function onFocusByName() {
  const name = focusProcessName.value.trim()
  if (!name) return
  try {
    const r: any = await api.setFocus({
      enabled: false,
      process_names: [name],
      include_children: focusIncludeChildren.value,
      hosts: focusHosts.value,
      methods: focusMethods.value,
      status_codes: focusStatusCodes.value.map((x: string) => Number(x)).filter((n: number) => !isNaN(n)),
      content_types: focusContentTypes.value,
      protocols: focusProtocols.value,
    } as any)
    if (r?.pids) {
      focusPids.value = r.pids
      focusProcessSelect.value = r.pids
    }
    ElMessage.success(`专注进程 ${name}：匹配 ${r?.pids?.length || 0} 个 PID`)
  } catch (e: any) {
    ElMessage.error('专注失败：' + (e?.message || e))
  }
}

// 重置专注：清空所有条件（后端自动关闭）
async function resetFocus() {
  focusHosts.value = []
  focusProcessSelect.value = []
  focusPids.value = []
  focusProcessName.value = ''
  focusIncludeChildren.value = true
  focusMethods.value = []
  focusStatusCodes.value = []
  focusContentTypes.value = []
  focusProtocols.value = []
  try {
    await api.setFocus({ enabled: false, pids: [], hosts: [], methods: [], status_codes: [], content_types: [], protocols: [] } as any)
  } catch { /* ignore */ }
}

// host 通配符匹配（* → .*, ? → .）
// 性能优化：正则编译缓存，pattern 不变时复用，避免每次 new RegExp
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

// ---------- 列显示控制 + 拖拽排序 ----------
const flowColumns = computed(() => props.flowColumns || [])
const colVisible = (key: string) => flowColumns.value.includes(key)

// 列定义：数据驱动渲染，支持拖拽排序
type ColDef = {
  key: string
  label: string
  width: string
  always?: boolean         // 必显列（不可隐藏）
  cellClass: string        // 单元格 class
  text: (f: any) => string
  // 可选：对单元格文本加状态色 class（如 status-2xx）
  statusColor?: (f: any) => string
}

const COL_DEFS: ColDef[] = [
  { key: 'id', label: '#', width: '60px', always: true, cellClass: 'col-id text-dim', text: (f) => idWithScheme(f) },
  { key: 'status', label: '结果', width: '56px', cellClass: 'col-code', text: (f) => String(f.status_code ?? '...'), statusColor: (f) => statusClass(f.status_code) },
  { key: 'method', label: '方法', width: '72px', cellClass: 'col-method text-muted', text: (f) => f.method || '' },
  { key: 'protocol', label: '协议', width: '54px', cellClass: 'col-proto text-muted', text: (f) => protocolLabel(f) },
  { key: 'host', label: 'Host', width: '1.4fr', always: true, cellClass: 'col-host', text: (f) => f.host || '' },
  { key: 'url', label: 'URL', width: '2.2fr', always: true, cellClass: 'col-url text-muted', text: (f) => f.path || '' },
  { key: 'content_type', label: 'Content-Type', width: '1.2fr', cellClass: 'col-ctype text-muted', text: (f) => extractContentType(f) },
  { key: 'pid', label: 'PID', width: '64px', cellClass: 'col-pid text-muted', text: (f) => String(f.pid ?? '-') },
  { key: 'proc', label: '进程', width: '1fr', always: true, cellClass: 'col-proc text-muted', text: (f) => f.process_name || '-' },
  { key: 'size', label: '大小', width: '70px', cellClass: 'col-size text-muted', text: (f) => formatSize(f.size) },
  { key: 'duration', label: '耗时', width: '64px', cellClass: 'col-time text-muted', text: (f) => f.duration_ms !== null ? f.duration_ms + 'ms' : '' },
  { key: 'ip_region', label: 'IP 属地', width: '90px', cellClass: 'col-ip-region text-muted', text: (f) => (f.ip_region && !f.ip_region.startsWith('base64:')) ? f.ip_region : '-' },
]
const COL_ORDER_KEY = 'telnix_col_order'

function loadColOrder(): string[] {
  let saved: string[] = []
  try {
    saved = JSON.parse(localStorage.getItem(COL_ORDER_KEY) || '[]')
  } catch {
    // 缓存损坏时回退默认列顺序，避免组件初始化崩溃导致整页白屏
    return COL_DEFS.map(c => c.key)
  }
  if (!Array.isArray(saved) || !saved.length) return COL_DEFS.map(c => c.key)
  // 按保存顺序排列，新列追加末尾
  const allKeys = new Set(COL_DEFS.map(c => c.key))
  const result: string[] = []
  for (const k of saved) {
    if (allKeys.has(k)) {
      result.push(k)
      allKeys.delete(k)
    }
  }
  for (const k of allKeys) result.push(k)
  return result
}

const colOrder = ref<string[]>(loadColOrder())

function persistColOrder() {
  localStorage.setItem(COL_ORDER_KEY, JSON.stringify(colOrder.value))
  syncPrefs()
}

// 按顺序排列的列定义（仅可见列）
const visibleCols = computed<ColDef[]>(() => {
  const byKey: Record<string, ColDef> = {}
  for (const c of COL_DEFS) byKey[c.key] = c
  return colOrder.value
    .map(k => byKey[k])
    .filter(c => c && (c.always || colVisible(c.key)))
})

const gridCols = computed(() => {
  const parts: string[] = []
  if (multiSelectMode.value) parts.push('32px')
  for (const c of visibleCols.value) parts.push(c.width)
  return parts.join(' ')
})

// 列拖拽排序
const dragColKey = ref<string | null>(null)

function onColDragStart(key: string) {
  dragColKey.value = key
}

function onColDragOver(e: DragEvent, key: string) {
  e.preventDefault()
  if (!dragColKey.value || dragColKey.value === key) return
  const fromIdx = colOrder.value.indexOf(dragColKey.value)
  const toIdx = colOrder.value.indexOf(key)
  if (fromIdx < 0 || toIdx < 0) return
  const items = [...colOrder.value]
  const [moved] = items.splice(fromIdx, 1)
  items.splice(toIdx, 0, moved)
  colOrder.value = items
  persistColOrder()
}

function onColDragEnd() {
  dragColKey.value = null
}

function cellFullClass(c: ColDef, f: any): string {
  let cls = c.cellClass
  if (c.statusColor) cls += ' ' + (c.statusColor(f) || '')
  return cls
}

// 性能优化：extractContentType 结果缓存（按 flow.id），避免渲染期反复 JSON.parse
// LRU 上限 5000，防止长期运行内存泄漏
// 注意：lite flow（SSE 推送，缺少 response_headers）不缓存，
// 否则 select() 拉取完整数据后 response_headers 变更，缓存仍是旧空值不会失效。
const _ctCache = new Map<number, string>()
function extractContentType(flow: any): string {
  if (!flow) return ''
  const fid = flow.id
  let cached = _ctCache.get(fid)
  if (cached !== undefined) return cached
  const raw = flow.response_headers
  let result = ''
  if (raw) {
    try {
      const obj = typeof raw === 'string' ? JSON.parse(raw) : raw
      for (const k of Object.keys(obj)) {
        if (k.toLowerCase() === 'content-type') {
          result = String(obj[k]).split(';')[0].trim()
          break
        }
      }
    } catch { /* ignore */ }
  }
  // 仅当 response_headers 存在时缓存（lite flow 不缓存，等完整数据到达后再缓存）
  if (raw !== undefined) {
    if (_ctCache.size >= 5000) {
      const firstKey = _ctCache.keys().next().value
      if (firstKey !== undefined) _ctCache.delete(firstKey)
    }
    _ctCache.set(fid, result)
  }
  return result
}

function protocolLabel(flow: any): string {
  const proto = flow.protocol || flow.scheme || 'http'
  if (proto === 'tcp') return 'TCP'
  if (proto === 'udp') return 'UDP'
  if (proto === 'ws') return 'WS'
  if (proto === 'dns') return 'DNS'
  // HTTP/HTTPS：附带显示 HTTP 版本（HTTP/2 → HTTPS/2）
  const ver = flow.http_version || ''
  const isH2 = ver === 'HTTP/2'
  if (proto === 'https') return isH2 ? 'HTTPS/2' : 'HTTPS'
  return isH2 ? 'HTTP/2' : 'HTTP'
}

function statusClass(code: number | null): string {
  if (code === null) return 'status-null'
  if (code < 300) return 'status-2xx'
  if (code < 400) return 'status-3xx'
  if (code < 500) return 'status-4xx'
  return 'status-5xx'
}

function rowClass(flow: any): string {
  // 断点行最高优先级（已有红/紫底色）
  if (flow.breakpoint_status === 'pending_request') return 'bp-request-row'
  if (flow.breakpoint_status === 'pending_response') return 'bp-response-row'
  return ''
}

function formatSize(n: number | null): string {
  if (n === null || n === undefined) return ''
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(2) + ' MB'
}

function formatTime(ts: string): string {
  if (!ts) return ''
  const d = new Date(ts)
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

// #序号（纯数字）
function idWithScheme(flow: any): string {
  return String(flow.id)
}

function select(flow: any) {
  store.select(flow.id)
}

// ---------- 流量显示（专注 + 筛选 全前端过滤）----------
// 性能优化：
// 1. 去掉 [...store.flows].sort()（store 已保证降序，重复排序 O(n log n) 浪费）
// 2. 合并 6 次链式 filter 为单次遍历，减少中间数组分配
const displayFlows = computed(() => {
  const src = store.flows
  const focusOn = focusEnabled.value
  const fPids = focusPids.value
  const fHosts = focusHosts.value
  const fMethods = focusMethods.value
  const fStatusCodes = focusStatusCodes.value
  const fContentTypes = focusContentTypes.value
  const fProtocols = focusProtocols.value
  const hasFocusAny = fPids.length > 0 || fHosts.length > 0 || fMethods.length > 0 ||
                      fStatusCodes.length > 0 || fContentTypes.length > 0 || fProtocols.length > 0
  const filterPids = store.filterProcessPids
  const filterHostsLocal = filterHosts.value
  const filterMethodsLocal = store.filterMethods
  const filterStatusCodesLocal = store.filterStatusCodes
  const filterContentTypesLocal = store.filterContentTypes
  const filterProtocolsLocal = store.filterProtocols
  const hasFilterAny = filterPids.length > 0 || filterHostsLocal.length > 0 ||
                       filterMethodsLocal.length > 0 || filterStatusCodesLocal.length > 0 ||
                       filterContentTypesLocal.length > 0 || filterProtocolsLocal.length > 0
  // Flowfilter DSL：与现有筛选协同（AND 关系）
  const dslConditions = dslFilter.value.conditions
  const hasDsl = dslConditions.length > 0

  // 无任何过滤条件：直接返回源数组（零开销）
  if (!focusOn && !hasFilterAny && !hasDsl) return src

  const result: any[] = []
  for (const f of src) {
    // 专注过滤（OR 语义）
    if (focusOn) {
      let focusMatch = false
      if (fPids.length > 0 && f.pid !== null && fPids.includes(f.pid)) focusMatch = true
      else if (fHosts.length > 0 && fHosts.some(p => matchHostWildcard(f.host || '', p))) focusMatch = true
      else if (fMethods.length > 0 && fMethods.includes((f.method || '').toUpperCase())) focusMatch = true
      else if (fStatusCodes.length > 0) {
        const code = f.status_code != null ? String(f.status_code) : ''
        if (code && fStatusCodes.some(p => matchStatusCode(code, p))) focusMatch = true
      }
      else if (fContentTypes.length > 0) {
        const mainType = extractContentTypeMain(f)
        if (fContentTypes.includes(mainType)) focusMatch = true
      }
      else if (fProtocols.length > 0) {
        const proto = (f.protocol || f.scheme || 'http').toLowerCase()
        if (fProtocols.some(p => {
          const lp = p.toLowerCase()
          if (lp === 'http') return proto === 'http' || proto === 'https'
          return proto === lp
        })) focusMatch = true
      }
      if (!focusMatch && hasFocusAny) continue
    }
    // 筛选过滤（AND 语义：所有启用的筛选条件都必须满足）
    if (filterPids.length > 0 && !(f.pid !== null && filterPids.includes(f.pid))) continue
    if (filterHostsLocal.length > 0 && !filterHostsLocal.some(p => matchHostWildcard(f.host || '', p))) continue
    if (filterMethodsLocal.length > 0 && !filterMethodsLocal.includes((f.method || '').toUpperCase())) continue
    if (filterStatusCodesLocal.length > 0) {
      const code = f.status_code != null ? String(f.status_code) : ''
      if (!code || !filterStatusCodesLocal.some(p => matchStatusCode(code, p))) continue
    }
    if (filterContentTypesLocal.length > 0) {
      const mainType = extractContentTypeMain(f)
      if (!filterContentTypesLocal.includes(mainType)) continue
    }
    if (filterProtocolsLocal.length > 0) {
      const proto = (f.protocol || f.scheme || 'http').toLowerCase()
      const matched = filterProtocolsLocal.some(p => {
        const lp = p.toLowerCase()
        if (lp === 'http') return proto === 'http' || proto === 'https'
        return proto === lp
      })
      if (!matched) continue
    }
    // Flowfilter DSL 过滤（AND 关系：所有 DSL 条件必须满足）
    if (hasDsl && !matchFlow(f, dslFilter.value)) continue
    result.push(f)
  }
  return result
})

// 提取 Content-Type 主类型（如 image/png → image，application/json → application）
function extractContentTypeMain(f: any): string {
  const ct = extractContentType(f) || ''
  return ct.split('/')[0].toLowerCase() || 'unknown'
}

// 状态码匹配：支持精确（200）和通配（2xx、3xx、4xx）
function matchStatusCode(code: string, pattern: string): boolean {
  const p = pattern.trim().toLowerCase()
  if (!p) return false
  if (/^\d+$/.test(p)) return code === p
  // 2xx/3xx/4xx/5xx
  const m = p.match(/^(\d)xx$/)
  if (m) {
    return code.length === 3 && code[0] === m[1]
  }
  // 通配符
  return matchHostWildcard(code, p)
}

// ---------- 筛选（全前端过滤，不持久化，刷新即消失）----------
// 筛选 host 列表（前端过滤，支持通配符，按 enter 分割）
const filterHosts = ref<string[]>([])
// 已出现的 host / method / statusCode 列表：从 computed 改为 ref + debounce，
// 避免每次 flows 变化（如 SSE 批量推送）都立即 O(n) 遍历全部流量计算，
// 这些值只在打开 filter popup 时才需要显示，300ms 延迟不影响交互。
const knownHosts = ref<string[]>([])
const knownMethods = ref<string[]>([])
const knownStatusCodes = ref<string[]>([])

let knownDebounceTimer: number | null = null
function scheduleUpdateKnowns() {
  if (knownDebounceTimer !== null) return
  knownDebounceTimer = window.setTimeout(() => {
    knownDebounceTimer = null
    const hostSet = new Set<string>()
    const methodSet = new Set<string>()
    const codeSet = new Set<string>()
    for (const f of store.flows as any[]) {
      if (f.host) hostSet.add(f.host)
      if (f.method) methodSet.add((f.method as string).toUpperCase())
      if (f.status_code !== null && f.status_code !== undefined) codeSet.add(String(f.status_code))
    }
    knownHosts.value = [...hostSet].sort()
    knownMethods.value = [...methodSet].sort()
    knownStatusCodes.value = [...codeSet].sort()
  }, 300)
}

watch(() => store.flows, () => {
  scheduleUpdateKnowns()
}, { deep: false })

function resetFilters() {
  store.filterProcessPids = []
  store.filterProcessName = ''
  store.filterHost = ''
  store.filterMethods = []
  store.filterStatusCodes = []
  store.filterContentTypes = []
  store.filterProtocols = []
  filterHosts.value = []
}
const hasActiveFilters = computed(() =>
  store.filterProcessPids.length > 0 || filterHosts.value.length > 0 ||
  store.filterMethods.length > 0 || store.filterStatusCodes.length > 0 ||
  store.filterContentTypes.length > 0 || store.filterProtocols.length > 0
)

// el-select 选中 option 后清空输入框（修复 Element Plus 多选残留 bug）
function onHostSelectChange() {
  // 多选 el-select 的 change 后清空输入
  nextTick(() => {
    // 通过 ref 获取
    if (filterHostSelectRef.value) {
      const sel: any = filterHostSelectRef.value
      if (sel.states) sel.states.inputValue = ''
    }
    if (focusHostSelectRef.value) {
      const sel: any = focusHostSelectRef.value
      if (sel.states) sel.states.inputValue = ''
    }
    if (focusProcessSelectRef.value) {
      const sel: any = focusProcessSelectRef.value
      if (sel.states) sel.states.inputValue = ''
    }
    if (filterProcessSelectRef.value) {
      const sel: any = filterProcessSelectRef.value
      if (sel.states) sel.states.inputValue = ''
    }
    if (filterMethodSelectRef.value) {
      const sel: any = filterMethodSelectRef.value
      if (sel.states) sel.states.inputValue = ''
    }
    if (filterStatusCodeSelectRef.value) {
      const sel: any = filterStatusCodeSelectRef.value
      if (sel.states) sel.states.inputValue = ''
    }
    if (filterProtocolSelectRef.value) {
      const sel: any = filterProtocolSelectRef.value
      if (sel.states) sel.states.inputValue = ''
    }
  })
}

// el-select 引用
const filterProcessSelectRef = ref<any>(null)
const filterHostSelectRef = ref<any>(null)
const filterMethodSelectRef = ref<any>(null)
const filterStatusCodeSelectRef = ref<any>(null)
const filterProtocolSelectRef = ref<any>(null)
const focusProcessSelectRef = ref<any>(null)
const focusHostSelectRef = ref<any>(null)

// ---------- 自动滚动 ----------
let scrollPauseTimer: number | null = null
let programmaticScroll = false

function onBodyScroll() {
  if (programmaticScroll) {
    programmaticScroll = false
    return
  }
  if (multiSelectMode.value) hideFloatBar()
  if (!store.autoScroll) return
  store.autoScrollPaused = true
  if (scrollPauseTimer !== null) clearTimeout(scrollPauseTimer)
  scrollPauseTimer = window.setTimeout(() => {
    // 恢复前再次检查开关状态（用户可能在期间关闭了自动滚动）
    if (!store.autoScroll) {
      store.autoScrollPaused = false
      return
    }
    store.autoScrollPaused = false
    const el = bodyRef.value
    if (el) {
      programmaticScroll = true
      el.scrollTo({ top: 0, behavior: 'smooth' })
    }
  }, store.autoScrollDelay * 1000)
}

watch(
  () => store.flows.length,
  async () => {
    // 跨页跳转注入 flow 时跳过自动滚动（避免把页面滚回顶部，干扰选中行定位）
    if (store.skipNextAutoScroll) {
      store.skipNextAutoScroll = false
      return
    }
    if (!store.autoScroll || store.autoScrollPaused) return
    await nextTick()
    const el = bodyRef.value
    if (el) {
      programmaticScroll = true
      el.scrollTo({ top: 0, behavior: 'smooth' })
    }
  }
)

// flows 列表内容变化后（如 loadAllFlows 完成、跨页跳转注入 flow），
// 检查 selectedId 是否在视口内，不在则滚动到该行。
// 解决：跨页跳转 → selectFlow 注入 flow → router.push → CaptureView onMounted
// 调 loadAllFlows 覆盖列表 → selectedId 仍在但行未渲染 → 需要在 flows 变化后重新定位
watch(
  () => store.flows,
  async () => {
    if (store.selectedId == null) return
    await nextTick()
    const el = bodyRef.value
    if (!el) return
    const row = el.querySelector(`[data-flow-id="${store.selectedId}"]`) as HTMLElement | null
    if (row) {
      // 只在行不在视口内时滚动，避免每次 flows 变化都跳动
      const cRect = el.getBoundingClientRect()
      const rRect = row.getBoundingClientRect()
      const rowTop = rRect.top - cRect.top + el.scrollTop
      const rowBottom = rowTop + rRect.height
      const viewTop = el.scrollTop
      const viewBottom = viewTop + el.clientHeight
      if (rowTop < viewTop || rowBottom > viewBottom) {
        programmaticScroll = true
        scrollRowIntoView(row)
      }
    }
  },
  { deep: false }
)

// 手动滚动行到容器可视区（不使用 scrollIntoView，避免冒泡滚动 body 导致整页上移）
function scrollRowIntoView(row: HTMLElement) {
  const el = bodyRef.value
  if (!el || !row) return
  const cRect = el.getBoundingClientRect()
  const rRect = row.getBoundingClientRect()
  const rowTop = rRect.top - cRect.top + el.scrollTop
  const rowBottom = rowTop + rRect.height
  const viewTop = el.scrollTop
  const viewBottom = viewTop + el.clientHeight
  if (rowTop < viewTop) {
    el.scrollTop = rowTop
  } else if (rowBottom > viewBottom) {
    el.scrollTop = rowBottom - el.clientHeight
  }
}

// selectedId 变化时滚动到选中行（跨页跳转后定位）
watch(
  () => store.selectedId,
  async (id) => {
    if (id == null) return
    await nextTick()
    const el = bodyRef.value
    if (!el) return
    const row = el.querySelector(`[data-flow-id="${id}"]`) as HTMLElement | null
    if (row) {
      programmaticScroll = true
      scrollRowIntoView(row)
    }
  }
)

function ignorePid() {
  const f = store.selectedFlow
  if (f && f.pid !== null && f.pid > 0) {
    emit('ignore-process', { pid: f.pid, name: f.process_name || `PID ${f.pid}` })
  }
}

function ignoreProcess() {
  const f = store.selectedFlow
  if (f && f.process_name) {
    // 按进程名忽略：pid 用 -1 表示「仅按名称忽略」（后端约定）
    emit('ignore-process', { pid: -1, name: f.process_name })
  }
}

function ignoreHost() {
  const f = store.selectedFlow
  if (f && f.host) {
    emit('ignore-host', f.host)
  }
}

const processOptions = computed(() => props.processes)

// ---------- 右键菜单 ----------
const ctxMenu = ref<{ visible: boolean; x: number; y: number; flow: any }>({
  visible: false, x: 0, y: 0, flow: null,
})
// 二级菜单显示控制（复制 / 忽略 / 自动修改）
const showCopySubmenu = ref(false)
const showIgnoreSubmenu = ref(false)
const showModifySubmenu = ref(false)
// 子菜单对齐方向：right（向右展开）/ left（向左展开）
const submenuAlign = ref<'left' | 'right'>('right')
// 子菜单 fixed 定位（彻底解决屏幕外问题：用 position:fixed + JS 实时测量）
const submenuPos = ref<{ left: number; top: number }>({ left: 0, top: 0 })
const ctxMenuRef = ref<HTMLElement | null>(null)

// 子菜单 hover：用主菜单 + 子菜单实际尺寸做双向水平 + 垂直边界检查
function onSubmenuEnter(e: MouseEvent, which: 'ignore' | 'copy' | 'modify') {
  const menuItem = e.currentTarget as HTMLElement
  const menu = menuItem.closest('.ctx-menu') as HTMLElement | null
  if (!menu) {
    if (which === 'ignore') showIgnoreSubmenu.value = true
    else if (which === 'copy') showCopySubmenu.value = true
    else showModifySubmenu.value = true
    return
  }

  const itemRect = menuItem.getBoundingClientRect()
  const menuRect = menu.getBoundingClientRect()
  const vw = window.innerWidth
  const vh = window.innerHeight

  // 第一阶段：用估算尺寸先定位（避免显示在 (0,0) 闪烁）
  const EST_W = 170, EST_H = 140
  let left = menuRect.right + 2
  submenuAlign.value = 'right'
  if (left + EST_W > vw - 4) {
    // 右边不够，向左
    left = menuRect.left - EST_W - 2
    submenuAlign.value = 'left'
    if (left < 4) {
      left = Math.max(4, vw - EST_W - 4)
      submenuAlign.value = 'right'
    }
  }
  let top = itemRect.top - 4
  if (top + EST_H > vh - 4) top = Math.max(4, vh - EST_H - 4)
  submenuPos.value = { left, top }

  // 显示子菜单
  if (which === 'ignore') showIgnoreSubmenu.value = true
  else if (which === 'copy') showCopySubmenu.value = true
  else showModifySubmenu.value = true

  // 第二阶段：DOM 渲染后用实际尺寸修正（确保不超出）
  nextTick(() => {
    const panel = menuItem.querySelector('.ctx-submenu-panel') as HTMLElement | null
    if (!panel) return
    const panelRect = panel.getBoundingClientRect()

    // 重新计算水平
    let newLeft = menuRect.right + 2
    let newAlign: 'left' | 'right' = 'right'
    if (newLeft + panelRect.width > vw - 4) {
      // 右边不够，尝试向左
      newLeft = menuRect.left - panelRect.width - 2
      newAlign = 'left'
      if (newLeft < 4) {
        // 左边也不够，靠右对齐视口
        newLeft = Math.max(4, vw - panelRect.width - 4)
        newAlign = 'right'
      }
    }
    submenuAlign.value = newAlign

    // 重新计算垂直
    let newTop = itemRect.top - 4
    if (newTop + panelRect.height > vh - 4) {
      newTop = Math.max(4, vh - panelRect.height - 4)
    }

    submenuPos.value = { left: newLeft, top: newTop }
  })
}

function onSubmenuLeave(which: 'ignore' | 'copy' | 'modify') {
  if (which === 'ignore') showIgnoreSubmenu.value = false
  else if (which === 'copy') showCopySubmenu.value = false
  else showModifySubmenu.value = false
}

function onContextMenu(e: MouseEvent, flow: any) {
  e.preventDefault()
  store.select(flow.id)
  // 先用点击位置定位，显示后 nextTick 测量菜单尺寸做边界回退
  ctxMenu.value = { visible: true, x: e.clientX, y: e.clientY, flow }
  nextTick(() => {
    const el = ctxMenuRef.value
    if (!el) return
    const rect = el.getBoundingClientRect()
    const vw = window.innerWidth
    const vh = window.innerHeight
    let x = e.clientX
    let y = e.clientY
    // 右边界超出：向左偏移菜单宽度
    if (x + rect.width > vw - 4) x = Math.max(4, vw - rect.width - 4)
    // 下边界超出：向上偏移菜单高度
    if (y + rect.height > vh - 4) y = Math.max(4, vh - rect.height - 4)
    ctxMenu.value = { ...ctxMenu.value, x, y }
  })
}

function closeCtxMenu() {
  ctxMenu.value.visible = false
}

function ctxReplay() {
  if (ctxMenu.value.flow) emit('replay', ctxMenu.value.flow.id)
  closeCtxMenu()
}

function ctxAI() {
  if (ctxMenu.value.flow) emit('ai-analyze', ctxMenu.value.flow.id)
  closeCtxMenu()
}

function ctxViewInAnalyze() {
  if (ctxMenu.value.flow) emit('view-in-analyze', ctxMenu.value.flow.id)
  closeCtxMenu()
}

// 右键忽略：按 PID / 按进程名 / 按 Host（二级菜单）
function ctxIgnorePid() {
  const f = ctxMenu.value.flow
  if (f && f.pid !== null && f.pid > 0) {
    emit('ignore-process', { pid: f.pid, name: f.process_name || `PID ${f.pid}` })
  }
  closeCtxMenu()
}

function ctxIgnoreProcess() {
  const f = ctxMenu.value.flow
  if (f && f.process_name) {
    emit('ignore-process', { pid: -1, name: f.process_name })
  }
  closeCtxMenu()
}

function ctxIgnoreHost() {
  const f = ctxMenu.value.flow
  if (f && f.host) {
    emit('ignore-host', f.host)
  }
  closeCtxMenu()
}

// 右键「自动修改」子菜单：自动请求（基于请求预填）/ 自动响应（基于响应预填）
// 实际规则构造在 CaptureView 完成，FlowList 只负责传递 flow + mode
function ctxAutoModify(mode: 'request' | 'response') {
  const f = ctxMenu.value.flow
  if (f) emit('auto-modify', f, mode)
  closeCtxMenu()
}

function ctxCopyUrl() {
  const f = ctxMenu.value.flow
  if (f) {
    navigator.clipboard.writeText(f.url || '').catch(() => {})
  }
  closeCtxMenu()
}

function ctxCopyCurl() {
  const f = ctxMenu.value.flow
  if (!f) return
  let curl = `curl -X ${f.method} '${f.url}'`
  try {
    const headers = JSON.parse(f.request_headers || '{}')
    for (const [k, v] of Object.entries(headers)) {
      curl += ` \\\n  -H '${k}: ${v}'`
    }
  } catch { /* ignore */ }
  if (f.request_body) {
    curl += ` \\\n  -d '${f.request_body}'`
  }
  navigator.clipboard.writeText(curl).catch(() => {})
  closeCtxMenu()
}

// ---------- 复制项管理（hover 二级菜单） ----------
// 可复制字段：url/curl 是固定项，其余对应 flow 字段
const COPY_FIELD_DEFS: { key: string; label: string; field: string }[] = [
  { key: 'url', label: 'URL', field: 'url' },
  { key: 'curl', label: 'cURL', field: '_curl' },
  { key: 'host', label: 'Host', field: 'host' },
  { key: 'method', label: '方法', field: 'method' },
  { key: 'path', label: 'Path', field: 'path' },
  { key: 'status', label: '状态码', field: 'status_code' },
  { key: 'protocol', label: '协议', field: 'protocol' },
  { key: 'content_type', label: 'Content-Type', field: '_content_type' },
  { key: 'pid', label: 'PID', field: 'pid' },
  { key: 'process', label: '进程', field: 'process_name' },
  { key: 'size', label: '大小', field: 'size' },
  { key: 'duration', label: '耗时', field: 'duration_ms' },
  { key: 'remote_ip', label: '对端 IP', field: 'remote_ip' },
  { key: 'ip_region', label: 'IP 属地', field: 'ip_region' },
]
const COPY_PREF_KEY = 'telnix_copy_fields'
// 默认复制项：url 和 curl
function loadCopyFields(): string[] {
  const saved = localStorage.getItem(COPY_PREF_KEY)
  if (saved) {
    try {
      const arr = JSON.parse(saved)
      if (Array.isArray(arr) && arr.length) return arr
    } catch { /* ignore */ }
  }
  return ['url', 'curl']
}
const enabledCopyFields = ref<string[]>(loadCopyFields())

function buildCurl(f: any): string {
  let curl = `curl -X ${f.method} '${f.url}'`
  try {
    const headers = JSON.parse(f.request_headers || '{}')
    for (const [k, v] of Object.entries(headers)) {
      curl += ` \\\n  -H '${k}: ${v}'`
    }
  } catch { /* ignore */ }
  if (f.request_body) {
    curl += ` \\\n  -d '${f.request_body}'`
  }
  return curl
}

function getFieldValue(f: any, field: string): string {
  if (field === '_curl') return buildCurl(f)
  if (field === '_content_type') return extractContentType(f) || ''
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

// 当前启用的复制项（按用户设置过滤，# 不参与复制）
const enabledCopyItems = computed(() => {
  return COPY_FIELD_DEFS.filter(d => enabledCopyFields.value.includes(d.key))
})

function ctxMultiSelect() {
  const f = ctxMenu.value.flow
  if (!f) return
  multiSelectMode.value = true
  selectedFlowIds.value = new Set([f.id])
  closeCtxMenu()
}

async function ctxDelete() {
  const f = ctxMenu.value.flow
  if (!f) return
  try {
    await ElMessageBox.confirm(`确定删除该流量 #${f.id}？`, '删除', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch {
    closeCtxMenu()
    return
  }
  try {
    await api.deleteFlow(f.id)
    ElMessage.success('已删除')
    store.removeFlows([f.id])
  } catch (e: any) {
    ElMessage.error('删除失败：' + (e?.message || e))
  }
  closeCtxMenu()
}

onMounted(() => {
  loadFocus()
  scheduleUpdateKnowns()
  // 跨页跳转后：如果 selectedId 有值，滚动到选中行
  if (store.selectedId != null) {
    nextTick(() => {
      const el = bodyRef.value
      if (!el) return
      const row = el.querySelector(`[data-flow-id="${store.selectedId}"]`) as HTMLElement | null
      if (row) {
        programmaticScroll = true
        scrollRowIntoView(row)
      }
    })
  }
})
</script>

<template>
  <div class="flow-list full flex flex-col" @click="closeCtxMenu">
    <!-- 筛选栏（精简：筛选按钮 + 忽略 + 专注 + 自动滚动 + 多选） -->
    <div class="filter-bar">
      <el-button size="small" :type="hasActiveFilters ? 'primary' : 'default'" @click="togglePopup('filter')">
        <el-icon><Filter /></el-icon>&nbsp;筛选
        <span v-if="hasActiveFilters" class="filter-badge"></span>
      </el-button>
      <!-- Flowfilter DSL 输入框：支持 ~d host ~m POST ~s 4xx 语法 -->
      <el-input
        v-model="dslQuery"
        size="small"
        :class="['dsl-input', { 'dsl-error': dslError }]"
        placeholder="DSL 过滤：~d host ~m POST ~s 4xx"
        clearable
        @keydown.enter="applyDsl"
      >
        <template #prefix>
          <el-icon class="dsl-icon"><Search /></el-icon>
        </template>
        <template #suffix>
          <el-tooltip content="语法帮助" placement="bottom">
            <el-icon class="dsl-help" @click="showDslHelp = !showDslHelp"><QuestionFilled /></el-icon>
          </el-tooltip>
        </template>
      </el-input>
      <transition name="el-fade-in">
        <div v-if="showDslHelp" class="dsl-help-panel">
          <div class="dsl-help-title">DSL 语法</div>
          <div class="dsl-help-row"><code>~d host</code> host 包含（支持 * 通配）</div>
          <div class="dsl-help-row"><code>~m POST</code> 方法等于</div>
          <div class="dsl-help-row"><code>~s 4xx</code> 状态码（4xx 通配或 200 精确）</div>
          <div class="dsl-help-row"><code>~p http</code> 协议（http/https/tcp/udp/ws）</div>
          <div class="dsl-help-row"><code>~u /api</code> URL 包含</div>
          <div class="dsl-help-row"><code>~h Auth</code> Header 包含</div>
          <div class="dsl-help-row"><code>~b text</code> Body 包含</div>
          <div class="dsl-help-row"><code>"text"</code> 任意字段包含</div>
          <div class="dsl-help-hint">空格分隔 = AND（与关系）</div>
        </div>
      </transition>
      <el-dropdown size="small" :disabled="!store.selectedFlow" @command="(c: string) => { c === 'pid' && ignorePid(); c === 'process' && ignoreProcess(); c === 'host' && ignoreHost() }">
        <el-button size="small" :disabled="!store.selectedFlow">
          <el-icon><RemoveFilled /></el-icon>&nbsp;忽略<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="pid" :disabled="!(store.selectedFlow?.pid && store.selectedFlow.pid > 0)">按 PID</el-dropdown-item>
            <el-dropdown-item command="process" :disabled="!store.selectedFlow?.process_name">按进程名</el-dropdown-item>
            <el-dropdown-item command="host" :disabled="!store.selectedFlow?.host">按 Host</el-dropdown-item>
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
      <div v-if="actionsRight" class="flex-1"></div>
      <el-tooltip :content="store.autoScroll ? (store.autoScrollPaused ? `自动滚动：暂停中（${store.autoScrollDelay}s 后恢复）` : '自动滚动：开') : '自动滚动：关'" placement="bottom">
        <el-button size="small" :type="store.autoScroll ? (store.autoScrollPaused ? 'warning' : 'primary') : 'default'" circle @click="store.autoScroll = !store.autoScroll">
          <el-icon><Bottom /></el-icon>
        </el-button>
      </el-tooltip>
      <el-tooltip :content="multiSelectMode ? '退出多选' : '多选模式'" placement="bottom">
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
              <label class="fp-label">进程</label>
              <el-select
                ref="filterProcessSelectRef"
                v-model="store.filterProcessPids"
                multiple
                filterable
                placeholder="选择进程（可多选）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onHostSelectChange"
              >
                <el-option v-for="p in processOptions" :key="p.pid" :label="p.name + ' (' + p.pid + ')'" :value="p.pid" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">Host</label>
              <el-select
                ref="filterHostSelectRef"
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
                @change="onHostSelectChange"
              >
                <el-option v-for="h in knownHosts" :key="h" :label="h" :value="h" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">方法</label>
              <el-select
                ref="filterMethodSelectRef"
                v-model="store.filterMethods"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                placeholder="选择或输入 HTTP 方法（可多选）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onHostSelectChange"
              >
                <el-option label="GET" value="GET" />
                <el-option label="POST" value="POST" />
                <el-option label="PUT" value="PUT" />
                <el-option label="DELETE" value="DELETE" />
                <el-option label="PATCH" value="PATCH" />
                <el-option label="HEAD" value="HEAD" />
                <el-option label="OPTIONS" value="OPTIONS" />
                <el-option v-for="m in knownMethods" :key="m" :label="m" :value="m" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">协议</label>
              <el-select
                ref="filterProtocolSelectRef"
                v-model="store.filterProtocols"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                placeholder="选择协议（可多选）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onHostSelectChange"
              >
                <el-option label="HTTP" value="http" />
                <el-option label="TCP" value="tcp" />
                <el-option label="UDP" value="udp" />
                <el-option label="WS" value="ws" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">状态码</label>
              <el-select
                ref="filterStatusCodeSelectRef"
                v-model="store.filterStatusCodes"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                placeholder="输入 200 / 2xx / 4* 按 Enter 添加，支持通配符"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onHostSelectChange"
              >
                <el-option v-for="c in knownStatusCodes" :key="c" :label="c" :value="c" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">Content-Type</label>
              <el-select
                v-model="store.filterContentTypes"
                multiple
                filterable
                default-first-option
                :reserve-keyword="false"
                placeholder="选择 Content-Type 大类（可多选）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
              >
                <el-option label="application（应用数据/json/xml 等）" value="application" />
                <el-option label="text（文本/html/css/js 等）" value="text" />
                <el-option label="image（图片）" value="image" />
                <el-option label="video（视频）" value="video" />
                <el-option label="audio（音频）" value="audio" />
                <el-option label="font（字体）" value="font" />
                <el-option label="multipart（表单上传）" value="multipart" />
                <el-option label="message（消息）" value="message" />
                <el-option label="model（3D 模型）" value="model" />
                <el-option label="unknown（未知/无）" value="unknown" />
              </el-select>
            </div>
            <div class="fp-actions">
              <el-button size="small" @click="resetFilters">重置</el-button>
              <el-button size="small" type="primary" @click="closePopup">完成</el-button>
            </div>
          </template>
          <!-- 专注 -->
          <template v-if="activePopup === 'focus'">
            <div class="fp-row">
              <label class="fp-label">进程</label>
              <el-select
                ref="focusProcessSelectRef"
                v-model="focusProcessSelect"
                multiple
                filterable
                :reserve-keyword="false"
                placeholder="按 PID 选择进程"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onFocusPidsChange"
              >
                <el-option v-for="p in processOptions" :key="p.pid" :label="p.name + ' (' + p.pid + ')'" :value="p.pid" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">进程名</label>
              <el-input v-model="focusProcessName" placeholder="如 chrome.exe（含子进程）" size="small" clearable style="width: 200px"
                @keyup.enter="onFocusByName" />
              <el-checkbox v-model="focusIncludeChildren" size="small">子进程</el-checkbox>
              <el-button size="small" @click="onFocusByName">应用</el-button>
            </div>
            <div class="fp-row">
              <label class="fp-label">Host</label>
              <el-select
                ref="focusHostSelectRef"
                v-model="focusHosts"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                placeholder="通配符如 *.bilibili.com 或选择已出现 host"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onFocusHostsChange"
              >
                <el-option v-for="h in knownHosts" :key="h" :label="h" :value="h" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">方法</label>
              <el-select
                v-model="focusMethods"
                multiple
                :reserve-keyword="false"
                placeholder="HTTP 方法（GET/POST/...）"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onFocusExtraChange"
              >
                <el-option label="GET" value="GET" />
                <el-option label="POST" value="POST" />
                <el-option label="PUT" value="PUT" />
                <el-option label="DELETE" value="DELETE" />
                <el-option label="PATCH" value="PATCH" />
                <el-option label="HEAD" value="HEAD" />
                <el-option label="OPTIONS" value="OPTIONS" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">状态码</label>
              <el-select
                v-model="focusStatusCodes"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                placeholder="如 200、2xx、404"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onFocusExtraChange"
              >
                <el-option label="2xx（成功）" value="2xx" />
                <el-option label="3xx（重定向）" value="3xx" />
                <el-option label="4xx（客户端错误）" value="4xx" />
                <el-option label="5xx（服务端错误）" value="5xx" />
                <el-option label="200" value="200" />
                <el-option label="201" value="201" />
                <el-option label="204" value="204" />
                <el-option label="301" value="301" />
                <el-option label="302" value="302" />
                <el-option label="304" value="304" />
                <el-option label="400" value="400" />
                <el-option label="401" value="401" />
                <el-option label="403" value="403" />
                <el-option label="404" value="404" />
                <el-option label="500" value="500" />
                <el-option label="502" value="502" />
                <el-option label="503" value="503" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">Content-Type</label>
              <el-select
                v-model="focusContentTypes"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                placeholder="按主类型（application/image/text...），可直接输入"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onFocusExtraChange"
              >
                <el-option label="application（JSON/XML/...）" value="application" />
                <el-option label="text（文本）" value="text" />
                <el-option label="image（图片）" value="image" />
                <el-option label="video（视频）" value="video" />
                <el-option label="audio（音频）" value="audio" />
                <el-option label="font（字体）" value="font" />
                <el-option label="multipart（表单上传）" value="multipart" />
                <el-option label="message（消息）" value="message" />
                <el-option label="model（3D 模型）" value="model" />
                <el-option label="unknown（未知/无）" value="unknown" />
              </el-select>
            </div>
            <div class="fp-row">
              <label class="fp-label">协议</label>
              <el-select
                v-model="focusProtocols"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                placeholder="选择协议（可多选），http 含 https"
                size="small"
                class="vertical-tags"
                style="width: 360px"
                @change="onFocusExtraChange"
              >
                <el-option label="HTTP（含 HTTPS）" value="http" />
                <el-option label="TCP" value="tcp" />
                <el-option label="UDP" value="udp" />
              </el-select>
            </div>
            <div class="fp-actions">
              <el-button size="small" @click="resetFocus">重置</el-button>
              <el-button size="small" type="primary" @click="closePopup">完成</el-button>
            </div>
          </template>
        </div>
      </div>
    </transition>

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
          type="primary"
          :disabled="!selectedCount"
          @click="batchAIAnalyze"
        >
          <el-icon><MagicStick /></el-icon>&nbsp;发送到AI
        </el-button>
        <el-button
          size="small"
          :disabled="!selectedCount"
          @click="batchReplay"
          title="按指定 QPS 依次重放选中流量"
        >
          <el-icon><RefreshRight /></el-icon>&nbsp;批量重放
        </el-button>
        <el-button
          v-if="hasPendingBreakpoint"
          size="small"
          type="success"
          :disabled="!selectedCount"
          @click="batchReleaseFlows('release')"
        >
          <el-icon><Promotion /></el-icon>&nbsp;放行断点
        </el-button>
        <el-button
          v-if="hasPendingBreakpoint"
          size="small"
          type="warning"
          :disabled="!selectedCount"
          @click="batchReleaseFlows('drop')"
        >
          <el-icon><CircleClose /></el-icon>&nbsp;丢弃断点
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

    <!-- 表头 -->
    <div class="fl-head mono" :style="{ gridTemplateColumns: gridCols }">
      <div v-if="multiSelectMode" class="col-check"></div>
      <div
        v-for="c in visibleCols"
        :key="c.key"
        class="fl-head-cell"
        :class="[c.cellClass.split(' ')[0], { dragging: dragColKey === c.key }]"
        draggable="true"
        @dragstart="onColDragStart(c.key)"
        @dragover="onColDragOver($event, c.key)"
        @dragend="onColDragEnd"
      >{{ c.label }}</div>
    </div>
    <!-- 表体 -->
    <div ref="bodyRef" class="fl-body flex-1 overflow-auto" @scroll="onBodyScroll">
      <div
        v-for="f in displayFlows"
        :key="f.id"
        v-memo="[f.id, f.breakpoint_status, store.selectedId === f.id, selectedFlowIds.has(f.id), gridCols]"
        class="fl-row mono"
        :class="[rowClass(f), { selected: store.selectedId === f.id, checked: selectedFlowIds.has(f.id) }]"
        :style="{ gridTemplateColumns: gridCols }"
        :data-flow-id="f.id"
        @click="multiSelectMode ? toggleFlowCheck(f.id) : select(f)"
        @contextmenu="onContextMenu($event, f)"
      >
        <div v-if="multiSelectMode" class="col-check" @click.stop="toggleFlowCheck(f.id)">
          <el-checkbox :model-value="selectedFlowIds.has(f.id)" size="small" />
        </div>
        <div
          v-for="c in visibleCols"
          :key="c.key"
          :class="cellFullClass(c, f)"
        >{{ c.text(f) }}</div>
      </div>
      <div v-if="!store.flows.length" class="empty-text text-dim">暂无流量，开始抓包后此处显示会话</div>
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
        <div class="ctx-item" @click="ctxReplay"><el-icon><RefreshRight /></el-icon>&nbsp;重放请求</div>
        <div class="ctx-item" @click="ctxAI"><el-icon><MagicStick /></el-icon>&nbsp;发送到AI</div>
        <div class="ctx-item" @click="ctxViewInAnalyze"><el-icon><DataLine /></el-icon>&nbsp;在全局分析查看</div>
        <div class="ctx-item" @click="ctxMultiSelect"><el-icon><CircleCheck /></el-icon>&nbsp;多选模式</div>
        <!-- 自动修改：hover 展开二级菜单（自动请求 / 自动响应） -->
        <div class="ctx-item ctx-submenu" @mouseenter="onSubmenuEnter($event, 'modify')" @mouseleave="onSubmenuLeave('modify')">
          <el-icon><MagicStick /></el-icon>&nbsp;自动修改
          <el-icon class="ctx-arrow"><ArrowRight /></el-icon>
          <div v-if="showModifySubmenu" class="ctx-submenu-panel" :class="{ 'ctx-submenu-left': submenuAlign === 'left' }" :style="{ left: submenuPos.left + 'px', top: submenuPos.top + 'px' }">
            <div class="ctx-item" @click="ctxAutoModify('request')">自动请求（基于请求预填）</div>
            <div class="ctx-item" @click="ctxAutoModify('response')">自动响应（基于响应预填）</div>
          </div>
        </div>
        <!-- 忽略：hover 展开二级菜单（按 PID / 按进程名 / 按 Host） -->
        <div class="ctx-item ctx-submenu" @mouseenter="onSubmenuEnter($event, 'ignore')" @mouseleave="onSubmenuLeave('ignore')">
          <el-icon><RemoveFilled /></el-icon>&nbsp;忽略
          <el-icon class="ctx-arrow"><ArrowRight /></el-icon>
          <div v-if="showIgnoreSubmenu" class="ctx-submenu-panel" :class="{ 'ctx-submenu-left': submenuAlign === 'left' }" :style="{ left: submenuPos.left + 'px', top: submenuPos.top + 'px' }">
            <div class="ctx-item" @click="ctxIgnorePid">按 PID</div>
            <div class="ctx-item" @click="ctxIgnoreProcess">按进程名</div>
            <div class="ctx-item" @click="ctxIgnoreHost">按 Host</div>
          </div>
        </div>
        <div class="ctx-sep"></div>
        <!-- 复制：hover 展开二级菜单，含 url/curl 及启用的所有显示列 -->
        <div class="ctx-item ctx-submenu" @mouseenter="onSubmenuEnter($event, 'copy')" @mouseleave="onSubmenuLeave('copy')">
          <el-icon><CopyDocument /></el-icon>&nbsp;复制
          <el-icon class="ctx-arrow"><ArrowRight /></el-icon>
          <div v-if="showCopySubmenu" class="ctx-submenu-panel" :class="{ 'ctx-submenu-left': submenuAlign === 'left' }" :style="{ left: submenuPos.left + 'px', top: submenuPos.top + 'px' }">
            <div
              v-for="item in enabledCopyItems"
              :key="item.key"
              class="ctx-item"
              @click="copyField(item.field)"
            >{{ item.label }}</div>
            <div v-if="enabledCopyItems.length === 0" class="ctx-item ctx-disabled">未启用复制项</div>
          </div>
        </div>
        <div class="ctx-sep"></div>
        <div class="ctx-item ctx-danger" @click="ctxDelete"><el-icon><Delete /></el-icon>&nbsp;删除流量</div>
      </div>
    </teleport>
  </div>
</template>

<style scoped>
.flow-list { background: var(--on-bg-elevated); position: relative; }

/* 多选悬浮工具栏 */
.multi-float-bar {
  position: absolute;
  top: 46px;
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

.filter-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
/* Flowfilter DSL 输入框 */
.dsl-input {
  width: 280px;
  flex-shrink: 0;
}
.dsl-input.dsl-error :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--on-rose, #f43f5e) inset;
}
.dsl-icon {
  color: var(--on-text-dim);
}
.dsl-help {
  color: var(--on-text-dim);
  cursor: pointer;
  transition: color 0.2s;
}
.dsl-help:hover {
  color: var(--on-accent, #2dd4bf);
}
/* DSL 帮助浮层 */
.dsl-help-panel {
  position: absolute;
  top: 42px;
  left: 60px;
  z-index: 200;
  min-width: 260px;
  padding: 10px 12px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-md, 6px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
  font-size: 12px;
  line-height: 1.6;
}
.dsl-help-title {
  font-weight: 600;
  color: var(--on-text);
  margin-bottom: 6px;
  font-size: 13px;
}
.dsl-help-row {
  color: var(--on-text-dim);
  margin: 2px 0;
}
.dsl-help-row code {
  display: inline-block;
  min-width: 90px;
  padding: 1px 6px;
  background: var(--on-bg, #f5f5f5);
  border-radius: 3px;
  color: var(--on-accent, #2dd4bf);
  font-family: var(--on-font-mono, Menlo, Consolas, monospace);
}
.dsl-help-hint {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--on-border-light);
  color: var(--on-text-dim);
  font-size: 11px;
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
.fp-label {
  width: 48px; color: var(--on-text-muted); font-size: 12px;
  flex-shrink: 0;
  line-height: 28px; /* 与单行输入框对齐 */
}
/* 竖向 tag：每行一个，最多 5 行，超过滚动 */
.vertical-tags {
  position: relative;
}
.vertical-tags :deep(.el-select__tags) {
  flex-direction: column;
  align-items: stretch;
  max-height: 140px; /* 约 5 行（每行 ~26px + 间距） */
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
  flex-shrink: 0; /* 不被压缩，保证一行一个 */
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
.fp-actions {
  display: flex; justify-content: flex-end; gap: 8px;
  padding-top: 4px;
  border-top: 1px solid var(--on-border-light);
}

/* 表格 */
.fl-head, .fl-row {
  display: grid;
  gap: 4px; padding: 0 10px; align-items: center;
}
.fl-head {
  height: 30px; font-size: 11px; color: var(--on-text-muted); font-weight: 600;
  border-bottom: 1px solid var(--on-border); background: var(--on-bg);
  position: sticky; top: 0; z-index: 1;
}
.fl-head-cell { cursor: grab; user-select: none; }
.fl-head-cell:active { cursor: grabbing; }
.fl-head-cell.dragging { opacity: 0.4; }
.fl-row {
  height: 26px; font-size: 12px; cursor: pointer;
  border-bottom: 1px solid var(--on-border-light);
  border-left: 2px solid transparent;
}
.fl-row:hover { background: var(--on-bg-hover); }
.fl-row.selected { background: var(--on-accent-glow); border-left: 2px solid var(--on-accent); padding-left: 8px; }
.fl-row.checked { background: rgba(45, 212, 191, 0.08); }

.col-check { display: flex; align-items: center; justify-content: center; }
.fl-row > div { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.col-url { color: var(--on-text-muted); }
.empty-text { text-align: center; padding: 30px; color: var(--on-text-dim); }

/* 右键菜单 */
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
.ctx-item.ctx-disabled { color: var(--on-text-muted, #666); cursor: not-allowed; }
.ctx-sep { height: 1px; background: var(--on-border, #333344); margin: 4px 0; }
/* 二级菜单：用 position:fixed + JS 实时测量定位，彻底避免屏幕外问题 */
.ctx-submenu { position: relative; }
.ctx-arrow { margin-left: auto; font-size: 10px; opacity: 0.6; }
.ctx-submenu-panel {
  position: fixed;
  /* left/top 由 JS 绑定 submenuPos */
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-border, #333344);
  border-radius: var(--on-radius-md);
  padding: 4px 0;
  min-width: 160px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  z-index: 10001;
}
/* 向左展开时箭头翻转（仅做视觉提示，位置已由 JS 控制） */
.ctx-item.ctx-submenu:has(.ctx-submenu-left) .ctx-arrow {
  transform: rotate(180deg);
}
</style>
