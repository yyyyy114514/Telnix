<script setup lang="ts">
// 协议深度解析视图：展示后端 /flows/{id}/decode 返回的结构化字段
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../api/client'

const { t } = useI18n()

const props = defineProps<{
  flowId: number | null
  field: string  // 'raw_data' | 'request_body' | 'response_body'
}>()

interface DecodeField {
  label: string
  value: string
  color?: string
}
interface DecodeResult {
  protocol: string
  summary?: string
  error?: string
  fields: DecodeField[]
}

const loading = ref(false)
const result = ref<DecodeResult | null>(null)

async function load() {
  if (!props.flowId) {
    result.value = null
    return
  }
  loading.value = true
  try {
    const r = await api.decodeFlow(props.flowId, props.field)
    result.value = r
  } catch (e: any) {
    result.value = {
      protocol: 'error',
      error: e?.message || String(e),
      fields: [],
    }
  } finally {
    loading.value = false
  }
}

watch(() => [props.flowId, props.field], load, { immediate: true })
</script>

<template>
  <div class="proto-view">
    <div v-if="loading" class="text-dim proto-loading">
      <el-icon class="is-loading"><Loading /></el-icon>
      &nbsp;{{ t('protocolView.parsing') }}
    </div>
    <template v-else-if="result">
      <div v-if="result.error" class="proto-error">
        <el-icon><WarningFilled /></el-icon>&nbsp;{{ result.error }}
      </div>
      <div v-else-if="!result.fields?.length" class="empty-text text-dim proto-empty">
        {{ t('protocolView.noData') }}
      </div>
      <template v-else>
        <div v-if="result.summary" class="proto-summary">
          <span class="proto-badge">{{ result.protocol }}</span>
          <span class="proto-summary-text">{{ result.summary }}</span>
        </div>
        <div class="proto-fields">
          <div
            v-for="(f, i) in result.fields"
            :key="i"
            class="proto-field"
            :class="f.color ? `clr-${f.color}` : ''"
          >
            <div class="proto-label">{{ f.label }}</div>
            <pre class="proto-value mono">{{ f.value || t('protocolView.empty') }}</pre>
          </div>
        </div>
      </template>
    </template>
    <div v-else class="empty-text text-dim proto-empty">{{ t('protocolView.noData') }}</div>
  </div>
</template>

<style scoped>
.proto-view {
  height: 100%;
  overflow: auto;
  padding: 8px 12px;
  font-size: 12.5px;
  background: var(--on-bg);
}
.proto-loading {
  padding: 12px;
  display: flex;
  align-items: center;
}
.proto-error {
  padding: 8px 12px;
  color: var(--on-rose, #f43f5e);
  background: rgba(244, 63, 94, 0.08);
  border-radius: 4px;
  border-left: 2px solid var(--on-rose, #f43f5e);
  display: flex;
  align-items: center;
}
.proto-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0 10px;
  border-bottom: 1px solid var(--on-border-light);
  margin-bottom: 10px;
}
.proto-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 3px;
  background: var(--on-accent-glow, rgba(45, 212, 191, 0.15));
  color: var(--on-accent, #2dd4bf);
  font-weight: 600;
  font-size: 11px;
  letter-spacing: 0.5px;
}
.proto-summary-text {
  color: var(--on-text);
  font-weight: 500;
}
.proto-fields {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.proto-field {
  display: flex;
  flex-direction: column;
  padding: 4px 0;
  border-bottom: 1px dashed var(--on-border-light);
}
.proto-field:last-child {
  border-bottom: none;
}
.proto-label {
  color: var(--on-text-dim);
  font-size: 11px;
  margin-bottom: 2px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.proto-value {
  margin: 0;
  padding: 2px 4px;
  background: var(--on-bg-elevated, rgba(0,0,0,0.03));
  border-radius: 3px;
  color: var(--on-text);
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 12px;
  line-height: 1.5;
  max-height: 300px;
  overflow: auto;
}
/* 颜色高亮 */
.clr-red .proto-label { color: var(--on-rose, #f43f5e); }
.clr-red .proto-value { border-left: 2px solid var(--on-rose, #f43f5e); }
.clr-amber .proto-label { color: var(--on-amber, #f59e0b); }
.clr-amber .proto-value { border-left: 2px solid var(--on-amber, #f59e0b); }
.clr-green .proto-label { color: var(--on-emerald, #10b981); }
.clr-green .proto-value { border-left: 2px solid var(--on-emerald, #10b981); }
.clr-blue .proto-label { color: #3b82f6; }
.clr-blue .proto-value { border-left: 2px solid #3b82f6; }
.clr-dim .proto-label { color: var(--on-text-dim); opacity: 0.7; }
.proto-empty {
  padding: 24px;
  text-align: center;
}
</style>
