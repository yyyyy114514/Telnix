<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed, nextTick, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type LogEntry, type LogStats } from '../api/client'
import { useRouter } from 'vue-router'

// 日志查看页：筛选、查看、清空、导出、实时监控、智能分析、关联跳转
const { t } = useI18n()
const router = useRouter()

// ============ 基础状态 ============
const entries = ref<LogEntry[]>([])
const total = ref(0)
const loading = ref(false)
const levelFilter = ref('')
const categoryFilter = ref('')
const keyword = ref('')
const page = ref(1)
const pageSize = 100

// ============ 实时监控状态 ============
const isStreaming = ref(false)
const autoScroll = ref(true)
const streamError = ref('')
let eventSource: EventSource | null = null

// ============ 智能分析面板状态 ============
const showAnalytics = ref(false)
const logStats = ref<LogStats | null>(null)
const analyticsLoading = ref(false)

// ============ 日志分类（前端缓存） ============
const categories = ref<string[]>([])

// ============ 最近异常日志（用于分析面板） ============
const recentErrors = ref<LogEntry[]>([])

// ============ 计算属性 ============
const levelOptions = computed(() => [
  { label: t('logs.all'), value: '' },
  { label: 'DEBUG', value: 'DEBUG' },
  { label: 'INFO', value: 'INFO' },
  { label: 'WARNING', value: 'WARNING' },
  { label: 'ERROR', value: 'ERROR' },
])

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

// 错误率统计
const errorRate = computed(() => {
  if (!logStats.value || logStats.value.total === 0) return 0
  const errCount = logStats.value.by_level?.ERROR || 0
  return ((errCount / logStats.value.total) * 100).toFixed(1)
})

// 日志分类饼图数据
const categoryChartData = computed(() => {
  if (!logStats.value?.by_category) return []
  return Object.entries(logStats.value.by_category)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 8)
})

// 日志级别分布数据
const levelChartData = computed(() => {
  if (!logStats.value?.by_level) return []
  const byLevel = logStats.value.by_level
  return [
    { label: 'ERROR', value: byLevel.ERROR || 0, color: '#f56c6c' },
    { label: 'WARNING', value: byLevel.WARNING || 0, color: '#e6a23c' },
    { label: 'INFO', value: byLevel.INFO || 0, color: '#409eff' },
    { label: 'DEBUG', value: byLevel.DEBUG || 0, color: '#909399' },
  ].filter(item => item.value > 0)
})

// ============ 方法 ============

// 饼图颜色数组
const chartColors = [
  '#409eff', '#67c23a', '#e6a23c', '#f56c6c',
  '#909399', '#c71585', '#00ced1', '#ffa500'
]

function getChartColor(index: number): string {
  return chartColors[index % chartColors.length]
}

// 检测消息中的 flow_id 模式
function extractFlowIds(text: string): { id: number; start: number; end: number }[] {
  const pattern = /flow[_\s]?id[:\s=]*(\d+)/gi
  const matches: { id: number; start: number; end: number }[] = []
  let match
  while ((match = pattern.exec(text)) !== null) {
    matches.push({ id: parseInt(match[1]), start: match.index, end: match.index + match[0].length })
  }
  return matches
}

// 渲染带高亮的日志消息
function renderMessage(entry: LogEntry): { text: string; flowIds: number[] } {
  const flowIds = extractFlowIds(entry.message)
  return { text: entry.message, flowIds: flowIds.map(f => f.id) }
}

// 跳转到抓包页查看指定 flow
function jumpToFlow(flowId: number) {
  // 使用 router 跳转，flow_id 作为查询参数
  router.push({ path: '/', query: { flowId: String(flowId) } })
}

async function loadLogs(newPage?: number) {
  if (typeof newPage === 'number') page.value = newPage
  loading.value = true
  try {
    const res = await api.logList({
      level: levelFilter.value || undefined,
      category: categoryFilter.value || undefined,
      keyword: keyword.value || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    })
    entries.value = res.entries || []
    total.value = res.total || 0
    const cats = new Set<string>()
    for (const e of entries.value) {
      if (e.category) cats.add(e.category)
    }
    categories.value = Array.from(cats).sort()
  } catch (e: any) {
    ElMessage.error(t('logs.loadFailed', { msg: e?.message || e }))
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  analyticsLoading.value = true
  try {
    logStats.value = await api.logStats()
    // 更新最近错误日志
    if (logStats.value?.recent_errors) {
      recentErrors.value = logStats.value.recent_errors.map(e => ({
        id: e.id,
        timestamp: e.timestamp,
        level: 'ERROR',
        category: e.category,
        message: e.message,
        detail: e.detail,
      }))
    }
  } catch (e: any) {
    console.error('Failed to load log stats:', e)
  } finally {
    analyticsLoading.value = false
  }
}

function clearLogs() {
  ElMessageBox.confirm(t('logs.clearConfirmMsg'), t('logs.clearLogs'), {
    confirmButtonText: t('logs.clear'),
    cancelButtonText: t('logs.cancel'),
    type: 'warning',
  }).then(async () => {
    try {
      await api.logClear()
      entries.value = []
      total.value = 0
      ElMessage.success(t('logs.cleared'))
    } catch (e: any) {
      ElMessage.error(t('logs.clearFailed', { msg: e?.message || e }))
    }
  }).catch(() => {})
}

async function exportLogs() {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const fname = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.jsonl`
  try {
    const blob = await api.logExport({
      level: levelFilter.value || undefined,
      category: categoryFilter.value || undefined,
      keyword: keyword.value || undefined,
    })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = fname
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success(t('logs.exportSuccess'))
  } catch (e: any) {
    ElMessage.error(t('logs.exportFailed') + (e?.message || ''))
  }
}

function onFilterChange() {
  page.value = 1
  loadLogs()
}

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

function levelTag(level: string): TagType {
  switch (level) {
    case 'ERROR': return 'danger'
    case 'WARNING': return 'warning'
    case 'INFO': return 'info'
    case 'DEBUG': return 'success'
    default: return 'info'
  }
}

function formatTs(s: string): string {
  if (!s) return ''
  return s.replace('T', ' ').replace(/\.\d+$/, '')
}

async function copyLogRow(e: LogEntry) {
  const text = [formatTs(e.timestamp), e.level, e.category, e.message, e.detail || ''].filter(Boolean).join(' | ')
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(t('common.copied'))
  } catch {
    ElMessage.error(t('common.copyFailed'))
  }
}

// ============ SSE 实时监控 ============

const logListRef = ref<HTMLElement | null>(null)

function startStream() {
  if (eventSource) return

  try {
    eventSource = api.createLogStream()

    eventSource.addEventListener('log', (e: MessageEvent) => {
      try {
        const entry: LogEntry = JSON.parse(e.data)
        // 添加到列表顶部（最新在前）
        entries.value.unshift(entry)
        // 限制本地缓存数量
        if (entries.value.length > 500) {
          entries.value = entries.value.slice(0, 500)
        }
        total.value++

        // 收集新分类
        if (entry.category && !categories.value.includes(entry.category)) {
          categories.value.push(entry.category)
          categories.value.sort()
        }

        // 如果是错误日志，更新最近错误
        if (entry.level === 'ERROR') {
          recentErrors.value.unshift(entry)
          if (recentErrors.value.length > 10) {
            recentErrors.value = recentErrors.value.slice(0, 10)
          }
        }

        // 自动滚动
        if (autoScroll.value) {
          nextTick(() => {
            if (logListRef.value) {
              logListRef.value.scrollTop = 0
            }
          })
        }
      } catch (err) {
        console.error('Failed to parse log entry:', err)
      }
    })

    eventSource.addEventListener('stats', (e: MessageEvent) => {
      try {
        logStats.value = JSON.parse(e.data)
      } catch (err) {
        console.error('Failed to parse stats:', err)
      }
    })

    eventSource.onerror = (e) => {
      console.error('SSE error:', e)
      streamError.value = 'Connection lost, reconnecting...'
      stopStream()
      // 5秒后自动重连
      setTimeout(() => {
        if (isStreaming.value) {
          startStream()
        }
      }, 5000)
    }

    eventSource.onopen = () => {
      isStreaming.value = true
      streamError.value = ''
    }
  } catch (e: any) {
    streamError.value = 'Failed to start stream: ' + (e?.message || e)
    console.error('Failed to start SSE stream:', e)
  }
}

function stopStream() {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  isStreaming.value = false
}

function toggleStream() {
  if (isStreaming.value) {
    stopStream()
  } else {
    startStream()
  }
}

function toggleAutoScroll() {
  autoScroll.value = !autoScroll.value
}

// ============ 生命周期 ============

onMounted(() => {
  loadLogs()
  loadStats()
  startStream()
})

onUnmounted(() => {
  stopStream()
})

// 监听筛选变化
watch([levelFilter, categoryFilter, keyword], () => {
  if (!isStreaming.value) {
    loadLogs()
  }
})

// 页面可见性变化时管理 SSE 连接
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // 页面不可见时可以暂停 SSE 节省资源
  } else {
    // 页面可见时恢复
    if (!isStreaming.value && isStreaming.value !== undefined) {
      startStream()
    }
  }
})
</script>

<template>
  <div class="log-view full flex flex-col">
    <!-- 工具栏 -->
    <div class="log-toolbar">
      <el-select v-model="levelFilter" :placeholder="t('logs.level')" size="small" style="width: 110px" @change="onFilterChange">
        <el-option v-for="o in levelOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-select v-model="categoryFilter" :placeholder="t('logs.category')" size="small" clearable style="width: 120px" @change="onFilterChange">
        <el-option v-for="c in categories" :key="c" :label="c" :value="c" />
      </el-select>
      <el-input v-model="keyword" :placeholder="t('logs.keywordSearch')" size="small" clearable style="width: 200px" @change="onFilterChange" @clear="onFilterChange" />
      <el-button size="small" @click="loadLogs()" :loading="loading">
        <el-icon><Refresh /></el-icon>&nbsp;{{ t('logs.refresh') }}
      </el-button>
      <el-button size="small" @click="exportLogs">
        <el-icon><Download /></el-icon>&nbsp;{{ t('logs.export') }}
      </el-button>
      <el-button size="small" type="danger" @click="clearLogs">
        <el-icon><Delete /></el-icon>&nbsp;{{ t('logs.clear') }}
      </el-button>

      <div class="flex-1"></div>

      <!-- 实时监控开关 -->
      <el-button size="small" :type="isStreaming ? 'success' : 'default'" @click="toggleStream">
        <el-icon><VideoPlay v-if="!isStreaming" /><VideoPause v-else /></el-icon>
        &nbsp;{{ isStreaming ? t('logs.streamingOn') : t('logs.streamingOff') }}
      </el-button>

      <!-- 自动滚动开关 -->
      <el-button size="small" :type="autoScroll ? 'primary' : 'default'" @click="toggleAutoScroll" :disabled="!isStreaming">
        <el-icon><Bottom v-if="autoScroll" /><Top v-else /></el-icon>
        &nbsp;{{ autoScroll ? t('logs.autoScrollOn') : t('logs.autoScrollOff') }}
      </el-button>

      <!-- 分析面板开关 -->
      <el-button size="small" :type="showAnalytics ? 'warning' : 'default'" @click="showAnalytics = !showAnalytics">
        <el-icon><DataAnalysis /></el-icon>
        &nbsp;{{ t('logs.analytics') }}
      </el-button>

      <span class="text-dim" style="font-size: 12px">{{ t('logs.totalEntries', { n: total }) }}</span>
    </div>

    <!-- 流式状态提示 -->
    <div v-if="streamError" class="stream-error">
      <el-icon><Warning /></el-icon>&nbsp;{{ streamError }}
    </div>

    <!-- 主体区域：左侧日志列表 + 右侧分析面板 -->
    <div class="log-main flex flex-1 overflow-hidden">
      <!-- 日志列表 -->
      <div class="log-list flex-1 overflow-auto" ref="logListRef">
        <div v-if="!entries.length && !loading" class="empty-text text-dim">
          {{ t('logs.noLogs') }}
        </div>
        <div v-for="e in entries" :key="e.id" class="log-row no-select" :class="'lv-' + e.level.toLowerCase()" @click="copyLogRow(e)">
          <span class="log-ts text-dim mono">{{ formatTs(e.timestamp) }}</span>
          <el-tag :type="levelTag(e.level)" size="small" class="log-level">{{ e.level }}</el-tag>
          <span class="log-cat mono">{{ e.category }}</span>
          <span class="log-msg">
            <!-- 带 flow_id 高亮的消息 -->
            <template v-for="(part, idx) in (() => {
              const parts = []
              const text = e.message
              const flowIds = extractFlowIds(text)
              let lastEnd = 0
              for (const f of flowIds) {
                if (f.start > lastEnd) {
                  parts.push({ type: 'text', content: text.slice(lastEnd, f.start) })
                }
                parts.push({ type: 'flowId', content: text.slice(f.start, f.end), id: f.id })
                lastEnd = f.end
              }
              if (lastEnd < text.length) {
                parts.push({ type: 'text', content: text.slice(lastEnd) })
              }
              return parts.length ? parts : [{ type: 'text', content: text }]
            })()" :key="idx">
              <span v-if="part.type === 'flowId'" class="flow-id-link" @click.stop="jumpToFlow(part.id!)">{{ part.content }}</span>
              <span v-else>{{ part.content }}</span>
            </template>
          </span>
          <div v-if="e.detail" class="log-detail mono text-dim">{{ e.detail }}</div>
        </div>
      </div>

      <!-- 智能分析面板 -->
      <div v-if="showAnalytics" class="log-analytics">
        <div class="analytics-header">
          <span>{{ t('logs.analyticsPanel') }}</span>
          <el-button size="small" text @click="loadStats" :loading="analyticsLoading">
            <el-icon><Refresh /></el-icon>
          </el-button>
        </div>

        <!-- 错误率统计卡片 -->
        <div class="analytics-card">
          <div class="card-title">{{ t('logs.errorRate') }}</div>
          <div class="error-rate-display" :class="{ 'high-error': Number(errorRate) > 10 }">
            <span class="rate-value">{{ errorRate }}</span>
            <span class="rate-unit">%</span>
          </div>
          <div class="error-count text-dim">
            {{ Number(logStats?.by_level?.ERROR || 0) }} / {{ logStats?.total || 0 }} {{ t('logs.errors') }}
          </div>
        </div>

        <!-- 日志级别分布 -->
        <div class="analytics-card">
          <div class="card-title">{{ t('logs.levelDistribution') }}</div>
          <div class="level-bars">
            <div v-for="item in levelChartData" :key="item.label" class="level-bar-item">
              <span class="level-label" :style="{ color: item.color }">{{ item.label }}</span>
              <div class="level-bar">
                <div class="level-bar-fill" :style="{ width: logStats?.total ? (item.value / logStats.total * 100) + '%' : '0%', backgroundColor: item.color }"></div>
              </div>
              <span class="level-count">{{ item.value }}</span>
            </div>
          </div>
        </div>

        <!-- 日志分类分布饼图（简化版） -->
        <div class="analytics-card">
          <div class="card-title">{{ t('logs.categoryDistribution') }}</div>
          <div class="category-list">
            <div v-for="(cat, idx) in categoryChartData" :key="cat.name" class="category-item">
              <span class="category-color" :style="{ backgroundColor: getChartColor(idx) }"></span>
              <span class="category-name">{{ cat.name }}</span>
              <span class="category-count">{{ cat.value }}</span>
            </div>
            <div v-if="!categoryChartData.length" class="text-dim" style="font-size: 12px; text-align: center;">
              {{ t('logs.noData') }}
            </div>
          </div>
        </div>

        <!-- 最近异常日志 -->
        <div class="analytics-card">
          <div class="card-title">{{ t('logs.recentErrors') }}</div>
          <div class="recent-errors">
            <div v-for="err in recentErrors" :key="err.id" class="error-item" @click="jumpToFlow(err.id)">
              <el-tag type="danger" size="small">{{ err.category }}</el-tag>
              <span class="error-msg">{{ err.message.slice(0, 60) }}{{ err.message.length > 60 ? '...' : '' }}</span>
              <el-icon class="jump-icon"><Right /></el-icon>
            </div>
            <div v-if="!recentErrors.length" class="text-dim" style="font-size: 12px; text-align: center;">
              {{ t('logs.noErrors') }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 分页 -->
    <div class="log-pager">
      <el-pagination
        v-model:current-page="page"
        :total="total"
        :page-size="pageSize"
        layout="prev, pager, next"
        small
        @current-change="(p: number) => loadLogs(p)"
      />
    </div>
  </div>
</template>

<style scoped>
.log-view { background: var(--on-bg); }
.log-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
  flex-wrap: wrap;
}
.log-list { padding: 4px 0; }
.log-row {
  padding: 4px 12px; border-bottom: 1px solid var(--on-border-light);
  display: grid; grid-template-columns: 140px 70px 80px 1fr;
  gap: 8px; align-items: start; font-size: 12px; line-height: 1.6;
}
.log-row .log-detail {
  grid-column: 4 / 5; margin-top: 2px;
  font-size: 11px; white-space: pre-wrap; word-break: break-all;
  max-height: 120px; overflow-y: auto;
}
.log-ts { font-size: 11px; }
.log-cat { color: var(--on-text-muted); font-size: 11px; }
.log-msg { color: var(--on-text); }
.log-row.lv-error .log-msg { color: var(--on-error); }
.log-row.lv-warning .log-msg { color: var(--on-warn); }
.empty-text { text-align: center; padding: 40px; font-size: 13px; }
.log-pager {
  display: flex; justify-content: center;
  padding: 6px; border-top: 1px solid var(--on-border-light);
}
.mono { font-family: 'Consolas', 'Monaco', monospace; }

/* 实时监控状态提示 */
.stream-error {
  display: flex; align-items: center; gap: 4px;
  padding: 4px 12px; background: #fef0f0; color: #f56c6c; font-size: 12px;
}

/* 主区域布局 */
.log-main {
  display: flex;
  overflow: hidden;
}

/* 智能分析面板 */
.log-analytics {
  width: 280px;
  border-left: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

.analytics-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  font-weight: 500; font-size: 13px;
}

.analytics-card {
  padding: 12px;
  border-bottom: 1px solid var(--on-border-light);
}

.card-title {
  font-size: 12px; font-weight: 500; color: var(--on-text-muted);
  margin-bottom: 8px;
}

/* 错误率统计 */
.error-rate-display {
  display: flex; align-items: baseline; gap: 2px;
  margin-bottom: 4px;
}
.error-rate-display.high-error .rate-value { color: #f56c6c; }
.rate-value { font-size: 28px; font-weight: bold; color: var(--on-text); }
.rate-unit { font-size: 14px; color: var(--on-text-muted); }
.error-count { font-size: 11px; }

/* 日志级别分布条形图 */
.level-bars { display: flex; flex-direction: column; gap: 6px; }
.level-bar-item { display: flex; align-items: center; gap: 8px; }
.level-label { width: 55px; font-size: 11px; font-weight: 500; }
.level-bar { flex: 1; height: 8px; background: var(--on-border-light); border-radius: 4px; overflow: hidden; }
.level-bar-fill { height: 100%; border-radius: 4px; transition: width 0.3s ease; }
.level-count { width: 40px; text-align: right; font-size: 11px; color: var(--on-text-muted); }

/* 分类分布列表 */
.category-list { display: flex; flex-direction: column; gap: 4px; }
.category-item { display: flex; align-items: center; gap: 6px; font-size: 11px; }
.category-color { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
.category-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.category-count { color: var(--on-text-muted); }

/* 最近异常日志 */
.recent-errors { display: flex; flex-direction: column; gap: 6px; max-height: 300px; overflow-y: auto; }
.error-item {
  display: flex; align-items: flex-start; gap: 6px;
  padding: 6px 8px; background: #fef0f0; border-radius: 4px; cursor: pointer;
  transition: background 0.2s;
}
.error-item:hover { background: #fde2e2; }
.error-item .error-msg { flex: 1; font-size: 11px; line-height: 1.4; color: var(--on-text); }
.error-item .jump-icon { color: var(--on-text-muted); flex-shrink: 0; }

/* Flow ID 可点击高亮 */
.flow-id-link {
  color: #409eff;
  cursor: pointer;
  text-decoration: underline;
  text-decoration-style: dotted;
}
.flow-id-link:hover { color: #66b1ff; text-decoration-style: solid; }
</style>
