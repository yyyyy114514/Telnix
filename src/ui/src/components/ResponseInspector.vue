<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { ElMessage } from 'element-plus'
import type { Flow } from '../api/client'
import HeaderView from './HeaderView.vue'
import JsonView from './JsonView.vue'
import RawView from './RawView.vue'
import HexView from './HexView.vue'
import PreviewView from './PreviewView.vue'

// 响应检查器：多标签页，断点时可编辑
const props = defineProps<{
  flow: Flow
  editable: boolean
  enabledTabs: string[]
  // 切换流量时是否自动回到 preview 标签（默认 true）
  autoSwitchPreview?: boolean
}>()
const emit = defineEmits<{
  modify: [partial: { response_headers?: string; response_body?: string }]
}>()

const headersStr = ref(props.flow.response_headers || '')
const bodyStr = ref(props.flow.response_body || '')
const activeTab = ref('preview')

watch(
  () => props.flow.id,
  () => {
    headersStr.value = props.flow.response_headers || ''
    bodyStr.value = props.flow.response_body || ''
  }
)

function onHeaders(v: string) {
  headersStr.value = v
  emit('modify', { response_headers: v })
}
function onBody(v: string) {
  bodyStr.value = v
  emit('modify', { response_body: v })
}

// 右键复制 URL（pane-title 上右键直接复制）
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

// 缓存相关头
const cacheRows = computed(() => {
  const keys = ['cache-control', 'expires', 'etag', 'last-modified', 'age', 'pragma']
  const rows: { key: string; value: string }[] = []
  try {
    const obj = JSON.parse(headersStr.value)
    for (const k of Object.keys(obj)) {
      if (keys.includes(k.toLowerCase())) {
        rows.push({ key: k, value: String(obj[k]) })
      }
    }
  } catch {
    /* ignore */
  }
  return rows
})

const tabs = computed(() => {
  // TCP/UDP/WS 流量没有 HTTP headers/json/preview，只显示 Hex
  if (props.flow.protocol === 'tcp' || props.flow.protocol === 'udp'
      || props.flow.protocol === 'ws') {
    return [{ name: 'hex', label: 'Hex' }]
  }
  const list = [
    { name: 'preview', label: 'Preview' },
    { name: 'headers', label: 'Headers' },
    { name: 'json', label: 'JSON' },
    { name: 'raw', label: 'Raw' },
    { name: 'hex', label: 'Hex' },
  ]
  if (props.enabledTabs.includes('cache')) list.push({ name: 'cache', label: 'Cache' })
  if (props.enabledTabs.includes('xml')) list.push({ name: 'xml', label: 'XML' })
  return list
})

// 选项卡自动切换逻辑：
// - WS 流量：默认不选任何 tab（需求4）
// - autoSwitch 开启时：HTTP→Preview，TCP/UDP→Hex
// - autoSwitch 关闭时：保持当前 tab，如果当前 tab 在新流量中不存在则不选
watch(
  () => props.flow.id,
  () => {
    const proto = props.flow.protocol
    // WS/TCP/UDP 流量默认选中 Hex
    if (proto === 'ws' || proto === 'tcp' || proto === 'udp') {
      activeTab.value = 'hex'
      return
    }
    // HTTP 流量
    if (props.autoSwitchPreview !== false) {
      activeTab.value = 'preview'
    } else {
      // 保持当前 tab，如果当前 tab 不在可用列表中则不选
      const availableTabs = ['preview', 'headers', 'json', 'raw', 'hex']
      if (props.enabledTabs.includes('cache')) availableTabs.push('cache')
      if (props.enabledTabs.includes('xml')) availableTabs.push('xml')
      if (!availableTabs.includes(activeTab.value)) {
        activeTab.value = ''
      }
    }
  }
)

// 提取响应的 Content-Type，供 Preview 使用
const contentType = computed(() => extractHeader('content-type'))

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

const statusClass = computed(() => {
  const c = props.flow.status_code
  if (c === null) return 'status-null'
  if (c < 300) return 'status-2xx'
  if (c < 400) return 'status-3xx'
  if (c < 500) return 'status-4xx'
  return 'status-5xx'
})

// TCP/UDP 流量没有 HTTP headers/json/preview
const isTcpUdp = computed(() => props.flow.protocol === 'tcp' || props.flow.protocol === 'udp'
  || props.flow.protocol === 'ws')
</script>

<template>
  <div class="inspector-pane full flex flex-col">
    <div class="pane-title" @contextmenu="onUrlContextMenu">
      <span class="status-tag" :class="statusClass">{{ flow.status_code ?? '...' }}</span>
      <span class="pane-url mono" :title="flow.url + '（右键复制）'">{{ flow.url }}</span>
      <span class="pane-meta mono">
        {{ flow.size ? (flow.size + ' B') : '' }}
        <span v-if="flow.duration_ms !== null"> · {{ flow.duration_ms }} ms</span>
      </span>
    </div>
    <el-tabs v-model="activeTab" class="flex-1 insp-tabs">
      <template v-if="isTcpUdp">
        <!-- TCP/UDP 流量：显示 Hex + Raw 两个 tab -->
        <el-tab-pane label="Hex" name="hex" lazy>
          <HexView :data="bodyStr" />
        </el-tab-pane>
        <el-tab-pane label="Raw" name="raw" lazy>
          <RawView :flow="flow" type="response" :editable-body="editable ? bodyStr : undefined" @update:editable-body="onBody" />
        </el-tab-pane>
      </template>
      <template v-else>
        <el-tab-pane label="Preview" name="preview" lazy>
          <PreviewView
            :body="bodyStr"
            :content-type="contentType"
            :editable="editable"
            @update:body="onBody"
          />
        </el-tab-pane>
        <el-tab-pane label="Headers" name="headers" lazy>
          <HeaderView :model-value="headersStr" :editable="editable" @update:model-value="onHeaders" />
        </el-tab-pane>
        <el-tab-pane label="JSON" name="json" lazy>
          <JsonView :model-value="isJson ? bodyStr : ''" :editable="editable" @update:model-value="onBody" />
        </el-tab-pane>
        <el-tab-pane label="Raw" name="raw" lazy>
          <RawView :flow="flow" type="response" :editable-body="editable ? bodyStr : undefined" @update:editable-body="onBody" />
        </el-tab-pane>
        <el-tab-pane label="Hex" name="hex" lazy>
          <HexView :data="bodyStr" />
        </el-tab-pane>
        <el-tab-pane v-if="enabledTabs.includes('cache')" label="Cache" name="cache" lazy>
          <div class="cache-view overflow-auto">
            <table class="kv-table mono" v-if="cacheRows.length">
              <thead><tr><th>名称</th><th>值</th></tr></thead>
              <tbody>
                <tr v-for="(c, i) in cacheRows" :key="i"><td>{{ c.key }}</td><td>{{ c.value }}</td></tr>
              </tbody>
            </table>
            <div v-else class="empty-text text-dim">（无缓存相关头）</div>
          </div>
        </el-tab-pane>
        <el-tab-pane v-if="enabledTabs.includes('xml')" label="XML" name="xml" lazy>
          <JsonView :model-value="bodyStr" :editable="editable" lang="xml" @update:model-value="onBody" />
        </el-tab-pane>
      </template>
    </el-tabs>
  </div>
</template>

<style scoped>
.inspector-pane { background: var(--on-bg-elevated); height: 100%; }
.pane-title {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 10px; border-bottom: 1px solid var(--on-border-light);
  font-size: 12.5px;
}
.status-tag {
  font-family: var(--on-font-mono); font-weight: 700; font-size: 12px;
  padding: 2px 8px; border-radius: 3px; border: 1px solid currentColor;
}
.pane-meta { color: var(--on-text-muted); margin-left: auto; white-space: nowrap; }
.pane-url { color: var(--on-text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1 1 auto; min-width: 0; }
.insp-tabs { padding: 0 10px; display: flex; flex-direction: column; height: 100%; }
/* el-tabs__content 不滚动，让各 tab 内部组件自己处理滚动，确保 TextSearch 等组件的悬浮窗定位正确 */
.insp-tabs :deep(.el-tabs__content) { flex: 1; overflow: hidden; }
.insp-tabs :deep(.el-tab-pane) { height: 100%; }
.kv-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.kv-table th { text-align: left; padding: 6px 8px; color: var(--on-text-muted); border-bottom: 1px solid var(--on-border-light); }
.kv-table td { padding: 5px 8px; border-bottom: 1px solid var(--on-border-light); word-break: break-all; }
.empty-text { text-align: center; padding: 18px; }
.cache-view { padding: 8px; }
</style>
