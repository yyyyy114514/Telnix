<script setup lang="ts">
import { computed, onMounted, onUnmounted, shallowRef, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { api, type AutoReplyRule } from '../api/client'
import RuleEditor from '../components/RuleEditor.vue'

// 使用 shallowRef 优化大型规则列表渲染
const rules = shallowRef<AutoReplyRule[]>([])
const editorVisible = ref(false)
const editingRule = shallowRef<AutoReplyRule | null>(null)
const loading = ref(false)
const { t } = useI18n()
const router = useRouter()
const importFileRef = ref<HTMLInputElement | null>(null)

// 多选
const multiSelectMode = ref(false)
// 使用 shallowRef 避免 Set 深度追踪
const selectedRuleIds = shallowRef<Set<string>>(new Set())

let loadingTimer: number | null = null
let loadThrottleTimer: number | null = null
let pendingLoad = false
const LOADING_TIMEOUT_MS = 5000
const LOAD_THROTTLE_MS = 300

// ========== P0 功能增强：分组侧边栏 ==========
interface RuleGroup {
  id: number
  name: string
  enabled: boolean
  sort_order: number
  rule_count?: number
}
const groups = shallowRef<RuleGroup[]>([])
const selectedGroupId = ref<number | null>(null)
const groupManageVisible = ref(false)
const newGroupName = ref('')

async function loadGroups() {
  try {
    groups.value = await api.getRuleGroups()
  } catch {
    groups.value = []
  }
}

async function createGroup() {
  if (!newGroupName.value.trim()) {
    ElMessage.warning(t('autoReply.groupNamePlaceholder'))
    return
  }
  try {
    await api.createRuleGroup({ name: newGroupName.value.trim() })
    ElMessage.success(t('autoReply.saveSuccess'))
    newGroupName.value = ''
    await loadGroups()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function deleteGroup(g: RuleGroup) {
  try {
    await ElMessageBox.confirm(
      t('autoReply.groupDeleteConfirm', { name: g.name }),
      t('autoReply.groupDelete'),
      { confirmButtonText: t('autoReply.deleteButton'), cancelButtonText: t('autoReply.cancelButton'), type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await api.deleteRuleGroup(g.id)
    if (selectedGroupId.value === g.id) {
      selectedGroupId.value = null
    }
    await loadGroups()
    await load()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function toggleGroupEnabled(g: RuleGroup) {
  try {
    await api.updateRuleGroup(g.id, { enabled: !g.enabled })
    await loadGroups()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
    g.enabled = !g.enabled
  }
}

// 按分组过滤规则
const filteredRules = computed(() => {
  if (selectedGroupId.value === null) {
    return rules.value
  }
  return rules.value.filter(r => r.group_id === selectedGroupId.value)
})

// ========== P0 功能增强：命中统计面板（实时监控） ==========
interface HitStats {
  total_hits: number
  leaderboard: Array<{ rule_id: string; pattern: string; action: string; note: string; hit_count: number; last_hit_at: string }>
  recent_hits: Array<{ id: string; pattern: string; action: string; hit_count: number; last_hit_at: string; last_hit_flow_id: number | null }>
}
// 实时命中追踪数据类型
interface RealtimeHitStats {
  total_hits: number
  heatmap: Array<{ rule_id: string; rule_name: string; hit_count: number; avg_ms: number; min_ms: number; max_ms: number }>
  timeline: Array<{ ts: string; rule_id: string; rule_name: string; duration_ms: number; flow_id: number | null }>
  leaderboard: Array<{ rule_id: string; rule_name: string; hit_count: number; avg_ms: number; last_hit_at: string }>
}

const hitStats = ref<HitStats | null>(null)
const realtimeHitStats = ref<RealtimeHitStats | null>(null)
const hitStatsLoading = ref(false)
const showHitStats = ref(false)
const isRealtimeActive = ref(false)

// SSE EventSource 实例
let hitStatsStream: EventSource | null = null
let pollTimer: number | null = null

async function loadHitStats() {
  hitStatsLoading.value = true
  try {
    hitStats.value = await api.getRuleHitStats()
  } catch {
    hitStats.value = null
  } finally {
    hitStatsLoading.value = false
  }
}

async function loadRealtimeHitStats() {
  try {
    realtimeHitStats.value = await api.getRealtimeHitStats()
  } catch {
    // 静默失败，保留旧数据
  }
}

// 启动实时监控（SSE 流 + 轮询备用）
let reconnectTimer: number | null = null
const RECONNECT_DELAY_MS = 3000
const MAX_RECONNECT_ATTEMPTS = 5
let reconnectAttempts = 0

function startRealtimeMonitor() {
  if (isRealtimeActive.value) return
  isRealtimeActive.value = true
  reconnectAttempts = 0

  // 立即加载一次数据
  loadRealtimeHitStats()

  // 方法1：SSE 流
  try {
    hitStatsStream = api.createHitStatsStream()
    hitStatsStream.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        // 重置重连计数
        reconnectAttempts = 0
        if (data.type === 'init' || data.type === 'snapshot' || data.type === 'keepalive') {
          realtimeHitStats.value = {
            total_hits: data.total_hits,
            heatmap: data.heatmap || [],
            timeline: data.timeline || [],
            leaderboard: data.leaderboard || [],
          }
        } else if (data.type === 'hit' && data.data) {
          // 增量更新：将新命中添加到时间线头部
          if (realtimeHitStats.value) {
            const newTimeline = [data.data, ...realtimeHitStats.value.timeline].slice(0, 100)
            realtimeHitStats.value = {
              ...realtimeHitStats.value,
              total_hits: data.total_hits,
              timeline: newTimeline,
            }
            // 更新排行榜和热力图的命中计数
            if (data.heatmap_update) {
              realtimeHitStats.value = {
                ...realtimeHitStats.value,
                heatmap: data.heatmap_update,
              }
            }
          }
        }
      } catch {
        // 解析失败，忽略
      }
    }
    hitStatsStream.onerror = () => {
      // SSE 断开时自动重连或切换到轮询
      stopRealtimeMonitor()
      if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        scheduleReconnect()
      } else {
        startPolling()
      }
    }
  } catch {
    // SSE 不可用，切换到轮询
    startPolling()
  }
}

// 延迟重连
function scheduleReconnect() {
  if (reconnectTimer !== null) return
  reconnectAttempts++
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null
    if (isRealtimeActive.value) {
      startRealtimeMonitor()
    }
  }, RECONNECT_DELAY_MS)
}

// 停止 SSE 流
function stopRealtimeMonitor() {
  isRealtimeActive.value = false
  if (hitStatsStream) {
    hitStatsStream.close()
    hitStatsStream = null
  }
  // 清除重连定时器
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}

// 轮询备用方案（每 2 秒刷新）
function startPolling() {
  if (pollTimer !== null) return
  pollTimer = window.setInterval(() => {
    if (isRealtimeActive.value) {
      loadRealtimeHitStats()
    }
  }, 2000)
}

// 停止轮询
function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function clearHitStats() {
  try {
    await ElMessageBox.confirm(t('autoReply.hitClearConfirm'), t('autoReply.hitClear'), {
      confirmButtonText: t('autoReply.deleteButton'),
      cancelButtonText: t('autoReply.cancelButton'),
      type: 'warning'
    })
  } catch {
    return
  }
  try {
    await api.clearRuleHitStats()
    ElMessage.success(t('autoReply.hitClearSuccess'))
    await loadHitStats()
    await loadRealtimeHitStats()
    await load()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

// 打开/关闭命中统计面板时控制实时监控
watch(showHitStats, (val) => {
  if (val) {
    startRealtimeMonitor()
  } else {
    stopRealtimeMonitor()
    stopPolling()
  }
})

function toggleMultiSelect() {
  multiSelectMode.value = !multiSelectMode.value
  if (!multiSelectMode.value) {
    selectedRuleIds.value.clear()
  }
}

function toggleRuleCheck(id: string) {
  const newSet = new Set(selectedRuleIds.value)
  if (newSet.has(id)) {
    newSet.delete(id)
  } else {
    newSet.add(id)
  }
  selectedRuleIds.value = newSet
}

function selectAllRules() {
  selectedRuleIds.value = new Set(rules.value.map(r => r.id!).filter(Boolean) as string[])
}

function clearSelection() {
  selectedRuleIds.value = new Set()
}

const selectedCount = computed(() => selectedRuleIds.value.size)

// ---------- 多选悬浮工具栏（参照 FlowList.vue）----------
// 滚动时隐藏，N 秒不操作再显示（默认 1 秒）
const scrollContainerRef = ref<HTMLElement | null>(null)
const floatBarVisible = ref(true)
let floatBarTimer: number | null = null
const FLOAT_BAR_DELAY_SEC = 1

function hideFloatBar() {
  if (!multiSelectMode.value) return
  floatBarVisible.value = false
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  floatBarTimer = window.setTimeout(() => {
    if (multiSelectMode.value) floatBarVisible.value = true
  }, FLOAT_BAR_DELAY_SEC * 1000)
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

onUnmounted(() => {
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  if (loadThrottleTimer !== null) clearTimeout(loadThrottleTimer)
  // 清理实时监控
  stopRealtimeMonitor()
  stopPolling()
})

// 主动触发加载时使用节流
function loadWithThrottle() {
  scheduleLoad()
}

function clearLoadingTimer() {
  if (loadingTimer !== null) {
    clearTimeout(loadingTimer)
    loadingTimer = null
  }
}

// 带节流的加载函数
function scheduleLoad() {
  if (loadThrottleTimer !== null) {
    pendingLoad = true
    return
  }
  loadThrottleTimer = window.setTimeout(() => {
    loadThrottleTimer = null
    if (pendingLoad) {
      pendingLoad = false
      doLoad()
    }
  }, LOAD_THROTTLE_MS)
}

async function doLoad() {
  clearLoadingTimer()
  loading.value = true
  loadingTimer = window.setTimeout(() => {
    loading.value = false
  }, LOADING_TIMEOUT_MS)
  try {
    const data = await api.getRules()
    // 使用浅拷贝更新规则列表
    rules.value = data ? [...data] : []
  } catch (e: any) {
    ElMessage.error(t('autoReply.loadFailed') + (e?.message || e))
  } finally {
    clearLoadingTimer()
    loading.value = false
  }
}

async function load() {
  doLoad()
}

async function onExportRules() {
  try {
    const blob = await api.exportRules()
    const url = URL.createObjectURL(blob)
    const ts = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    const fname = `telnix_auto_reply_${ts.getFullYear()}${pad(ts.getMonth() + 1)}${pad(ts.getDate())}_${pad(ts.getHours())}${pad(ts.getMinutes())}${pad(ts.getSeconds())}.json`
    const a = document.createElement('a')
    a.href = url
    a.download = fname
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    ElMessage.success(t('autoReply.exportRulesSuccess'))
  } catch (e: any) {
    ElMessage.error(t('autoReply.exportRulesFailed') + (e?.message || e))
  }
}

function onImportClick() {
  importFileRef.value?.click()
}

async function onImportFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    const text = await file.text()
    const data = JSON.parse(text)
    const importedRules = Array.isArray(data) ? data : data?.rules
    if (!Array.isArray(importedRules)) {
      throw new Error(t('autoReply.importRulesInvalidFormat'))
    }
    const res = await api.importRules({ rules: importedRules, mode: 'merge' })
    ElMessage.success(t('autoReply.importRulesSuccess', { n: res.imported }))
    await load()
  } catch (e: any) {
    ElMessage.error(t('autoReply.importRulesFailed') + (e?.message || e))
  } finally {
    input.value = ''
  }
}

function newRule() {
  editingRule.value = null
  editorVisible.value = true
}

function editRule(r: AutoReplyRule) {
  editingRule.value = r
  editorVisible.value = true
}

async function saveRule(r: AutoReplyRule) {
  try {
    if (r.id) {
      await api.updateRule(r.id, r)
    } else {
      await api.createRule(r)
    }
    ElMessage.success(t('autoReply.saveSuccess'))
    await load()
  } catch (e: any) {
    ElMessage.error(t('autoReply.saveFailed') + (e?.message || e))
  }
}

async function deleteRule(r: AutoReplyRule) {
  try {
    await ElMessageBox.confirm(t('autoReply.deleteConfirmMessage'), t('autoReply.deleteTitle'), {
      confirmButtonText: t('autoReply.deleteButton'), cancelButtonText: t('autoReply.cancelButton'), type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.deleteRule(r.id!)
    ElMessage.success(t('autoReply.deleted'))
    await load()
  } catch (e: any) {
    ElMessage.error(t('autoReply.deleteFailed') + (e?.message || e))
  }
}

async function toggleRule(r: AutoReplyRule) {
  r.enabled = !r.enabled
  try {
    await api.updateRule(r.id!, r)
  } catch (e: any) {
    r.enabled = !r.enabled
    ElMessage.error(t('autoReply.updateFailed') + (e?.message || e))
  }
}

// 批量删除
async function batchDelete() {
  const ids = [...selectedRuleIds.value]
  if (!ids.length) return
  try {
    await ElMessageBox.confirm(t('autoReply.batchDeleteConfirmMessage', { n: ids.length }), t('autoReply.batchDeleteTitle'), {
      confirmButtonText: t('autoReply.deleteButton'), cancelButtonText: t('autoReply.cancelButton'), type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.batchDeleteRules(ids)
    ElMessage.success(t('autoReply.batchDeleted', { n: ids.length }))
    selectedRuleIds.value = new Set()
    multiSelectMode.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(t('autoReply.batchDeleteFailed') + (e?.message || e))
  }
}

// 批量启用/禁用
async function batchSetEnabled(enabled: boolean) {
  const ids = [...selectedRuleIds.value]
  if (!ids.length) return
  try {
    await api.batchUpdateRules(ids, enabled)
    ElMessage.success(t(enabled ? 'autoReply.batchEnabled' : 'autoReply.batchDisabled', { n: ids.length }))
    selectedRuleIds.value = new Set()
    multiSelectMode.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(t('autoReply.batchUpdateFailed') + (e?.message || e))
  }
}

const actionLabel = computed<Record<string, string>>(() => ({
  mock: t('autoReply.actionMock'),
  mock_request: t('autoReply.actionMockRequest'),
  modify_request: t('autoReply.actionModifyRequest'),
  modify_response: t('autoReply.actionModifyResponse'),
  script: t('autoReply.actionScript'),
}))
const matchLabel = computed<Record<string, string>>(() => ({
  wildcard: t('autoReply.matchWildcard'),
  exact: t('autoReply.matchExact'),
  regex: t('autoReply.matchRegex'),
}))

// 实时监控辅助函数
function formatTime(ts: string): string {
  if (!ts) return '-'
  try {
    const d = new Date(ts)
    return d.toLocaleTimeString()
  } catch {
    return ts
  }
}

// ========== P0 功能增强：预览匹配 ==========
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewResult = ref<{
  total: number
  samples: Array<{ flow_id: number; method: string; host: string; path: string; url: string }>
} | null>(null)
const previewRule = shallowRef<AutoReplyRule | null>(null)

async function previewRuleMatch(r: AutoReplyRule) {
  if (!r.pattern?.trim()) {
    ElMessage.warning(t('autoReply.previewMatchNoPattern'))
    return
  }
  previewRule.value = r
  previewVisible.value = true
  previewLoading.value = true
  previewResult.value = null
  try {
    const result = await api.previewMatch({
      pattern: r.pattern,
      match_mode: r.match_mode,
      limit: 20,
    })
    previewResult.value = result
  } catch (e: any) {
    ElMessage.error(t('autoReply.previewMatchFailed') + (e?.message || e))
  } finally {
    previewLoading.value = false
  }
}

function getHeatmapColor(avgMs: number): string {
  // 根据平均耗时返回渐变色：绿色(快) -> 黄色(中) -> 红色(慢)
  if (avgMs < 5) return '#52c41a'  // 绿色
  if (avgMs < 20) return '#faad14'  // 黄色
  if (avgMs < 50) return '#fa8c16'  // 橙色
  return '#f5222d'  // 红色
}

function getTimelineColor(durationMs: number): string {
  // 根据耗时返回时间线颜色
  if (durationMs < 5) return '#52c41a'
  if (durationMs < 20) return '#1890ff'
  if (durationMs < 50) return '#faad14'
  return '#f5222d'
}

onMounted(async () => {
  await load()
  await loadGroups()
  await loadHitStats()
})
</script>

<template>
  <div class="auto-reply-view full flex flex-row">
    <!-- P0: 左侧分组侧边栏 -->
    <div class="group-sidebar">
      <div class="sidebar-header">
        <span class="sidebar-title">{{ t('autoReply.groups') }}</span>
        <el-button size="small" link @click="groupManageVisible = true">
          <el-icon><Setting /></el-icon>
        </el-button>
      </div>
      <div class="group-list">
        <div
          class="group-item"
          :class="{ active: selectedGroupId === null }"
          @click="selectedGroupId = null"
        >
          <span class="group-name">{{ t('autoReply.groupNoRules') }}</span>
          <span class="group-count">{{ rules.length }}</span>
        </div>
        <div
          v-for="g in groups"
          :key="g.id"
          class="group-item"
          :class="{ active: selectedGroupId === g.id }"
          @click="selectedGroupId = g.id"
        >
          <el-switch
            :model-value="g.enabled"
            size="small"
            @click.stop="toggleGroupEnabled(g)"
          />
          <span class="group-name" :class="{ disabled: !g.enabled }">{{ g.name }}</span>
          <span class="group-count">{{ g.rule_count || 0 }}</span>
        </div>
      </div>
      <!-- P0: 命中统计入口 -->
      <div class="sidebar-stats" @click="showHitStats = true; loadHitStats()">
        <el-icon><TrendCharts /></el-icon>&nbsp;{{ t('autoReply.hitStats') }}
      </div>
    </div>

    <!-- 右侧主内容区 -->
    <div class="main-content flex-1 flex flex-col">
    <div class="page-header">
      <div class="page-title">
        <el-icon><SetUp /></el-icon>&nbsp;{{ t('autoReply.pageTitle') }}
        <span v-if="selectedGroupId !== null" class="current-group">
          / {{ groups.find(g => g.id === selectedGroupId)?.name || '' }}
        </span>
      </div>
      <div style="display: flex; gap: 8px; align-items: center">
        <el-button size="small" @click="router.push('/mock')" :title="t('nav.mock')">
          <el-icon><Coffee /></el-icon>&nbsp;{{ t('nav.mock') }}
        </el-button>
        <el-tooltip :content="multiSelectMode ? t('autoReply.exitMultiSelect') : t('autoReply.multiSelectMode')" placement="bottom">
          <el-button
            size="small"
            :type="multiSelectMode ? 'warning' : 'default'"
            circle
            @click="toggleMultiSelect"
          >
            <el-icon><CircleCheck /></el-icon>
          </el-button>
        </el-tooltip>
        <el-button type="primary" size="small" @click="newRule" :disabled="multiSelectMode">
          <el-icon><Plus /></el-icon>&nbsp;{{ t('autoReply.newRule') }}
        </el-button>
        <el-button size="small" @click="onImportClick">
          <el-icon><Upload /></el-icon>&nbsp;{{ t('autoReply.importRules') }}
        </el-button>
        <el-button size="small" @click="onExportRules">
          <el-icon><Download /></el-icon>&nbsp;{{ t('autoReply.exportRules') }}
        </el-button>
        <input
          ref="importFileRef"
          type="file"
          accept=".json,application/json"
          style="display: none"
          @change="onImportFile"
        >
      </div>
    </div>
    <div
      ref="scrollContainerRef"
      class="flex-1 overflow-auto rules-scroll-container"
      style="padding: 0 16px 16px"
      @scroll="hideFloatBar"
    >
      <!-- 多选模式悬浮工具栏（贴右上角，滚动隐藏，N 秒后显示） -->
      <transition name="float-bar">
        <div
          v-if="multiSelectMode && floatBarVisible"
          class="multi-float-bar"
          @mouseenter="showFloatBarNow"
        >
          <span class="mfb-count">{{ t('autoReply.selectedCount', { n: selectedCount }) }}</span>
          <el-button size="small" @click="selectAllRules">{{ t('autoReply.selectAll') }}</el-button>
          <el-button size="small" @click="clearSelection" :disabled="!selectedCount">{{ t('autoReply.clearSelection') }}</el-button>
          <el-button
            size="small"
            type="success"
            :disabled="!selectedCount"
            @click="batchSetEnabled(true)"
          >
            <el-icon><Open /></el-icon>&nbsp;{{ t('autoReply.enable') }}
          </el-button>
          <el-button
            size="small"
            type="warning"
            :disabled="!selectedCount"
            @click="batchSetEnabled(false)"
          >
            <el-icon><TurnOff /></el-icon>&nbsp;{{ t('autoReply.disable') }}
          </el-button>
          <el-button
            size="small"
            type="danger"
            :disabled="!selectedCount"
            @click="batchDelete"
          >
            <el-icon><Delete /></el-icon>&nbsp;{{ t('autoReply.delete') }}
          </el-button>
        </div>
      </transition>
      <el-table :data="filteredRules" size="small" border stripe>
        <el-table-column v-if="multiSelectMode" label="" width="40">
          <template #default="{ row }">
            <el-checkbox
              :model-value="selectedRuleIds.has(row.id)"
              size="small"
              @change="toggleRuleCheck(row.id)"
            />
          </template>
        </el-table-column>
        <el-table-column :label="t('autoReply.enabled')" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" @change="toggleRule(row as any)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('autoReply.note')" min-width="140">
          <template #default="{ row }">
            <span v-if="row.note" :title="row.note">{{ row.note }}</span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('autoReply.matchMode')" width="100">
          <template #default="{ row }">{{ matchLabel[row.match_mode] || row.match_mode }}</template>
        </el-table-column>
        <el-table-column :label="t('autoReply.urlPattern')" prop="pattern" class-name="mono" />
        <el-table-column :label="t('autoReply.action')" width="120">
          <template #default="{ row }">{{ actionLabel[row.action] || row.action }}</template>
        </el-table-column>
        <!-- P0: 命中次数列 -->
        <el-table-column :label="t('autoReply.hitCount', { n: '' })" width="100" align="center">
          <template #default="{ row }">
            <span v-if="(row as any).hit_count > 0" class="hit-count">
              {{ (row as any).hit_count }}
            </span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <!-- P0: 标签列 -->
        <el-table-column :label="t('autoReply.tags')" width="140">
          <template #default="{ row }">
            <span v-if="(row as any).tags" class="rule-tags">
              <el-tag
                v-for="tag in ((row as any).tags || '').split(',').filter(Boolean)"
                :key="tag"
                size="small"
                type="info"
                class="rule-tag"
              >
                {{ tag.trim() }}
              </el-tag>
            </span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('autoReply.operations')" width="200">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="editRule(row as any)">{{ t('autoReply.edit') }}</el-button>
            <el-button link type="info" size="small" @click="previewRuleMatch(row as any)">
              <el-icon><Search /></el-icon>
            </el-button>
            <el-button link type="danger" size="small" @click="deleteRule(row as any)">{{ t('autoReply.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('autoReply.emptyHint') }}</div>
        </template>
      </el-table>
    </div>
    </div>

    <!-- P0: 命中统计对话框（实时监控增强版） -->
    <el-dialog
      v-model="showHitStats"
      :title="t('autoReply.hitStats')"
      width="900px"
      :append-to-body="true"
      class="hit-stats-dialog"
    >
      <div v-if="hitStatsLoading && !realtimeHitStats" class="stats-loading">
        <el-icon class="is-loading"><Loading /></el-icon>&nbsp;{{ t('common.loading') }}
      </div>

      <!-- 实时监控概览 -->
      <div class="stats-overview" v-if="realtimeHitStats">
        <div class="overview-card">
          <div class="overview-value">{{ realtimeHitStats.total_hits }}</div>
          <div class="overview-label">{{ t('autoReply.hitTotal', { n: '' }) }}</div>
        </div>
        <div class="overview-card">
          <div class="overview-value">{{ realtimeHitStats.heatmap.length }}</div>
          <div class="overview-label">{{ t('autoReply.hitHeatmap') }}</div>
        </div>
        <div class="overview-card">
          <div class="overview-value">
            <el-tag type="success" size="small" :class="{ 'pulse-tag': isRealtimeActive }">
              <el-icon v-if="isRealtimeActive"><CircleCheckFilled /></el-icon>
              {{ isRealtimeActive ? t('autoReply.hitLive') : 'SSE' }}
            </el-tag>
          </div>
          <div class="overview-label">{{ t('autoReply.hitRealtime') }}</div>
        </div>
      </div>

      <!-- 三栏布局：排行榜 | 热力图 | 时间线 -->
      <div class="stats-grid" v-if="realtimeHitStats">
        <!-- 排行榜 TOP 10 -->
        <div class="stats-column">
          <div class="column-header">{{ t('autoReply.hitLeaderboard') }}</div>
          <div v-if="realtimeHitStats.leaderboard.length > 0" class="leaderboard">
            <div
              v-for="(item, idx) in realtimeHitStats.leaderboard"
              :key="item.rule_id"
              class="leaderboard-item"
            >
              <span class="rank" :class="`rank-${idx + 1}`">#{{ idx + 1 }}</span>
              <span class="rule-name" :title="item.rule_name">{{ item.rule_name }}</span>
              <span class="hit-count-badge">{{ item.hit_count }}</span>
            </div>
          </div>
          <el-empty v-else :description="t('autoReply.hitNoData')" :image-size="40" />
        </div>

        <!-- 规则执行耗时热力图 -->
        <div class="stats-column">
          <div class="column-header">{{ t('autoReply.hitHeatmap') }}</div>
          <div v-if="realtimeHitStats.heatmap.length > 0" class="heatmap">
            <div
              v-for="item in realtimeHitStats.heatmap"
              :key="item.rule_id"
              class="heatmap-row"
            >
              <div class="heatmap-rule" :title="item.rule_name">{{ item.rule_name }}</div>
              <div class="heatmap-bar-container">
                <div
                  class="heatmap-bar"
                  :style="{
                    width: `${Math.min(100, (item.avg_ms / Math.max(...realtimeHitStats.heatmap.map(h => h.avg_ms))) * 100)}%`,
                    backgroundColor: getHeatmapColor(item.avg_ms)
                  }"
                ></div>
              </div>
              <div class="heatmap-stats">
                <span class="avg-ms">{{ item.avg_ms.toFixed(1) }}ms</span>
                <span class="range-ms">{{ item.min_ms.toFixed(0) }}-{{ item.max_ms.toFixed(0) }}</span>
              </div>
            </div>
          </div>
          <el-empty v-else :description="t('autoReply.hitNoData')" :image-size="40" />
        </div>

        <!-- 最近命中时间线 -->
        <div class="stats-column">
          <div class="column-header">{{ t('autoReply.hitTimeline') }}</div>
          <div v-if="realtimeHitStats.timeline.length > 0" class="timeline">
            <div
              v-for="(item, idx) in realtimeHitStats.timeline"
              :key="`${item.rule_id}-${idx}`"
              class="timeline-item"
            >
              <div class="timeline-dot" :style="{ backgroundColor: getTimelineColor(item.duration_ms) }"></div>
              <div class="timeline-content">
                <div class="timeline-rule">{{ item.rule_name }}</div>
                <div class="timeline-meta">
                  <span class="timeline-time">{{ formatTime(item.ts) }}</span>
                  <span class="timeline-duration">{{ item.duration_ms.toFixed(1) }}ms</span>
                </div>
              </div>
            </div>
          </div>
          <el-empty v-else :description="t('autoReply.hitNoData')" :image-size="40" />
        </div>
      </div>

      <!-- 兼容旧接口（无实时数据时显示） -->
      <template v-else-if="hitStats">
        <div class="stats-summary">
          <el-tag type="success" size="large">{{ t('autoReply.hitTotal', { n: hitStats.total_hits }) }}</el-tag>
        </div>

        <!-- 排行榜 -->
        <div class="stats-section">
          <div class="stats-section-title">{{ t('autoReply.hitLeaderboard') }}</div>
          <div v-if="hitStats.leaderboard.length > 0" class="leaderboard">
            <div
              v-for="(item, idx) in hitStats.leaderboard"
              :key="item.rule_id"
              class="leaderboard-item"
            >
              <span class="rank">#{{ idx + 1 }}</span>
              <span class="pattern mono">{{ item.pattern }}</span>
              <span class="hit-count-badge">{{ t('autoReply.hitCount', { n: item.hit_count }) }}</span>
            </div>
          </div>
          <el-empty v-else :description="t('autoReply.hitNoData')" :image-size="60" />
        </div>

        <!-- 最近命中时间线 -->
        <div class="stats-section">
          <div class="stats-section-title">{{ t('autoReply.hitRecent') }}</div>
          <div v-if="hitStats.recent_hits.length > 0" class="recent-hits">
            <div
              v-for="item in hitStats.recent_hits"
              :key="item.id"
              class="recent-hit-item"
            >
              <el-tag size="small" type="info">{{ item.action }}</el-tag>
              <span class="pattern mono">{{ item.pattern }}</span>
              <span class="time">{{ item.last_hit_at ? new Date(item.last_hit_at).toLocaleString() : '-' }}</span>
            </div>
          </div>
          <el-empty v-else :description="t('autoReply.hitNoData')" :image-size="60" />
        </div>
      </template>
      <template #footer>
        <el-button @click="clearHitStats">{{ t('autoReply.hitClear') }}</el-button>
        <el-button type="primary" @click="showHitStats = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>

    <!-- P0: 管理分组对话框 -->
    <el-dialog
      v-model="groupManageVisible"
      :title="t('autoReply.groupManage')"
      width="400px"
      :append-to-body="true"
    >
      <div class="group-manage-content">
        <div class="new-group-form">
          <el-input
            v-model="newGroupName"
            :placeholder="t('autoReply.groupNamePlaceholder')"
            @keyup.enter="createGroup"
          />
          <el-button type="primary" size="small" @click="createGroup">
            <el-icon><Plus /></el-icon>&nbsp;{{ t('autoReply.groupNew') }}
          </el-button>
        </div>
        <div class="group-list-manage">
          <div
            v-for="g in groups"
            :key="g.id"
            class="group-manage-item"
          >
            <el-switch :model-value="g.enabled" @change="toggleGroupEnabled(g)" size="small" />
            <span class="group-manage-name">{{ g.name }}</span>
            <span class="group-manage-count">{{ g.rule_count || 0 }}</span>
            <el-button link type="danger" size="small" @click="deleteGroup(g)">
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
          <el-empty v-if="groups.length === 0" :description="t('autoReply.emptyHint')" :image-size="60" />
        </div>
      </div>
      <template #footer>
        <el-button type="primary" @click="groupManageVisible = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>

    <RuleEditor v-model="editorVisible" :rule="editingRule" @save="saveRule" />

    <!-- P0: 预览匹配结果对话框（列表页独立版本） -->
    <el-dialog
      v-model="previewVisible"
      :title="previewRule?.note ? previewRule.note : t('autoReply.previewMatchTitle')"
      width="650px"
      :append-to-body="true"
      class="preview-match-dialog"
    >
      <div class="preview-rule-info" v-if="previewRule">
        <el-tag size="small" type="info">{{ matchLabel[previewRule.match_mode] || previewRule.match_mode }}</el-tag>
        <span class="preview-rule-pattern mono">{{ previewRule.pattern }}</span>
      </div>
      <div v-if="previewLoading" class="preview-loading">
        <el-icon class="is-loading"><Loading /></el-icon>&nbsp;{{ t('autoReply.previewMatchLoading') }}
      </div>
      <template v-else-if="previewResult">
        <div class="preview-summary">
          <el-tag :type="previewResult.total > 0 ? 'success' : 'info'" size="default">
            {{ t('autoReply.previewMatchCount', { n: previewResult.total }) }}
          </el-tag>
        </div>
        <div v-if="previewResult.samples.length > 0" class="preview-samples">
          <div class="preview-samples-title">{{ t('autoReply.previewMatchSample') }}</div>
          <div class="preview-samples-list">
            <div
              v-for="sample in previewResult.samples"
              :key="sample.flow_id"
              class="preview-sample-item"
              @click="router.push(`/capture?highlight=${sample.flow_id}`)"
              style="cursor: pointer"
            >
              <el-tag size="small" type="primary">{{ sample.method }}</el-tag>
              <span class="preview-sample-url" :title="sample.url">{{ sample.host }}{{ sample.path }}</span>
            </div>
          </div>
        </div>
        <el-empty v-else :description="t('autoReply.previewMatchNoFlow')" :image-size="60" />
      </template>
      <template #footer>
        <el-button type="primary" @click="previewVisible = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.auto-reply-view { background: var(--on-bg); }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.page-title { font-size: 15px; font-weight: 600; display: flex; align-items: center; }
.page-title .current-group { font-size: 13px; font-weight: 400; color: var(--on-text-dim); margin-left: 4px; }
.empty-text { padding: 30px; }

/* P0: 分组侧边栏 */
.group-sidebar {
  width: 180px;
  background: var(--on-bg-card);
  border-right: 1px solid var(--on-border-light);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 12px 8px;
  border-bottom: 1px solid var(--on-border-light);
}

.sidebar-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--on-text-dim);
  text-transform: uppercase;
}

.group-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.group-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  cursor: pointer;
  transition: background 0.15s;
  border-left: 3px solid transparent;
}

.group-item:hover {
  background: var(--on-bg-hover);
}

.group-item.active {
  background: var(--on-bg-hover);
  border-left-color: var(--on-accent);
}

.group-name {
  flex: 1;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.group-name.disabled {
  color: var(--on-text-dim);
}

.group-count {
  font-size: 11px;
  color: var(--on-text-dim);
  background: var(--on-bg-hover);
  padding: 2px 6px;
  border-radius: 10px;
}

.sidebar-stats {
  padding: 10px 12px;
  border-top: 1px solid var(--on-border-light);
  cursor: pointer;
  font-size: 12px;
  color: var(--on-text-dim);
  display: flex;
  align-items: center;
  transition: color 0.15s;
}

.sidebar-stats:hover {
  color: var(--on-accent);
}

/* P0: 命中统计样式 */
.stats-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
  color: var(--on-text-dim);
}

.stats-summary {
  margin-bottom: 16px;
}

.stats-section {
  margin-top: 16px;
}

.stats-section-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--on-text);
}

.leaderboard {
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
  overflow: hidden;
}

.leaderboard-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--on-border-light);
}

.leaderboard-item:last-child {
  border-bottom: none;
}

.leaderboard-item .rank {
  font-size: 12px;
  font-weight: 600;
  color: var(--on-accent);
  min-width: 28px;
}

.leaderboard-item .pattern {
  flex: 1;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hit-count-badge {
  font-size: 11px;
  background: var(--on-accent);
  color: white;
  padding: 2px 6px;
  border-radius: 10px;
}

.recent-hits {
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
  overflow: hidden;
}

.recent-hit-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--on-border-light);
  font-size: 12px;
}

.recent-hit-item:last-child {
  border-bottom: none;
}

.recent-hit-item .pattern {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-hit-item .time {
  font-size: 11px;
  color: var(--on-text-dim);
  white-space: nowrap;
}

/* P0: 规则表格样式 */
.hit-count {
  color: var(--on-accent);
  font-weight: 600;
}

.rule-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.rule-tag {
  max-width: 80px;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* P0: 分组管理对话框 */
.group-manage-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.new-group-form {
  display: flex;
  gap: 8px;
}

.new-group-form .el-input {
  flex: 1;
}

.group-list-manage {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.group-manage-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--on-bg-hover);
  border-radius: 4px;
}

.group-manage-name {
  flex: 1;
  font-size: 13px;
}

.group-manage-count {
  font-size: 11px;
  color: var(--on-text-dim);
}

/* 多选悬浮工具栏（贴滚动容器右上角，滚动时隐藏，N 秒后显示） */
.rules-scroll-container { position: relative; }
.multi-float-bar {
  position: absolute;
  top: 8px;
  right: 24px;
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
  white-space: nowrap;
}

/* 过渡动画 */
.float-bar-enter-active,
.float-bar-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}
.float-bar-enter-from,
.float-bar-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}

/* ========== 实时命中监控增强样式 ========== */
/* P0: 预览匹配对话框样式 */
.preview-rule-info {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding: 8px 12px;
  background: var(--on-bg-hover);
  border-radius: 4px;
}

.preview-rule-pattern {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

.preview-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
  color: var(--on-text-dim);
}

.preview-summary {
  margin-bottom: 12px;
}

.preview-samples {
  margin-top: 12px;
}

.preview-samples-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--on-text);
}

.preview-samples-list {
  max-height: 320px;
  overflow-y: auto;
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
}

.preview-sample-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--on-border-light);
  font-size: 12px;
  transition: background 0.15s;
}

.preview-sample-item:last-child {
  border-bottom: none;
}

.preview-sample-item:hover {
  background: var(--on-bg-hover);
}

.preview-sample-url {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: 'Consolas', 'Monaco', monospace;
  color: var(--on-text);
}

/* 三栏布局 */
.stats-grid {
  display: grid;
  grid-template-columns: 200px 1fr 280px;
  gap: 16px;
  margin-top: 16px;
  max-height: 400px;
  overflow: hidden;
}

.stats-column {
  background: var(--on-bg-card);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.column-header {
  font-size: 12px;
  font-weight: 600;
  padding: 8px 12px;
  border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-hover);
  color: var(--on-text);
}

/* 概览卡片 */
.stats-overview {
  display: flex;
  gap: 16px;
  margin-bottom: 16px;
}

.overview-card {
  flex: 1;
  background: var(--on-bg-card);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius);
  padding: 12px 16px;
  text-align: center;
}

.overview-value {
  font-size: 24px;
  font-weight: 600;
  color: var(--on-accent);
}

.overview-label {
  font-size: 11px;
  color: var(--on-text-dim);
  margin-top: 4px;
}

/* 实时标签动画 */
.pulse-tag {
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

/* 排行榜（新版） */
.leaderboard {
  flex: 1;
  overflow-y: auto;
}

.leaderboard-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--on-border-light);
  font-size: 11px;
}

.leaderboard-item:last-child {
  border-bottom: none;
}

.leaderboard-item .rank {
  font-weight: 600;
  min-width: 24px;
}

.leaderboard-item .rank-1 { color: #ffd700; }
.leaderboard-item .rank-2 { color: #c0c0c0; }
.leaderboard-item .rank-3 { color: #cd7f32; }
.leaderboard-item .rank-1,
.leaderboard-item .rank-2,
.leaderboard-item .rank-3 { min-width: 28px; }

.rule-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.leaderboard-item .hit-count-badge {
  font-size: 10px;
  padding: 1px 5px;
}

/* 热力图 */
.heatmap {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.heatmap-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 11px;
}

.heatmap-rule {
  width: 80px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-text);
}

.heatmap-bar-container {
  flex: 1;
  height: 12px;
  background: var(--on-bg-hover);
  border-radius: 3px;
  overflow: hidden;
}

.heatmap-bar {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
  min-width: 2px;
}

.heatmap-stats {
  display: flex;
  gap: 6px;
  font-size: 10px;
  color: var(--on-text-dim);
  min-width: 80px;
  justify-content: flex-end;
}

.avg-ms {
  color: var(--on-accent);
  font-weight: 500;
}

.range-ms {
  color: var(--on-text-dim);
}

/* 时间线 */
.timeline {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.timeline-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
  position: relative;
}

.timeline-item::before {
  content: '';
  position: absolute;
  left: 5px;
  top: 14px;
  bottom: -8px;
  width: 1px;
  background: var(--on-border-light);
}

.timeline-item:last-child::before {
  display: none;
}

.timeline-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
  margin-top: 4px;
  border: 2px solid var(--on-bg-card);
  box-shadow: 0 0 0 1px var(--on-border-light);
}

.timeline-content {
  flex: 1;
  min-width: 0;
}

.timeline-rule {
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-text);
}

.timeline-meta {
  display: flex;
  gap: 8px;
  font-size: 10px;
  color: var(--on-text-dim);
  margin-top: 2px;
}

.timeline-duration {
  color: var(--on-accent);
  font-weight: 500;
}

/* 响应式调整 */
@media (max-width: 900px) {
  .stats-grid {
    grid-template-columns: 1fr;
    max-height: none;
  }

  .stats-column {
    max-height: 300px;
  }
}
</style>
