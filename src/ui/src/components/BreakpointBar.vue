<script setup lang="ts">
import { computed } from 'vue'
import { useCaptureStore } from '../stores/capture'

// 断点控制条：请求断点 / 响应断点 两个开关 + 全部放行/丢弃
const capture = useCaptureStore()

const pendingCount = computed(() => capture.bpStatus.pending_flows?.length || 0)
</script>

<template>
  <div class="bp-bar">
    <span class="bp-label">断点：</span>
    <el-button
      :type="capture.bpStatus.break_on_request ? 'warning' : 'default'"
      size="small"
      @click="capture.toggleBpRequest()"
    >
      <el-icon><VideoPause /></el-icon>&nbsp;请求断点
      <span class="bp-state" :class="{ on: capture.bpStatus.break_on_request }">
        {{ capture.bpStatus.break_on_request ? '已开启' : '关闭' }}
      </span>
    </el-button>
    <el-button
      :type="capture.bpStatus.break_on_response ? 'primary' : 'default'"
      size="small"
      @click="capture.toggleBpResponse()"
    >
      <el-icon><VideoPause /></el-icon>&nbsp;响应断点
      <span class="bp-state" :class="{ on: capture.bpStatus.break_on_response }">
        {{ capture.bpStatus.break_on_response ? '已开启' : '关闭' }}
      </span>
    </el-button>
    <!-- 有暂停流量时显示全部放行/丢弃 -->
    <template v-if="pendingCount > 0">
      <div class="bp-sep"></div>
      <span class="pending-count">{{ pendingCount }} 个待处理</span>
      <el-button type="success" size="small" @click="capture.releaseAll()">
        <el-icon><Check /></el-icon>&nbsp;全部放行
      </el-button>
      <el-button type="danger" size="small" @click="capture.dropAll()">
        <el-icon><Close /></el-icon>&nbsp;全部丢弃
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
</style>
