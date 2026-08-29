<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useFlowsStore } from '../stores/flows'
import { ArrowLeft, Timer, Plus, DataLine, Setting, List } from '@element-plus/icons-vue'
import type { DelayRule, DelayHitLog, DelayHeatmapData, DelayJitterConfig } from '../api/client'

const router = useRouter()
const flows = useFlowsStore()
const { t } = useI18n()

// Props: 支持从外部传入预填数据（从 FlowList 右键菜单跳转）
const props = defineProps<{
  prefilledPattern?: string
  prefilledHost?: string
  prefilledPhase?: 'request' | 'response'
  prefilledDelayMs?: number
}>()

// Tab 切换
type TabType = 'rules' | 'heatmap' | 'jitter' | 'hitlogs'
const activeTab = ref<TabType>('rules')

const rules = ref<DelayRule[]>([])
const hitLogs = ref<DelayHitLog[]>([])
const heatmapData = ref<DelayHeatmapData | null>(null)
const jitterConfig = ref<DelayJitterConfig>({ enabled: false, base_ms: 300, variance_ms: 50 })
const loading = ref(false)
const dialogVisible = ref(false)
const editing = ref<DelayRule | null>(null)
const submitting = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

// 热力图加载状态
const heatmapLoading = ref(false)
const heatmapBuckets = ref<Array<{ time: string; host: string; avg_delay: number; count: number }>>([])
const hostDistribution = ref<Array<{ host: string; total_count: number; avg_delay: number }>>([])

const matchOptions = computed<{ label: string; value: string }[]>(() => [
  { label: t('delay.matchWildcard'), value: 'wildcard' },
  { label: t('delay.matchRegex'), value: 'regex' },
  { label: t('delay.matchExact'), value: 'exact' },
])
const phaseOptions = computed<{ label: string; value: string }[]>(() => [
  { label: t('delay.phaseRequest'), value: 'request' },
  { label: t('delay.phaseResponse'), value: 'response' },
])
const matchLabel = computed<Record<string, string>>(() => ({
  wildcard: t('delay.matchWildcard'),
  regex: t('delay.matchRegex'),
  exact: t('delay.matchExact'),
}))
const phaseLabel = computed<Record<string, string>>(() => ({
  request: t('delay.phaseRequest'),
  response: t('delay.phaseResponse'),
}))

function unwrap<T>(res: any, fallback: T): T {
  return res?.data?.code === 0 ? (res.data.data as T) : fallback
}

// 预填表单（从 FlowList 右键菜单跳转时）
const prefillForm = () => {
  if (props.prefilledPattern) {
    form.value.pattern = props.prefilledPattern
  }
  if (props.prefilledHost) {
    form.value.host = props.prefilledHost
  }
  if (props.prefilledPhase) {
    form.value.phase = props.prefilledPhase
  }
  if (props.prefilledDelayMs !== undefined) {
    form.value.delay_ms = props.prefilledDelayMs
  }
}

async function load() {
  loading.value = true
  try {
    const res = await axios.get('/api/delay-rules')
    rules.value = unwrap<DelayRule[]>(res, [])
  } catch (e: any) {
    ElMessage.error(t('delay.loadFailed') + (e?.message || e))
  } finally {
    loading.value = false
  }
}

async function loadHitLogs() {
  try {
    const res = await axios.get('/api/delay-rules/hits', { params: { limit: 100 } })
    hitLogs.value = unwrap<DelayHitLog[]>(res, [])
  } catch (e: any) {
    // 静默失败
  }
}

async function loadHeatmap() {
  heatmapLoading.value = true
  try {
    const res = await axios.get('/api/delay-rules/heatmap', { params: { bucket_seconds: 300, top_n: 20 } })
    const data = unwrap<DelayHeatmapData>(res, { buckets: [], host_distribution: [] })
    heatmapData.value = data
    heatmapBuckets.value = data.buckets || []
    hostDistribution.value = data.host_distribution || []
  } catch (e: any) {
    ElMessage.error(t('delay.loadFailed') + (e?.message || e))
  } finally {
    heatmapLoading.value = false
  }
}

async function loadJitterConfig() {
  try {
    const res = await axios.get('/api/delay-rules/jitter-config')
    jitterConfig.value = unwrap<DelayJitterConfig>(res, { enabled: false, base_ms: 300, variance_ms: 50 })
  } catch (e: any) {
    // 使用默认值
  }
}

async function saveJitterConfig() {
  try {
    await axios.put('/api/delay-rules/jitter-config', jitterConfig.value)
    ElMessage.success(t('delay.saveSuccess'))
  } catch (e: any) {
    ElMessage.error(t('delay.saveFailed') + (e?.message || e))
  }
}

async function clearHitLogs() {
  try {
    await axios.delete('/api/delay-rules/hits')
    hitLogs.value = []
    ElMessage.success(t('delay.hitLogsCleared'))
  } catch (e: any) {
    ElMessage.error(t('delay.clearHitLogsFailed') + (e?.message || e))
  }
}

function openNew() {
  editing.value = null
  form.value = {
    id: undefined,
    enabled: true,
    pattern: '',
    match_mode: 'wildcard',
    phase: 'request',
    delay_ms: 500,
    host: '',
    note: '',
    jitter_base: undefined,
    jitter_variance: undefined,
  }
  // 应用预填数据
  prefillForm()
  dialogVisible.value = true
}

function openEdit(r: DelayRule) {
  editing.value = r
  form.value = {
    id: r.id,
    enabled: r.enabled,
    pattern: r.pattern,
    match_mode: r.match_mode,
    phase: r.phase,
    delay_ms: r.delay_ms,
    host: r.host,
    note: r.note,
    jitter_base: r.jitter_base,
    jitter_variance: r.jitter_variance,
  }
  dialogVisible.value = true
}

const form = ref<DelayRule>({
  enabled: true,
  pattern: '',
  match_mode: 'wildcard',
  phase: 'request',
  delay_ms: 500,
  host: '',
  note: '',
  jitter_base: undefined,
  jitter_variance: undefined,
})

async function submit() {
  if (!form.value.pattern.trim()) {
    ElMessage.warning(t('delay.patternRequired'))
    return
  }
  submitting.value = true
  try {
    if (editing.value && editing.value.id) {
      const res = await axios.put(`/api/delay-rules/${editing.value.id}`, form.value)
      const updated = unwrap<DelayRule>(res, form.value)
      const idx = rules.value.findIndex(x => x.id === editing.value!.id)
      if (idx >= 0) rules.value[idx] = updated
      ElMessage.success(t('delay.saveSuccess'))
    } else {
      const res = await axios.post('/api/delay-rules', form.value)
      const created = unwrap<DelayRule>(res, form.value)
      rules.value.push(created)
      ElMessage.success(t('delay.saveSuccess'))
    }
    dialogVisible.value = false
  } catch (e: any) {
    ElMessage.error(t('delay.saveFailed') + (e?.message || e))
  } finally {
    submitting.value = false
  }
}

async function deleteRule(r: DelayRule) {
  try {
    await ElMessageBox.confirm(t('delay.deleteConfirm'), t('delay.deleteTitle'), {
      confirmButtonText: t('delay.deleteButton'),
      cancelButtonText: t('delay.cancelButton'),
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await axios.delete(`/api/delay-rules/${r.id}`)
    rules.value = rules.value.filter(x => x.id !== r.id)
    ElMessage.success(t('delay.deleted'))
  } catch (e: any) {
    ElMessage.error(t('delay.deleteFailed') + (e?.message || e))
  }
}

async function toggleRule(r: DelayRule) {
  const prev = r.enabled
  try {
    const res = await axios.post(`/api/delay-rules/${r.id}/toggle`)
    const updated = unwrap<DelayRule>(res, r)
    r.enabled = updated.enabled
  } catch (e: any) {
    r.enabled = prev
    ElMessage.error(t('delay.toggleFailed') + (e?.message || e))
  }
}

// 热力图颜色计算
function getHeatmapColor(avgDelay: number): string {
  // 基于延迟值的颜色映射：绿色(快) -> 黄色(中) -> 红色(慢)
  if (avgDelay < 100) return 'rgba(34, 197, 94, 0.6)' // 绿色 - 快
  if (avgDelay < 300) return 'rgba(250, 204, 21, 0.6)' // 黄色 - 中
  if (avgDelay < 500) return 'rgba(249, 115, 22, 0.6)' // 橙色 - 较慢
  return 'rgba(239, 68, 68, 0.6)' // 红色 - 慢
}

// 格式化延迟显示
function formatDelay(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

onMounted(() => {
  load()
  loadHitLogs()
  loadJitterConfig()
  // 定期刷新命中日志
  pollTimer = setInterval(() => {
    if (activeTab.value === 'hitlogs') {
      loadHitLogs()
    }
  }, 3000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="delay-view full flex flex-col">
    <div class="page-header">
      <div class="page-title">
        <el-button link size="small" @click="router.push(flows.lastPage || '/settings')" style="margin-right: 4px; padding: 0">
          <el-icon><ArrowLeft /></el-icon>
        </el-button>
        <el-icon><Timer /></el-icon>&nbsp;{{ t('delay.pageTitle') }}
      </div>
      <el-button type="primary" size="small" @click="openNew">
        <el-icon><Plus /></el-icon>&nbsp;{{ t('delay.newRule') }}
      </el-button>
    </div>

    <!-- Tab 切换 -->
    <div class="tab-bar">
      <div class="tab-item" :class="{ active: activeTab === 'rules' }" @click="activeTab = 'rules'">
        <el-icon><List /></el-icon>&nbsp;{{ t('delay.title') }}
      </div>
      <div class="tab-item" :class="{ active: activeTab === 'heatmap' }" @click="activeTab = 'heatmap'; loadHeatmap()">
        <el-icon><DataLine /></el-icon>&nbsp;{{ t('delay.heatmap') }}
      </div>
      <div class="tab-item" :class="{ active: activeTab === 'jitter' }" @click="activeTab = 'jitter'">
        <el-icon><Setting /></el-icon>&nbsp;{{ t('delay.jitterConfig') }}
      </div>
      <div class="tab-item" :class="{ active: activeTab === 'hitlogs' }" @click="activeTab = 'hitlogs'; loadHitLogs()">
        <el-icon><List /></el-icon>&nbsp;{{ t('delay.hitLogs') }}
      </div>
    </div>

    <!-- 规则列表 Tab -->
    <div v-show="activeTab === 'rules'" class="tab-content flex-1 overflow-auto" style="padding: 0 16px 16px">
      <el-table :data="rules" size="small" border stripe>
        <el-table-column :label="t('delay.enabled')" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" @change="toggleRule(row as DelayRule)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('delay.matchMode')" width="100">
          <template #default="{ row }">{{ matchLabel[row.match_mode] || row.match_mode }}</template>
        </el-table-column>
        <el-table-column :label="t('delay.urlPattern')" prop="pattern" class-name="mono" min-width="160" />
        <el-table-column :label="t('delay.phase')" width="100">
          <template #default="{ row }">{{ phaseLabel[row.phase] || row.phase }}</template>
        </el-table-column>
        <el-table-column :label="t('delay.delayMs')" width="100">
          <template #default="{ row }">{{ row.delay_ms }} ms</template>
        </el-table-column>
        <el-table-column :label="t('delay.host')" prop="host" width="140">
          <template #default="{ row }">
            <span v-if="row.host" class="mono">{{ row.host }}</span>
            <span v-else class="text-dim">*</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('delay.note')" min-width="120">
          <template #default="{ row }">
            <span v-if="row.note" :title="row.note">{{ row.note }}</span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('delay.operations')" width="140">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row as DelayRule)">{{ t('delay.edit') }}</el-button>
            <el-button link type="danger" size="small" @click="deleteRule(row as DelayRule)">{{ t('delay.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('delay.emptyHint') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 延迟分布热力图 Tab -->
    <div v-show="activeTab === 'heatmap'" class="tab-content flex-1 overflow-auto" style="padding: 16px">
      <div v-if="heatmapLoading" class="loading-state">
        <el-icon class="is-loading"><Loading /></el-icon>&nbsp;{{ t('common.loading') }}
      </div>
      <div v-else-if="!heatmapBuckets.length" class="empty-text text-dim">{{ t('delay.heatmapEmpty') }}</div>
      <div v-else class="heatmap-container">
        <!-- Host 分布卡片 -->
        <div class="heatmap-section">
          <div class="section-title">{{ t('delay.host') }} {{ t('analyze.groupByHost') }}</div>
          <div class="host-cards">
            <div
              v-for="item in hostDistribution"
              :key="item.host"
              class="host-card"
              :style="{ borderLeftColor: getHeatmapColor(item.avg_delay) }"
            >
              <div class="host-name mono">{{ item.host }}</div>
              <div class="host-stats">
                <span class="stat-item">
                  <span class="stat-label">{{ t('delay.hitDelay') }}:</span>
                  <span class="stat-value">{{ formatDelay(item.avg_delay) }}</span>
                </span>
                <span class="stat-item">
                  <span class="stat-label">{{ t('record.flowCount') }}:</span>
                  <span class="stat-value">{{ item.total_count }}</span>
                </span>
              </div>
            </div>
          </div>
        </div>
        <!-- 时间分布热力图 -->
        <div class="heatmap-section">
          <div class="section-title">{{ t('timeline.title') }}</div>
          <div class="heatmap-grid">
            <div
              v-for="(bucket, idx) in heatmapBuckets"
              :key="idx"
              class="heatmap-cell"
              :style="{ backgroundColor: getHeatmapColor(bucket.avg_delay) }"
              :title="`${bucket.host}: ${formatDelay(bucket.avg_delay)} (${bucket.count})`"
            >
              <span class="cell-text">{{ bucket.count }}</span>
            </div>
          </div>
          <!-- 图例 -->
          <div class="heatmap-legend">
            <span class="legend-item"><span class="legend-color" style="background: rgba(34, 197, 94, 0.6)"></span>&lt;100ms</span>
            <span class="legend-item"><span class="legend-color" style="background: rgba(250, 204, 21, 0.6)"></span>100-300ms</span>
            <span class="legend-item"><span class="legend-color" style="background: rgba(249, 115, 22, 0.6)"></span>300-500ms</span>
            <span class="legend-item"><span class="legend-color" style="background: rgba(239, 68, 68, 0.6)"></span>&gt;500ms</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 梯度延迟配置 Tab -->
    <div v-show="activeTab === 'jitter'" class="tab-content flex-1 overflow-auto" style="padding: 16px">
      <div class="jitter-config-card">
        <div class="config-title">{{ t('delay.jitterConfig') }}</div>
        <div class="config-hint text-dim">{{ t('delay.jitterHint') }}</div>
        <el-form label-width="140px" size="default" style="max-width: 500px; margin-top: 16px">
          <el-form-item :label="t('delay.jitterEnabled')">
            <el-switch v-model="jitterConfig.enabled" />
          </el-form-item>
          <el-form-item :label="t('delay.jitterBase')">
            <el-input-number v-model="jitterConfig.base_ms" :min="0" :max="60000" :step="100" style="width: 100%" />
          </el-form-item>
          <el-form-item :label="t('delay.jitterVariance')">
            <el-input-number v-model="jitterConfig.variance_ms" :min="0" :max="10000" :step="10" style="width: 100%" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="saveJitterConfig">{{ t('common.save') }}</el-button>
          </el-form-item>
        </el-form>
      </div>
    </div>

    <!-- 命中日志 Tab -->
    <div v-show="activeTab === 'hitlogs'" class="tab-content flex-1 overflow-auto" style="padding: 16px">
      <div class="hitlogs-header">
        <div class="section-title">{{ t('delay.hitLogsTitle') }}</div>
        <el-button size="small" @click="clearHitLogs">{{ t('delay.clearHitLogs') }}</el-button>
      </div>
      <el-table :data="hitLogs" size="small" border stripe max-height="400">
        <el-table-column :label="t('delay.hitTimestamp')" prop="timestamp" width="180" />
        <el-table-column :label="t('delay.hitRule')" prop="rule_pattern" class-name="mono" min-width="160" show-overflow-tooltip />
        <el-table-column :label="t('delay.hitFlow')" width="80">
          <template #default="{ row }">
            <span class="mono">#{{ row.flow_id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="URL" prop="url" class-name="mono" min-width="200" show-overflow-tooltip />
        <el-table-column :label="t('delay.hitDelay')" width="100">
          <template #default="{ row }">{{ row.matched_delay_ms }} ms</template>
        </el-table-column>
        <el-table-column :label="t('delay.hitActualDelay')" width="110">
          <template #default="{ row }">
            <span v-if="row.actual_delay_ms">{{ row.actual_delay_ms }} ms</span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('delay.hitLogsEmpty') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 新增/编辑规则对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editing ? t('delay.editTitle') : t('delay.newTitle')"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form :model="form" label-width="100px" size="default">
        <el-form-item :label="t('delay.matchMode')">
          <el-select v-model="form.match_mode" style="width: 100%">
            <el-option v-for="o in matchOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('delay.urlPattern')">
          <el-input v-model="form.pattern" :placeholder="t('delay.patternPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('delay.phase')">
          <el-select v-model="form.phase" style="width: 100%">
            <el-option v-for="o in phaseOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('delay.delayMs')">
          <el-input-number v-model="form.delay_ms" :min="0" :max="60000" :step="100" style="width: 100%" />
        </el-form-item>
        <el-form-item :label="t('delay.host')">
          <el-input v-model="form.host" :placeholder="t('delay.hostPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('delay.note')">
          <el-input v-model="form.note" :placeholder="t('delay.notePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('delay.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('delay.cancelButton') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">{{ t('delay.saveButton') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.delay-view { background: var(--on-bg); }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.page-title { font-size: 15px; font-weight: 600; display: flex; align-items: center; }
.tab-bar {
  display: flex; align-items: center; gap: 4px;
  padding: 8px 16px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.tab-item {
  display: flex; align-items: center; gap: 4px;
  padding: 6px 14px; border-radius: var(--on-radius-md);
  cursor: pointer; font-size: 13px; color: var(--on-text-secondary);
  transition: all 0.2s;
}
.tab-item:hover { background: var(--on-bg-hover); color: var(--on-text); }
.tab-item.active { background: var(--on-accent); color: white; font-weight: 500; }
.tab-content { background: var(--on-bg); }
.empty-text { padding: 30px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

/* 热力图样式 */
.heatmap-container { display: flex; flex-direction: column; gap: 20px; }
.heatmap-section { background: var(--on-bg-elevated); border-radius: var(--on-radius-md); padding: 16px; }
.section-title { font-size: 14px; font-weight: 600; margin-bottom: 12px; }
.host-cards { display: flex; flex-wrap: wrap; gap: 12px; }
.host-card {
  background: var(--on-bg);
  border-left: 4px solid;
  border-radius: var(--on-radius-sm);
  padding: 12px 16px;
  min-width: 200px;
}
.host-name { font-size: 13px; font-weight: 500; margin-bottom: 6px; }
.host-stats { display: flex; gap: 12px; font-size: 12px; }
.stat-item { display: flex; gap: 4px; }
.stat-label { color: var(--on-text-secondary); }
.stat-value { font-weight: 500; }
.heatmap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(60px, 1fr));
  gap: 2px;
  margin-bottom: 12px;
}
.heatmap-cell {
  aspect-ratio: 1;
  display: flex; align-items: center; justify-content: center;
  border-radius: 2px;
  font-size: 10px;
  color: white;
  cursor: pointer;
  min-height: 40px;
}
.cell-text { font-weight: 600; }
.heatmap-legend { display: flex; gap: 16px; font-size: 11px; color: var(--on-text-secondary); }
.legend-item { display: flex; align-items: center; gap: 4px; }
.legend-color { display: inline-block; width: 14px; height: 14px; border-radius: 2px; }

/* 梯度延迟配置 */
.jitter-config-card { background: var(--on-bg-elevated); border-radius: var(--on-radius-md); padding: 20px; }
.config-title { font-size: 15px; font-weight: 600; }
.config-hint { font-size: 12px; margin-top: 8px; }

/* 命中日志 */
.hitlogs-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.loading-state { display: flex; align-items: center; justify-content: center; padding: 40px; color: var(--on-text-secondary); }
</style>
