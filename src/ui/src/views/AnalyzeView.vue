<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useFlowsStore } from '../stores/flows'
import { api, type Flow } from '../api/client'
import PreviewView from '../components/PreviewView.vue'
import TextSearch from '../components/TextSearch.vue'
import ImportButton from '../components/ImportButton.vue'

// 全局分析页面：按 host/进程/Content-Type/状态码 分组折叠显示流量
// 单击：右侧显示响应 Preview + 请求 Raw
// 双击：跳转抓包页面并选中对应流量
// 跨会话加载所有历史流量，不依赖活动会话
const router = useRouter()
const route = useRoute()
const flows = useFlowsStore()

type GroupBy = 'host' | 'process' | 'content_type' | 'status_code' | 'ip_region'
const groupBy = ref<GroupBy>('host')
// 请求分析 vs 响应分析：影响右侧详情显示内容
const analyzeMode = ref<'request' | 'response'>('response')
// 视图模式：list 列表 / bar 条形图 / pie 饼图（图表显示全量数据，换页不换图）
const viewMode = ref<'list' | 'bar' | 'pie' | 'timeline'>('list')

// 统计图颜色板（性能优先：纯 CSS，不用图表库）
const CHART_COLORS = [
  '#2dd4bf', '#f59e0b', '#8b5cf6', '#ef4444', '#3b82f6',
  '#10b981', '#ec4899', '#6366f1', '#14b8a6', '#f97316',
  '#84cc16', '#06b6d4', '#a855f7', '#eab308', '#64748b',
]
function groupColor(index: number): string {
  return CHART_COLORS[index % CHART_COLORS.length]
}

// 统计图数据：全量分组统计（来自 /flows/stats 接口，不分页，换页不换图）
interface StatsGroup {
  key: string
  label: string
  count: number
}
const statsGroups = ref<StatsGroup[]>([])
const statsTotal = ref(0)
const statsLoading = ref(false)
// 排序方式：desc 大→小 / asc 小→大
const sortOrder = ref<'desc' | 'asc'>('desc')

async function loadStats() {
  statsLoading.value = true
  try {
    const res: any = await api.getFlowsStats(groupBy.value, filterHost.value || undefined, filterProcess.value || undefined)
    const groups: StatsGroup[] = (res.groups || []).map((g: any) => ({
      key: g.key,
      label: groupLabel(g.key),
      count: g.count,
    }))
    // 应用排序
    groups.sort((a, b) => sortOrder.value === 'desc' ? b.count - a.count : a.count - b.count)
    statsGroups.value = groups
    statsTotal.value = res.total || 0
  } catch (e: any) {
    ElMessage.error('统计加载失败：' + (e?.message || e))
    statsGroups.value = []
    statsTotal.value = 0
  } finally {
    statsLoading.value = false
  }
}

// 切换排序
function toggleSortOrder() {
  sortOrder.value = sortOrder.value === 'desc' ? 'asc' : 'desc'
  loadStats()
}

// 条形图悬停 tooltip（跟随鼠标，自动避免屏外）
const hoverTip = ref({
  visible: false,
  x: 0,
  y: 0,
  label: '',
  count: 0,
  pct: '0.0',
  color: '',
})
const TOOLTIP_W = 160
const TOOLTIP_H = 80
const TOOLTIP_PAD = 12

function onSegmentHover(e: MouseEvent, g: StatsGroup, idx: number) {
  hoverTip.value = {
    visible: true,
    x: 0,
    y: 0,
    label: g.label,
    count: g.count,
    pct: (g.count / (statsTotal.value || 1) * 100).toFixed(1),
    color: groupColor(idx),
  }
  onSegmentMove(e)
}
function onSegmentMove(e: MouseEvent) {
  // 相对于滚动容器定位，自动避免屏外
  const container = (e.currentTarget as HTMLElement).closest('.chart-body') as HTMLElement
  if (!container) return
  const rect = container.getBoundingClientRect()
  let x = e.clientX - rect.left + container.scrollLeft + TOOLTIP_PAD
  let y = e.clientY - rect.top + container.scrollTop + TOOLTIP_PAD
  // 右边界
  if (x + TOOLTIP_W > container.scrollLeft + rect.width) {
    x = e.clientX - rect.left + container.scrollLeft - TOOLTIP_W - TOOLTIP_PAD
  }
  // 下边界
  if (y + TOOLTIP_H > container.scrollTop + rect.height) {
    y = e.clientY - rect.top + container.scrollTop - TOOLTIP_H - TOOLTIP_PAD
  }
  hoverTip.value.x = Math.max(4, x)
  hoverTip.value.y = Math.max(4, y)
}
function onSegmentLeave() {
  hoverTip.value.visible = false
}
function onChartScroll() {
  // 滚动时隐藏 tooltip（避免错位）
  hoverTip.value.visible = false
}

// 饼图：conic-gradient 字符串（按各组 count 比例分配角度）
const pieGradient = computed(() => {
  const groups = statsGroups.value
  const total = statsTotal.value || 1
  if (!groups.length) return 'conic-gradient(var(--on-border) 0deg 360deg)'
  let acc = 0
  const stops: string[] = []
  groups.forEach((g, i) => {
    const start = (acc / total) * 360
    acc += g.count
    const end = (acc / total) * 360
    stops.push(`${groupColor(i)} ${start}deg ${end}deg`)
  })
  return `conic-gradient(${stops.join(', ')})`
})

// ===== 时序图（timeline）=====
// 粒度（秒）：1 / 5 / 60
const timelineGranularity = ref<number>(1)
// 指标：qps（每秒请求数）或 bytes（每秒字节）
const timelineMetric = ref<'qps' | 'bytes'>('qps')
// SVG 尺寸
const timelineWidth = 800
const timelineHeight = 320
const timelinePadding = { top: 20, right: 20, bottom: 30, left: 50 }

// 时间桶：按粒度分桶统计 count 和 bytes
const timelineBuckets = computed(() => {
  const flows = allFlows.value
  if (flows.length < 2) return []
  const gran = timelineGranularity.value
  // 找出最早和最晚时间
  const times = flows.map(f => new Date(f.timestamp).getTime() / 1000)
  // 用 reduce 替代 Math.min/max(...spread)，避免大数组栈溢出
  const minT = times.reduce((a, b) => a < b ? a : b, Infinity)
  const maxT = times.reduce((a, b) => a > b ? a : b, -Infinity)
  const span = Math.max(1, maxT - minT)
  const bucketCount = Math.min(200, Math.ceil(span / gran) + 1)
  const buckets: { ts: number; count: number; bytes: number }[] = []
  for (let i = 0; i < bucketCount; i++) {
    buckets.push({ ts: minT + i * gran, count: 0, bytes: 0 })
  }
  for (const f of flows) {
    const t = new Date(f.timestamp).getTime() / 1000
    const idx = Math.min(bucketCount - 1, Math.max(0, Math.floor((t - minT) / gran)))
    buckets[idx].count++
    buckets[idx].bytes += f.size || 0
  }
  return buckets
})

// 峰值
const timelineMaxBucket = computed(() => {
  const b = timelineBuckets.value
  if (!b.length) return 0
  // 用 reduce 替代 Math.max(...spread)，避免大数组栈溢出
  return timelineMetric.value === 'qps'
    ? b.reduce((m, x) => x.count > m ? x.count : m, 0)
    : b.reduce((m, x) => x.bytes > m ? x.bytes : m, 0)
})

// 折线 points（x,y 对）
const timelineQpsPoints = computed(() => {
  return buildTimelinePoints(b => b.count)
})
const timelineBytesPoints = computed(() => {
  return buildTimelinePoints(b => b.bytes)
})
function buildTimelinePoints(selector: (b: { count: number; bytes: number }) => number): string {
  const b = timelineBuckets.value
  if (!b.length) return ''
  // 用 reduce 替代 Math.max(...spread)，避免大数组栈溢出
  const maxVal = Math.max(1, b.reduce((m, x) => { const v = selector(x); return v > m ? v : m }, 0))
  const innerW = timelineWidth - timelinePadding.left - timelinePadding.right
  const innerH = timelineHeight - timelinePadding.top - timelinePadding.bottom
  return b.map((bucket, i) => {
    const x = timelinePadding.left + (i / Math.max(1, b.length - 1)) * innerW
    const y = timelinePadding.top + innerH - (selector(bucket) / maxVal) * innerH
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

// 区域填充 points（在折线两端加底部）
const timelineQpsAreaPoints = computed(() => {
  const pts = timelineQpsPoints.value
  return buildTimelineArea(pts)
})
const timelineBytesAreaPoints = computed(() => {
  const pts = timelineBytesPoints.value
  return buildTimelineArea(pts)
})
function buildTimelineArea(linePoints: string): string {
  if (!linePoints) return ''
  const pts = linePoints.split(' ')
  if (pts.length < 2) return ''
  const first = pts[0].split(',')
  const last = pts[pts.length - 1].split(',')
  const baseY = timelineHeight - timelinePadding.bottom
  return `${first[0]},${baseY} ${linePoints} ${last[0]},${baseY}`
}

// Y 轴刻度（5 条网格线）
const timelineYGrids = computed(() => {
  const b = timelineBuckets.value
  if (!b.length) return []
  // 用 reduce 替代 Math.max(...spread)，避免大数组栈溢出
  const maxVal = timelineMetric.value === 'qps'
    ? b.reduce((m, x) => x.count > m ? x.count : m, 0)
    : b.reduce((m, x) => x.bytes > m ? x.bytes : m, 0)
  const top = Math.max(1, maxVal)
  const innerH = timelineHeight - timelinePadding.top - timelinePadding.bottom
  const grids: { y: number; label: string }[] = []
  for (let i = 0; i <= 4; i++) {
    const val = (top * i) / 4
    const y = timelinePadding.top + innerH - (val / top) * innerH
    const label = timelineMetric.value === 'qps'
      ? String(Math.round(val))
      : val >= 1024 ? (val / 1024).toFixed(1) + 'K' : String(Math.round(val))
    grids.push({ y, label })
  }
  return grids
})

// X 轴刻度（5 个时间点）
const timelineXLabels = computed(() => {
  const b = timelineBuckets.value
  if (b.length < 2) return ['—', '—']
  const gran = timelineGranularity.value
  const minT = b[0].ts
  const maxT = b[b.length - 1].ts
  const result: string[] = []
  for (let i = 0; i <= 4; i++) {
    const t = minT + (maxT - minT) * (i / 4)
    const d = new Date(t * 1000)
    const label = gran >= 60
      ? `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`
      : `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
    result.push(label)
  }
  return result
})

// 所有流量（跨会话，分页加载）
const allFlows = ref<Flow[]>([])
const totalCount = ref(0)
const loading = ref(false)
const currentPage = ref(1)
// 全局分析一页显示全部流量，不分页（移除 el-pagination 控件）
const pageSize = ref(100000)
const clearLoading = ref(false)

// 过滤条件
const filterHost = ref('')
const filterProcess = ref('')

// 展开的分组 key 集合
const expandedGroups = ref<Set<string>>(new Set())
// 单击选中的流量（显示详情）
const selectedFlowId = ref<number | null>(null)
// selectedFlow：优先从当前页找，找不到则 fallback 到 store 注入的 flow（从其他页面跳转过来时）
const selectedFlow = computed(() =>
  allFlows.value.find(f => f.id === selectedFlowId.value)
  || (flows.selectedFlow && flows.selectedFlow.id === selectedFlowId.value ? flows.selectedFlow : null)
)

// 分组函数：根据 groupBy 返回分组的 key
function groupKey(f: Flow): string {
  switch (groupBy.value) {
    case 'host':
      return f.host || '(无 host)'
    case 'process':
      return f.process_name || '(未知进程)'
    case 'content_type':
      return extractContentTypeMain(f)
    case 'status_code': {
      const code = f.status_code
      if (code === null || code === undefined) return '无状态码'
      if (code < 200) return '1xx 信息'
      if (code < 300) return '2xx 成功'
      if (code < 400) return '3xx 重定向'
      if (code < 500) return '4xx 客户端错误'
      return '5xx 服务器错误'
    }
    case 'ip_region':
      return f.ip_region || '(未知属地)'
  }
}

// 提取 Content-Type 主类型
function extractContentTypeMain(f: Flow): string {
  const ct = extractContentType(f) || ''
  return ct.split('/')[0].toLowerCase() || 'unknown'
}

// 性能优化：extractContentType 结果缓存（按 flow.id），避免 groupedFlows 每次重算都反复 JSON.parse
const _ctCache = new Map<number, string>()
const _CT_CACHE_MAX = 5000
// 从 response_headers 提取 Content-Type
function extractContentType(f: Flow): string {
  if (!f) return ''
  const fid = f.id
  let cached = _ctCache.get(fid)
  if (cached !== undefined) return cached
  let result = ''
  try {
    const obj = JSON.parse(f.response_headers || '{}')
    for (const k of Object.keys(obj)) {
      if (k.toLowerCase() === 'content-type') {
        result = String(obj[k])
        break
      }
    }
  } catch { /* ignore */ }
  // 写入缓存前检查大小：超过上限时清理一半旧条目（Map 按插入顺序遍历，近似 LRU）
  if (_ctCache.size >= _CT_CACHE_MAX) {
    const keys = _ctCache.keys()
    for (let i = 0; i < _CT_CACHE_MAX / 2; i++) {
      const k = keys.next().value
      if (k === undefined) break
      _ctCache.delete(k)
    }
  }
  _ctCache.set(fid, result)
  return result
}

// 分组结果：[{ key, label, count, globalCount, globalPct, flows }]
// 显示所有全局 statsGroups 分组（跨页一致），本页无流量的分组显示为空折叠状态
interface Group {
  key: string
  label: string
  count: number         // 本页内该分组的流量数
  globalCount: number   // 全局该分组的流量数（来自 stats）
  globalPct: number     // 全局占比（%）
  flows: Flow[]
  hasPageFlows: boolean // 本页是否有该分组的流量
}
const groupedFlows = computed<Group[]>(() => {
  // 先按本页 flows 建立映射
  const map = new Map<string, Flow[]>()
  for (const f of allFlows.value) {
    const k = groupKey(f)
    if (!map.has(k)) map.set(k, [])
    map.get(k)!.push(f)
  }
  const total = statsTotal.value || 1
  const groups: Group[] = []

  // 先按 statsGroups 全局顺序添加所有分组（包括本页没有的）
  for (const sg of statsGroups.value) {
    const fls = map.get(sg.key) || []
    groups.push({
      key: sg.key,
      label: groupLabel(sg.key),
      count: fls.length,
      globalCount: sg.count,
      globalPct: (sg.count / total) * 100,
      flows: fls,
      hasPageFlows: fls.length > 0,
    })
    map.delete(sg.key)
  }
  // 添加本页中存在但 statsGroups 中没有的分组（边缘情况，如 stats 未加载完）
  for (const [key, fls] of map.entries()) {
    groups.push({
      key,
      label: groupLabel(key),
      count: fls.length,
      globalCount: fls.length,
      globalPct: (fls.length / total) * 100,
      flows: fls,
      hasPageFlows: true,
    })
  }
  return groups
})

// 统计图：最大 count（用于条形长度比例）
// 性能优化：用 reduce 替代 Math.max(...spread)，避免大数组 spread 参数上限
const maxGroupCount = computed(() => {
  const groups = groupedFlows.value
  let m = 1
  for (const g of groups) {
    if (g.count > m) m = g.count
  }
  return m
})

// 点击统计图项：切回列表视图。若按 host/process 分组，把该项填入过滤栏定位
function onChartBarClick(g: StatsGroup) {
  viewMode.value = 'list'
  if (groupBy.value === 'host' && g.key && !g.key.startsWith('(')) {
    filterHost.value = g.key
    currentPage.value = 1
    loadFlows()
  } else if (groupBy.value === 'process' && g.key && !g.key.startsWith('(')) {
    filterProcess.value = g.key
    currentPage.value = 1
    loadFlows()
  }
}

function groupLabel(key: string): string {
  if (groupBy.value === 'content_type') {
    const map: Record<string, string> = {
      'application': 'application（应用数据/json/xml 等）',
      'text': 'text（文本/html/css/js 等）',
      'image': 'image（图片）',
      'video': 'video（视频）',
      'audio': 'audio（音频）',
      'font': 'font（字体）',
      'multipart': 'multipart（表单上传）',
      'message': 'message（消息）',
      'model': 'model（3D 模型）',
      'unknown': 'unknown（未知/无）',
    }
    return map[key] || key
  }
  return key
}

function toggleGroup(key: string) {
  if (expandedGroups.value.has(key)) {
    expandedGroups.value.delete(key)
  } else {
    expandedGroups.value.add(key)
  }
}

// 单击：选中显示详情
function onFlowClick(f: Flow) {
  selectedFlowId.value = f.id
}

// 双击：跳转抓包页面并选中（用 selectFlow 注入 flow 对象，不依赖 store 列表查找）
function onFlowDblClick(f: Flow) {
  flows.selectFlow(f)
  flows.aiFlowIds = []
  router.push('/capture')
}

// 关闭详情面板
function closeDetail() {
  selectedFlowId.value = null
}

// 断点状态显示
function bpBadge(f: Flow): string {
  const s = f.breakpoint_status
  if (!s) return ''
  if (s === 'pending_request') return '请求已拦截'
  if (s === 'pending_response') return '响应已拦截'
  return '已拦截'
}

// 跨会话加载所有流量（不依赖活动会话），分页 + 过滤
// 性能优化：lite 模式不加载 body/headers（大字段），选中时再单独 GET /flows/{id} 补齐
async function loadFlows(silent = false) {
  if (!silent) loading.value = true
  try {
    const params: any = {
      limit: pageSize.value,
      offset: (currentPage.value - 1) * pageSize.value,
      lite: true,
    }
    if (filterHost.value) params.host = filterHost.value
    if (filterProcess.value) params.process = filterProcess.value
    const res: any = await api.getAllFlows(params)
    allFlows.value = res.flows || []
    totalCount.value = res.total || 0
    // 切换页/过滤后清空选中（silent 刷新时序图时不打断用户选择）
    if (!silent) {
      selectedFlowId.value = null
      expandedGroups.value.clear()
    }
  } catch (e: any) {
    if (!silent) ElMessage.error('加载失败：' + (e?.message || e))
  } finally {
    if (!silent) loading.value = false
  }
}

// 时序图自动刷新：切到 timeline 视图时定时（silent）重载，保证图随新流量实时更新。
// 仅 timeline 视图轮询，离开即停止，避免无谓请求。
let _timelineTimer: ReturnType<typeof setInterval> | undefined
function _stopTimelineRefresh() {
  if (_timelineTimer) {
    clearInterval(_timelineTimer)
    _timelineTimer = undefined
  }
}
function _startTimelineRefresh() {
  _stopTimelineRefresh()
  _timelineTimer = setInterval(() => {
    if (viewMode.value === 'timeline') loadFlows(true)
  }, 3000)
}
watch(viewMode, (v) => {
  if (v === 'timeline') _startTimelineRefresh()
  else _stopTimelineRefresh()
})

// 选中流量时补齐详情（lite 模式只有列表字段，需要单独 GET 拿 body/headers）
async function loadFlowDetail(id: number) {
  try {
    const f: any = await api.getFlow(id)
    if (f) {
      // 替换列表中的 lite 记录为完整记录
      const idx = allFlows.value.findIndex(x => x.id === id)
      if (idx >= 0) {
        allFlows.value[idx] = f
      } else {
        allFlows.value.unshift(f)
      }
    }
  } catch {
    /* ignore */
  }
}

// 选中变化时加载详情
watch(selectedFlowId, (id) => {
  if (id == null) return
  const f = allFlows.value.find(x => x.id === id)
  // lite 模式下 response_headers 字段不存在或为 undefined，触发详情加载
  if (f && f.response_headers === undefined) {
    loadFlowDetail(id)
  }
})

function applyFilter() {
  currentPage.value = 1
  loadFlows()
  loadStats()
}

function clearFilter() {
  filterHost.value = ''
  filterProcess.value = ''
  currentPage.value = 1
  loadFlows()
  loadStats()
}

// groupBy 变化时：列表分组和统计图都要重载
watch(groupBy, () => {
  loadStats()
})

// 清理流量数据：全部 / 仅当前页之前
async function onClearFlows(mode: 'all' | 'before') {
  try {
    if (mode === 'all') {
      await ElMessageBox.confirm(
        `确认清空全部流量数据？共 ${totalCount.value} 条，此操作不可恢复。`,
        '清理确认',
        { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' }
      )
      clearLoading.value = true
      const r: any = await api.clearAllFlows('all')
      ElMessage.success(`已清空 ${r.deleted} 条流量`)
    } else {
      // 清理当前页之前的旧数据（保留当前页及之后）
      if (!allFlows.value.length) {
        ElMessage.warning('当前无流量可清理')
        return
      }
      // 用 reduce 替代 Math.min(...spread)，避免大数组栈溢出
      const minId = allFlows.value.reduce((m, f) => f.id < m ? f.id : m, Infinity)
      await ElMessageBox.confirm(
        `确认删除 id < ${minId} 的旧流量数据？此操作不可恢复。`,
        '清理确认',
        { type: 'warning', confirmButtonText: '清理', cancelButtonText: '取消' }
      )
      clearLoading.value = true
      const r: any = await api.clearAllFlows('before_id', minId)
      ElMessage.success(`已清理 ${r.deleted} 条旧流量`)
    }
    currentPage.value = 1
    await loadFlows()
    loadStats()
  } catch (e: any) {
    if (e === 'cancel' || e?.toString?.().includes('cancel')) return
    ElMessage.error('清理失败：' + (e?.message || e))
  } finally {
    clearLoading.value = false
  }
}

// 请求 Raw 文本（用于请求分析详情）
const requestRaw = computed(() => {
  const f = selectedFlow.value
  if (!f) return ''
  const headers = f.request_headers || '{}'
  let headerStr = ''
  try {
    const obj = JSON.parse(headers)
    headerStr = Object.entries(obj).map(([k, v]) => `${k}: ${v}`).join('\n')
  } catch {
    headerStr = headers
  }
  const line = `${f.method} ${f.url} HTTP/1.1`
  const body = f.request_body || ''
  return body ? `${line}\n${headerStr}\n\n${body}` : `${line}\n${headerStr}`
})

// 响应 Content-Type（供 PreviewView）
const responseContentType = computed(() => {
  const f = selectedFlow.value
  return f ? extractContentType(f) : ''
})

// 响应 Raw 文本（无响应体时自动显示，用于响应分析 fallback）
const responseRaw = computed(() => {
  const f = selectedFlow.value
  if (!f) return ''
  const headers = f.response_headers || '{}'
  let headerStr = ''
  try {
    const obj = JSON.parse(headers)
    headerStr = Object.entries(obj).map(([k, v]) => `${k}: ${v}`).join('\n')
  } catch {
    headerStr = headers
  }
  const statusLine = f.status_code ? `HTTP/1.1 ${f.status_code}` : 'HTTP/1.1'
  const body = f.response_body || ''
  return body ? `${statusLine}\n${headerStr}\n\n${body}` : `${statusLine}\n${headerStr}`
})

onMounted(async () => {
  // 检查 query 是否有 host 参数（从抓包页面跳转过来时传入）
  const queryHost = route.query.host as string | undefined
  if (queryHost) {
    groupBy.value = 'host'
    filterHost.value = queryHost
  }

  // 并行加载流量和统计，避免第一次渲染 groupedFlows 时 statsGroups 为空导致按本页 count 排序
  await Promise.all([
    (async () => {
      await loadFlows()
      if (flows.selectedFlow) {
        selectedFlowId.value = flows.selectedFlow.id
      }
    })(),
    loadStats(),
  ])

  // 从抓包页面跳转过来且有 host 参数：自动展开对应 host 的分组并滚动到该分组
  if (queryHost) {
    expandedGroups.value.add(queryHost)
    await nextTick()
    const el = document.querySelector(`[data-group-key="${CSS.escape(queryHost)}"]`) as HTMLElement
    if (el) {
      // 找最近的可滚动祖先，只滚动它（避免 scrollIntoView 冒泡到 body 导致整页上移）
      let parent = el.parentElement
      while (parent) {
        const st = getComputedStyle(parent)
        if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && parent.scrollHeight > parent.clientHeight) {
          const pRect = parent.getBoundingClientRect()
          const eRect = el.getBoundingClientRect()
          const elTop = eRect.top - pRect.top + parent.scrollTop
          parent.scrollTo({ top: Math.max(0, elTop - parent.clientHeight / 2 + el.offsetHeight / 2), behavior: 'smooth' })
          break
        }
        parent = parent.parentElement
      }
    }
  }
})

// 组件卸载时停止时序图轮询，避免泄漏定时器
onBeforeUnmount(() => _stopTimelineRefresh())
</script>

<template>
  <div class="analyze-view full flex flex-col">
    <div class="page-header">
      <div class="page-title"><el-icon><DataAnalysis /></el-icon>&nbsp;全局分析</div>
      <div class="header-actions">
        <el-button size="small" @click="() => { loadFlows(); loadStats() }" :loading="loading">
          <el-icon><Refresh /></el-icon>&nbsp;刷新
        </el-button>
        <ImportButton @imported="() => { loadFlows(); loadStats() }" />
        <el-button size="small" @click="onClearFlows('before')" :loading="clearLoading" title="删除当前页之前的旧流量，保留最近数据">
          <el-icon><Delete /></el-icon>&nbsp;清理旧数据
        </el-button>
        <el-button size="small" type="danger" @click="onClearFlows('all')" :loading="clearLoading">
          <el-icon><Delete /></el-icon>&nbsp;清空全部
        </el-button>
      </div>
    </div>

    <!-- 工具栏：分组方式 + 视图切换 -->
    <div class="analyze-toolbar">
      <span class="text-dim" style="font-size: 12px">分组：</span>
      <el-radio-group v-model="groupBy" size="small">
        <el-radio-button value="host">按 Host</el-radio-button>
        <el-radio-button value="process">按进程</el-radio-button>
        <el-radio-button value="content_type">按 Content-Type</el-radio-button>
        <el-radio-button value="status_code">按状态码</el-radio-button>
        <el-radio-button value="ip_region">按 IP 属地</el-radio-button>
      </el-radio-group>
      <div class="flex-1"></div>
      <!-- 排序切换（所有视图模式都显示，影响列表分组顺序和图表顺序） -->
      <el-button
        size="small"
        @click="toggleSortOrder"
        :title="sortOrder === 'desc' ? '当前：占比大到小，点击切换为小到大' : '当前：占比小到大，点击切换为大到小'"
      >
        <el-icon><Sort /></el-icon>&nbsp;{{ sortOrder === 'desc' ? '大到小' : '小到大' }}
      </el-button>
      <!-- 视图切换：列表 / 条形图 / 饼图 / 时序图（图表显示全量数据，换页不换图） -->
      <el-radio-group v-model="viewMode" size="small" style="margin-left: 8px">
        <el-radio-button value="list">列表</el-radio-button>
        <el-radio-button value="bar">条形图</el-radio-button>
        <el-radio-button value="pie">饼图</el-radio-button>
        <el-radio-button value="timeline">时序图</el-radio-button>
      </el-radio-group>
      <span class="text-dim" style="font-size: 12px; margin-left: 12px">本页 {{ allFlows.length }} 条 / 共 {{ totalCount }} 条</span>
    </div>

    <!-- 过滤栏 -->
    <div class="filter-bar">
      <span class="text-dim" style="font-size: 12px">过滤：</span>
      <el-input v-model="filterHost" placeholder="host 模糊匹配" size="small" clearable style="width: 200px" @keyup.enter="applyFilter" @clear="applyFilter" />
      <el-input v-model="filterProcess" placeholder="进程名模糊匹配" size="small" clearable style="width: 180px" @keyup.enter="applyFilter" @clear="applyFilter" />
      <el-button size="small" type="primary" plain @click="applyFilter">应用</el-button>
      <el-button size="small" @click="clearFilter">重置</el-button>
      <div class="flex-1"></div>
    </div>

    <!-- 主体：条形图视图（viewMode === 'bar'）— 连续堆叠条形图，显示全量数据比例，换页不换图 -->
    <div v-if="viewMode === 'bar'" class="analyze-body chart-body flex-1 overflow-auto" @scroll="onChartScroll">
      <div v-if="statsLoading" class="empty-text text-dim">统计加载中...</div>
      <div v-else-if="!statsGroups.length" class="empty-text text-dim">（无流量数据）</div>
      <div v-else class="bar-container">
        <!-- 连续堆叠条形：所有分组在一个条形里，宽度按 count 比例 -->
        <div class="stack-bar">
          <div
            v-for="(g, i) in statsGroups"
            :key="g.key"
            class="stack-segment"
            :style="{ width: (g.count / statsTotal * 100) + '%', background: groupColor(i) }"
            @click="onChartBarClick(g)"
            @mouseenter="onSegmentHover($event, g, i)"
            @mousemove="onSegmentMove($event)"
            @mouseleave="onSegmentLeave"
          ></div>
        </div>
        <!-- 图例 -->
        <div class="bar-legend">
          <div
            v-for="(g, i) in statsGroups"
            :key="g.key"
            class="legend-item"
            @click="onChartBarClick(g)"
          >
            <span class="legend-dot" :style="{ background: groupColor(i) }"></span>
            <span class="legend-label text-truncate">{{ g.label }}</span>
            <span class="legend-count mono text-dim">{{ g.count }}</span>
            <span class="legend-pct mono text-dim">{{ (g.count / statsTotal * 100).toFixed(1) }}%</span>
          </div>
        </div>
      </div>
      <!-- 浮动 tooltip（跟随鼠标，自动避免屏外） -->
      <div
        v-if="hoverTip.visible"
        class="bar-tooltip mono"
        :style="{ left: hoverTip.x + 'px', top: hoverTip.y + 'px' }"
      >
        <div class="tt-label" :style="{ color: hoverTip.color }">{{ hoverTip.label }}</div>
        <div class="tt-row"><span class="tt-k">数量</span><span class="tt-v">{{ hoverTip.count }}</span></div>
        <div class="tt-row"><span class="tt-k">占比</span><span class="tt-v">{{ hoverTip.pct }}%</span></div>
      </div>
    </div>

    <!-- 主体：饼图视图（viewMode === 'pie'）— conic-gradient 饼图，显示全量数据比例，换页不换图 -->
    <div v-else-if="viewMode === 'pie'" class="analyze-body chart-body flex-1 overflow-auto">
      <div v-if="statsLoading" class="empty-text text-dim">统计加载中...</div>
      <div v-else-if="!statsGroups.length" class="empty-text text-dim">（无流量数据）</div>
      <div v-else class="pie-container">
        <div class="pie-wrap">
          <div class="pie-chart" :style="{ background: pieGradient }">
            <div class="pie-hole">
              <div class="pie-center-count mono">{{ statsTotal }}</div>
              <div class="pie-center-label text-dim">总流量</div>
            </div>
          </div>
          <div class="pie-legend">
            <div
              v-for="(g, i) in statsGroups"
              :key="g.key"
              class="legend-item"
              @click="onChartBarClick(g)"
              :title="`点击查看「${g.label}」分组详情`"
            >
              <span class="legend-dot" :style="{ background: groupColor(i) }"></span>
              <span class="legend-label text-truncate">{{ g.label }}</span>
              <span class="legend-count mono text-dim">{{ g.count }}</span>
              <span class="legend-pct mono text-dim">{{ (g.count / statsTotal * 100).toFixed(1) }}%</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 主体：时序图视图（viewMode === 'timeline'）— SVG 折线图，每秒 QPS 和字节数 -->
    <div v-else-if="viewMode === 'timeline'" class="analyze-body chart-body flex-1 overflow-auto timeline-view">
      <div v-if="allFlows.length < 2" class="empty-text text-dim">（流量少于 2 条，无法生成时序图）</div>
      <div v-else class="timeline-container">
        <div class="timeline-controls">
          <span class="text-dim" style="font-size: 12px;">粒度：</span>
          <el-radio-group v-model="timelineGranularity" size="small">
            <el-radio-button value="1">1秒</el-radio-button>
            <el-radio-button value="5">5秒</el-radio-button>
            <el-radio-button value="60">1分钟</el-radio-button>
          </el-radio-group>
          <span class="text-dim" style="font-size: 12px; margin-left: 16px;">指标：</span>
          <el-radio-group v-model="timelineMetric" size="small">
            <el-radio-button value="qps">QPS</el-radio-button>
            <el-radio-button value="bytes">字节</el-radio-button>
          </el-radio-group>
          <span class="text-dim timeline-summary">
            总 {{ allFlows.length }} 条 / {{ timelineBuckets.length }} 个时间桶 / 峰值 {{ timelineMaxBucket }}{{ timelineMetric === 'qps' ? ' req' : ' B' }}
          </span>
        </div>
        <div class="timeline-svg-wrap">
          <svg :viewBox="`0 0 ${timelineWidth} ${timelineHeight}`" preserveAspectRatio="xMidYMid meet" class="timeline-svg">
            <!-- 网格线 -->
            <line v-for="(g, i) in timelineYGrids" :key="'grid-' + i"
                  :x1="timelinePadding.left" :y1="g.y" :x2="timelineWidth - timelinePadding.right" :y2="g.y"
                  stroke="var(--on-border-light)" stroke-width="1" stroke-dasharray="2 4" />
            <!-- Y 轴刻度 -->
            <text v-for="(g, i) in timelineYGrids" :key="'y-' + i"
                  :x="timelinePadding.left - 8" :y="g.y + 4" text-anchor="end"
                  fill="var(--on-text-dim)" font-size="11" font-family="var(--on-font-mono, Menlo, monospace)">
              {{ g.label }}
            </text>
            <!-- QPS 折线（青色） -->
            <polyline v-if="timelineMetric === 'qps'"
                      :points="timelineQpsPoints" fill="none"
                      stroke="var(--on-accent, #2dd4bf)" stroke-width="2" />
            <!-- Bytes 折线（蓝色） -->
            <polyline v-else
                      :points="timelineBytesPoints" fill="none"
                      stroke="#3b82f6" stroke-width="2" />
            <!-- 区域填充 -->
            <polygon v-if="timelineMetric === 'qps' && timelineQpsAreaPoints"
                     :points="timelineQpsAreaPoints"
                     fill="var(--on-accent, #2dd4bf)" fill-opacity="0.1" />
            <polygon v-else-if="timelineBytesAreaPoints"
                     :points="timelineBytesAreaPoints"
                     fill="#3b82f6" fill-opacity="0.1" />
            <!-- X 轴刻度 -->
            <text v-for="(label, i) in timelineXLabels" :key="'x-' + i"
                  :x="timelinePadding.left + i * (timelineWidth - timelinePadding.left - timelinePadding.right) / (timelineXLabels.length - 1 || 1)"
                  :y="timelineHeight - timelinePadding.bottom + 16" text-anchor="middle"
                  fill="var(--on-text-dim)" font-size="11" font-family="var(--on-font-mono, Menlo, monospace)">
              {{ label }}
            </text>
          </svg>
        </div>
      </div>
    </div>

    <!-- 主体：列表视图（viewMode === 'list'）+ 右侧详情 -->
    <div v-else class="analyze-body flex-1 flex overflow-hidden">
      <div class="group-list">
        <div v-if="loading" class="empty-text text-dim">加载中...</div>
        <div v-else-if="!allFlows.length" class="empty-text text-dim">
          （无流量数据，请先抓包或在过滤栏调整条件）
        </div>
        <div v-for="g in groupedFlows" :key="g.key" class="group-item" :class="{ 'group-empty': !g.hasPageFlows }" :data-group-key="g.key">
          <div class="group-header" @click="toggleGroup(g.key)">
            <el-icon class="expand-icon" :class="{ expanded: expandedGroups.has(g.key) }"><ArrowRight /></el-icon>
            <span class="group-label">{{ g.label }}</span>
            <span class="group-pct">{{ g.globalPct.toFixed(1) }}%</span>
            <span class="group-count" :title="`全局 ${g.globalCount} 条 / 本页 ${g.count} 条`">{{ g.globalCount }}</span>
            <span v-if="g.count !== g.globalCount" class="group-page-count text-dim" :title="`本页 ${g.count} 条`">·页{{ g.count }}</span>
          </div>
          <div v-if="expandedGroups.has(g.key) && g.hasPageFlows" class="group-flows">
            <div
              v-for="f in g.flows"
              :key="f.id"
              class="flow-row"
              :class="{ selected: f.id === selectedFlowId }"
              @click="onFlowClick(f)"
              @dblclick="onFlowDblClick(f)"
            >
              <span class="flow-id mono text-dim">{{ f.id }}</span>
              <span class="flow-method mono" :class="'m-' + (f.method || '').toLowerCase()">{{ f.method }}</span>
              <span class="flow-host mono text-truncate">{{ f.host }}{{ f.path }}</span>
              <span v-if="f.status_code !== null" class="flow-status mono">{{ f.status_code }}</span>
              <span v-if="bpBadge(f)" class="flow-bp">⚠ {{ bpBadge(f) }}</span>
            </div>
          </div>
          <div v-else-if="expandedGroups.has(g.key) && !g.hasPageFlows" class="group-empty-hint text-dim">
            （本页无该分组的流量，切换页码或查看其他页）
          </div>
        </div>
      </div>

      <div class="detail-pane">
        <div v-if="!selectedFlow" class="empty-text text-dim">
          （单击左侧流量查看详情，双击跳转抓包页面）
        </div>
        <template v-else>
          <!-- 顶部条：分析模式 + 标题 + 关闭按钮 -->
          <div class="detail-header">
            <el-radio-group v-model="analyzeMode" size="small">
              <el-radio-button value="response">响应</el-radio-button>
              <el-radio-button value="request">请求</el-radio-button>
            </el-radio-group>
            <span class="detail-title text-dim mono">
              #{{ selectedFlow.id }} {{ selectedFlow.method }} {{ selectedFlow.host }}{{ selectedFlow.path }}
            </span>
            <div class="flex-1"></div>
            <el-button size="small" circle plain @click="closeDetail" title="关闭">
              <el-icon><Close /></el-icon>
            </el-button>
          </div>
          <!-- 请求分析：显示请求 Raw（代码高亮 + 搜索 + 右键解码） -->
          <div v-if="analyzeMode === 'request'" class="detail-content">
            <TextSearch :text="requestRaw" searchable language="http" class="detail-code-wrap" />
          </div>
          <!-- 响应分析：有响应体时显示 Preview，无响应体时自动显示 Raw -->
          <div v-else class="detail-content">
            <PreviewView v-if="selectedFlow.response_body" :body="selectedFlow.response_body" :content-type="responseContentType" />
            <TextSearch v-else :text="responseRaw" searchable language="http" class="detail-code-wrap" />
          </div>
        </template>
      </div>
    </div>

    <!-- 无分页栏：全局分析一页显示全部流量 -->
  </div>
</template>

<style scoped>
.analyze-view { background: var(--on-bg); }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 16px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.page-title { font-size: 15px; font-weight: 600; color: var(--on-text); display: flex; align-items: center; }
.header-actions { display: flex; gap: 6px; }
.analyze-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.toolbar-sep { width: 1px; height: 20px; background: var(--on-border); margin: 0 4px; }
.filter-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}

.analyze-body { min-height: 0; }
.group-list {
  width: 45%; min-width: 300px;
  overflow-y: auto; padding: 6px;
  border-right: 1px solid var(--on-border-light);
}
.empty-text { text-align: center; padding: 24px; }
.group-item { margin-bottom: 2px; }
.group-item.group-empty .group-header { opacity: 0.55; }
.group-item.group-empty .group-header:hover { opacity: 0.85; }
.group-empty-hint { padding: 4px 8px 4px 26px; font-size: 11px; font-style: italic; }
.group-header {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 8px; cursor: pointer;
  border-radius: 4px; font-size: 13px;
  color: var(--on-text);
  transition: background .12s ease;
}
.group-header:hover { background: var(--on-bg-hover); }
.expand-icon { transition: transform .15s ease; font-size: 12px; color: var(--on-text-muted); }
.expand-icon.expanded { transform: rotate(90deg); }
.group-label { flex: 1; font-weight: 500; }
.group-pct {
  font-size: 12px; font-weight: 600;
  color: var(--on-accent);
  font-family: var(--on-font-mono);
  flex-shrink: 0;
  min-width: 48px; text-align: right;
}
.group-count {
  font-size: 11px; color: var(--on-text-muted);
  background: var(--on-bg-hover); padding: 1px 6px; border-radius: 10px;
  font-family: var(--on-font-mono);
  flex-shrink: 0;
}
.group-page-count {
  font-size: 10px; flex-shrink: 0;
  font-family: var(--on-font-mono);
}
.group-flows { padding-left: 18px; }
.flow-row {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 8px; cursor: pointer;
  border-radius: 3px; font-size: 12px;
  color: var(--on-text);
  transition: background .1s ease;
}
.flow-row:hover { background: var(--on-bg-hover); }
.flow-row.selected { background: rgba(64, 158, 255, 0.18); }
.flow-id { width: 40px; color: var(--on-text-muted); flex-shrink: 0; }
.flow-method {
  width: 50px; flex-shrink: 0; font-size: 10px; font-weight: 700;
  padding: 1px 5px; border-radius: 3px; text-align: center;
}
.m-get { color: var(--on-success); background: rgba(46,160,67,0.12); border: 1px solid rgba(46,160,67,0.4); }
.m-post { color: var(--on-accent); background: rgba(88,166,255,0.12); border: 1px solid rgba(88,166,255,0.4); }
.m-put { color: var(--on-warn); background: rgba(210,153,34,0.12); border: 1px solid rgba(210,153,34,0.4); }
.m-delete { color: var(--on-error); background: rgba(248,81,73,0.12); border: 1px solid rgba(248,81,73,0.4); }
.flow-host { flex: 1; min-width: 0; color: var(--on-text-muted); }
.text-truncate { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.flow-status {
  width: 36px; flex-shrink: 0; text-align: right;
  color: var(--on-text-muted);
}
.flow-bp {
  font-size: 10px; color: var(--on-warn);
  padding: 1px 5px; border: 1px solid var(--on-warn); border-radius: 3px;
  flex-shrink: 0;
}

.detail-pane {
  flex: 1; min-width: 0; overflow: hidden;
  display: flex; flex-direction: column;
}
.detail-header {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.detail-title {
  font-size: 12px;
  /* 单行横向滚动，保证不换行也不挤压请求/响应切换按钮 */
  overflow-x: auto; overflow-y: hidden;
  white-space: nowrap;
  flex: 1 1 auto; min-width: 80px; max-width: calc(100% - 220px);
  scrollbar-width: none; /* Firefox：默认隐藏滚动条 */
}
.detail-title::-webkit-scrollbar { height: 0; }
.detail-title::-webkit-scrollbar-thumb { background: transparent; border-radius: 2px; }
.detail-title::-webkit-scrollbar-track { background: transparent; }
/* hover 时显示 4px 小滚动条 */
.detail-title:hover { scrollbar-width: thin; }
.detail-title:hover::-webkit-scrollbar { height: 4px; }
.detail-title:hover::-webkit-scrollbar-thumb { background: var(--on-border-light, rgba(128,128,128,.3)); border-radius: 2px; }
.detail-content { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.detail-pre {
  flex: 1; margin: 0; padding: 10px 12px;
  font-size: 12.5px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all;
  color: var(--on-text); overflow: auto;
  background: var(--on-bg);
}
/* TextSearch 代码块容器 */
.detail-code-wrap {
  flex: 1; height: 0;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
}

/* 统计图视图（连续堆叠条形图）*/
.chart-body { padding: 12px; background: var(--on-bg); }
.bar-container {
  max-width: 1000px; margin: 0 auto; padding: 12px;
}
.bar-hint { font-size: 12px; margin-bottom: 12px; padding: 6px 10px; background: var(--on-bg-elevated); border-radius: 4px; line-height: 1.6; }
.stack-bar {
  display: flex; height: 36px; border-radius: 6px; overflow: hidden;
  border: 1px solid var(--on-border);
  margin-bottom: 16px;
}
.stack-segment {
  cursor: pointer; transition: opacity .12s ease, transform .12s ease;
  min-width: 1px;
}
.stack-segment:hover { opacity: 0.75; transform: scaleY(1.08); }
.bar-legend {
  display: flex; flex-direction: column; gap: 2px;
}
.legend-item {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 8px; cursor: pointer; border-radius: 3px;
  font-size: 12.5px; transition: background .12s ease;
}
.legend-item:hover { background: var(--on-bg-hover); }
.legend-dot {
  width: 12px; height: 12px; border-radius: 2px; flex-shrink: 0;
}
.legend-label { flex: 1; min-width: 0; color: var(--on-text); }
.legend-count { font-size: 12px; font-weight: 600; }
.legend-pct { font-size: 11px; width: 48px; text-align: right; }

/* 条形图浮动 tooltip */
.bar-tooltip {
  position: absolute;
  z-index: 1000;
  pointer-events: none;
  min-width: 140px;
  padding: 8px 10px;
  background: var(--on-bg-elevated, #2a2a3a);
  border: 1px solid var(--on-border, #444);
  border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
  font-size: 12px;
  line-height: 1.5;
}
.bar-tooltip .tt-label {
  font-weight: 600;
  margin-bottom: 4px;
  word-break: break-all;
  max-width: 240px;
}
.bar-tooltip .tt-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.bar-tooltip .tt-k { color: var(--on-text-muted, #888); }
.bar-tooltip .tt-v { color: var(--on-text, #d4d4d4); font-weight: 600; }

/* 饼图视图（conic-gradient 环形图，显示全量比例）*/
.pie-container {
  max-width: 1000px; margin: 0 auto; padding: 12px;
}

/* 时序图视图（SVG 折线图） */
.timeline-view {
  display: flex;
  flex-direction: column;
}
.timeline-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px;
}
.timeline-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.timeline-summary {
  margin-left: auto;
  font-size: 12px;
}
.timeline-svg-wrap {
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-md, 6px);
  padding: 12px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}
.timeline-svg {
  width: 100%;
  height: auto;
  display: block;
}
.pie-wrap {
  display: flex; align-items: center; gap: 24px;
  flex-wrap: wrap; justify-content: center;
}
.pie-chart {
  width: 260px; height: 260px; border-radius: 50%;
  position: relative; flex-shrink: 0;
  border: 1px solid var(--on-border);
  box-shadow: 0 4px 16px rgba(0,0,0,0.25);
  transition: transform .2s ease;
}
.pie-chart:hover { transform: scale(1.02); }
.pie-hole {
  position: absolute; top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  width: 130px; height: 130px; border-radius: 50%;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}
.pie-center-count {
  font-size: 24px; font-weight: 700;
  color: var(--on-text); line-height: 1.2;
}
.pie-center-label {
  font-size: 11px; margin-top: 4px;
}
.pie-legend {
  display: flex; flex-direction: column; gap: 2px;
  min-width: 280px; max-width: 480px; flex: 1;
}
</style>
