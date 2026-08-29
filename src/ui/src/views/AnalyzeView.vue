<script setup lang="ts">
import { computed, ref, shallowRef, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
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
const { t } = useI18n()

type GroupBy = 'host' | 'process' | 'content_type' | 'status_code' | 'ip_region'
const groupBy = ref<GroupBy>('host')
// 请求分析 vs 响应分析：影响右侧详情显示内容
const analyzeMode = ref<'request' | 'response'>('response')
// 视图模式：list 列表 / bar 条形图 / pie 饼图（图表显示全量数据，换页不换图）
// 新增高级视图：latency 延迟分析 / cross 交叉分析 / anomalies 异常检测
const viewMode = ref<'list' | 'bar' | 'pie' | 'timeline' | 'heatmap' | 'topology' | 'latency' | 'cross' | 'anomalies'>('list')

// ========== P1 高级分析状态 ==========
// 延迟分析数据
interface LatencyStats {
  histogram: { range: string; label: string; count: number; pct: number }[]
  percentiles: { p50: number; p75: number; p90: number; p95: number; p99: number }
  total: number
  avg_ms: number
  slow_count: number
  slow_pct: number
}
const latencyStats = ref<LatencyStats | null>(null)
const latencyLoading = ref(false)
// 交叉分析数据
interface CrossAnalysis {
  host_status: { dimensions: string[]; buckets: string[]; matrix: number[][] }
  content_size: { dimensions: string[]; buckets: string[]; matrix: number[][] }
}
const crossAnalysis = ref<CrossAnalysis | null>(null)
const crossLoading = ref(false)
// 交叉矩阵 X/Y 轴切换
const crossAxisX = ref<'host_status' | 'content_size'>('host_status')
const crossAxisY = ref<'status' | 'size'>('status')
// 异常检测数据
interface Anomaly {
  type: string
  severity: 'info' | 'warning' | 'error'
  message: string
  details: Record<string, any>
  count: number
}
interface AnomalyResult {
  anomalies: Anomaly[]
  summary: { total: number; error_count: number; error_rate: number; avg_ms: number; anomaly_count: number }
}
const anomalyResult = ref<AnomalyResult | null>(null)
const anomalyLoading = ref(false)
const anomaliesExpanded = ref(true)

// 过滤条件（必须在 loadLatencyStats 等函数之前定义）
const filterHost = ref('')
const filterProcess = ref('')

// 加载高级分析数据
async function loadLatencyStats() {
  latencyLoading.value = true
  try {
    const res: any = await api.getLatencyStats({
      host: filterHost.value || undefined,
      process: filterProcess.value || undefined,
    })
    latencyStats.value = res
  } catch (e: any) {
    ElMessage.error(t('analyze.statsLoadFailed') + (e?.message || e))
    latencyStats.value = null
  } finally {
    latencyLoading.value = false
  }
}

async function loadCrossAnalysis() {
  crossLoading.value = true
  try {
    const res: any = await api.getCrossAnalysis({
      host: filterHost.value || undefined,
      process: filterProcess.value || undefined,
    })
    crossAnalysis.value = res
  } catch (e: any) {
    ElMessage.error(t('analyze.statsLoadFailed') + (e?.message || e))
    crossAnalysis.value = null
  } finally {
    crossLoading.value = false
  }
}

async function loadAnomalies() {
  anomalyLoading.value = true
  try {
    const res: any = await api.getAnomalies({
      host: filterHost.value || undefined,
      process: filterProcess.value || undefined,
    })
    anomalyResult.value = res
  } catch (e: any) {
    ElMessage.error(t('analyze.statsLoadFailed') + (e?.message || e))
    anomalyResult.value = null
  } finally {
    anomalyLoading.value = false
  }
}

// 导出报表
async function exportReport(format: 'json' | 'csv' | 'html') {
  try {
    const blob = await api.exportReport(format, {
      host: filterHost.value || undefined,
      process: filterProcess.value || undefined,
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `telnix_report.${format}`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success(t('analyze.exportSuccess') || `导出成功: telnix_report.${format}`)
  } catch (e: any) {
    ElMessage.error(t('analyze.exportFailed') + (e?.message || e))
  }
}

// 延迟柱状图颜色（按范围渐变）
function latencyBarColor(range: string): string {
  if (range === '<50ms') return 'var(--on-success, #52c41a)'
  if (range === '50-200ms') return 'var(--on-accent, #409eff)'
  if (range === '200-500ms') return 'var(--on-amber, #f59e0b)'
  if (range === '500ms-1s') return '#f97316'
  return 'var(--on-error, #f56c6c)'
}

// 异常严重性颜色
function anomalyColor(severity: string): string {
  if (severity === 'error') return 'var(--on-error, #f56c6c)'
  if (severity === 'warning') return 'var(--on-amber, #f59e0b)'
  return 'var(--on-accent, #409eff)'
}

// 热力图颜色（交叉分析）
function crossColor(val: number, max: number): string {
  if (!val) return 'transparent'
  const ratio = Math.min(1, val / max)
  const alpha = 0.1 + ratio * 0.9
  return `rgba(64, 158, 255, ${alpha.toFixed(2)})`
}

// 交叉分析最大值
const crossMax = computed(() => {
  const data = crossAnalysis.value
  if (!data) return 1
  const matrix = crossAxisX.value === 'host_status' ? data.host_status.matrix : data.content_size.matrix
  let m = 1
  for (const row of matrix) {
    for (const v of row) if (v > m) m = v
  }
  return m
})

// 获取交叉矩阵
const crossMatrix = computed(() => {
  const data = crossAnalysis.value
  if (!data) return { dimensions: [], buckets: [], matrix: [] }
  return crossAxisX.value === 'host_status' ? data.host_status : data.content_size
})

// 切换视图时加载对应数据
watch(viewMode, (v) => {
  if (v === 'heatmap') loadHeatmap()
  if (v === 'topology') loadTopology()
  if (v === 'latency') loadLatencyStats()
  if (v === 'cross') loadCrossAnalysis()
  if (v === 'anomalies') loadAnomalies()
})

// 过滤条件变化时刷新高级分析
watch([filterHost, filterProcess], () => {
  if (viewMode.value === 'latency') loadLatencyStats()
  if (viewMode.value === 'cross') loadCrossAnalysis()
  if (viewMode.value === 'anomalies') loadAnomalies()
})

// 统计图颜色板（统一使用 CSS 变量）
const CHART_COLORS = [
  'var(--on-accent)', 'var(--on-amber)', 'var(--on-purple)', 'var(--on-rose)', 'var(--on-blue)',
  'var(--on-emerald)', 'var(--on-pink)', 'var(--on-indigo)', 'var(--on-cyan)', 'var(--on-text-dim)',
  '#84cc16', 'var(--on-cyan)', '#a855f7', 'var(--on-amber)', 'var(--on-text-dim)',
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
    ElMessage.error(t('analyze.statsLoadFailed') + (e?.message || e))
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

// ===== 热力图（heatmap）=====
interface HeatmapBucket { epoch: number; label: string; full: string }
interface HeatmapDim { key: string; label: string; total: number }
const heatmapData = ref<{
  buckets: HeatmapBucket[]
  dimensions: HeatmapDim[]
  matrix: number[][]
  total: number
}>({ buckets: [], dimensions: [], matrix: [], total: 0 })
const heatmapLoading = ref(false)
// 热力图分桶粒度（秒）：60=1分钟 / 300=5分钟 / 600=10分钟
const heatmapBucketSec = ref<number>(60)
// 热力图复用 groupBy（host/process/method/status_range/ip_region）

async function loadHeatmap() {
  heatmapLoading.value = true
  try {
    const res: any = await api.getFlowsHeatmap(
      groupBy.value === 'content_type' ? 'host' : groupBy.value,
      heatmapBucketSec.value,
      120,
      20,
      filterHost.value || undefined,
      filterProcess.value || undefined,
    )
    heatmapData.value = {
      buckets: res.buckets || [],
      dimensions: res.dimensions || [],
      matrix: res.matrix || [],
      total: res.total || 0,
    }
  } catch (e: any) {
    ElMessage.error(t('analyze.statsLoadFailed') + (e?.message || e))
    heatmapData.value = { buckets: [], dimensions: [], matrix: [], total: 0 }
  } finally {
    heatmapLoading.value = false
  }
}

// 热力图最大值（用于颜色映射）
const heatmapMax = computed(() => {
  let m = 0
  for (const row of heatmapData.value.matrix) {
    for (const v of row) if (v > m) m = v
  }
  return m || 1
})

// 热力图颜色：0 → 透明，max → 高亮色（accent）
function heatmapColor(v: number): string {
  if (!v) return 'transparent'
  const ratio = Math.min(1, v / heatmapMax.value)
  // 从浅到深：rgba(accent) 透明度 0.15 → 1.0
  const alpha = 0.15 + ratio * 0.85
  return `rgba(64, 158, 255, ${alpha.toFixed(2)})`
}

// 切换到热力图时加载数据
watch(viewMode, (v) => {
  if (v === 'heatmap') loadHeatmap()
  if (v === 'topology') loadTopology()
})
watch(heatmapBucketSec, () => {
  if (viewMode.value === 'heatmap') loadHeatmap()
})
// groupBy 或过滤器变化时，若当前是热力图则重新加载
watch(groupBy, () => {
  if (viewMode.value === 'heatmap') loadHeatmap()
})

// ===== 网络拓扑图（topology）=====
interface TopoNode { id: string; label: string; type: string; count: number; x?: number; y?: number }
interface TopoEdge { source: string; target: string; count: number; size: number }
const topoData = ref<{ nodes: TopoNode[]; edges: TopoEdge[] }>({ nodes: [], edges: [] })
const topoLoading = ref(false)

async function loadTopology() {
  topoLoading.value = true
  try {
    const res: any = await api.getFlowsTopology(
      filterHost.value || undefined,
      filterProcess.value || undefined,
      80,
    )
    const nodes = (res.nodes || []) as TopoNode[]
    // 分层布局：process 左 / IP 中 / host 右，每层内均匀垂直分布
    // 动态高度：保证节点间至少 40px 间距
    const processes = nodes.filter(n => n.type === 'process')
    const ips = nodes.filter(n => n.type === 'ip')
    const hosts = nodes.filter(n => n.type === 'host')
    const maxNodes = Math.max(processes.length, ips.length, hosts.length)
    const W = 900
    const MIN_H = 600
    const NODE_GAP = 50  // 节点间最小间距（px）
    const H = Math.max(MIN_H, maxNodes * NODE_GAP + 120)  // 动态高度
    const colX = { process: 150, ip: 450, host: 750 }
    function layoutColumn(items: TopoNode[], x: number) {
      const n = items.length
      if (n === 0) return
      if (n === 1) {
        items[0].x = x
        items[0].y = H / 2
        return
      }
      // 均匀分布在 60 ~ H-60 之间，保证最小间距
      const top = 60, bottom = H - 60
      const step = Math.max(NODE_GAP, (bottom - top) / (n - 1))
      const totalH = (n - 1) * step
      const startY = (H - totalH) / 2
      items.forEach((node, i) => {
        node.x = x
        node.y = top + step * i
      })
    }
    layoutColumn(processes, colX.process)
    layoutColumn(ips, colX.ip)
    layoutColumn(hosts, colX.host)
    topoData.value = { nodes, edges: res.edges || [] }
  } catch (e: any) {
    ElMessage.error(t('analyze.statsLoadFailed') + (e?.message || e))
    topoData.value = { nodes: [], edges: [] }
  } finally {
    topoLoading.value = false
  }
}

// 拓扑图节点颜色
function topoNodeColor(type: string): string {
  if (type === 'process') return 'var(--on-accent)'
  if (type === 'ip') return 'var(--on-amber, #f59e0b)'
  return 'var(--on-success)'
}

// 节点查找
function findNode(id: string): TopoNode | undefined {
  return topoData.value.nodes.find(n => n.id === id)
}

// 边的贝塞尔曲线路径（水平 S 形曲线，比直线更清晰）
function edgePath(e: TopoEdge): string {
  const s = findNode(e.source)
  const t = findNode(e.target)
  if (!s || !t || s.x == null || s.y == null || t.x == null || t.y == null) return ''
  const x1 = s.x, y1 = s.y, x2 = t.x, y2 = t.y
  // 控制点：水平偏移产生 S 形曲线
  const dx = Math.abs(x2 - x1) * 0.4
  const cx1 = x1 + dx, cy1 = y1
  const cx2 = x2 - dx, cy2 = y2
  return `M ${x1},${y1} C ${cx1},${cy1} ${cx2},${cy2} ${x2},${y2}`
}

// ===== 时序图（timeline）=====
// 粒度（秒）：1 / 5 / 60
const timelineGranularity = ref<number>(1)
// 指标：qps（每秒请求数）或 bytes（每秒字节）
const timelineMetric = ref<'qps' | 'bytes'>('qps')
// SVG 尺寸（高分辨率，占满全屏宽度）
const timelineWidth = 1600
const timelineHeight = 480
const timelinePadding = { top: 20, right: 50, bottom: 30, left: 50 }

// 时间桶：按粒度分桶统计 count 和 bytes
// 性能优化：仅在 timeline 视图时计算，避免其他视图下无谓遍历 5000 条
const timelineBuckets = computed(() => {
  if (viewMode.value !== 'timeline') return []  // 懒计算：非 timeline 视图跳过
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
const allFlows = shallowRef<Flow[]>([])
const totalCount = ref(0)
const loading = ref(false)
const currentPage = ref(1)
// 全局分析：加载上限 5000 条（与 store MAX_FLOWS 对齐），避免全量加载 10w 条导致 1-2s 卡顿。
// store 已有缓存时直接复用，实现进入页面即时渲染。
const pageSize = ref(5000)
const clearLoading = ref(false)

// 展开的分组 key 集合
const expandedGroups = ref<Set<string>>(new Set())
// 分组展开时每组初始渲染的最大行数（避免单个大分组展开瞬间创建数千 DOM 导致卡顿）。
// 超出部分通过「显示更多」按需追加。
const GROUP_ROW_STEP = 200
// key -> 当前该分组已渲染的行数上限（点击「显示更多」时递增）
const groupRowLimit = ref<Record<string, number>>({})
function groupRowCap(key: string): number {
  return groupRowLimit.value[key] ?? GROUP_ROW_STEP
}
function visibleGroupFlows(g: { key: string; flows: Flow[] }): Flow[] {
  const cap = groupRowCap(g.key)
  return g.flows.length > cap ? g.flows.slice(0, cap) : g.flows
}
function showMoreGroup(key: string, total: number) {
  const cur = groupRowCap(key)
  groupRowLimit.value = { ...groupRowLimit.value, [key]: Math.min(cur + GROUP_ROW_STEP, total) }
}
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
      return f.host || t('analyze.noHost')
    case 'process':
      return f.process_name || t('analyze.unknownProcess')
    case 'content_type':
      return extractContentTypeMain(f)
    case 'status_code': {
      const code = f.status_code
      if (code === null || code === undefined) return t('analyze.noStatusCode')
      if (code < 200) return t('analyze.status1xx')
      if (code < 300) return t('analyze.status2xx')
      if (code < 400) return t('analyze.status3xx')
      if (code < 500) return t('analyze.status4xx')
      return t('analyze.status5xx')
    }
    case 'ip_region':
      return f.ip_region || t('analyze.unknownRegion')
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
  // 性能优化：仅在 list 视图时计算分组，避免其他视图下无谓遍历 5000 条
  if (viewMode.value !== 'list') return []
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
      'application': t('analyze.contentTypeApplication'),
      'text': t('analyze.contentTypeText'),
      'image': t('analyze.contentTypeImage'),
      'video': t('analyze.contentTypeVideo'),
      'audio': t('analyze.contentTypeAudio'),
      'font': t('analyze.contentTypeFont'),
      'multipart': t('analyze.contentTypeMultipart'),
      'message': t('analyze.contentTypeMessage'),
      'model': t('analyze.contentTypeModel'),
      'unknown': t('analyze.contentTypeUnknown'),
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
  flows.rememberPage('/analyze')
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
  if (s === 'pending_request') return t('analyze.requestIntercepted')
  if (s === 'pending_response') return t('analyze.responseIntercepted')
  return t('analyze.intercepted')
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
    if (!silent) {
      selectedFlowId.value = null
      expandedGroups.value.clear()
      groupRowLimit.value = {}
    }
  } catch (e: any) {
    if (!silent) ElMessage.error(t('analyze.loadFailed') + (e?.message || e))
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
  // 性能优化：从 2s 改为 3s，减少高频请求和重渲染
  _timelineTimer = setInterval(() => {
    // 性能修复：页面不可见时跳过轮询
    if (document.hidden) return
    if (viewMode.value === 'timeline') {
      loadFlows(true)
      // P1-PF-006: 同步刷新统计图表，保证与 timeline 列表数据一致（后端 stats_flows 有 2s TTL 缓存）
      loadStats()
    }
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
  // lite 模式下 response_body 为空，触发详情加载
  if (f && (!f.response_body || f.response_body === '')) {
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
        t('analyze.clearAllConfirmMessage', { n: totalCount.value }),
        t('analyze.clearConfirmTitle'),
        { type: 'warning', confirmButtonText: t('analyze.clearButton'), cancelButtonText: t('analyze.cancelButton') }
      )
      clearLoading.value = true
      const r: any = await api.clearAllFlows('all')
      ElMessage.success(t('analyze.clearedFlows', { n: r.deleted }))
    } else {
      // 清理当前页之前的旧数据（保留当前页及之后）
      if (!allFlows.value.length) {
        ElMessage.warning(t('analyze.noFlowsToClear'))
        return
      }
      // 用 reduce 替代 Math.min(...spread)，避免大数组栈溢出
      const minId = allFlows.value.reduce((m, f) => f.id < m ? f.id : m, Infinity)
      await ElMessageBox.confirm(
        t('analyze.clearOldConfirmMessage', { id: minId }),
        t('analyze.clearConfirmTitle'),
        { type: 'warning', confirmButtonText: t('analyze.clearOldButton'), cancelButtonText: t('analyze.cancelButton') }
      )
      clearLoading.value = true
      const r: any = await api.clearAllFlows('before_id', minId)
      ElMessage.success(t('analyze.clearedOldFlows', { n: r.deleted }))
    }
    currentPage.value = 1
    await loadFlows()
    loadStats()
  } catch (e: any) {
    if (e === 'cancel' || e?.toString?.().includes('cancel')) return
    ElMessage.error(t('analyze.clearFailed') + (e?.message || e))
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
  flows.rememberPage('/analyze')
  // 检查 query 是否有 host 参数（从抓包页面跳转过来时传入）
  const queryHost = route.query.host as string | undefined
  if (queryHost) {
    groupBy.value = 'host'
    filterHost.value = queryHost
  }

  // 性能优化：store 已有流量缓存时直接复用，实现即时渲染（避免 1-2s 空白等待）
  const hasStoreCache = flows.flows.length > 0
  if (hasStoreCache) {
    allFlows.value = flows.flows as Flow[]
    totalCount.value = flows.total || flows.flows.length
    if (flows.selectedFlow) {
      selectedFlowId.value = flows.selectedFlow.id
    }
  }

  // 后台并行加载流量和统计（store 有缓存时跳过流量请求，仅刷新统计）
  await Promise.all([
    (async () => {
      if (!hasStoreCache) {
        await loadFlows()
      } else {
        // 有缓存时静默刷新 stats（保持 stats 最新）
        await loadStats()
      }
      if (!hasStoreCache && flows.selectedFlow) {
        selectedFlowId.value = flows.selectedFlow.id
      }
    })(),
    hasStoreCache ? Promise.resolve() : loadStats(),
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

// ---------- 右键菜单 ----------
const ctxMenu = ref<{ visible: boolean; x: number; y: number; flow: Flow | null }>({
  visible: false, x: 0, y: 0, flow: null,
})
const ctxMenuRef = ref<HTMLElement | null>(null)

const COPY_FIELDS = computed(() => [
  { key: 'id', label: 'ID', field: 'id' },
  { key: 'method', label: 'Method', field: 'method' },
  { key: 'host', label: 'Host', field: 'host' },
  { key: 'path', label: 'Path', field: 'path' },
  { key: 'status', label: 'Status', field: 'status_code' },
  { key: 'process', label: 'Process', field: 'process_name' },
  { key: 'pid', label: 'PID', field: 'pid' },
  { key: 'size', label: 'Size', field: 'size' },
  { key: 'time', label: 'Time', field: 'timestamp' },
])

function buildHttpCurl(f: any): string {
  const method = f.method || 'GET'
  const url = f.url || `http://${f.host}${f.path}`
  const headers = f.request_headers || '{}'
  let headerStr = ''
  try {
    const obj = JSON.parse(headers)
    for (const [k, v] of Object.entries(obj)) {
      headerStr += `  -H '${k}: ${v}' \\\n`
    }
  } catch { /* ignore */ }
  const body = f.request_body ? `  -d '${f.request_body}' \\\n` : ''
  let curl = `curl -X ${method} \\\n${body}  -H 'Accept: */*' \\\n${headerStr}  '${url}'`
  return curl
}

function getFieldValue(f: any, field: string): string {
  if (field === '_curl') return buildHttpCurl(f)
  if (field === '_url') return f.url || `http://${f.host}${f.path}`
  const v = f[field]
  return v === null || v === undefined ? '' : String(v)
}

function onContextMenu(e: MouseEvent, flow: Flow) {
  e.preventDefault()
  selectedFlowId.value = flow.id
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

function copyField(field: string) {
  const f = ctxMenu.value.flow
  if (!f) return
  const text = getFieldValue(f, field)
  navigator.clipboard.writeText(text).catch(() => {})
  ElMessage.success(t('analyze.copied', { text: text.length > 40 ? text.slice(0, 40) + '...' : text }))
  closeCtxMenu()
}

function ctxCopyUrl() {
  const f = ctxMenu.value.flow
  if (!f) return
  const url = f.url || `http://${f.host}${f.path}`
  navigator.clipboard.writeText(url).catch(() => {})
  ElMessage.success(t('analyze.copied', { text: url.length > 40 ? url.slice(0, 40) + '...' : url }))
  closeCtxMenu()
}

function ctxCopyCurl() {
  const f = ctxMenu.value.flow
  if (!f) return
  const curl = buildHttpCurl(f)
  navigator.clipboard.writeText(curl).catch(() => {})
  ElMessage.success(t('analyze.copied', { text: 'cURL' }))
  closeCtxMenu()
}

function ctxCopyRequest() {
  const f = ctxMenu.value.flow
  if (!f) return
  const body = f.request_body || ''
  navigator.clipboard.writeText(body).catch(() => {})
  ElMessage.success(t('analyze.copied', { text: body.length > 40 ? body.slice(0, 40) + '...' : body }))
  closeCtxMenu()
}

function ctxCopyResponse() {
  const f = ctxMenu.value.flow
  if (!f) return
  const body = f.response_body || ''
  navigator.clipboard.writeText(body).catch(() => {})
  ElMessage.success(t('analyze.copied', { text: body.length > 40 ? body.slice(0, 40) + '...' : body }))
  closeCtxMenu()
}

async function ctxIgnoreProcess() {
  const f = ctxMenu.value.flow
  if (!f || !f.process_name) {
    ElMessage.warning(t('analyze.noProcessInfo'))
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreProcess({ pid: null, name: f.process_name })
    ElMessage.success(t('analyze.ignoredProcess', { name: f.process_name }))
  } catch (e: any) {
    ElMessage.error(t('analyze.ignoreFailed', { msg: e?.message || e }))
  }
  closeCtxMenu()
}

async function ctxIgnorePid() {
  const f = ctxMenu.value.flow
  if (!f || !f.pid) {
    ElMessage.warning(t('analyze.noPidInfo'))
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreProcess({ pid: f.pid, name: f.process_name || `PID ${f.pid}` })
    ElMessage.success(t('analyze.ignoredPid', { pid: f.pid }))
  } catch (e: any) {
    ElMessage.error(t('analyze.ignoreFailed', { msg: e?.message || e }))
  }
  closeCtxMenu()
}

async function ctxIgnoreHost() {
  const f = ctxMenu.value.flow
  if (!f || !f.host) {
    ElMessage.warning(t('analyze.noHostInfo'))
    closeCtxMenu()
    return
  }
  try {
    await api.ignoreHost(f.host)
    ElMessage.success(t('analyze.ignoredHost', { host: f.host }))
  } catch (e: any) {
    ElMessage.error(t('analyze.ignoreFailed', { msg: e?.message || e }))
  }
  closeCtxMenu()
}

function onGlobalClick() {
  if (ctxMenu.value.visible) closeCtxMenu()
}

onMounted(() => {
  document.addEventListener('click', onGlobalClick)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onGlobalClick)
})
</script>

<template>
  <div class="analyze-view full flex flex-col">
    <div class="page-header">
      <div class="page-title"><el-icon><DataAnalysis /></el-icon>&nbsp;{{ t('analyze.globalAnalysis') }}</div>
      <div class="header-actions">
        <el-button-group size="small">
          <el-button @click="() => { flows.rememberPage('/analyze'); router.push('/site-map') }" :title="t('nav.siteMap')">
            <el-icon><Share /></el-icon>&nbsp;{{ t('nav.siteMap') }}
          </el-button>
          <el-button @click="() => { flows.rememberPage('/analyze'); router.push('/timeline') }" :title="t('nav.timeline')">
            <el-icon><Timer /></el-icon>&nbsp;{{ t('nav.timeline') }}
          </el-button>
          <el-button @click="() => { flows.rememberPage('/analyze'); router.push('/cookies') }" :title="t('nav.cookies')">
            <CookieIcon style="font-size: 14px" />&nbsp;{{ t('nav.cookies') }}
          </el-button>
          <el-button @click="() => { flows.rememberPage('/analyze'); router.push('/record') }" :title="t('nav.record')">
            <el-icon><VideoCamera /></el-icon>&nbsp;{{ t('nav.record') }}
          </el-button>
        </el-button-group>
        <!-- 导出报表按钮 -->
        <el-dropdown size="small" trigger="click" @command="(f: string) => exportReport(f as any)">
          <el-button size="small">
            <el-icon><Download /></el-icon>&nbsp;{{ t('analyze.exportReport') }}
            <el-icon style="margin-left: 4px"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="json">JSON</el-dropdown-item>
              <el-dropdown-item command="csv">CSV</el-dropdown-item>
              <el-dropdown-item command="html">HTML</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button size="small" @click="() => { loadFlows(); loadStats() }" :loading="loading">
          <el-icon><Refresh /></el-icon>&nbsp;{{ t('analyze.refresh') }}
        </el-button>
        <ImportButton @imported="() => { loadFlows(); loadStats() }" />
        <el-button size="small" @click="onClearFlows('before')" :loading="clearLoading" :title="t('analyze.clearOldDataTitle')">
          <el-icon><Delete /></el-icon>&nbsp;{{ t('analyze.clearOldData') }}
        </el-button>
        <el-button size="small" type="danger" @click="onClearFlows('all')" :loading="clearLoading">
          <el-icon><Delete /></el-icon>&nbsp;{{ t('analyze.clearAll') }}
        </el-button>
      </div>
    </div>

    <!-- 工具栏：分组方式 + 视图切换 -->
    <div class="toolbar-row">
      <span class="text-dim" style="font-size: 12px">{{ t('analyze.groupByLabel') }}</span>
      <el-radio-group v-model="groupBy" size="small">
        <el-radio-button value="host">{{ t('analyze.groupByHost') }}</el-radio-button>
        <el-radio-button value="process">{{ t('analyze.groupByProcess') }}</el-radio-button>
        <el-radio-button value="content_type">{{ t('analyze.groupByContentType') }}</el-radio-button>
        <el-radio-button value="status_code">{{ t('analyze.groupByStatusCode') }}</el-radio-button>
        <el-radio-button value="ip_region">{{ t('analyze.groupByIpRegion') }}</el-radio-button>
      </el-radio-group>
      <div class="flex-1"></div>
      <el-button
        size="small"
        @click="toggleSortOrder"
        :title="sortOrder === 'desc' ? t('analyze.sortDescTitle') : t('analyze.sortAscTitle')"
      >
        <el-icon><Sort /></el-icon>&nbsp;{{ sortOrder === 'desc' ? t('analyze.sortDesc') : t('analyze.sortAsc') }}
      </el-button>
      <el-radio-group v-model="viewMode" size="small" style="margin-left: 8px">
        <el-radio-button value="list">{{ t('analyze.viewList') }}</el-radio-button>
        <el-radio-button value="bar">{{ t('analyze.viewBar') }}</el-radio-button>
        <el-radio-button value="pie">{{ t('analyze.viewPie') }}</el-radio-button>
        <el-radio-button value="timeline">{{ t('analyze.viewTimeline') }}</el-radio-button>
        <el-radio-button value="heatmap">{{ t('analyze.viewHeatmap') }}</el-radio-button>
        <el-radio-button value="topology">{{ t('analyze.viewTopology') }}</el-radio-button>
        <el-radio-button value="latency">{{ t('analyze.viewLatency') }}</el-radio-button>
        <el-radio-button value="cross">{{ t('analyze.viewCross') }}</el-radio-button>
        <el-radio-button value="anomalies">{{ t('analyze.viewAnomalies') }}</el-radio-button>
      </el-radio-group>
      <span class="text-dim" style="font-size: 12px; margin-left: 12px">{{ t('analyze.flowCountSummary', { page: allFlows.length, total: totalCount }) }}</span>
    </div>

    <!-- 过滤栏 -->
    <div class="toolbar-row" style="padding: 6px 16px">
      <span class="text-dim" style="font-size: 12px">{{ t('analyze.filterLabel') }}</span>
      <el-input v-model="filterHost" :placeholder="t('analyze.hostFilterPlaceholder')" size="small" clearable style="width: 200px" @keyup.enter="applyFilter" @clear="applyFilter" />
      <el-input v-model="filterProcess" :placeholder="t('analyze.processFilterPlaceholder')" size="small" clearable style="width: 180px" @keyup.enter="applyFilter" @clear="applyFilter" />
      <el-button size="small" type="primary" plain @click="applyFilter">{{ t('analyze.apply') }}</el-button>
      <el-button size="small" @click="clearFilter">{{ t('analyze.reset') }}</el-button>
      <div class="flex-1"></div>
    </div>

    <!-- 主体：条形图视图（viewMode === 'bar'）— 连续堆叠条形图，显示全量数据比例，换页不换图 -->
    <div v-if="viewMode === 'bar'" class="analyze-body chart-body flex-1 overflow-auto" @scroll="onChartScroll">
      <div v-if="statsLoading" class="empty-text text-dim">{{ t('analyze.statsLoading') }}</div>
      <div v-else-if="!statsGroups.length" class="empty-text text-dim">{{ t('analyze.noFlowData') }}</div>
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
        <div class="tt-row"><span class="tt-k">{{ t('analyze.count') }}</span><span class="tt-v">{{ hoverTip.count }}</span></div>
        <div class="tt-row"><span class="tt-k">{{ t('analyze.percentage') }}</span><span class="tt-v">{{ hoverTip.pct }}%</span></div>
      </div>
    </div>

    <!-- 主体：饼图视图（viewMode === 'pie'）— conic-gradient 饼图，显示全量数据比例，换页不换图 -->
    <div v-else-if="viewMode === 'pie'" class="analyze-body chart-body flex-1 overflow-auto">
      <div v-if="statsLoading" class="empty-text text-dim">{{ t('analyze.statsLoading') }}</div>
      <div v-else-if="!statsGroups.length" class="empty-text text-dim">{{ t('analyze.noFlowData') }}</div>
      <div v-else class="pie-container">
        <div class="pie-wrap">
          <div class="pie-chart" :style="{ background: pieGradient }">
            <div class="pie-hole">
              <div class="pie-center-count mono">{{ statsTotal }}</div>
              <div class="pie-center-label text-dim">{{ t('analyze.totalFlows') }}</div>
            </div>
          </div>
          <div class="pie-legend">
            <div
              v-for="(g, i) in statsGroups"
              :key="g.key"
              class="legend-item"
              @click="onChartBarClick(g)"
              :title="t('analyze.clickToViewGroup', { label: g.label })"
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
      <div v-if="allFlows.length < 2" class="empty-text text-dim">{{ t('analyze.notEnoughForTimeline') }}</div>
      <div v-else class="timeline-container">
        <div class="timeline-controls">
          <span class="text-dim" style="font-size: 12px;">{{ t('analyze.granularityLabel') }}</span>
          <el-radio-group v-model="timelineGranularity" size="small">
            <el-radio-button :value="1">{{ t('analyze.granularity1s') }}</el-radio-button>
            <el-radio-button :value="5">{{ t('analyze.granularity5s') }}</el-radio-button>
            <el-radio-button :value="60">{{ t('analyze.granularity1m') }}</el-radio-button>
          </el-radio-group>
          <span class="text-dim" style="font-size: 12px; margin-left: 16px;">{{ t('analyze.metricLabel') }}</span>
          <el-radio-group v-model="timelineMetric" size="small">
            <el-radio-button value="qps">QPS</el-radio-button>
            <el-radio-button value="bytes">{{ t('analyze.metricBytes') }}</el-radio-button>
          </el-radio-group>
          <span class="text-dim timeline-summary">
            {{ t('analyze.timelineSummary', { total: allFlows.length, buckets: timelineBuckets.length, peak: timelineMaxBucket, unit: timelineMetric === 'qps' ? ' req' : ' B' }) }}
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
                      stroke="var(--on-accent)" stroke-width="2" />
            <!-- Bytes 折线（蓝色） -->
            <polyline v-else
                      :points="timelineBytesPoints" fill="none"
                      stroke="var(--on-blue)" stroke-width="2" />
            <!-- 区域填充 -->
            <polygon v-if="timelineMetric === 'qps' && timelineQpsAreaPoints"
                     :points="timelineQpsAreaPoints"
                     fill="var(--on-accent)" fill-opacity="0.1" />
            <polygon v-else-if="timelineBytesAreaPoints"
                     :points="timelineBytesAreaPoints"
                     fill="var(--on-blue)" fill-opacity="0.1" />
            <!-- X 轴刻度（首尾标签用 start/end 锚点避免被裁切） -->
            <text v-for="(label, i) in timelineXLabels" :key="'x-' + i"
                  :x="timelinePadding.left + i * (timelineWidth - timelinePadding.left - timelinePadding.right) / (timelineXLabels.length - 1 || 1)"
                  :y="timelineHeight - timelinePadding.bottom + 16"
                  :text-anchor="i === 0 ? 'start' : i === timelineXLabels.length - 1 ? 'end' : 'middle'"
                  fill="var(--on-text-dim)" font-size="11" font-family="var(--on-font-mono, Menlo, monospace)">
              {{ label }}
            </text>
          </svg>
        </div>
      </div>
    </div>

    <!-- 主体：热力图视图（viewMode === 'heatmap'）— 2D 矩阵：时间桶 × 维度 -->
    <div v-else-if="viewMode === 'heatmap'" class="analyze-body chart-body flex-1 overflow-auto heatmap-view">
      <div v-if="heatmapLoading" class="empty-text text-dim">{{ t('analyze.statsLoading') }}</div>
      <div v-else-if="!heatmapData.buckets.length || !heatmapData.dimensions.length" class="empty-text text-dim">{{ t('analyze.noFlowData') }}</div>
      <div v-else class="heatmap-container">
        <div class="heatmap-controls">
          <span class="text-dim" style="font-size: 12px;">{{ t('analyze.granularityLabel') }}</span>
          <el-radio-group v-model="heatmapBucketSec" size="small">
            <el-radio-button :value="60">{{ t('analyze.granularity1m') }}</el-radio-button>
            <el-radio-button :value="300">{{ t('analyze.granularity5m') }}</el-radio-button>
            <el-radio-button :value="600">10m</el-radio-button>
          </el-radio-group>
          <span class="text-dim" style="font-size: 12px; margin-left: 16px;">
            {{ t('analyze.timelineSummary', { total: heatmapData.total, buckets: heatmapData.buckets.length, peak: heatmapMax, unit: ' req' }) }}
          </span>
        </div>
        <div class="heatmap-table-wrap">
          <table class="heatmap-table">
            <thead>
              <tr>
                <th class="heatmap-dim-col">{{ groupBy }}</th>
                <th v-for="(b, i) in heatmapData.buckets" :key="'hb-' + i" class="heatmap-bucket-col" :title="b.full">
                  <span v-if="i % Math.ceil(heatmapData.buckets.length / 12) === 0 || i === heatmapData.buckets.length - 1">{{ b.label }}</span>
                </th>
                <th class="heatmap-total-col">Σ</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(dim, di) in heatmapData.dimensions" :key="'hd-' + di">
                <td class="heatmap-dim-col" :title="dim.label">{{ dim.label }}</td>
                <td v-for="(b, bi) in heatmapData.buckets" :key="'hc-' + di + '-' + bi"
                    class="heatmap-cell"
                    :style="{ background: heatmapColor(heatmapData.matrix[di]?.[bi] || 0) }"
                    :title="`${dim.label} @ ${b.full}: ${heatmapData.matrix[di]?.[bi] || 0}`">
                  <span v-if="(heatmapData.matrix[di]?.[bi] || 0) > 0" class="heatmap-cell-text">{{ heatmapData.matrix[di][bi] }}</span>
                </td>
                <td class="heatmap-total-col">{{ dim.total }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 主体：拓扑图视图（viewMode === 'topology'）— process → IP → host 连接关系图 -->
    <div v-else-if="viewMode === 'topology'" class="analyze-body chart-body flex-1 overflow-auto topology-view">
      <div v-if="topoLoading" class="empty-text text-dim">{{ t('analyze.statsLoading') }}</div>
      <div v-else-if="!topoData.nodes.length" class="empty-text text-dim">{{ t('analyze.noFlowData') }}</div>
      <div v-else class="topology-container">
        <div class="topology-legend">
          <span class="legend-item"><span class="legend-dot" style="background: var(--on-accent)"></span>{{ t('analyze.topoProcess') }}</span>
          <span class="legend-item"><span class="legend-dot" style="background: var(--on-amber, #f59e0b)"></span>{{ t('analyze.topoIp') }}</span>
          <span class="legend-item"><span class="legend-dot" style="background: var(--on-success)"></span>{{ t('analyze.topoHost') }}</span>
          <span class="text-dim" style="font-size: 12px; margin-left: 16px">{{ t('analyze.topoSummary', { nodes: topoData.nodes.length, edges: topoData.edges.length }) }}</span>
        </div>
        <svg class="topology-svg" :viewBox="`0 0 900 ${Math.max(600, topoData.nodes.length * 50 + 120)}`" preserveAspectRatio="xMidYMid meet">
          <!-- 边（贝塞尔曲线连线，比直线更清晰） -->
          <path v-for="(e, i) in topoData.edges" :key="'te-' + i"
                :d="edgePath(e)"
                fill="none"
                stroke="var(--on-border)" :stroke-width="Math.max(1, e.size / 2)" opacity="0.4" />
          <!-- 节点 -->
          <g v-for="(node, i) in topoData.nodes" :key="'tn-' + i" :transform="`translate(${node.x},${node.y})`">
            <circle :r="Math.max(10, Math.min(28, 8 + node.count / 4))"
                    :fill="topoNodeColor(node.type)" fill-opacity="0.7"
                    stroke="var(--on-bg)" stroke-width="2" />
            <text y="-4" text-anchor="middle" fill="var(--on-text)" font-size="10"
                  font-family="var(--on-font-mono, monospace)" font-weight="600">
              {{ node.label.length > 12 ? node.label.slice(0, 10) + '..' : node.label }}
            </text>
            <text y="10" text-anchor="middle" fill="var(--on-text-dim)" font-size="9"
                  font-family="var(--on-font-mono, monospace)">
              {{ node.count }}
            </text>
            <title>{{ node.label }} ({{ node.count }})</title>
          </g>
        </svg>
      </div>
    </div>

    <!-- 主体：延迟分析视图（viewMode === 'latency'）— 延迟分布直方图 + 分位数统计 -->
    <div v-else-if="viewMode === 'latency'" class="analyze-body chart-body flex-1 overflow-auto latency-view">
      <div v-if="latencyLoading" class="empty-text text-dim">{{ t('analyze.statsLoading') }}</div>
      <template v-else-if="latencyStats">
        <div class="latency-summary">
          <div class="latency-stat-card">
            <div class="stat-value">{{ latencyStats.total.toLocaleString() }}</div>
            <div class="stat-label">{{ t('analyze.totalRequests') }}</div>
          </div>
          <div class="latency-stat-card">
            <div class="stat-value">{{ latencyStats.avg_ms.toFixed(1) }}<span class="stat-unit">ms</span></div>
            <div class="stat-label">{{ t('analyze.avgLatency') }}</div>
          </div>
          <div class="latency-stat-card warning">
            <div class="stat-value">{{ latencyStats.slow_count.toLocaleString() }}</div>
            <div class="stat-label">{{ t('analyze.slowRequests') }} ({{ latencyStats.slow_pct.toFixed(1) }}%)</div>
          </div>
        </div>
        <!-- 分位数卡片 -->
        <div class="percentile-cards">
          <div class="percentile-card">
            <div class="p-label">P50</div>
            <div class="p-value" style="color: var(--on-accent)">{{ latencyStats.percentiles.p50 }}ms</div>
          </div>
          <div class="percentile-card">
            <div class="p-label">P75</div>
            <div class="p-value">{{ latencyStats.percentiles.p75 }}ms</div>
          </div>
          <div class="percentile-card">
            <div class="p-label">P90</div>
            <div class="p-value">{{ latencyStats.percentiles.p90 }}ms</div>
          </div>
          <div class="percentile-card">
            <div class="p-label">P95</div>
            <div class="p-value" style="color: var(--on-amber, #f59e0b)">{{ latencyStats.percentiles.p95 }}ms</div>
          </div>
          <div class="percentile-card">
            <div class="p-label">P99</div>
            <div class="p-value" style="color: var(--on-error, #f56c6c)">{{ latencyStats.percentiles.p99 }}ms</div>
          </div>
        </div>
        <!-- 延迟分布直方图 -->
        <div class="latency-histogram">
          <h3>{{ t('analyze.latencyDistribution') }}</h3>
          <div class="histogram-bars">
            <div v-for="(h, i) in latencyStats.histogram" :key="i" class="histogram-item">
              <div class="bar-wrap">
                <div
                  class="bar-fill"
                  :style="{
                    height: (h.count / (Math.max(...latencyStats.histogram.map(x => x.count)) || 1) * 100) + '%',
                    background: latencyBarColor(h.range)
                  }"
                ></div>
              </div>
              <div class="bar-label">{{ h.range }}</div>
              <div class="bar-count">{{ h.count.toLocaleString() }}</div>
              <div class="bar-pct text-dim">{{ h.pct.toFixed(1) }}%</div>
            </div>
          </div>
        </div>
      </template>
      <div v-else class="empty-text text-dim">{{ t('analyze.noFlowData') }}</div>
    </div>

    <!-- 主体：交叉分析视图（viewMode === 'cross'）— Host x Status Code / Content-Type x Size -->
    <div v-else-if="viewMode === 'cross'" class="analyze-body chart-body flex-1 overflow-auto cross-view">
      <div v-if="crossLoading" class="empty-text text-dim">{{ t('analyze.statsLoading') }}</div>
      <template v-else-if="crossAnalysis">
        <!-- 切换维度 -->
        <div class="cross-controls">
          <span class="text-dim" style="font-size: 12px">{{ t('analyze.dimensionLabel') }}</span>
          <el-radio-group v-model="crossAxisX" size="small">
            <el-radio-button value="host_status">{{ t('analyze.hostStatus') }}</el-radio-button>
            <el-radio-button value="content_size">{{ t('analyze.contentSize') }}</el-radio-button>
          </el-radio-group>
        </div>
        <!-- 交叉矩阵表格 -->
        <div class="cross-matrix-wrap">
          <table class="cross-matrix-table">
            <thead>
              <tr>
                <th class="cross-dim-col">{{ crossAxisX === 'host_status' ? 'Host' : 'Content-Type' }}</th>
                <th v-for="b in crossMatrix.buckets" :key="b" class="cross-bucket-col">{{ b }}</th>
                <th class="cross-total-col">Σ</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(dim, di) in crossMatrix.dimensions" :key="dim">
                <td class="cross-dim-col" :title="dim">{{ dim.length > 24 ? dim.slice(0, 22) + '..' : dim }}</td>
                <td v-for="(b, bi) in crossMatrix.buckets" :key="b"
                    class="cross-cell"
                    :style="{ background: crossColor(crossMatrix.matrix[di]?.[bi] || 0, crossMax) }"
                    :title="`${dim} @ ${b}: ${crossMatrix.matrix[di]?.[bi] || 0}`">
                  <span v-if="(crossMatrix.matrix[di]?.[bi] || 0) > 0">{{ crossMatrix.matrix[di][bi] }}</span>
                </td>
                <td class="cross-total-col">{{ crossMatrix.matrix[di]?.reduce((a: number, b: number) => a + b, 0) || 0 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <div v-else class="empty-text text-dim">{{ t('analyze.noFlowData') }}</div>
    </div>

    <!-- 主体：异常检测视图（viewMode === 'anomalies'） -->
    <div v-else-if="viewMode === 'anomalies'" class="analyze-body chart-body flex-1 overflow-auto anomalies-view">
      <div v-if="anomalyLoading" class="empty-text text-dim">{{ t('analyze.statsLoading') }}</div>
      <template v-else-if="anomalyResult">
        <!-- 摘要统计 -->
        <div class="anomaly-summary">
          <div class="anomaly-stat-card">
            <div class="stat-value">{{ anomalyResult.summary.total.toLocaleString() }}</div>
            <div class="stat-label">{{ t('analyze.totalRequests') }}</div>
          </div>
          <div class="anomaly-stat-card" :class="{ error: anomalyResult.summary.error_rate > 10 }">
            <div class="stat-value">{{ anomalyResult.summary.error_rate.toFixed(1) }}%</div>
            <div class="stat-label">{{ t('analyze.errorRate') }}</div>
          </div>
          <div class="anomaly-stat-card">
            <div class="stat-value">{{ anomalyResult.summary.avg_ms.toFixed(1) }}<span class="stat-unit">ms</span></div>
            <div class="stat-label">{{ t('analyze.avgLatency') }}</div>
          </div>
          <div class="anomaly-stat-card" :class="{ warning: anomalyResult.summary.anomaly_count > 0 }">
            <div class="stat-value">{{ anomalyResult.summary.anomaly_count }}</div>
            <div class="stat-label">{{ t('analyze.anomalyCount') }}</div>
          </div>
        </div>
        <!-- 异常列表 -->
        <div class="anomaly-list">
          <div class="anomaly-list-header">
            <h3>{{ t('analyze.anomalies') }}</h3>
            <el-button size="small" text @click="anomaliesExpanded = !anomaliesExpanded">
              {{ anomaliesExpanded ? t('analyze.collapse') : t('analyze.expand') }}
            </el-button>
          </div>
          <div v-if="anomaliesExpanded">
            <div v-if="anomalyResult.anomalies.length === 0" class="empty-text text-dim">
              {{ t('analyze.noAnomalies') }}
            </div>
            <div v-else>
              <div
                v-for="(a, i) in anomalyResult.anomalies"
                :key="i"
                class="anomaly-item"
                :class="'anomaly-' + a.severity"
              >
                <div class="anomaly-header">
                  <span class="anomaly-badge" :style="{ background: anomalyColor(a.severity) }">{{ a.severity.toUpperCase() }}</span>
                  <span class="anomaly-type">{{ a.type }}</span>
                </div>
                <div class="anomaly-message">{{ a.message }}</div>
                <div v-if="a.details" class="anomaly-details">
                  <span v-for="(v, k) in a.details" :key="k" class="detail-tag">
                    {{ k }}: {{ typeof v === 'number' ? v.toLocaleString() : v }}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </template>
      <div v-else class="empty-text text-dim">{{ t('analyze.noFlowData') }}</div>
    </div>

    <!-- 主体：列表视图（viewMode === 'list'）+ 右侧详情 -->
    <div v-else class="analyze-body flex-1 flex overflow-hidden">
      <div class="group-list">
        <div v-if="loading" class="empty-text text-dim">{{ t('analyze.loading') }}</div>
        <div v-else-if="!allFlows.length" class="empty-text text-dim">
          {{ t('analyze.noFlowDataHint') }}
        </div>
        <div v-for="g in groupedFlows" :key="g.key" class="group-item" :class="{ 'group-empty': !g.hasPageFlows }" :data-group-key="g.key">
          <div class="group-header" @click="toggleGroup(g.key)">
            <el-icon class="expand-icon" :class="{ expanded: expandedGroups.has(g.key) }"><ArrowRight /></el-icon>
            <span class="group-label">{{ g.label }}</span>
            <span class="group-pct">{{ g.globalPct.toFixed(1) }}%</span>
            <span class="group-count" :title="t('analyze.groupCountTitle', { global: g.globalCount, page: g.count })">{{ g.globalCount }}</span>
            <span v-if="g.count !== g.globalCount" class="group-page-count text-dim" :title="t('analyze.pageCountTitle', { n: g.count })">{{ t('analyze.pageCount', { n: g.count }) }}</span>
          </div>
          <div v-if="expandedGroups.has(g.key) && g.hasPageFlows && groupBy !== 'host'" class="group-flows">
            <div
              v-for="f in visibleGroupFlows(g)"
              :key="f.id"
              class="flow-row"
              :class="{ selected: f.id === selectedFlowId }"
              v-memo="[f.id === selectedFlowId, f.status_code, bpBadge(f)]"
              @click="onFlowClick(f)"
              @dblclick="onFlowDblClick(f)"
              @contextmenu="onContextMenu($event, f)"
            >
              <span class="flow-id mono text-dim">{{ f.id }}</span>
              <span class="flow-method mono" :class="'m-' + (f.method || '').toLowerCase()">{{ f.method }}</span>
              <span class="flow-host mono text-truncate">{{ f.host }}{{ f.path }}</span>
              <span v-if="f.status_code !== null" class="flow-status mono">{{ f.status_code }}</span>
              <span v-if="bpBadge(f)" class="flow-bp">⚠ {{ bpBadge(f) }}</span>
            </div>
            <div
              v-if="g.flows.length > groupRowCap(g.key)"
              class="group-show-more text-dim"
              @click="showMoreGroup(g.key, g.flows.length)"
            >
              {{ t('analyze.showMore', { n: Math.min(GROUP_ROW_STEP, g.flows.length - groupRowCap(g.key)), total: g.flows.length - groupRowCap(g.key) }) }}
            </div>
          </div>
          <div v-else-if="expandedGroups.has(g.key) && !g.hasPageFlows" class="group-empty-hint text-dim">
            {{ t('analyze.noGroupFlowsOnPage') }}
          </div>
        </div>
      </div>

      <div class="detail-pane">
        <div v-if="!selectedFlow" class="empty-text text-dim">
          {{ t('analyze.detailEmptyHint') }}
        </div>
        <template v-else>
          <!-- 顶部条：分析模式 + 标题 + 关闭按钮 -->
          <div class="detail-header">
            <el-radio-group v-model="analyzeMode" size="small">
              <el-radio-button value="response">{{ t('analyze.responseMode') }}</el-radio-button>
              <el-radio-button value="request">{{ t('analyze.requestMode') }}</el-radio-button>
            </el-radio-group>
            <span class="detail-title text-dim mono">
              #{{ selectedFlow.id }} {{ selectedFlow.method }} {{ selectedFlow.host }}{{ selectedFlow.path }}
            </span>
            <div class="flex-1"></div>
            <el-button size="small" circle plain @click="closeDetail" :title="t('analyze.close')">
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

    <!-- 右键菜单 -->
    <teleport to="body">
      <div
        v-if="ctxMenu.visible"
        ref="ctxMenuRef"
        class="ctx-menu"
        :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
        @click.stop
      >
        <div class="ctx-item" @click="onFlowDblClick(ctxMenu.flow!)"><el-icon><Aim /></el-icon>&nbsp;{{ t('analyze.viewInCapture') }}</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxCopyUrl"><el-icon><Link /></el-icon>&nbsp;{{ t('analyze.copyUrl') }}</div>
        <div class="ctx-item" @click="ctxCopyCurl"><el-icon><DocumentCopy /></el-icon>&nbsp;{{ t('analyze.copyCurl') }}</div>
        <div class="ctx-item" @click="ctxCopyRequest"><el-icon><Top /></el-icon>&nbsp;{{ t('analyze.copyRequest') }}</div>
        <div class="ctx-item" @click="ctxCopyResponse"><el-icon><Bottom /></el-icon>&nbsp;{{ t('analyze.copyResponse') }}</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxIgnoreProcess"><el-icon><Filter /></el-icon>&nbsp;{{ t('analyze.ignoreProcess') }}</div>
        <div class="ctx-item" @click="ctxIgnorePid"><el-icon><Filter /></el-icon>&nbsp;{{ t('analyze.ignoreByPid') }}</div>
        <div class="ctx-item" @click="ctxIgnoreHost"><el-icon><Filter /></el-icon>&nbsp;{{ t('analyze.ignoreByHost') }}</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item ctx-submenu">
          <el-icon><CopyDocument /></el-icon>&nbsp;{{ t('analyze.copy') }}
          <el-icon class="ctx-arrow"><ArrowRight /></el-icon>
        </div>
        <div class="ctx-submenu-panel">
          <div v-for="item in COPY_FIELDS" :key="item.key" class="ctx-item" @click="copyField(item.field)">{{ item.label }}</div>
        </div>
      </div>
    </teleport>
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
.analyze-toolbar, .filter-bar { display: none; } /* 使用统一的toolbar-row */

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
  background: var(--on-bg-hover); padding: 1px 6px; border-radius: var(--on-radius-lg);
  font-family: var(--on-font-mono);
  flex-shrink: 0;
}
.group-page-count {
  font-size: 10px; flex-shrink: 0;
  font-family: var(--on-font-mono);
}
.group-flows { padding-left: 18px; }
.group-show-more {
  padding: 5px 8px 5px 26px; font-size: 11px; cursor: pointer;
  text-decoration: underline; text-underline-offset: 2px;
}
.group-show-more:hover { color: var(--on-accent, #409eff); }
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
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border);
  border-radius: var(--on-radius-md);
  box-shadow: var(--on-shadow-md);
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
.bar-tooltip .tt-k { color: var(--on-text-muted); }
.bar-tooltip .tt-v { color: var(--on-text); font-weight: 600; }

/* 饼图视图（conic-gradient 环形图，显示全量比例）*/
.pie-container {
  max-width: 1000px; margin: 0 auto; padding: 12px;
}

/* 拓扑图视图（process → IP → host 连接关系图） */
.topology-view { display: flex; flex-direction: column; }
.topology-container { width: 100%; margin: 0 auto; padding: 12px; display: flex; flex-direction: column; gap: 12px; }
.topology-legend { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.legend-item { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--on-text-dim); }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.topology-svg { width: 100%; height: 600px; background: var(--on-bg); border: 1px solid var(--on-border-light); border-radius: 6px; }

/* 热力图视图（2D 矩阵：时间桶 × 维度） */
.heatmap-view {
  display: flex;
  flex-direction: column;
}
.heatmap-container {
  width: 100%;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px;
}
.heatmap-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.heatmap-table-wrap {
  overflow: auto;
  border: 1px solid var(--on-border-light);
  border-radius: 6px;
  background: var(--on-bg);
}
.heatmap-table {
  border-collapse: collapse;
  font-size: 11px;
  font-family: var(--on-font-mono, Menlo, monospace);
  width: 100%;
}
.heatmap-table th,
.heatmap-table td {
  border: 1px solid var(--on-border-light);
  padding: 2px 4px;
  text-align: center;
  white-space: nowrap;
}
.heatmap-table th {
  background: var(--on-bg-alt, #f5f7fa);
  color: var(--on-text-dim);
  font-weight: 500;
  position: sticky;
  top: 0;
  z-index: 2;
}
.heatmap-dim-col {
  text-align: left;
  min-width: 120px;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  position: sticky;
  left: 0;
  background: var(--on-bg-alt, #f5f7fa);
  z-index: 1;
}
.heatmap-table th.heatmap-dim-col {
  z-index: 3;
}
.heatmap-bucket-col {
  min-width: 36px;
  max-width: 60px;
  color: var(--on-text-dim);
  font-size: 10px;
}
.heatmap-cell {
  min-width: 36px;
  height: 24px;
  cursor: default;
  transition: outline 0.1s;
}
.heatmap-cell:hover {
  outline: 2px solid var(--on-accent);
  outline-offset: -2px;
}
.heatmap-cell-text {
  color: var(--on-text);
  font-size: 10px;
}
.heatmap-total-col {
  min-width: 50px;
  font-weight: 600;
  color: var(--on-text);
  background: var(--on-bg-alt, #f5f7fa);
  position: sticky;
  right: 0;
  z-index: 1;
}
.heatmap-table th.heatmap-total-col {
  z-index: 3;
}

/* 时序图视图（SVG 折线图） */
.timeline-view {
  display: flex;
  flex-direction: column;
}
.timeline-container {
  width: 100%;
  margin: 0 auto;
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
  width: 100%;
  margin: 0 auto;
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

/* ========== P1 高级分析视图样式 ========== */

/* 延迟分析视图 */
.latency-view {
  display: flex;
  flex-direction: column;
  padding: 16px;
}
.latency-summary {
  display: flex;
  gap: 16px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}
.latency-stat-card {
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: 8px;
  padding: 16px 24px;
  text-align: center;
  min-width: 140px;
}
.latency-stat-card.warning {
  border-color: var(--on-amber, #f59e0b);
  background: rgba(245, 158, 11, 0.08);
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
  color: var(--on-text);
  font-family: var(--on-font-mono, monospace);
}
.stat-unit {
  font-size: 14px;
  color: var(--on-text-dim);
}
.stat-label {
  font-size: 12px;
  color: var(--on-text-dim);
  margin-top: 4px;
}
.percentile-cards {
  display: flex;
  gap: 12px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}
.percentile-card {
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: 8px;
  padding: 12px 20px;
  text-align: center;
  min-width: 90px;
}
.p-label {
  font-size: 11px;
  color: var(--on-text-dim);
  margin-bottom: 4px;
}
.p-value {
  font-size: 18px;
  font-weight: 700;
  font-family: var(--on-font-mono, monospace);
}
.latency-histogram {
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: 8px;
  padding: 20px;
}
.latency-histogram h3 {
  margin: 0 0 16px 0;
  font-size: 14px;
  color: var(--on-text);
}
.histogram-bars {
  display: flex;
  gap: 12px;
  align-items: flex-end;
  height: 200px;
}
.histogram-item {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 60px;
}
.bar-wrap {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: flex-end;
  min-height: 20px;
}
.bar-fill {
  width: 100%;
  min-height: 4px;
  border-radius: 4px 4px 0 0;
  transition: height 0.3s ease;
}
.bar-label {
  font-size: 11px;
  color: var(--on-text-dim);
  margin-top: 8px;
  text-align: center;
}
.bar-count {
  font-size: 13px;
  font-weight: 600;
  font-family: var(--on-font-mono, monospace);
  margin-top: 4px;
}
.bar-pct {
  font-size: 11px;
  margin-top: 2px;
}

/* 交叉分析视图 */
.cross-view {
  display: flex;
  flex-direction: column;
}
.cross-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  flex-wrap: wrap;
}
.cross-matrix-wrap {
  overflow: auto;
  padding: 0 16px 16px;
}
.cross-matrix-table {
  border-collapse: collapse;
  font-size: 11px;
  font-family: var(--on-font-mono, monospace);
  width: 100%;
}
.cross-matrix-table th,
.cross-matrix-table td {
  border: 1px solid var(--on-border-light);
  padding: 6px 8px;
  text-align: center;
  white-space: nowrap;
}
.cross-matrix-table th {
  background: var(--on-bg-alt, #f5f7fa);
  color: var(--on-text-dim);
  font-weight: 600;
  position: sticky;
  top: 0;
  z-index: 2;
}
.cross-dim-col {
  text-align: left !important;
  min-width: 150px;
  max-width: 250px;
  overflow: hidden;
  text-overflow: ellipsis;
  position: sticky;
  left: 0;
  background: var(--on-bg-alt, #f5f7fa);
  z-index: 1;
}
.cross-matrix-table th.cross-dim-col {
  z-index: 3;
}
.cross-bucket-col {
  min-width: 60px;
  font-size: 10px;
}
.cross-cell {
  cursor: default;
  transition: outline 0.1s;
  font-size: 12px;
}
.cross-cell:hover {
  outline: 2px solid var(--on-accent);
  outline-offset: -2px;
}
.cross-total-col {
  min-width: 60px;
  font-weight: 600;
  background: var(--on-bg-alt, #f5f7fa);
  position: sticky;
  right: 0;
  z-index: 1;
}
.cross-matrix-table th.cross-total-col {
  z-index: 3;
}

/* 异常检测视图 */
.anomalies-view {
  display: flex;
  flex-direction: column;
}
.anomaly-summary {
  display: flex;
  gap: 16px;
  padding: 16px;
  flex-wrap: wrap;
}
.anomaly-stat-card {
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: 8px;
  padding: 16px 24px;
  text-align: center;
  min-width: 140px;
}
.anomaly-stat-card.warning {
  border-color: var(--on-amber, #f59e0b);
}
.anomaly-stat-card.error {
  border-color: var(--on-error, #f56c6c);
}
.anomaly-list {
  padding: 0 16px 16px;
}
.anomaly-list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.anomaly-list-header h3 {
  margin: 0;
  font-size: 14px;
  color: var(--on-text);
}
.anomaly-item {
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 12px;
}
.anomaly-item.anomaly-error {
  border-left: 4px solid var(--on-error, #f56c6c);
}
.anomaly-item.anomaly-warning {
  border-left: 4px solid var(--on-amber, #f59e0b);
}
.anomaly-item.anomaly-info {
  border-left: 4px solid var(--on-accent, #409eff);
}
.anomaly-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.anomaly-badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 3px;
  color: white;
  font-weight: 600;
}
.anomaly-type {
  font-size: 12px;
  color: var(--on-text-dim);
  font-family: var(--on-font-mono, monospace);
}
.anomaly-message {
  font-size: 13px;
  color: var(--on-text);
  margin-bottom: 8px;
}
.anomaly-details {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.detail-tag {
  font-size: 11px;
  padding: 2px 8px;
  background: var(--on-bg);
  border-radius: 3px;
  color: var(--on-text-dim);
  font-family: var(--on-font-mono, monospace);
}
</style>
