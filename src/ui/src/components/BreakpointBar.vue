<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useCaptureStore } from '../stores/capture'

// 断点控制条：请求断点 / 响应断点 两个开关 + 全部放行/丢弃
const capture = useCaptureStore()
const { t } = useI18n()

const pendingCount = computed(() => capture.bpStatus.pending_flows?.length || 0)
</script>

<template>
  <div class="bp-bar">
    <span class="bp-label">{{ t('breakpointBar.label') }}</span>
    <el-button
      :type="capture.bpStatus.break_on_request ? 'warning' : 'default'"
      size="small"
      @click="capture.toggleBpRequest()"
    >
      <el-icon><VideoPause /></el-icon>&nbsp;{{ t('breakpointBar.requestBp') }}
      <span class="bp-state" :class="{ on: capture.bpStatus.break_on_request }">
        {{ capture.bpStatus.break_on_request ? t('breakpointBar.enabled') : t('breakpointBar.disabled') }}
      </span>
    </el-button>
    <el-button
      :class="{ 'bp-response-active': capture.bpStatus.break_on_response }"
      :type="capture.bpStatus.break_on_response ? 'primary' : 'default'"
      size="small"
      @click="capture.toggleBpResponse()"
    >
      <el-icon><VideoPause /></el-icon>&nbsp;{{ t('breakpointBar.responseBp') }}
      <span class="bp-state" :class="{ on: capture.bpStatus.break_on_response }">
        {{ capture.bpStatus.break_on_response ? t('breakpointBar.enabled') : t('breakpointBar.disabled') }}
      </span>
    </el-button>
    <!-- 有暂停流量时显示全部放行/丢弃 -->
    <template v-if="pendingCount > 0">
      <div class="bp-sep"></div>
      <span class="pending-count">{{ t('breakpointBar.pendingCount', { n: pendingCount }) }}</span>
      <el-button type="success" size="small" @click="capture.releaseAll()">
        <el-icon><Check /></el-icon>&nbsp;{{ t('breakpointBar.releaseAll') }}
      </el-button>
      <el-button type="danger" size="small" @click="capture.dropAll()">
        <el-icon><Close /></el-icon>&nbsp;{{ t('breakpointBar.dropAll') }}
      </el-button>
    </template>
  </div>
</template>

<style scoped>
.bp-bar { display: flex; align-items: center; gap: 8px; }
.bp-label { color: var(--on-text-muted); font-size: 12.5px; }
.bp-state { margin-left: 4px; font-size: 11px; opacity: 0.85; }
.bp-state.on { font-weight: 600; }
.bp-sep { width: 1px; height: 18px; background: var(--on-border); margin: 0 4px; }
.pending-count { font-size: 12px; color: var(--on-warn, #f0a020); font-weight: 600; }

/* 响应断点激活时使用深蓝色（覆盖 Element Plus primary 的默认蓝色） */
:deep(.bp-response-active) {
  background-color: #1e3a8a !important;
  border-color: #1e3a8a !important;
  color: #fff !important;
}
:deep(.bp-response-active:hover) {
  background-color: #1e40af !important;
  border-color: #1e40af !important;
}
</style>
