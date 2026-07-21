<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { ElMessage } from 'element-plus'
import type { Flow } from '../api/client'
import HeaderView from './HeaderView.vue'
import JsonView from './JsonView.vue'
import RawView from './RawView.vue'
import HexView from './HexView.vue'

// 请求检查器：多标签页，断点时可编辑
const props = defineProps<{
  flow: Flow
  editable: boolean
  enabledTabs: string[]
}>()
const emit = defineEmits<{
  modify: [partial: { request_headers?: string; request_body?: string }]
}>()

const headersStr = ref(props.flow.request_headers || '')
const bodyStr = ref(props.flow.request_body || '')
const activeTab = ref('headers')

watch(
  () => props.flow.id,
  () => {
    headersStr.value = props.flow.request_headers || ''
    bodyStr.value = props.flow.request_body || ''
    // TCP/UDP 流量没有 HTTP headers，默认选中 Hex
    if (props.flow.protocol === 'tcp' || props.flow.protocol === 'udp') {
      activeTab.value = 'hex'
    }
  },
  { immediate: true }
)

function onHeaders(v: string) {
  headersStr.value = v
  emit('modify', { request_headers: v })
}
function onBody(v: string) {
  bodyStr.value = v
  emit('modify', { request_body: v })
}

// 右键复制 URL（pane-url 上右键直接复制）
function onUrlContextMenu(e: MouseEvent) {
  e.preventDefault()
  const url = props.flow.url || ''
  if (url) {
    navigator.clipboard.writeText(url).catch(() => {})
    ElMessage.success('已复制 URL')
  }
}

function extractHeader(name: string): string {
  try {
    const obj = JSON.parse(headersStr.value)
    for (const k of Object.keys(obj)) {
      if (k.toLowerCase() === name.toLowerCase()) return String(obj[k])
    }
  } catch {
    /* ignore */
  }
  return ''
}

const cookiesText = computed(() => extractHeader('Cookie'))
const authText = computed(() => extractHeader('Authorization'))

// 解析 Cookie 为表格行
const cookieRows = computed(() => {
  const raw = cookiesText.value
  if (!raw) return []
  return raw.split(';').map((p) => {
    const idx = p.indexOf('=')
    return {
      key: idx > 0 ? p.slice(0, idx).trim() : p.trim(),
      value: idx > 0 ? p.slice(idx + 1).trim() : '',
    }
  })
})

// 标签页列表
const tabs = computed(() => {
  // TCP/UDP 流量没有 HTTP headers/json，只显示 Hex
  if (props.flow.protocol === 'tcp' || props.flow.protocol === 'udp') {
    return [{ name: 'hex', label: 'Hex' }]
  }
  const list = [
    { name: 'headers', label: 'Headers' },
    { name: 'json', label: 'JSON' },
    { name: 'raw', label: 'Raw' },
    { name: 'hex', label: 'Hex' },
  ]
  if (props.enabledTabs.includes('cookies')) list.push({ name: 'cookies', label: 'Cookies' })
  if (props.enabledTabs.includes('auth')) list.push({ name: 'auth', label: 'Auth' })
  if (props.enabledTabs.includes('xml')) list.push({ name: 'xml', label: 'XML' })
  return list
})

// 判断 body 是否 JSON
const isJson = computed(() => {
  const b = bodyStr.value || ''
  if (!b) return false
  try {
    JSON.parse(b)
    return true
  } catch {
    return false
  }
})

// TCP/UDP 流量没有 HTTP headers/json
const isTcpUdp = computed(() => props.flow.protocol === 'tcp' || props.flow.protocol === 'udp')
</script>

<template>
  <div class="inspector-pane full flex flex-col">
    <div class="pane-title">
      <span class="method-tag" :class="'m-' + flow.method.toLowerCase()">{{ flow.method }}</span>
      <span class="pane-url mono" title="右键复制 URL" @contextmenu="onUrlContextMenu">{{ flow.url }}</span>
    </div>
    <el-tabs v-model="activeTab" class="flex-1 insp-tabs">
      <template v-if="isTcpUdp">
        <!-- TCP/UDP 流量：显示 Hex + Raw 两个 tab -->
        <el-tab-pane label="Hex" name="hex" lazy>
          <HexView :data="bodyStr" />
        </el-tab-pane>
        <el-tab-pane label="Raw" name="raw" lazy>
          <RawView :flow="flow" type="request" :editable-body="editable ? bodyStr : undefined" @update:editable-body="onBody" />
        </el-tab-pane>
      </template>
      <template v-else>
        <el-tab-pane label="Headers" name="headers" lazy>
          <HeaderView :model-value="headersStr" :editable="editable" @update:model-value="onHeaders" />
        </el-tab-pane>
        <el-tab-pane label="JSON" name="json" lazy>
          <JsonView :model-value="isJson ? bodyStr : ''" :editable="editable" @update:model-value="onBody" />
        </el-tab-pane>
        <el-tab-pane label="Raw" name="raw" lazy>
          <RawView :flow="flow" type="request" :editable-body="editable ? bodyStr : undefined" @update:editable-body="onBody" />
        </el-tab-pane>
        <el-tab-pane label="Hex" name="hex" lazy>
          <HexView :data="bodyStr" />
        </el-tab-pane>
        <el-tab-pane v-if="enabledTabs.includes('cookies')" label="Cookies" name="cookies" lazy>
          <div class="cookies-view overflow-auto">
            <table class="kv-table mono" v-if="cookieRows.length">
              <thead><tr><th>名称</th><th>值</th></tr></thead>
              <tbody>
                <tr v-for="(c, i) in cookieRows" :key="i"><td>{{ c.key }}</td><td>{{ c.value }}</td></tr>
              </tbody>
            </table>
            <div v-else class="empty-text text-dim">（无 Cookie）</div>
          </div>
        </el-tab-pane>
        <el-tab-pane v-if="enabledTabs.includes('auth')" label="Auth" name="auth" lazy>
          <div class="auth-view mono overflow-auto">{{ authText || '（无 Authorization 头）' }}</div>
        </el-tab-pane>
        <el-tab-pane v-if="enabledTabs.includes('xml')" label="XML" name="xml" lazy>
          <JsonView :model-value="bodyStr" :editable="editable" lang="xml" @update:model-value="onBody" />
        </el-tab-pane>
      </template>
    </el-tabs>
  </div>
</template>

<style scoped>
.inspector-pane { background: var(--on-bg-elevated); }
.pane-title {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-bottom: 1px solid var(--on-border-light);
  font-size: 12.5px;
}
.method-tag {
  font-family: var(--on-font-mono); font-weight: 700; font-size: 11px;
  padding: 2px 6px; border-radius: 3px; color: var(--on-accent);
  background: var(--on-accent-glow); border: 1px solid var(--on-accent-dim);
}
.m-get { color: var(--on-ok); background: rgba(63,185,80,0.12); border-color: rgba(63,185,80,0.4); }
.m-post { color: var(--on-redirect); background: rgba(88,166,255,0.12); border-color: rgba(88,166,255,0.4); }
.m-put { color: var(--on-warn); background: rgba(210,153,34,0.12); border-color: rgba(210,153,34,0.4); }
.m-delete { color: var(--on-error); background: rgba(248,81,73,0.12); border-color: rgba(248,81,73,0.4); }
.pane-url { color: var(--on-text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.insp-tabs { padding: 0 10px; display: flex; flex-direction: column; height: 100%; }
.insp-tabs :deep(.el-tabs__content) { flex: 1; overflow: hidden; }
.insp-tabs :deep(.el-tab-pane) { height: 100%; }
.kv-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.kv-table th { text-align: left; padding: 6px 8px; color: var(--on-text-muted); border-bottom: 1px solid var(--on-border-light); }
.kv-table td { padding: 5px 8px; border-bottom: 1px solid var(--on-border-light); word-break: break-all; }
.empty-text { text-align: center; padding: 18px; }
.cookies-view, .auth-view { padding: 8px; }
</style>
