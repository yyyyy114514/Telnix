<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useFlowsStore } from '../stores/flows'
import type { Flow } from '../api/client'

// 瀑布流时间线视图：横轴为时间，每行一个请求，类似 Chrome DevTools Network Waterfall
const { t } = useI18n()
const router = useRouter()
const flows = useFlowsStore()
const store = useFlowsStore()

// 时间范围：最近 100 / 500 / 全部
const range = ref<'100' | '500' | 'all'>('500')
// 按 host 分组开关
const groupByHost = ref(false)
// 按 host 筛选（精确匹配）
const hostFilter = ref('')
// 已知 host 列表（从 flows 聚合）
const knownHosts = ref<string[]>([])
function updateKnownHosts() {
  const set = new Set<string>()
  for (const f of store.flows) {
    if (f.host) set.add(f.host)
  }
  knownHosts.value = [...set].sort()
}
// 缩放倍数（横向比例尺）
const zoom = ref(1)

// 回放模式（Back-in-Time）：拖动时间滑块查看历史某时刻的流量状态
const playbackMode = ref(false)
// 回放时间点（epoch ms），0 表示最新
const playbackTime = ref(0)
// 全局时间边界（所有 flows，不受 range 限制）
const globalTimeBounds = computed(() => {
  const list = sortedFlows.value
  if (!list.length) return { min: 0, max: 0, span: 1 }
  let min = Infinity
  let max = -Infinity
  for (const f of list) {
    const t = new Date(f.timestamp).getTime()
    if (t < min) min = t
    if (t > max) max = t
  }
  return { min, max, span: Math.max(1, max - min) }
})
// 回放滑块位置（0-100 百分比）
const playbackPercent = computed({
  get: () => {
    if (!playbackTime.value) return 100
    const { min, span } = globalTimeBounds.value
    return Math.min(100, Math.max(0, ((playbackTime.value - min) / span) * 100))
  },
  set: (v: number) => {
    const { min, span } = globalTimeBounds.value
    playbackTime.value = min + (v / 100) * span
  },
})
// 回放时间点格式化
const playbackTimeLabel = computed(() => {
  if (!playbackTime.value) return ''
  const d = new Date(playbackTime.value)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
})
// 切换回放模式
function togglePlayback() {
  playbackMode.value = !playbackMode.value
  if (playbackMode.value) {
    // 进入回放模式时，默认定位到最新
    playbackTime.value = 0
  }
}
// 回放到最新
function playbackToLatest() {
  playbackTime.value = 0
}

// 左侧信息列宽度（与 CSS 中保持一致）
const INFO_WIDTH = 440
// 瀑布流区域基础宽度，随 zoom 缩放
const BASE_WATERFALL_WIDTH = 700

// store.flows 按 id 降序（最新在前），反转为升序按时间从前到后排列
const sortedFlows = computed<Flow[]>(() => {
  const list = [...store.flows]
  list.reverse()
  return list
})

// 按时间范围过滤：取最近的 N 条（反序后最近的在数组尾部）
const visibleFlows = computed<Flow[]>(() => {
  const list = sortedFlows.value
  // host 筛选
  const host = hostFilter.value.trim()
  const filtered = host ? list.filter(f => f.host === host) : list
  // 回放模式：只显示回放时间点之前的 flows
  if (playbackMode.value && playbackTime.value > 0) {
    return filtered.filter(f => new Date(f.timestamp).getTime() <= playbackTime.value)
  }
  if (range.value === 'all') return filtered
  const n = range.value === '100' ? 100 : 500
  return filtered.slice(-n)
})

// 时间边界：最早开始 ~ 最晚结束
const timeBounds = computed(() => {
  const list = visibleFlows.value
  if (!list.length) return { min: 0, max: 0, span: 1 }
  let min = Infinity
  let max = -Infinity
  for (const f of list) {
    const start = new Date(f.timestamp).getTime()
    const dur = f.duration_ms || 0
    const end = start + dur
    if (start < min) min = start
    if (end > max) max = end
  }
  const span = Math.max(1, max - min)
  return { min, max, span }
})

// 瀑布流区域像素宽度
const waterfallWidth = computed(() => Math.round(BASE_WATERFALL_WIDTH * zoom.value))
const contentWidth = computed(() => INFO_WIDTH + waterfallWidth.value)

// 时间轴刻度（11 个刻度，10 等分）
const axisTicks = computed(() => {
  const { min, span } = timeBounds.value
  const w = waterfallWidth.value
  const ticks: { x: number; label: string }[] = []
  const count = 10
  for (let i = 0; i <= count; i++) {
    const x = (i / count) * w
    const ts = min + (i / count) * span
    const d = new Date(ts)
    const label = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    ticks.push({ x, label })
  }
  return ticks
})

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

// 按 host 分组
interface FlowGroup {
  key: string
  flows: Flow[]
  bounds: { min: number; max: number; span: number }
}
const groups = computed<FlowGroup[]>(() => {
  const list = visibleFlows.value
  if (!groupByHost.value) return [{ key: '', flows: list, bounds: timeBounds.value }]
  const map = new Map<string, Flow[]>()
  for (const f of list) {
    const k = f.host || '(no host)'
    if (!map.has(k)) map.set(k, [])
    map.get(k)!.push(f)
  }
  return [...map.entries()].map(([key, flows]) => {
    // 按 host 分组时，每组使用自己的时间边界（只显示该 host 的数据范围）
    let min = Infinity
    let max = -Infinity
    for (const f of flows) {
      const start = new Date(f.timestamp).getTime()
      const end = start + (f.duration_ms || 0)
      if (start < min) min = start
      if (end > max) max = end
    }
    const span = Math.max(1, max - min)
    return { key, flows, bounds: { min, max, span } }
  })
})

// 状态码 → 颜色（2xx 绿 / 3xx 蓝 / 4xx 橙 / 5xx 红 / null 灰）
function statusColor(code: number | null): string {
  if (code == null) return 'var(--on-text-dim)'
  if (code < 300) return 'var(--on-success)'
  if (code < 400) return 'var(--on-redirect)'
  if (code < 500) return 'var(--on-warn)'
  return 'var(--on-error)'
}

// 瀑布条左边距（像素）—— 按 host 分组时使用组内时间边界
function barLeft(f: Flow, bounds?: { min: number; span: number }): number {
  const b = bounds || timeBounds.value
  const start = new Date(f.timestamp).getTime() - b.min
  return (start / b.span) * waterfallWidth.value
}
// 瀑布条宽度（像素，最小 2px 保证可见）
function barWidth(f: Flow, bounds?: { span: number }): number {
  const b = bounds || timeBounds.value
  const dur = f.duration_ms || 0
  const w = (dur / b.span) * waterfallWidth.value
  return Math.max(2, w)
}

// 生成组内时间轴刻度（按 host 分组时每组独立刻度）
function groupAxisTicks(bounds: { min: number; span: number }) {
  const w = waterfallWidth.value
  const ticks: { x: number; label: string }[] = []
  const count = 6
  for (let i = 0; i <= count; i++) {
    const x = (i / count) * w
    const ts = bounds.min + (i / count) * bounds.span
    const d = new Date(ts)
    const label = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    ticks.push({ x, label })
  }
  return ticks
}

// URL 截断
function truncateUrl(url: string, max = 55): string {
  if (!url) return ''
  if (url.length <= max) return url
  return url.slice(0, max - 1) + '…'
}

// 格式化大小
function formatSize(bytes: number | null): string {
  if (bytes == null) return '-'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(2) + ' MB'
}
// 格式化耗时
function formatDuration(ms: number | null): string {
  if (ms == null) return '-'
  if (ms < 1000) return ms + ' ms'
  return (ms / 1000).toFixed(2) + ' s'
}
// 格式化字节数
function formatBytes(n: number): string {
  if (!n) return '0 B'
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}
// 格式化开始时间（含毫秒）
function formatTime(ts: string): string {
  const d = new Date(ts)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, '0')}`
}

// 点击行选中 flow
function selectFlow(f: Flow) {
  store.select(f.id)
}

// 按 host 分组：选中的 host（聚合视图）
const selectedHost = ref<string>('')
function selectGroup(g: FlowGroup) {
  selectedHost.value = g.key
  // 选中该组的第一个 flow，使右侧详情面板能展示数据
  if (g.flows.length) store.select(g.flows[0].id)
}
// 聚合行：状态码分布
function statusSummary(flows: Flow[]): string {
  const map: Record<string, number> = {}
  for (const f of flows) {
    const c = f.status_code ?? 0
    const k = c === 0 ? '-' : (c < 300 ? '2xx' : c < 400 ? '3xx' : c < 500 ? '4xx' : '5xx')
    map[k] = (map[k] || 0) + 1
  }
  return Object.entries(map).map(([k, v]) => `${k}:${v}`).join(' ')
}
// 聚合行：总字节
function totalSize(flows: Flow[]): number {
  return flows.reduce((s, f) => s + (f.size || 0), 0)
}

// 悬浮 tooltip
const tooltip = ref({
  visible: false,
  x: 0,
  y: 0,
  url: '',
  start: '',
  duration: '',
  status: '',
  size: '',
})
const TOOLTIP_W = 280
const TOOLTIP_H = 130

function showTooltip(e: MouseEvent, f: Flow) {
  tooltip.value = {
    visible: true,
    x: 0,
    y: 0,
    url: f.url,
    start: formatTime(f.timestamp),
    duration: formatDuration(f.duration_ms),
    status: f.status_code != null ? String(f.status_code) : '-',
    size: formatSize(f.size),
  }
  moveTooltip(e)
}
function moveTooltip(e: MouseEvent) {
  // 避免超出视口右下边界
  let x = e.clientX + 16
  let y = e.clientY + 16
  if (x + TOOLTIP_W > window.innerWidth) x = e.clientX - TOOLTIP_W - 12
  if (y + TOOLTIP_H > window.innerHeight) y = e.clientY - TOOLTIP_H - 12
  tooltip.value.x = Math.max(8, x)
  tooltip.value.y = Math.max(8, y)
}
function hideTooltip() {
  tooltip.value.visible = false
}

const isEmpty = computed(() => visibleFlows.value.length === 0)

// 实时更新：启动 SSE 推送 + 兜底轮询，保证瀑布流随新流量实时刷新
let pollTimer: number | null = null
onMounted(() => {
  // 首次加载基线流量（若 store 为空）
  if (!store.flows.length) store.loadAllFlows()
  store.startSSE()
  // 兜底轮询：3 秒一次（SSE 正常时不触发），SSE 失活时补齐新流量
  // 性能优化：从 2s 改为 3s，SSE 正常时轮询仅检查健康状态不拉数据
  pollTimer = window.setInterval(() => {
    if (document.hidden) return
    const sseHealthy = store.sseActive && !store.sseStale()
    if (!sseHealthy) {
      if (store.maxFlowId) store.pollNewFlows()
      else store.loadAllFlows()
    }
  }, 3000)
})
onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
  // 清理 SSE：关闭 EventSource + 移除 window 事件监听器
  store.cleanup()
})
</script>

<template>
  <div class="timeline-view full flex flex-col">
    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <el-button size="small" @click="() => router.push(flows.lastPage || '/analyze')" :title="t('common.back')">
        <el-icon><ArrowLeft /></el-icon>&nbsp;{{ t('common.back') }}
      </el-button>
      <span class="page-title">
        <el-icon><Timer /></el-icon>&nbsp;{{ t('nav.timeline') }}
      </span>
      <span class="sep"></span>
      <span class="lbl">{{ t('timeline.range') }}</span>
      <el-radio-group v-model="range" size="small">
        <el-radio-button value="100">{{ t('timeline.range100') }}</el-radio-button>
        <el-radio-button value="500">{{ t('timeline.range500') }}</el-radio-button>
        <el-radio-button value="all">{{ t('timeline.rangeAll') }}</el-radio-button>
      </el-radio-group>
      <span class="sep"></span>
      <span class="lbl">{{ t('timeline.groupByHost') }}</span>
      <el-switch v-model="groupByHost" size="small" />
      <span class="sep"></span>
      <span class="lbl">{{ t('common.host') }}</span>
      <el-select
        v-model="hostFilter"
        size="small"
        clearable
        filterable
        :placeholder="t('flowList.hostEnterPlaceholder')"
        style="width: 200px"
        @focus="updateKnownHosts"
      >
        <el-option v-for="h in knownHosts" :key="h" :label="h" :value="h" />
      </el-select>
      <span class="sep"></span>
      <span class="lbl">{{ t('timeline.zoom') }}</span>
      <el-slider v-model="zoom" :min="1" :max="10" :step="0.5" size="small" style="width: 150px" />
      <span class="sep"></span>
      <el-button size="small" :type="playbackMode ? 'warning' : 'default'" @click="togglePlayback" :title="t('timeline.playback')">
        <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('timeline.playback') }}
      </el-button>
      <div class="flex-1"></div>
      <span class="count">{{ t('timeline.flowCount', { n: visibleFlows.length }) }}</span>
    </div>

    <!-- 回放时间滑块（仅回放模式显示） -->
    <div v-if="playbackMode" class="playback-bar">
      <span class="lbl">{{ t('timeline.playbackTime') }}</span>
      <el-slider v-model="playbackPercent" :min="0" :max="100" :step="0.1" size="small" style="flex: 1; max-width: 600px" />
      <span class="playback-time mono">{{ playbackTimeLabel || t('timeline.playbackLatest') }}</span>
      <el-button size="small" @click="playbackToLatest" :title="t('timeline.playbackLatest')">
        <el-icon><RefreshRight /></el-icon>
      </el-button>
      <span class="text-dim" style="font-size: 11px; margin-left: 8px">{{ t('timeline.playbackHint') }}</span>
    </div>

    <!-- 主体（横向 + 纵向滚动） -->
    <div class="timeline-body flex-1">
      <div v-if="isEmpty" class="empty">{{ t('timeline.empty') }}</div>
      <div v-else class="timeline-content" :style="{ width: contentWidth + 'px' }">
        <!-- 全局时间轴表头（仅未分组时显示，sticky 顶部） -->
        <div v-if="!groupByHost" class="axis-row">
          <div class="info-cols">
            <span class="col-method">{{ t('timeline.method') }}</span>
            <span class="col-url">{{ t('timeline.url') }}</span>
            <span class="col-status">{{ t('timeline.status') }}</span>
            <span class="col-duration">{{ t('timeline.duration') }}</span>
          </div>
          <div class="axis-ticks">
            <div
              v-for="(tick, i) in axisTicks"
              :key="i"
              class="tick"
              :style="{ left: tick.x + 'px' }"
            >
              <div class="tick-line"></div>
              <div class="tick-label">{{ tick.label }}</div>
            </div>
          </div>
        </div>

        <!-- 分组 / 行 -->
        <template v-if="groupByHost">
          <div v-for="g in groups" :key="g.key || '_all'" class="group-section"
               :class="{ 'group-selected': selectedHost === g.key }"
               @click="selectGroup(g)">
            <div class="group-header">
              <div class="info-cols">
                <span class="group-key">{{ g.key }}</span>
                <span class="group-count">{{ g.flows.length }}</span>
                <span class="group-status">{{ statusSummary(g.flows) }}</span>
                <span class="group-size">{{ formatBytes(totalSize(g.flows)) }}</span>
              </div>
              <div class="axis-ticks">
                <div
                  v-for="(tick, i) in groupAxisTicks(g.bounds)"
                  :key="i"
                  class="tick"
                  :style="{ left: tick.x + 'px' }"
                >
                  <div class="tick-line"></div>
                  <div class="tick-label">{{ tick.label }}</div>
                </div>
              </div>
            </div>
            <!-- 聚合瀑布条：每个 flow 一条，叠加在同一行，不逐条展开 URL -->
            <div class="waterfall-area group-waterfall">
              <div
                v-for="(tick, i) in groupAxisTicks(g.bounds)"
                :key="i"
                class="grid-line"
                :style="{ left: tick.x + 'px' }"
              ></div>
              <div
                v-for="f in g.flows"
                :key="f.id"
                class="bar group-bar"
                :style="{
                  left: barLeft(f, g.bounds) + 'px',
                  width: barWidth(f, g.bounds) + 'px',
                  background: statusColor(f.status_code),
                }"
                @mouseenter="showTooltip($event, f)"
                @mousemove="moveTooltip($event)"
                @mouseleave="hideTooltip"
                @click.stop="selectFlow(f)"
              ></div>
            </div>
          </div>
        </template>
        <template v-else>
          <div v-for="f in visibleFlows" :key="f.id" class="flow-row" :class="{ selected: f.id === store.selectedId }" @click="selectFlow(f)">
            <div class="info-cols">
              <span class="col-method mono" :class="'m-' + (f.method || '').toLowerCase()">{{ f.method }}</span>
              <span class="col-url" :title="f.url">{{ truncateUrl(f.url) }}</span>
              <span class="col-status mono" :style="{ color: statusColor(f.status_code) }">{{ f.status_code ?? '-' }}</span>
              <span class="col-duration mono">{{ formatDuration(f.duration_ms) }}</span>
            </div>
            <div class="waterfall-area">
              <div v-for="(tick, i) in axisTicks" :key="i" class="grid-line" :style="{ left: tick.x + 'px' }"></div>
              <div class="bar" :style="{ left: barLeft(f) + 'px', width: barWidth(f) + 'px', background: statusColor(f.status_code) }" @mouseenter="showTooltip($event, f)" @mousemove="moveTooltip($event)" @mouseleave="hideTooltip"></div>
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- 浮动 tooltip（teleport 到 body，fixed 定位跟随鼠标） -->
    <teleport to="body">
      <div
        v-if="tooltip.visible"
        class="tl-tooltip"
        :style="{ left: tooltip.x + 'px', top: tooltip.y + 'px' }"
      >
        <div class="tt-url">{{ tooltip.url }}</div>
        <div class="tt-row"><span class="tt-k">{{ t('timeline.tooltipStart') }}</span><span class="tt-v">{{ tooltip.start }}</span></div>
        <div class="tt-row"><span class="tt-k">{{ t('timeline.tooltipDuration') }}</span><span class="tt-v">{{ tooltip.duration }}</span></div>
        <div class="tt-row"><span class="tt-k">{{ t('timeline.tooltipStatus') }}</span><span class="tt-v">{{ tooltip.status }}</span></div>
        <div class="tt-row"><span class="tt-k">{{ t('timeline.tooltipSize') }}</span><span class="tt-v">{{ tooltip.size }}</span></div>
      </div>
    </teleport>
  </div>
</template>

<style scoped>
.timeline-view { background: var(--on-bg); }

/* 回放时间滑块栏 */
.playback-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 16px;
  background: var(--on-bg-active);
  border-bottom: 1px solid var(--on-border);
}
.playback-bar .lbl { font-size: 12px; color: var(--on-text-dim); white-space: nowrap; }
.playback-time { font-size: 13px; font-weight: 600; color: var(--on-accent); min-width: 60px; }

/* 工具栏 */
.toolbar {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.toolbar .page-title { font-size: 14px; font-weight: 600; color: var(--on-text); display: flex; align-items: center; }
.toolbar .lbl { font-size: 12px; color: var(--on-text-muted); white-space: nowrap; }
.toolbar .sep { width: 1px; height: 20px; background: var(--on-border-light); }
.toolbar .count { font-size: 12px; color: var(--on-text-dim); font-family: var(--on-font-mono); }

/* 主体滚动容器 */
.timeline-body {
  position: relative;
  overflow: auto;
  min-height: 0;
}
.empty {
  text-align: center; padding: 64px;
  color: var(--on-text-dim); font-size: 14px;
}

/* 内容区（宽度 = 信息列 + 瀑布流区） */
.timeline-content { position: relative; min-width: 100%; }

/* 时间轴表头：sticky 顶部 */
.axis-row {
  position: sticky; top: 0; z-index: 3;
  display: flex; height: 30px;
  background: var(--on-bg-elevated);
  border-bottom: 1px solid var(--on-border);
}

/* 左侧信息列：sticky 左侧（滚动时固定） */
.info-cols {
  position: sticky; left: 0; z-index: 2;
  display: flex; align-items: center;
  width: 440px; flex-shrink: 0;
  background: var(--on-bg-elevated);
  border-right: 1px solid var(--on-border);
}
.axis-row .info-cols { z-index: 4; } /* 左上角同时 sticky */

.col-method {
  width: 60px; flex-shrink: 0; padding: 0 8px;
  font-size: 11px; font-weight: 700; text-align: center;
}
.col-url {
  flex: 1; min-width: 0; padding: 0 8px;
  font-size: 12px; color: var(--on-text-muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.col-status {
  width: 50px; flex-shrink: 0; padding: 0 8px;
  text-align: right; font-size: 12px;
}
.col-duration {
  width: 90px; flex-shrink: 0; padding: 0 8px;
  text-align: right; font-size: 12px; color: var(--on-text-muted);
}
.axis-row .col-method,
.axis-row .col-url,
.axis-row .col-status,
.axis-row .col-duration {
  font-size: 11px; color: var(--on-text-dim); font-weight: 600;
}

/* 时间轴刻度 */
.axis-ticks { position: relative; flex: 1; height: 30px; }
.tick { position: absolute; top: 0; height: 100%; }
.tick-line { position: absolute; top: 0; left: 0; width: 1px; height: 8px; background: var(--on-border); }
.tick-label {
  position: absolute; top: 11px; left: 0; transform: translateX(-50%);
  font-size: 10px; color: var(--on-text-dim);
  font-family: var(--on-font-mono); white-space: nowrap;
}

/* 行 */
.flow-row {
  display: flex; height: 26px; align-items: center;
  border-bottom: 1px solid var(--on-border-light);
  cursor: pointer;
  transition: background .1s ease;
}
.flow-row:hover { background: var(--on-bg-hover); }
.flow-row.selected { background: var(--on-bg-active); }

/* 瀑布流区域 */
.waterfall-area { position: relative; flex: 1; height: 26px; min-width: 0; }
.grid-line {
  position: absolute; top: 0; width: 1px; height: 100%;
  background: var(--on-border-light); opacity: 0.5; pointer-events: none;
}
.bar {
  position: absolute; top: 5px; height: 16px;
  border-radius: 2px; opacity: 0.82; min-width: 2px;
  cursor: pointer;
  transition: opacity .1s ease;
}
.bar:hover { opacity: 1; }

/* 分组容器：每组独立 section（聚合行模式，点击选中整组） */
.group-section {
  border-bottom: 2px solid var(--on-border);
  margin-bottom: 4px;
  cursor: pointer;
  transition: background .12s ease;
}
.group-section:last-child { border-bottom: none; }
.group-section:hover { background: var(--on-bg-hover, rgba(64, 158, 255, 0.05)); }
.group-section.group-selected {
  background: var(--on-bg-active);
  box-shadow: inset 3px 0 0 var(--on-accent);
}

/* 分组头：按 host 分组时，组头带独立时间轴 */
.group-header {
  position: sticky; top: 0; z-index: 3;
  display: flex; align-items: center;
  height: 30px;
  background: var(--on-bg-active);
  border-bottom: 1px solid var(--on-border);
  font-size: 12px; font-weight: 600;
}
.group-header .info-cols {
  position: sticky; left: 0; z-index: 4;
  background: var(--on-bg-active);
  border-right: 1px solid var(--on-border);
  display: flex; align-items: center; gap: 8px;
}
.group-key { color: var(--on-text); font-weight: 600; flex: 1; min-width: 0; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.group-count {
  color: var(--on-text-dim); font-family: var(--on-font-mono); font-size: 11px;
  background: var(--on-bg-elevated); padding: 1px 6px; border-radius: 8px; flex-shrink: 0;
}
.group-status { color: var(--on-text-dim); font-family: var(--on-font-mono); font-size: 10px; flex-shrink: 0; }
.group-size { color: var(--on-text-dim); font-family: var(--on-font-mono); font-size: 10px; flex-shrink: 0; }

/* 聚合瀑布区域：所有 bar 叠加在一行 */
.group-waterfall {
  height: 26px; position: relative;
}
.group-bar {
  top: 5px; height: 16px; opacity: 0.7;
}
.group-bar:hover { opacity: 1; }

/* 方法标签颜色 */
.m-get { color: var(--on-success); }
.m-post { color: var(--on-accent); }
.m-put { color: var(--on-warn); }
.m-delete { color: var(--on-error); }
.m-patch { color: var(--on-redirect); }
.m-head, .m-options { color: var(--on-text-muted); }

/* Tooltip */
.tl-tooltip {
  position: fixed; z-index: 9999; pointer-events: none;
  min-width: 200px; max-width: 380px;
  padding: 8px 10px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border);
  border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);
  font-size: 12px;
}
.tl-tooltip .tt-url {
  font-family: var(--on-font-mono);
  word-break: break-all;
  margin-bottom: 6px;
  color: var(--on-text); font-weight: 600;
  max-height: 80px; overflow: hidden;
}
.tl-tooltip .tt-row { display: flex; justify-content: space-between; gap: 12px; line-height: 1.7; }
.tl-tooltip .tt-k { color: var(--on-text-muted); }
.tl-tooltip .tt-v { color: var(--on-text); font-family: var(--on-font-mono); }
</style>
