<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import type { Flow, ReplayOverride } from '../api/client'
import CodeEditor from './CodeEditor.vue'

const { t } = useI18n()

// 重放对话框：支持覆盖 method/host/port/body/headers
// Repeat Advanced：批量并发重放 N 次（可选间隔、并发数）
const props = defineProps<{
  modelValue: boolean
  flow: Flow | null
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  replay: [id: number, override?: ReplayOverride]
  repeat: [id: number, count: number, concurrency: number, intervalMs: number, override?: ReplayOverride]
}>()

// 模式：single = 单次重放（原行为），repeat = 批量重放
const mode = ref<'single' | 'repeat'>('single')

// 覆盖参数（空值表示用原始值）
const editMethod = ref('')
const editHost = ref('')
const editPort = ref<number | null>(null)
const editBody = ref('')
const editHeaders = ref('') // JSON 字符串
const useOverride = ref(false) // 是否启用参数覆盖

// Repeat Advanced 参数
const repeatCount = ref(5)
const repeatConcurrency = ref(1)
const repeatInterval = ref(0) // 毫秒

watch(
  () => props.modelValue,
  (v) => {
    if (v && props.flow) {
      editMethod.value = props.flow.method
      editHost.value = props.flow.host
      editPort.value = null
      editBody.value = props.flow.request_body || ''
      try {
        const h = JSON.parse(props.flow.request_headers || '{}')
        editHeaders.value = JSON.stringify(h, null, 2)
      } catch {
        editHeaders.value = '{}'
      }
      useOverride.value = false
      mode.value = 'single'
      // 重置 repeat 参数到默认值，避免上次设置残留
      repeatCount.value = 5
      repeatConcurrency.value = 1
      repeatInterval.value = 0
    }
  }
)

function buildOverride(): ReplayOverride | undefined {
  if (!useOverride.value) return undefined
  const override: ReplayOverride = {}
  if (editMethod.value && editMethod.value !== props.flow!.method) override.method = editMethod.value
  if (editHost.value && editHost.value !== props.flow!.host) override.host = editHost.value
  if (editPort.value !== null) override.port = editPort.value
  if (editBody.value !== (props.flow!.request_body || '')) override.body = editBody.value
  try {
    const h = JSON.parse(editHeaders.value || '{}')
    override.headers = h
  } catch {
    ElMessage.error(t('replay.headersJsonInvalid'))
    return undefined
  }
  return override
}

function confirm() {
  if (!props.flow) return
  if (mode.value === 'repeat') {
    // 参数校验
    if (repeatCount.value < 1 || repeatCount.value > 1000) {
      ElMessage.error(t('replay.repeatCountInvalid'))
      return
    }
    if (repeatConcurrency.value < 1 || repeatConcurrency.value > 50) {
      ElMessage.error(t('replay.repeatConcurrencyInvalid'))
      return
    }
    if (repeatInterval.value < 0 || repeatInterval.value > 60000) {
      ElMessage.error(t('replay.repeatIntervalInvalid'))
      return
    }
    const override = buildOverride()
    if (useOverride.value && override === undefined) return // JSON 解析失败已提示
    emit('repeat', props.flow.id, repeatCount.value, repeatConcurrency.value, repeatInterval.value, override)
  } else {
    const override = buildOverride()
    emit('replay', props.flow.id, override)
  }
  emit('update:modelValue', false)
}
function close() {
  emit('update:modelValue', false)
}

const confirmLabel = computed(() =>
  mode.value === 'repeat' ? t('replay.repeatConfirm') : t('replay.confirm')
)
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    @update:model-value="close"
    :title="t('replay.title')"
    width="min(640px, 95vw)"
  >
    <div v-if="flow" class="replay-body">
      <div class="replay-row">
        <span class="method-tag" :class="'m-' + flow.method.toLowerCase()">{{ flow.method }}</span>
        <span class="mono text-muted">{{ flow.url }}</span>
      </div>

      <!-- 模式切换 -->
      <el-radio-group v-model="mode" size="small" style="margin-top: 4px">
        <el-radio-button value="single">{{ t('replay.modeSingle') }}</el-radio-button>
        <el-radio-button value="repeat">{{ t('replay.modeRepeat') }}</el-radio-button>
      </el-radio-group>

      <!-- Repeat Advanced 参数 -->
      <div v-if="mode === 'repeat'" class="repeat-form">
        <div class="rp-row">
          <label class="rp-label">{{ t('replay.repeatCount') }}</label>
          <el-input-number v-model="repeatCount" :min="1" :max="1000" :step="1" size="small" style="width: 120px" />
          <label class="rp-label" style="margin-left: 16px">{{ t('replay.repeatConcurrency') }}</label>
          <el-input-number v-model="repeatConcurrency" :min="1" :max="50" :step="1" size="small" style="width: 100px" />
        </div>
        <div class="rp-row">
          <label class="rp-label">{{ t('replay.repeatInterval') }}</label>
          <el-input-number v-model="repeatInterval" :min="0" :max="60000" :step="100" size="small" style="width: 140px" />
          <span class="text-muted rp-unit">{{ t('replay.repeatIntervalUnit') }}</span>
        </div>
        <div class="replay-meta text-muted">{{ t('replay.repeatHint') }}</div>
      </div>

      <div v-else class="replay-meta text-muted">
        {{ t('replay.defaultHint') }}
      </div>

      <el-divider content-position="left">
        <el-switch v-model="useOverride" /> {{ t('replay.override') }}
      </el-divider>

      <div v-if="useOverride" class="override-form">
        <div class="ov-row">
          <label class="ov-label">{{ t('replay.methodLabel') }}</label>
          <el-select v-model="editMethod" size="small" style="width: 120px">
            <el-option label="GET" value="GET" />
            <el-option label="POST" value="POST" />
            <el-option label="PUT" value="PUT" />
            <el-option label="DELETE" value="DELETE" />
            <el-option label="PATCH" value="PATCH" />
            <el-option label="HEAD" value="HEAD" />
            <el-option label="OPTIONS" value="OPTIONS" />
          </el-select>
          <label class="ov-label" style="margin-left: 12px">{{ t('replay.hostLabel') }}</label>
          <el-input v-model="editHost" size="small" style="width: 200px" />
          <label class="ov-label" style="margin-left: 12px">{{ t('replay.portLabel') }}</label>
          <el-input-number v-model="editPort" :controls="false" size="small" style="width: 80px" :min="1" :max="65535" :placeholder="t('replay.portPlaceholder')" />
        </div>
        <div class="ov-row">
          <label class="ov-label">{{ t('replay.bodyLabel') }}</label>
          <CodeEditor v-model="editBody" language="plaintext" :min-height="'100px'" />
        </div>
        <div class="ov-row">
          <label class="ov-label">{{ t('replay.headersLabel') }}</label>
          <CodeEditor v-model="editHeaders" language="json" :min-height="'100px'" :placeholder="t('replay.headersPlaceholder')" />
        </div>
      </div>
    </div>
    <template #footer>
      <el-button @click="close">{{ t('common.cancel') }}</el-button>
      <el-button type="primary" @click="confirm">
        <el-icon><RefreshRight /></el-icon>&nbsp;{{ confirmLabel }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.replay-body { display: flex; flex-direction: column; gap: 10px; }
.replay-row { display: flex; align-items: center; gap: 8px; }
.method-tag {
  font-family: var(--on-font-mono); font-weight: 700; font-size: 11px;
  padding: 3px 8px; border-radius: 3px; color: var(--on-accent);
  background: var(--on-accent-glow); border: 1px solid var(--on-accent-dim);
}
.m-get { color: var(--on-ok); background: rgba(63,185,80,0.12); border-color: rgba(63,185,80,0.4); }
.m-post { color: var(--on-redirect); background: rgba(88,166,255,0.12); border-color: rgba(88,166,255,0.4); }
.m-put { color: var(--on-warn); background: rgba(210,153,34,0.12); border-color: rgba(210,153,34,0.4); }
.m-delete { color: var(--on-error); background: rgba(248,81,73,0.12); border-color: rgba(248,81,73,0.4); }
.replay-meta { font-size: 12px; line-height: 1.5; }
.repeat-form { display: flex; flex-direction: column; gap: 8px; padding: 8px 10px; background: var(--on-bg-elevated); border-radius: 4px; }
.rp-row { display: flex; align-items: center; gap: 8px; }
.rp-label { font-size: 12px; color: var(--on-text-muted); width: 70px; flex-shrink: 0; text-align: right; }
.rp-unit { font-size: 11px; }
.override-form { display: flex; flex-direction: column; gap: 10px; }
.ov-row { display: flex; align-items: flex-start; gap: 8px; }
.ov-label {
  font-size: 12px; color: var(--on-text-muted); width: 50px;
  flex-shrink: 0; padding-top: 6px; text-align: right;
}
</style>
