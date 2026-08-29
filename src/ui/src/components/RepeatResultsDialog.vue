<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { RepeatResponse } from '../api/client'

const props = defineProps<{
  modelValue: boolean
  result: RepeatResponse | null
  loading: boolean
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
}>()

const { t } = useI18n()

function close() {
  emit('update:modelValue', false)
}

// 表格只展示前 200 条，避免大量数据卡 UI
const tableData = computed(() => {
  if (!props.result) return []
  return props.result.results.slice(0, 200)
})

// 状态码颜色映射
function statusColor(code?: number): string {
  if (!code) return ''
  if (code < 300) return 'var(--on-ok)'
  if (code < 400) return 'var(--on-redirect)'
  if (code < 500) return 'var(--on-warn)'
  return 'var(--on-error)'
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    @update:model-value="close"
    :title="t('replay.repeatResultsTitle')"
    width="min(800px, 95vw)"
    :close-on-click-modal="false"
  >
    <div v-loading="loading" class="rr-body">
      <template v-if="result">
        <!-- 统计概览 -->
        <div class="rr-stats">
          <div class="rr-stat-item">
            <span class="rr-stat-label">{{ t('replay.statTotal') }}</span>
            <span class="rr-stat-value">{{ result.count }}</span>
          </div>
          <div class="rr-stat-item">
            <span class="rr-stat-label">{{ t('replay.statSuccess') }}</span>
            <span class="rr-stat-value" style="color: var(--on-ok)">{{ result.stats.success }}</span>
          </div>
          <div class="rr-stat-item">
            <span class="rr-stat-label">{{ t('replay.statFail') }}</span>
            <span class="rr-stat-value" style="color: var(--on-error)">{{ result.stats.fail }}</span>
          </div>
          <div class="rr-stat-item">
            <span class="rr-stat-label">{{ t('replay.statDurationAvg') }}</span>
            <span class="rr-stat-value">{{ result.stats.duration_avg_ms }} ms</span>
          </div>
          <div class="rr-stat-item">
            <span class="rr-stat-label">{{ t('replay.statDurationMin') }}</span>
            <span class="rr-stat-value">{{ result.stats.duration_min_ms }} ms</span>
          </div>
          <div class="rr-stat-item">
            <span class="rr-stat-label">{{ t('replay.statDurationMax') }}</span>
            <span class="rr-stat-value">{{ result.stats.duration_max_ms }} ms</span>
          </div>
        </div>

        <!-- 状态码分布 -->
        <div v-if="Object.keys(result.stats.status_distribution).length" class="rr-dist">
          <span class="rr-dist-label">{{ t('replay.statStatusDist') }}:</span>
          <el-tag
            v-for="(cnt, code) in result.stats.status_distribution"
            :key="code"
            size="small"
            style="margin: 0 4px"
          >
            {{ code }} × {{ cnt }}
          </el-tag>
        </div>

        <el-divider />

        <!-- 结果表格 -->
        <el-table :data="tableData" size="small" border style="width: 100%" max-height="360">
          <el-table-column prop="index" label="#" width="60" />
          <el-table-column :label="t('replay.colStatus')" width="90">
            <template #default="{ row }">
              <span v-if="row.ok" :style="{ color: statusColor(row.status_code), fontWeight: 600 }">
                {{ row.status_code || '-' }}
              </span>
              <span v-else style="color: var(--on-error)">ERR</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('replay.colDuration')" width="110">
            <template #default="{ row }">{{ row.duration_ms }} ms</template>
          </el-table-column>
          <el-table-column :label="t('replay.colSize')" width="100">
            <template #default="{ row }">{{ row.ok ? (row.size || 0) : '-' }}</template>
          </el-table-column>
          <el-table-column :label="t('replay.colError')">
            <template #default="{ row }">
              <span v-if="!row.ok" class="mono" style="color: var(--on-error); font-size: 11px">{{ row.error }}</span>
              <span v-else style="color: var(--on-text-muted)">-</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="result.results.length > 200" class="rr-truncated">
          {{ t('replay.truncatedHint', { shown: 200, total: result.results.length }) }}
        </div>
      </template>
      <div v-else-if="!loading" class="rr-empty">{{ t('replay.noResults') }}</div>
    </div>
    <template #footer>
      <el-button type="primary" @click="close">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.rr-body { min-height: 120px; }
.rr-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}
.rr-stat-item {
  display: flex; flex-direction: column; gap: 2px;
  padding: 8px 10px;
  background: var(--on-bg-elevated);
  border-radius: 4px;
  border: 1px solid var(--on-border);
}
.rr-stat-label { font-size: 11px; color: var(--on-text-muted); }
.rr-stat-value { font-size: 16px; font-weight: 600; font-family: var(--on-font-mono); }
.rr-dist {
  font-size: 12px; color: var(--on-text-muted);
  padding: 6px 0;
}
.rr-dist-label { margin-right: 6px; }
.rr-truncated {
  margin-top: 8px; font-size: 11px; color: var(--on-text-muted); text-align: center;
}
.rr-empty {
  text-align: center; color: var(--on-text-muted); padding: 40px 0;
}
</style>
