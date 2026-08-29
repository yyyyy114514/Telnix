<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import type { Flow } from '../api/client'
import HeaderView from './HeaderView.vue'
import JsonView from './JsonView.vue'
import RawView from './RawView.vue'
import HexView from './HexView.vue'
import CertInfoView from './CertInfoView.vue'

// 请求检查器：多标签页，断点时可编辑
const { t } = useI18n()
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

// 响应时间组成（timing 分解）
const timingData = computed(() => {
  if (!props.flow.timing) return null
  try {
    return JSON.parse(props.flow.timing)
  } catch {
    return null
  }
})

watch(
  () => [props.flow.id, props.flow.request_headers, props.flow.request_body],
  () => {
    headersStr.value = props.flow.request_headers || ''
    bodyStr.value = props.flow.request_body || ''
    const proto = props.flow.protocol
    // WS/TCP/UDP 流量默认选中 Hex
    if (proto === 'ws' || proto === 'tcp' || proto === 'udp') {
      activeTab.value = 'hex'
      return
    }
    // HTTP 流量：保持当前 tab，如果当前 tab 不在可用列表中则回到 headers
    const availableTabs = ['headers', 'json', 'raw', 'hex']
    if (props.enabledTabs.includes('cookies')) availableTabs.push('cookies')
    if (props.enabledTabs.includes('auth')) availableTabs.push('auth')
    if (props.enabledTabs.includes('xml')) availableTabs.push('xml')
    if (props.enabledTabs.includes('timeline')) availableTabs.push('timeline')
    if (props.flow.cert_info) availableTabs.push('cert')
    if (!availableTabs.includes(activeTab.value)) {
      activeTab.value = 'headers'
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
    ElMessage.success(t('inspector.copiedUrl'))
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
  // TCP/UDP/WS 流量没有 HTTP headers/json，只显示 Hex
  if (props.flow.protocol === 'tcp' || props.flow.protocol === 'udp'
      || props.flow.protocol === 'ws') {
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
  // 时序图 tab：通过设置勾选启用，默认不显示
  if (props.enabledTabs.includes('timeline')) list.push({ name: 'timeline', label: t('inspector.tcpTimeline') })
  // 证书 tab：flow.cert_info 非空时自动显示
  if (props.flow.cert_info) {
    list.push({ name: 'cert', label: t('inspector.certTab') })
  }
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

// TCP/UDP/WS 流量没有 HTTP headers/json/preview
const isTcpUdp = computed(() => props.flow.protocol === 'tcp' || props.flow.protocol === 'udp'
  || props.flow.protocol === 'ws')

const statusClass = computed(() => {
  const c = props.flow.status_code
  if (c === null) return 'status-null'
  if (c < 300) return 'status-2xx'
  if (c < 400) return 'status-3xx'
  if (c < 500) return 'status-4xx'
  return 'status-5xx'
})

function copyCookie(text: string) {
  navigator.clipboard.writeText(text).then(() => {
    ElMessage.success(t('common.copied'))
  }).catch(() => {})
}
</script>

<template>
  <div class="inspector-pane full flex flex-col">
    <div class="pane-title">
      <span class="method-tag" :class="'m-' + flow.method.toLowerCase()">{{ flow.method }}</span>
      <span v-if="flow.status_code" class="status-tag" :class="statusClass">{{ flow.status_code }}</span>
      <span class="pane-url mono" :title="t('inspector.copyUrlTitle')" @contextmenu="onUrlContextMenu">{{ flow.url }}</span>
      <span class="pane-meta mono">
        {{ flow.size ? (flow.size + ' B') : '' }}
        <span v-if="flow.duration_ms !== null"> · {{ flow.duration_ms }} ms</span>
      </span>
      <!-- 响应时间组成图：forward（connect+ssl+server）vs transfer（total-forward） -->
      <div v-if="timingData && flow.duration_ms" class="timing-bar" :title="t('inspector.timingTitle')">
        <div class="timing-seg timing-forward"
             :style="{ width: Math.min(100, ((timingData.forward_ms || 0) / (flow.duration_ms || 1)) * 100) + '%' }"
             :title="`Connect+SSL+Server: ${timingData.forward_ms || 0}ms`"></div>
        <div class="timing-seg timing-transfer"
             :style="{ width: Math.min(100, Math.max(0, ((flow.duration_ms - (timingData.forward_ms || 0)) / (flow.duration_ms || 1)) * 100)) + '%' }"
             :title="`Transfer: ${Math.max(0, (flow.duration_ms || 0) - (timingData.forward_ms || 0))}ms`"></div>
        <span class="timing-label">{{ timingData.forward_ms || 0 }} + {{ Math.max(0, (flow.duration_ms || 0) - (timingData.forward_ms || 0)) }}ms</span>
      </div>
    </div>
    <el-tabs v-model="activeTab" class="flex-1 insp-tabs">
      <template v-if="isTcpUdp">
        <!-- TCP/UDP/WS 流量：仅显示 Hex tab（用户偏好） -->
        <el-tab-pane label="Hex" name="hex" lazy>
          <HexView :data="bodyStr" />
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
              <thead><tr><th>{{ t('common.name') }}</th><th>{{ t('common.value') }}</th></tr></thead>
              <tbody>
                <tr v-for="(c, i) in cookieRows" :key="i"><td class="no-select kv-cell" @click="copyCookie(c.key)" title="点击复制名称">{{ c.key }}</td><td class="no-select kv-cell" @click="copyCookie(c.value)" title="点击复制值">{{ c.value }}</td></tr>
              </tbody>
            </table>
            <div v-else class="empty-text text-dim">{{ t('inspector.noCookie') }}</div>
          </div>
        </el-tab-pane>
        <el-tab-pane v-if="enabledTabs.includes('auth')" label="Auth" name="auth" lazy>
          <div class="auth-view mono overflow-auto">{{ authText || t('inspector.noAuth') }}</div>
        </el-tab-pane>
        <el-tab-pane v-if="enabledTabs.includes('xml')" label="XML" name="xml" lazy>
          <JsonView :model-value="bodyStr" :editable="editable" lang="xml" @update:model-value="onBody" />
        </el-tab-pane>
        <el-tab-pane v-if="flow.cert_info" :label="t('inspector.certTab')" name="cert" lazy>
          <CertInfoView :cert-info="flow.cert_info" />
        </el-tab-pane>
        <el-tab-pane v-if="enabledTabs.includes('timeline')" :label="t('inspector.tcpTimeline')" name="timeline" lazy>
          <div class="tcp-timeline-view">
            <svg class="tcp-timeline-svg" viewBox="0 0 800 320" preserveAspectRatio="xMidYMid meet">
              <!-- 箭头 marker 定义 -->
              <defs>
                <marker id="arrowR" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
                  <path d="M0,0 L10,4 L0,8 Z" fill="var(--on-accent)" />
                </marker>
                <marker id="arrowL" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto-start-reverse">
                  <path d="M0,0 L10,4 L0,8 Z" fill="var(--on-success)" />
                </marker>
              </defs>
              <!-- 客户端轴（左侧） -->
              <line x1="120" y1="50" x2="120" y2="270" stroke="var(--on-border)" stroke-width="2" />
              <text x="120" y="35" text-anchor="middle" fill="var(--on-text)" font-size="13" font-weight="600">Client</text>
              <!-- 服务端轴（右侧） -->
              <line x1="680" y1="50" x2="680" y2="270" stroke="var(--on-border)" stroke-width="2" />
              <text x="680" y="35" text-anchor="middle" fill="var(--on-text)" font-size="13" font-weight="600">Server</text>
              <!-- 时间刻度标签 -->
              <text x="400" y="300" text-anchor="middle" fill="var(--on-text-dim)" font-size="11">{{ t('inspector.timelineAxis') }}</text>
              <!-- 请求箭头（Client → Server） -->
              <g v-if="flow.method">
                <line x1="120" y1="90" x2="670" y2="90" stroke="var(--on-accent)" stroke-width="2" marker-end="url(#arrowR)" />
                <text x="395" y="83" text-anchor="middle" fill="var(--on-text-dim)" font-size="11" font-family="var(--on-font-mono, monospace)">{{ flow.method }} {{ flow.host }}{{ flow.path }}</text>
                <text x="395" y="103" text-anchor="middle" fill="var(--on-text-dim)" font-size="10">0ms</text>
              </g>
              <!-- 响应箭头（Server → Client） -->
              <g v-if="flow.status_code">
                <line x1="680" y1="180" x2="130" y2="180" stroke="var(--on-success)" stroke-width="2" marker-end="url(#arrowL)" />
                <text x="405" y="173" text-anchor="middle" fill="var(--on-text-dim)" font-size="11" font-family="var(--on-font-mono, monospace)">{{ flow.status_code }} ({{ flow.size || 0 }} B)</text>
                <text x="405" y="193" text-anchor="middle" fill="var(--on-text-dim)" font-size="10">{{ timingData?.forward_ms || flow.duration_ms || 0 }}ms</text>
              </g>
              <!-- 总耗时标注（左侧虚线） -->
              <g v-if="flow.duration_ms">
                <line x1="80" y1="90" x2="80" y2="180" stroke="var(--on-text-dim)" stroke-width="1" stroke-dasharray="3 3" />
                <text x="70" y="140" text-anchor="middle" fill="var(--on-text-dim)" font-size="11" font-family="var(--on-font-mono, monospace)" transform="rotate(-90, 70, 140)">{{ flow.duration_ms }}ms</text>
              </g>
            </svg>
            <div class="tcp-timeline-info mono">
              <div v-if="flow.src_port">{{ t('inspector.srcPort') }}: {{ flow.src_port }} → {{ t('inspector.dstPort') }}: {{ flow.dst_port }}</div>
              <div v-if="flow.remote_ip">{{ t('inspector.remoteIp') }}: {{ flow.remote_ip }}</div>
              <div v-if="timingData">Connect+SSL+Server: {{ timingData.forward_ms || 0 }}ms / Total: {{ flow.duration_ms || 0 }}ms</div>
            </div>
          </div>
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
  white-space: nowrap; flex-shrink: 0;
}
.m-get { color: var(--on-ok); background: rgba(63,185,80,0.12); border-color: rgba(63,185,80,0.4); }
.m-post { color: var(--on-redirect); background: rgba(88,166,255,0.12); border-color: rgba(88,166,255,0.4); }
.m-put { color: var(--on-warn); background: rgba(210,153,34,0.12); border-color: rgba(210,153,34,0.4); }
.m-delete { color: var(--on-error); background: rgba(248,81,73,0.12); border-color: rgba(248,81,73,0.4); }
.status-tag {
  font-family: var(--on-font-mono); font-weight: 700; font-size: 11px;
  padding: 2px 6px; border-radius: 3px; border: 1px solid currentColor;
  white-space: nowrap; flex-shrink: 0;
}
.status-2xx { color: var(--on-ok); }
.status-3xx { color: var(--on-redirect); }
.status-4xx { color: var(--on-warn); }
.status-5xx { color: var(--on-error); }
.status-null { color: var(--on-text-dim); }
.pane-url {
  color: var(--on-text-muted); white-space: nowrap;
  overflow-x: auto; overflow-y: hidden;
  flex: 1 1 auto; min-width: 0;
  scrollbar-width: none;
}
.pane-url::-webkit-scrollbar { height: 0; }
.pane-url:hover::-webkit-scrollbar { height: 4px; }
.pane-url:hover::-webkit-scrollbar-thumb { background: var(--on-border-light, rgba(128,128,128,.3)); border-radius: 2px; }
.pane-url:hover::-webkit-scrollbar-track { background: transparent; }
.pane-meta { color: var(--on-text-muted); white-space: nowrap; flex-shrink: 0; }

/* 响应时间组成条 */
.timing-bar {
  display: inline-flex; align-items: center;
  width: 120px; height: 10px;
  border-radius: 3px; overflow: hidden;
  flex-shrink: 0; position: relative;
  border: 1px solid var(--on-border-light);
}
.timing-seg { height: 100%; }
.timing-forward { background: var(--on-accent); }
.timing-transfer { background: var(--on-amber, #f59e0b); }
.timing-label {
  position: absolute; left: 100%; top: 50%; transform: translateY(-50%);
  margin-left: 6px; font-size: 10px; color: var(--on-text-dim);
  white-space: nowrap; font-family: var(--on-font-mono, monospace);
}

/* TCP 时序图 */
.tcp-timeline-view { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.tcp-timeline-svg { width: 100%; height: 320px; background: var(--on-bg); border: 1px solid var(--on-border-light); border-radius: 6px; }
.tcp-timeline-info { font-size: 12px; color: var(--on-text-dim); padding: 0 8px; }
.insp-tabs { padding: 0 10px; display: flex; flex-direction: column; height: 100%; }
.insp-tabs :deep(.el-tabs__content) { flex: 1; overflow: hidden; }
.insp-tabs :deep(.el-tab-pane) { height: 100%; }
.kv-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.kv-table th { text-align: left; padding: 6px 8px; color: var(--on-text-muted); border-bottom: 1px solid var(--on-border-light); }
.kv-table td { padding: 5px 8px; border-bottom: 1px solid var(--on-border-light); word-break: break-all; }
.kv-cell { cursor: pointer; transition: background 0.15s; }
.kv-cell:hover { background: var(--on-border-light); }
.empty-text { text-align: center; padding: 18px; }
.cookies-view, .auth-view { padding: 8px; }
</style>
