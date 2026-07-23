<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, type FlowOverview } from '../api/client'

// 多维聚合统计数据
const overview = ref<FlowOverview | null>(null)

// 实时流量历史（最近 60 个采样点）
interface RatePoint { t: number; in: number; out: number }
const rateHistory = ref<RatePoint[]>([])
const MAX_HISTORY = 60
let lastInBytes = 0
let lastOutBytes = 0
let lastSampleTs = 0

// 单位切换
type Unit = 'packets' | 'bytes'
const unit = ref<Unit>('packets')

let pollTimer: number | null = null

function formatBytes(n: number): string {
  if (!n) return '0 B'
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB'
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB'
}

function formatNumber(n: number): string {
  if (n < 1000) return String(n)
  if (n < 1000000) return (n / 1000).toFixed(1) + 'K'
  return (n / 1000000).toFixed(1) + 'M'
}

function formatRate(n: number): string {
  if (unit.value === 'bytes') return formatBytes(n) + '/s'
  return formatNumber(n) + ' pkt/s'
}

// 协议环形图数据
const PROTO_COLORS: Record<string, string> = {
  http: '#8B5CF6',
  https: '#a78bfa',
  ws: '#2dd4bf',
  tcp: '#58a6ff',
  udp: '#fb7185',
  dns: '#fefc86',
}
const PROTO_LABELS: Record<string, string> = {
  http: 'HTTP', https: 'HTTPS', ws: 'WebSocket',
  tcp: 'TCP', udp: 'UDP', dns: 'DNS',
}

const donutSegments = computed(() => {
  const data = overview.value
  if (!data) return []
  const total = data.total || 1
  const radius = 54
  const circ = 2 * Math.PI * radius
  let offset = 0
  return data.by_protocol.map(p => {
    const pct = p.count / total
    const dash = pct * circ
    const seg = {
      label: PROTO_LABELS[p.key] || p.key.toUpperCase(),
      value: p.count,
      pct,
      color: PROTO_COLORS[p.key] || '#8b949e',
      dashArray: `${dash} ${circ - dash}`,
      dashOffset: -offset * circ,
    }
    offset += pct
    return seg
  })
})

// 实时流量图 SVG 路径（上下双向面积）
const chartW = 720
const chartH = 140
const pad = 6

const incomingPath = computed(() => buildAreaPath(rateHistory.value, 'in', true))
const outgoingPath = computed(() => buildAreaPath(rateHistory.value, 'out', false))

function buildAreaPath(data: RatePoint[], field: 'in' | 'out', upper: boolean): string {
  if (data.length < 2) return ''
  const maxVal = Math.max(...data.map(d => d[field]), 1)
  const step = (chartW - pad * 2) / (MAX_HISTORY - 1)
  const midY = chartH / 2
  const amp = chartH / 2 - pad
  const pts = data.map((d, i) => {
    const x = pad + i * step
    const v = d[field] / maxVal
    const y = upper ? midY - v * amp : midY + v * amp
    return `${x.toFixed(1)},${y.toFixed(1)}`
  })
  const first = pts[0]
  const last = pts[pts.length - 1]
  const baseY = midY
  return `M ${first} L ${pts.join(' L ')} L ${last.split(',')[0]},${baseY} L ${first.split(',')[0]},${baseY} Z`
}

// 当前速率
const currentRate = computed(() => {
  const last = rateHistory.value[rateHistory.value.length - 1]
  return { in: last?.in || 0, out: last?.out || 0 }
})

// 状态码分布
const STATUS_COLORS: Record<string, string> = {
  '1xx': '#58a6ff', '2xx': '#3fb950', '3xx': '#a78bfa',
  '4xx': '#fefc86', '5xx': '#f85149', 'other': '#8b949e',
}
const statusBars = computed(() => {
  const data = overview.value
  if (!data) return []
  const max = Math.max(...data.by_status_range.map(s => s.count), 1)
  return data.by_status_range.map(s => ({
    ...s,
    color: STATUS_COLORS[s.key] || '#8b949e',
    heightPct: (s.count / max) * 100,
  }))
})

// 方法分布（取前 8）
const methodBars = computed(() => {
  const data = overview.value
  if (!data) return []
  const max = Math.max(...data.by_method.map(m => m.count), 1)
  return data.by_method.slice(0, 8).map(m => ({
    ...m,
    widthPct: (m.count / max) * 100,
  }))
})

// Host / 进程 / 属地 列表
const topHosts = computed(() => {
  const data = overview.value
  if (!data) return []
  const max = Math.max(...data.by_host.map(h => h.bytes || h.count), 1)
  return data.by_host.slice(0, 10).map(h => ({
    name: h.key,
    count: h.count,
    bytes: h.bytes,
    widthPct: ((h.bytes || h.count) / max) * 100,
  }))
})

const topProcesses = computed(() => {
  const data = overview.value
  if (!data) return []
  const max = Math.max(...data.by_process.map(p => p.bytes || p.count), 1)
  return data.by_process.slice(0, 10).map(p => ({
    name: p.key,
    count: p.count,
    bytes: p.bytes,
    widthPct: ((p.bytes || p.count) / max) * 100,
  }))
})

const topRegions = computed(() => {
  const data = overview.value
  if (!data) return []
  const max = Math.max(...data.by_ip_region.map(r => r.count), 1)
  return data.by_ip_region.slice(0, 10).map(r => ({
    name: r.key,
    count: r.count,
    widthPct: (r.count / max) * 100,
  }))
})

async function loadStats() {
  try {
    const data = await api.getFlowsOverview()
    overview.value = data
    // 计算实时速率（基于 incoming_bytes / outgoing_bytes 的增量）
    const now = Date.now()
    const inBytes = data.incoming_bytes || 0
    const outBytes = data.outgoing_bytes || 0
    if (lastSampleTs > 0) {
      const dt = (now - lastSampleTs) / 1000
      const inRate = dt > 0 ? Math.max(0, (inBytes - lastInBytes) / dt) : 0
      const outRate = dt > 0 ? Math.max(0, (outBytes - lastOutBytes) / dt) : 0
      rateHistory.value.push({ t: now, in: inRate, out: outRate })
      if (rateHistory.value.length > MAX_HISTORY) rateHistory.value.shift()
    }
    lastInBytes = inBytes
    lastOutBytes = outBytes
    lastSampleTs = now
  } catch { /* ignore */ }
}

onMounted(() => {
  loadStats()
  // 性能优化：3 秒轮询（后端有 2 秒 TTL 缓存），避免高频聚合查询压垮 SQLite
  pollTimer = window.setInterval(loadStats, 3000)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="cool-page">
    <!-- 顶部标题栏 -->
    <header class="cool-header">
      <div class="cool-brand">
        <span class="cool-logo">◆</span>
        <span class="cool-title">CoolUI</span>
        <span class="cool-sub">流量监控仪表盘</span>
      </div>
      <div class="cool-unit">
        <button :class="{ on: unit === 'packets' }" @click="unit = 'packets'">包</button>
        <button :class="{ on: unit === 'bytes' }" @click="unit = 'bytes'">字节</button>
      </div>
    </header>

    <main class="cool-main">
      <!-- 第一行：统计卡片 + 协议环形图 -->
      <section class="row row-cards-donut">
        <div class="stat-cards">
          <div class="stat-card">
            <div class="stat-icon icon-total">◉</div>
            <div class="stat-body">
              <div class="stat-value">{{ formatNumber(overview?.total || 0) }}</div>
              <div class="stat-label">总流量</div>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-icon icon-in">↓</div>
            <div class="stat-body">
              <div class="stat-value">{{ formatBytes(overview?.incoming_bytes || 0) }}</div>
              <div class="stat-label">入站字节</div>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-icon icon-out">↑</div>
            <div class="stat-body">
              <div class="stat-value">{{ formatBytes(overview?.outgoing_bytes || 0) }}</div>
              <div class="stat-label">出站字节</div>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-icon icon-ok">✓</div>
            <div class="stat-body">
              <div class="stat-value">{{ formatNumber(overview?.success_count || 0) }}</div>
              <div class="stat-label">成功 (2xx)</div>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-icon icon-err">✕</div>
            <div class="stat-body">
              <div class="stat-value">{{ formatNumber(overview?.error_count || 0) }}</div>
              <div class="stat-label">错误 (4xx/5xx)</div>
            </div>
          </div>
        </div>

        <div class="donut-card">
          <div class="panel-title">协议分布</div>
          <div class="donut-wrap">
            <svg width="150" height="150" viewBox="0 0 150 150" class="donut-svg">
              <circle cx="75" cy="75" r="54" fill="none"
                      stroke="rgba(139,92,246,0.08)" stroke-width="16" />
              <circle v-for="seg in donutSegments" :key="seg.label"
                      cx="75" cy="75" r="54" fill="none"
                      :stroke="seg.color" stroke-width="16"
                      :stroke-dasharray="seg.dashArray"
                      :stroke-dashoffset="seg.dashOffset"
                      transform="rotate(-90 75 75)"
                      class="donut-seg" />
              <text x="75" y="72" text-anchor="middle" class="donut-center-val">
                {{ formatNumber(overview?.total || 0) }}
              </text>
              <text x="75" y="90" text-anchor="middle" class="donut-center-label">TOTAL</text>
            </svg>
            <div class="donut-legend">
              <div v-for="seg in donutSegments" :key="seg.label" class="legend-row">
                <span class="legend-dot" :style="{ background: seg.color }"></span>
                <span class="legend-name">{{ seg.label }}</span>
                <span class="legend-val">{{ seg.value }}</span>
                <span class="legend-pct">{{ (seg.pct * 100).toFixed(1) }}%</span>
              </div>
              <div v-if="!donutSegments.length" class="empty-hint">暂无数据</div>
            </div>
          </div>
        </div>
      </section>

      <!-- 第二行：实时流量曲线（上下双向） -->
      <section class="chart-card">
        <div class="chart-head">
          <span class="panel-title">实时流量</span>
          <div class="rate-show">
            <span class="rate-in"><i>↓</i>{{ formatRate(currentRate.in) }}</span>
            <span class="rate-out"><i>↑</i>{{ formatRate(currentRate.out) }}</span>
            <span class="rate-avg">平均耗时 {{ overview?.avg_duration_ms || 0 }}ms</span>
          </div>
        </div>
        <div class="chart-box">
          <svg :viewBox="`0 0 ${chartW} ${chartH}`" preserveAspectRatio="none" class="chart-svg">
            <!-- 网格线 -->
            <line :x1="pad" :y1="chartH / 2" :x2="chartW - pad" :y2="chartH / 2"
                  stroke="rgba(139,92,246,0.15)" stroke-width="0.5" stroke-dasharray="3,3" />
            <line :x1="pad" :y1="pad" :x2="chartW - pad" :y2="pad"
                  stroke="rgba(139,92,246,0.08)" stroke-width="0.5" />
            <line :x1="pad" :y1="chartH - pad" :x2="chartW - pad" :y2="chartH - pad"
                  stroke="rgba(139,92,246,0.08)" stroke-width="0.5" />
            <!-- 入站面积（上半，青色） -->
            <path :d="incomingPath" fill="rgba(45,212,191,0.22)" stroke="#2dd4bf" stroke-width="1.4" />
            <!-- 出站面积（下半，橙黄） -->
            <path :d="outgoingPath" fill="rgba(254,252,134,0.18)" stroke="#fefc86" stroke-width="1.4" />
          </svg>
          <div v-if="rateHistory.length < 2" class="chart-empty">采集中…</div>
        </div>
      </section>

      <!-- 第三行：Host / 进程 / IP 属地 三栏 -->
      <section class="row row-3col">
        <div class="list-card">
          <div class="panel-title">热门 Host</div>
          <div class="bar-list">
            <div v-for="h in topHosts" :key="h.name" class="bar-row">
              <span class="bar-name" :title="h.name">{{ h.name }}</span>
              <div class="bar-track">
                <div class="bar-fill bar-fill-in" :style="{ width: h.widthPct + '%' }"></div>
              </div>
              <span class="bar-val">{{ formatBytes(h.bytes) }}</span>
            </div>
            <div v-if="!topHosts.length" class="empty-hint">暂无数据</div>
          </div>
        </div>

        <div class="list-card">
          <div class="panel-title">网络程序</div>
          <div class="bar-list">
            <div v-for="p in topProcesses" :key="p.name" class="bar-row">
              <span class="bar-name" :title="p.name">{{ p.name }}</span>
              <div class="bar-track">
                <div class="bar-fill bar-fill-out" :style="{ width: p.widthPct + '%' }"></div>
              </div>
              <span class="bar-val">{{ formatBytes(p.bytes) }}</span>
            </div>
            <div v-if="!topProcesses.length" class="empty-hint">暂无数据</div>
          </div>
        </div>

        <div class="list-card">
          <div class="panel-title">IP 属地分布</div>
          <div class="bar-list">
            <div v-for="r in topRegions" :key="r.name" class="bar-row">
              <span class="bar-name" :title="r.name">{{ r.name }}</span>
              <div class="bar-track">
                <div class="bar-fill bar-fill-purple" :style="{ width: r.widthPct + '%' }"></div>
              </div>
              <span class="bar-val">{{ r.count }}</span>
            </div>
            <div v-if="!topRegions.length" class="empty-hint">暂无数据</div>
          </div>
        </div>
      </section>

      <!-- 第四行：状态码柱状图 + 方法分布 -->
      <section class="row row-2col">
        <div class="list-card">
          <div class="panel-title">状态码分布</div>
          <div class="status-bars">
            <div v-for="s in statusBars" :key="s.key" class="status-col">
              <div class="status-track">
                <div class="status-bar" :style="{ height: s.heightPct + '%', background: s.color }"></div>
              </div>
              <span class="status-label">{{ s.key }}</span>
              <span class="status-count">{{ s.count }}</span>
            </div>
          </div>
        </div>

        <div class="list-card">
          <div class="panel-title">请求方法</div>
          <div class="method-list">
            <div v-for="m in methodBars" :key="m.key" class="method-row">
              <span class="method-tag">{{ m.key }}</span>
              <div class="method-track">
                <div class="method-fill" :style="{ width: m.widthPct + '%' }"></div>
              </div>
              <span class="method-count">{{ m.count }}</span>
            </div>
            <div v-if="!methodBars.length" class="empty-hint">暂无数据</div>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.cool-page {
  height: 100%; overflow: auto;
  background: #0d1117;
  color: #c9d1d9;
  font-family: var(--on-font-mono, 'JetBrains Mono', 'Fira Code', 'Consolas', monospace);
}

/* 顶部标题栏 */
.cool-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 20px;
  background: linear-gradient(135deg, #1c315e 0%, #0d1117 100%);
  border-bottom: 1px solid rgba(139, 92, 246, 0.3);
}
.cool-brand { display: flex; align-items: center; gap: 10px; }
.cool-logo {
  color: #8B5CF6; font-size: 22px;
  animation: pulse 2s ease-in-out infinite;
  text-shadow: 0 0 12px rgba(139, 92, 246, 0.8);
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
.cool-title { font-size: 18px; font-weight: 700; color: #fff; letter-spacing: 1px; }
.cool-sub { font-size: 12px; color: #8b949e; margin-left: 6px; }

.cool-unit {
  display: flex; border: 1px solid rgba(139, 92, 246, 0.3);
  border-radius: 4px; overflow: hidden;
}
.cool-unit button {
  padding: 4px 12px; background: transparent; border: none;
  color: #8b949e; cursor: pointer; font-size: 12px;
  font-family: inherit; transition: all 0.2s;
}
.cool-unit button.on { background: rgba(139, 92, 246, 0.2); color: #a78bfa; }
.cool-unit button:hover:not(.on) { background: rgba(139, 92, 246, 0.08); color: #c9d1d9; }

/* 主体 */
.cool-main { padding: 16px; display: flex; flex-direction: column; gap: 14px; }
.row { display: flex; gap: 14px; }
.row-3col > * { flex: 1; }
.row-2col > * { flex: 1; }

/* 面板通用 */
.panel-title {
  font-size: 11px; color: #8b949e; font-weight: 600;
  text-transform: uppercase; letter-spacing: 1px;
  margin-bottom: 12px; padding-bottom: 8px;
  border-bottom: 1px solid rgba(139, 92, 246, 0.15);
}

/* 统计卡片 */
.row-cards-donut { align-items: stretch; }
.stat-cards { display: flex; gap: 10px; flex: 1; }
.stat-card {
  flex: 1; display: flex; align-items: center; gap: 10px;
  padding: 14px; border: 1px solid rgba(139, 92, 246, 0.2);
  border-radius: 6px; background: rgba(28, 49, 94, 0.3);
  transition: border-color 0.2s, transform 0.2s;
}
.stat-card:hover { border-color: #8B5CF6; transform: translateY(-2px); }
.stat-icon {
  font-size: 24px; line-height: 1; width: 32px; text-align: center;
}
.icon-total { color: #8B5CF6; text-shadow: 0 0 8px rgba(139, 92, 246, 0.6); }
.icon-in { color: #2dd4bf; text-shadow: 0 0 8px rgba(45, 212, 191, 0.6); }
.icon-out { color: #fefc86; text-shadow: 0 0 8px rgba(254, 252, 134, 0.6); }
.icon-ok { color: #3fb950; }
.icon-err { color: #f85149; }
.stat-value { font-size: 20px; font-weight: 700; color: #fff; }
.stat-label { font-size: 10px; color: #8b949e; margin-top: 2px; letter-spacing: 0.5px; }

/* 环形图 */
.donut-card {
  padding: 14px; border: 1px solid rgba(139, 92, 246, 0.2);
  border-radius: 6px; background: rgba(28, 49, 94, 0.3);
  min-width: 320px;
}
.donut-wrap { display: flex; align-items: center; gap: 14px; }
.donut-svg { flex-shrink: 0; }
.donut-seg { transition: stroke-dasharray 0.5s ease; }
.donut-center-val {
  font-size: 18px; font-weight: 700; fill: #fff;
  font-family: var(--on-font-mono, monospace);
}
.donut-center-label { font-size: 9px; fill: #8b949e; letter-spacing: 1.5px; }
.donut-legend { display: flex; flex-direction: column; gap: 5px; flex: 1; }
.legend-row {
  display: flex; align-items: center; gap: 8px; font-size: 11px;
}
.legend-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.legend-name { color: #c9d1d9; flex: 1; }
.legend-val { color: #fff; font-weight: 600; min-width: 36px; text-align: right; }
.legend-pct { color: #8b949e; font-size: 10px; min-width: 42px; text-align: right; }

/* 实时图表 */
.chart-card {
  padding: 14px; border: 1px solid rgba(139, 92, 246, 0.2);
  border-radius: 6px; background: rgba(28, 49, 94, 0.3);
}
.chart-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px;
}
.chart-head .panel-title { margin: 0; padding: 0; border: none; }
.rate-show { display: flex; gap: 16px; font-size: 12px; font-weight: 600; align-items: center; }
.rate-in { color: #2dd4bf; }
.rate-out { color: #fefc86; }
.rate-avg { color: #8b949e; font-size: 11px; font-weight: 400; }
.rate-show i { display: inline-block; margin-right: 4px; font-style: normal; }
.chart-box { position: relative; width: 100%; height: 140px; }
.chart-svg { width: 100%; height: 100%; display: block; }
.chart-empty {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: #484f58; font-size: 12px;
}

/* 列表卡片 */
.list-card {
  padding: 14px; border: 1px solid rgba(139, 92, 246, 0.2);
  border-radius: 6px; background: rgba(28, 49, 94, 0.3);
}
.bar-list { display: flex; flex-direction: column; gap: 7px; }
.bar-row { display: flex; align-items: center; gap: 8px; font-size: 11px; }
.bar-name {
  width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: #c9d1d9; flex-shrink: 0;
}
.bar-track {
  flex: 1; height: 6px; background: rgba(139, 92, 246, 0.08);
  border-radius: 3px; overflow: hidden;
}
.bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; }
.bar-fill-in { background: linear-gradient(90deg, #2dd4bf, #14b8a6); }
.bar-fill-out { background: linear-gradient(90deg, #fefc86, #fbbf24); }
.bar-fill-purple { background: linear-gradient(90deg, #8B5CF6, #a78bfa); }
.bar-val {
  color: #8b949e; font-size: 10px; min-width: 56px; text-align: right;
}

/* 状态码柱状图 */
.status-bars {
  display: flex; gap: 14px; height: 120px; align-items: flex-end; padding: 0 6px;
}
.status-col {
  flex: 1; display: flex; flex-direction: column; align-items: center; gap: 5px;
  height: 100%; justify-content: flex-end;
}
.status-track { width: 100%; flex: 1; display: flex; align-items: flex-end; }
.status-bar {
  width: 100%; min-height: 2px; border-radius: 3px 3px 0 0;
  transition: height 0.5s ease;
  box-shadow: 0 0 8px currentColor;
}
.status-label { font-size: 10px; color: #8b949e; font-weight: 600; }
.status-count { font-size: 11px; color: #c9d1d9; font-weight: 700; }

/* 方法分布 */
.method-list { display: flex; flex-direction: column; gap: 9px; }
.method-row { display: flex; align-items: center; gap: 10px; }
.method-tag {
  font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 3px;
  border: 1px solid rgba(139, 92, 246, 0.4);
  background: rgba(139, 92, 246, 0.12); color: #a78bfa;
  min-width: 64px; text-align: center; white-space: nowrap;
}
.method-track {
  flex: 1; height: 7px; background: rgba(139, 92, 246, 0.08);
  border-radius: 4px; overflow: hidden;
}
.method-fill {
  height: 100%; background: linear-gradient(90deg, #8B5CF6, #a78bfa);
  border-radius: 4px; transition: width 0.5s ease;
}
.method-count {
  color: #c9d1d9; font-size: 11px; font-weight: 600;
  min-width: 36px; text-align: right;
}

.empty-hint { color: #484f58; text-align: center; padding: 16px; font-size: 11px; }
</style>