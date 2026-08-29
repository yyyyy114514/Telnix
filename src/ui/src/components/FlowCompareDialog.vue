<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFlowsStore } from '../stores/flows'
import { type Flow } from '../api/client'
import { compare } from 'fast-json-patch'

const props = defineProps<{
  modelValue: boolean
  flowA: Flow | null
  flowB: Flow | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  'swap': []
}>()

const flowsStore = useFlowsStore()
const { t } = useI18n()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

// 加载完整 flow 数据用于对比
const flowAComplete = ref<Flow | null>(null)
const flowBComplete = ref<Flow | null>(null)

// 加载状态
const loading = ref(false)

// 加载完整数据
watch(() => [props.flowA, props.flowB], async ([a, b]) => {
  if (a && !flowAComplete.value) {
    loading.value = true
    try {
      const full = await import('../api/client').then(m => m.api.getFlow(a.id))
      flowAComplete.value = full
    } catch { /* ignore */ }
    loading.value = false
  }
  if (b && !flowBComplete.value) {
    loading.value = true
    try {
      const full = await import('../api/client').then(m => m.api.getFlow(b.id))
      flowBComplete.value = full
    } catch { /* ignore */ }
    loading.value = false
  }
}, { immediate: true })

// 请求头对比
const requestHeaderDiff = computed(() => {
  if (!flowAComplete.value?.request_headers || !flowBComplete.value?.request_headers) return null
  try {
    const a = JSON.parse(flowAComplete.value.request_headers)
    const b = JSON.parse(flowBComplete.value.request_headers)
    const patches = compare(a, b)
    return patches.length > 0 ? patches : null
  } catch { return null }
})

// 响应头对比
const responseHeaderDiff = computed(() => {
  if (!flowAComplete.value?.response_headers || !flowBComplete.value?.response_headers) return null
  try {
    const a = JSON.parse(flowAComplete.value.response_headers)
    const b = JSON.parse(flowBComplete.value.response_headers)
    const patches = compare(a, b)
    return patches.length > 0 ? patches : null
  } catch { return null }
})

// 请求体对比
const requestBodyDiff = computed(() => {
  if (!flowAComplete.value?.request_body && !flowBComplete.value?.request_body) return null
  try {
    const a = flowAComplete.value?.request_body ? JSON.parse(flowAComplete.value.request_body) : null
    const b = flowBComplete.value?.request_body ? JSON.parse(flowBComplete.value.request_body) : null
    if (!a && !b) return null
    const patches = compare(a || {}, b || {})
    return patches.length > 0 ? patches : null
  } catch { return null }
})

// 响应体对比
const responseBodyDiff = computed(() => {
  if (!flowAComplete.value?.response_body && !flowBComplete.value?.response_body) return null
  try {
    const a = flowAComplete.value?.response_body ? JSON.parse(flowAComplete.value.response_body) : null
    const b = flowBComplete.value?.response_body ? JSON.parse(flowBComplete.value.response_body) : null
    if (!a && !b) return null
    const patches = compare(a || {}, b || {})
    return patches.length > 0 ? patches : null
  } catch { return null }
})

// 通用 diff 渲染
function renderDiffLine(patch: any, type: 'request' | 'response', field: 'headers' | 'body'): string {
  const { op, path, value, oldValue } = patch
  let result = ''
  if (op === 'add' || op === 'replace') {
    result = `+ ${path}: ${JSON.stringify(value)}`
  } else if (op === 'remove') {
    result = `- ${path}: ${JSON.stringify(oldValue)}`
  }
  return result
}

// 格式化 JSON
function formatJson(str: string | null | undefined): string {
  if (!str) return ''
  try {
    return JSON.stringify(JSON.parse(str), null, 2)
  } catch {
    return str
  }
}

// 判断是否为 JSON
function isJson(str: string | null | undefined): boolean {
  if (!str) return false
  try {
    JSON.parse(str)
    return true
  } catch {
    return false
  }
}

// 获取 Content-Type
function getContentType(headers: string | null): string {
  if (!headers) return ''
  try {
    const obj = JSON.parse(headers)
    return obj['Content-Type'] || obj['content-type'] || ''
  } catch {
    return ''
  }
}

// 判断是否有差异
const hasAnyDiff = computed(() => {
  return requestHeaderDiff.value || responseHeaderDiff.value ||
         requestBodyDiff.value || responseBodyDiff.value
})

// 交换 A/B
function swap() {
  const tempA = flowAComplete.value
  const tempB = flowBComplete.value
  flowAComplete.value = tempB
  flowBComplete.value = tempA
  emit('swap')
}

// 关闭时清理
function handleClose() {
  visible.value = false
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="t('compare.title')"
    width="90%"
    :close-on-click-modal="true"
    class="compare-dialog"
  >
    <div v-if="loading" class="compare-loading">
      <el-icon class="is-loading"><Loading /></el-icon>
      {{ t('common.loading') }}
    </div>

    <div v-else-if="!flowA || !flowB" class="compare-empty">
      {{ t('compare.selectFlows') }}
    </div>

    <div v-else class="compare-container">
      <!-- 对比概览 -->
      <div class="compare-header">
        <div class="compare-flow-info flow-a">
          <span class="flow-label">A</span>
          <span class="flow-method">{{ flowA.method }}</span>
          <span class="flow-path">{{ flowA.host }}{{ flowA.path }}</span>
          <span class="flow-status">{{ flowA.status_code || '-' }}</span>
        </div>
        <el-button size="small" @click="swap">
          <el-icon><RefreshRight /></el-icon>
          {{ t('compare.swap') }}
        </el-button>
        <div class="compare-flow-info flow-b">
          <span class="flow-label">B</span>
          <span class="flow-method">{{ flowB.method }}</span>
          <span class="flow-path">{{ flowB.host }}{{ flowB.path }}</span>
          <span class="flow-status">{{ flowB.status_code || '-' }}</span>
        </div>
      </div>

      <!-- 对比状态 -->
      <div class="compare-status">
        <el-tag v-if="hasAnyDiff" type="warning">{{ t('compare.hasDiff') }}</el-tag>
        <el-tag v-else type="success">{{ t('compare.noDiff') }}</el-tag>
      </div>

      <!-- 对比分栏 -->
      <div class="compare-body">
        <!-- 请求头 -->
        <div class="compare-section">
          <div class="compare-section-header">
            {{ t('compare.requestHeaders') }}
            <el-tag v-if="requestHeaderDiff" size="small" type="warning">{{ t('compare.diff') }}</el-tag>
          </div>
          <div class="compare-columns">
            <div class="compare-column">
              <div class="column-header">A</div>
              <pre class="compare-content">{{ formatJson(flowAComplete?.request_headers) || '-' }}</pre>
            </div>
            <div class="compare-column">
              <div class="column-header">B</div>
              <pre class="compare-content">{{ formatJson(flowBComplete?.request_headers) || '-' }}</pre>
            </div>
          </div>
          <div v-if="requestHeaderDiff" class="diff-patches">
            <div v-for="(patch, idx) in requestHeaderDiff" :key="idx" class="diff-line diff-add">
              {{ renderDiffLine(patch, 'request', 'headers') }}
            </div>
          </div>
        </div>

        <!-- 请求体 -->
        <div class="compare-section">
          <div class="compare-section-header">
            {{ t('compare.requestBody') }}
            <el-tag v-if="requestBodyDiff" size="small" type="warning">{{ t('compare.diff') }}</el-tag>
            <el-tag v-else size="small" type="info">{{ t('compare.same') }}</el-tag>
          </div>
          <div class="compare-columns">
            <div class="compare-column">
              <div class="column-header">A</div>
              <pre class="compare-content">{{ formatJson(flowAComplete?.request_body) || '-' }}</pre>
            </div>
            <div class="compare-column">
              <div class="column-header">B</div>
              <pre class="compare-content">{{ formatJson(flowBComplete?.request_body) || '-' }}</pre>
            </div>
          </div>
          <div v-if="requestBodyDiff" class="diff-patches">
            <div v-for="(patch, idx) in requestBodyDiff" :key="idx" class="diff-line" :class="patch.op === 'remove' ? 'diff-remove' : 'diff-add'">
              {{ renderDiffLine(patch, 'request', 'body') }}
            </div>
          </div>
        </div>

        <!-- 响应头 -->
        <div class="compare-section">
          <div class="compare-section-header">
            {{ t('compare.responseHeaders') }}
            <el-tag v-if="responseHeaderDiff" size="small" type="warning">{{ t('compare.diff') }}</el-tag>
          </div>
          <div class="compare-columns">
            <div class="compare-column">
              <div class="column-header">A</div>
              <pre class="compare-content">{{ formatJson(flowAComplete?.response_headers) || '-' }}</pre>
            </div>
            <div class="compare-column">
              <div class="column-header">B</div>
              <pre class="compare-content">{{ formatJson(flowBComplete?.response_headers) || '-' }}</pre>
            </div>
          </div>
          <div v-if="responseHeaderDiff" class="diff-patches">
            <div v-for="(patch, idx) in responseHeaderDiff" :key="idx" class="diff-line diff-add">
              {{ renderDiffLine(patch, 'response', 'headers') }}
            </div>
          </div>
        </div>

        <!-- 响应体 -->
        <div class="compare-section">
          <div class="compare-section-header">
            {{ t('compare.responseBody') }}
            <el-tag v-if="responseBodyDiff" size="small" type="warning">{{ t('compare.diff') }}</el-tag>
            <el-tag v-else size="small" type="info">{{ t('compare.same') }}</el-tag>
          </div>
          <div class="compare-columns">
            <div class="compare-column">
              <div class="column-header">A</div>
              <pre class="compare-content">{{ formatJson(flowAComplete?.response_body) || '-' }}</pre>
            </div>
            <div class="compare-column">
              <div class="column-header">B</div>
              <pre class="compare-content">{{ formatJson(flowBComplete?.response_body) || '-' }}</pre>
            </div>
          </div>
          <div v-if="responseBodyDiff" class="diff-patches">
            <div v-for="(patch, idx) in responseBodyDiff" :key="idx" class="diff-line" :class="patch.op === 'remove' ? 'diff-remove' : 'diff-add'">
              {{ renderDiffLine(patch, 'response', 'body') }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
.compare-dialog {
  max-width: 1400px;
}

.compare-loading,
.compare-empty {
  text-align: center;
  padding: 40px;
  color: var(--on-text-muted);
}

.compare-container {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-height: 70vh;
  overflow: auto;
}

.compare-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px;
  background: var(--on-bg);
  border-radius: 8px;
}

.compare-flow-info {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.flow-label {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 4px;
  font-weight: 600;
  font-size: 11px;
}

.flow-a .flow-label {
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
}

.flow-b .flow-label {
  background: var(--el-color-success-light-8);
  color: var(--el-color-success);
}

.flow-method {
  font-weight: 600;
  color: var(--on-accent);
  font-family: var(--on-font-mono, monospace);
}

.flow-path {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-text);
}

.flow-status {
  font-weight: 600;
  font-family: var(--on-font-mono, monospace);
}

.compare-status {
  display: flex;
  gap: 8px;
}

.compare-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.compare-section {
  border: 1px solid var(--on-border-light);
  border-radius: 8px;
  overflow: hidden;
}

.compare-section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--on-bg);
  font-weight: 600;
  font-size: 13px;
  border-bottom: 1px solid var(--on-border-light);
}

.compare-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
}

.compare-column {
  display: flex;
  flex-direction: column;
}

.compare-column:first-child {
  border-right: 1px solid var(--on-border-light);
}

.column-header {
  padding: 4px 12px;
  background: var(--on-bg-hover);
  font-size: 11px;
  font-weight: 600;
  color: var(--on-text-muted);
  border-bottom: 1px solid var(--on-border-light);
}

.compare-content {
  margin: 0;
  padding: 12px;
  font-family: var(--on-font-mono, monospace);
  font-size: 11px;
  line-height: 1.5;
  overflow: auto;
  max-height: 200px;
  background: var(--on-bg-elevated);
}

.diff-patches {
  padding: 8px 12px;
  background: var(--on-bg);
  border-top: 1px solid var(--on-border-light);
}

.diff-line {
  font-family: var(--on-font-mono, monospace);
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 3px;
  margin: 2px 0;
}

.diff-add {
  background: rgba(34, 197, 94, 0.1);
  color: var(--el-color-success);
}

.diff-remove {
  background: rgba(239, 68, 68, 0.1);
  color: var(--el-color-danger);
}
</style>
