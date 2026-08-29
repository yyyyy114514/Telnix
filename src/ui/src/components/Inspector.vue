<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Flow } from '../api/client'
import RequestInspector from './RequestInspector.vue'
import ResponseInspector from './ResponseInspector.vue'

// 检查器：上下分栏，断点流量显示放行/丢弃按钮
const { t } = useI18n()
const props = defineProps<{
  flow: Flow | null
  enabledTabs: string[]
  // 响应切换流量时是否自动回到 preview（默认 true）
  autoSwitchPreview?: boolean
}>()
const emit = defineEmits<{
  release: [action: 'release' | 'drop', modified: any]
}>()

const splitRatio = ref(0.5)
const modified = ref<Record<string, any>>({})

watch(
  () => props.flow?.id,
  () => {
    modified.value = {}
  }
)

function onReqModify(p: any) {
  modified.value = { ...modified.value, ...p }
}
function onRespModify(p: any) {
  modified.value = { ...modified.value, ...p }
}

const isBreakpoint = computed(
  () => !!props.flow && !!props.flow.breakpoint_status
)
const bpLabel = computed(() => {
  const s = props.flow?.breakpoint_status
  if (s === 'pending_request') return t('inspector.requestIntercepted')
  if (s === 'pending_response') return t('inspector.responseIntercepted')
  return t('inspector.intercepted')
})

// 拖拽分隔
const dragging = ref(false)
const containerRef = ref<HTMLElement | null>(null)

function onDown(e: MouseEvent) {
  e.preventDefault()
  dragging.value = true
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}
function onMove(e: MouseEvent) {
  const el = containerRef.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  const r = (e.clientY - rect.top) / rect.height
  splitRatio.value = Math.min(0.85, Math.max(0.15, r))
}
function onUp() {
  dragging.value = false
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseup', onUp)
}

// 组件卸载时清理：若仍在拖拽中卸载，移除 window 监听器，避免泄漏
onBeforeUnmount(() => {
  if (dragging.value) {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    dragging.value = false
  }
})

function release(action: 'release' | 'drop') {
  const payload = action === 'release' && Object.keys(modified.value).length ? { ...modified.value } : {}
  emit('release', action, payload)
  // 不在此处显示成功消息，由父组件 CaptureView 的 onRelease 在 API 成功后提示
}
</script>

<template>
  <div class="inspector full flex flex-col">
    <div v-if="!flow" class="empty-inspector full flex items-center justify-center">
      <div class="text-dim">
        <el-icon :size="36"><Document /></el-icon>
        <div style="margin-top: 10px">{{ t('inspector.selectSessionHint') }}</div>
      </div>
    </div>

    <template v-else>
      <!-- 断点控制条 -->
      <div v-if="isBreakpoint" class="bp-control-bar">
        <el-icon class="bp-icon"><VideoPause /></el-icon>
        <span class="bp-text">{{ bpLabel }}{{ t('inspector.bpEditableHint') }}</span>
        <div class="flex-1"></div>
        <el-button type="primary" size="small" @click="release('release')">
          <el-icon><Check /></el-icon>&nbsp;{{ t('inspector.releaseBtn') }}
        </el-button>
        <el-button type="danger" size="small" @click="release('drop')">
          <el-icon><Close /></el-icon>&nbsp;{{ t('inspector.dropBtn') }}
        </el-button>
      </div>

      <!-- 上下分栏 -->
      <div ref="containerRef" class="flex-1 flex flex-col overflow-hidden">
        <div :style="{ height: `calc(${splitRatio * 100}% - 2px)` }" class="insp-block">
          <RequestInspector
            :flow="flow"
            :editable="isBreakpoint"
            :enabled-tabs="enabledTabs"
            @modify="onReqModify"
          />
        </div>
        <div
          class="splitter-h"
          :class="{ active: dragging }"
          @mousedown="onDown"
        ></div>
        <div :style="{ height: `calc(${(1 - splitRatio) * 100}% - 2px)` }" class="insp-block">
          <ResponseInspector
            :flow="flow"
            :editable="isBreakpoint"
            :enabled-tabs="enabledTabs"
            :auto-switch-preview="autoSwitchPreview"
            @modify="onRespModify"
          />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.inspector { background: var(--on-bg-elevated); max-width: 100%; overflow: hidden; }
.empty-inspector { color: var(--on-text-dim); }
.bp-control-bar {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 12px;
  background: linear-gradient(90deg, rgba(210, 153, 34, 0.15), rgba(88, 166, 255, 0.1));
  border-bottom: 1px solid var(--on-border);
  font-size: 12.5px;
}
.bp-icon { color: var(--on-warn); font-size: 16px; }
.bp-text { color: var(--on-text); font-weight: 600; }
.insp-block { overflow: hidden; min-height: 0; }
</style>
