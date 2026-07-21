<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import type { Flow, ReplayOverride } from '../api/client'
import CodeEditor from './CodeEditor.vue'

// 重放对话框：支持覆盖 method/host/port/body/headers
const props = defineProps<{
  modelValue: boolean
  flow: Flow | null
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  replay: [id: number, override?: ReplayOverride]
}>()

// 覆盖参数（空值表示用原始值）
const editMethod = ref('')
const editHost = ref('')
const editPort = ref<number | null>(null)
const editBody = ref('')
const editHeaders = ref('') // JSON 字符串
const useOverride = ref(false) // 是否启用参数覆盖

watch(
  () => props.modelValue,
  (v) => {
    if (v && props.flow) {
      editMethod.value = props.flow.method
      editHost.value = props.flow.host
      editPort.value = null
      editBody.value = props.flow.request_body || ''
      // 解析原始 headers 为可编辑 JSON
      try {
        const h = JSON.parse(props.flow.request_headers || '{}')
        editHeaders.value = JSON.stringify(h, null, 2)
      } catch {
        editHeaders.value = '{}'
      }
      useOverride.value = false
    }
  }
)

function confirm() {
  if (!props.flow) return
  if (!useOverride.value) {
    emit('replay', props.flow.id)
  } else {
    const override: ReplayOverride = {}
    if (editMethod.value && editMethod.value !== props.flow.method) override.method = editMethod.value
    if (editHost.value && editHost.value !== props.flow.host) override.host = editHost.value
    if (editPort.value !== null) override.port = editPort.value
    if (editBody.value !== (props.flow.request_body || '')) override.body = editBody.value
    try {
      const h = JSON.parse(editHeaders.value || '{}')
      override.headers = h
    } catch { /* 忽略无效 JSON */ }
    emit('replay', props.flow.id, override)
  }
  emit('update:modelValue', false)
}
function close() {
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    @update:model-value="close"
    title="重放流量"
    width="640px"
  >
    <div v-if="flow" class="replay-body">
      <div class="replay-row">
        <span class="method-tag" :class="'m-' + flow.method.toLowerCase()">{{ flow.method }}</span>
        <span class="mono text-muted">{{ flow.url }}</span>
      </div>
      <div class="replay-meta text-muted">
        默认按原始记录重放。如需修改参数，开启下方「参数覆盖」。
      </div>

      <el-divider content-position="left">
        <el-switch v-model="useOverride" /> 参数覆盖
      </el-divider>

      <div v-if="useOverride" class="override-form">
        <div class="ov-row">
          <label class="ov-label">Method</label>
          <el-select v-model="editMethod" size="small" style="width: 120px">
            <el-option label="GET" value="GET" />
            <el-option label="POST" value="POST" />
            <el-option label="PUT" value="PUT" />
            <el-option label="DELETE" value="DELETE" />
            <el-option label="PATCH" value="PATCH" />
            <el-option label="HEAD" value="HEAD" />
            <el-option label="OPTIONS" value="OPTIONS" />
          </el-select>
          <label class="ov-label" style="margin-left: 12px">Host</label>
          <el-input v-model="editHost" size="small" style="width: 200px" />
          <label class="ov-label" style="margin-left: 12px">Port</label>
          <el-input-number v-model="editPort" :controls="false" size="small" style="width: 80px" :min="1" :max="65535" placeholder="默认" />
        </div>
        <div class="ov-row">
          <label class="ov-label">Body</label>
          <CodeEditor v-model="editBody" language="plaintext" :min-height="'100px'" />
        </div>
        <div class="ov-row">
          <label class="ov-label">Headers</label>
          <CodeEditor v-model="editHeaders" language="json" :min-height="'100px'" placeholder="JSON 格式" />
        </div>
      </div>
    </div>
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" @click="confirm">
        <el-icon><RefreshRight /></el-icon>&nbsp;确认重放
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
.override-form { display: flex; flex-direction: column; gap: 10px; }
.ov-row { display: flex; align-items: flex-start; gap: 8px; }
.ov-label {
  font-size: 12px; color: var(--on-text-muted); width: 50px;
  flex-shrink: 0; padding-top: 6px; text-align: right;
}
</style>
