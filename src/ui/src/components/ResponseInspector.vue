<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Flow, TechFingerprint } from '../api/client'
import { api } from '../api/client'
import HeaderView from './HeaderView.vue'
import JsonView from './JsonView.vue'
import RawView from './RawView.vue'
import HexView from './HexView.vue'
import PreviewView from './PreviewView.vue'

// 响应检查器：多标签页，断点时可编辑
const { t } = useI18n()
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

// 监听 flow 字段变化（不仅 id）：SSE 响应补齐 / select() 异步拉取完整数据后，
// props.flow.response_body / response_headers 会被 Object.assign 原地更新，
// 需同步刷新本地 ref，否则 Preview 会一直显示旧的空 body
watch(
  () => [props.flow.id, props.flow.response_body, props.flow.response_headers],
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
    { name: 'tech', label: 'Tech' },
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
  () => [props.flow.id, props.flow.protocol],
  () => {
    const proto = props.flow.protocol
    // WS/TCP/UDP 流量默认选中 Hex
    if (proto === 'ws' || proto === 'tcp' || proto === 'udp') {
      activeTab.value = 'hex'
      return
    }
    // HTTP 流量
    if (props.autoSwitchPreview !== false) {
      // 自动切换开启：回到 preview（如用户所述"开启就自动换 preview"）
      activeTab.value = 'preview'
    } else {
      // 自动切换关闭：保持当前 tab
      // 当前 tabs 计算属性始终包含 preview/headers/json/raw/hex/tech，
      // 所以这些 tab 切换流量后都保持，无需清空
      // 仅 cache/xml 等条件性 tab 可能因新流量不可用而需要清空
      const availableTabs = ['preview', 'headers', 'json', 'raw', 'hex', 'tech']
      if (props.enabledTabs.includes('cache')) availableTabs.push('cache')
      if (props.enabledTabs.includes('xml')) availableTabs.push('xml')
      if (!availableTabs.includes(activeTab.value)) {
        // 仅当当前 tab 在新流量中不可用时才清空
        // tech/preview 等基础 tab 始终可用，不会被清空
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

// TCP/UDP 流量没有 HTTP headers/json/preview
const isTcpUdp = computed(() => props.flow.protocol === 'tcp' || props.flow.protocol === 'udp'
  || props.flow.protocol === 'ws')

// ---------- 技术栈识别 ----------
const techItems = ref<TechFingerprint[]>([])
const techLoading = ref(false)
const techLoaded = ref<number | null>(null)  // 已加载的 flow_id，避免重复请求

watch(
  () => props.flow.id,
  () => {
    techItems.value = []
    techLoaded.value = null
    // 若当前正停留在 Tech 标签页，切换 flow 后自动加载新技术栈
    if (activeTab.value === 'tech') loadTech()
  }
)

async function loadTech() {
  // 切换流量后已加载的会被 watch 重置；同一 flow 仅请求一次
  if (techLoaded.value === props.flow.id || techLoading.value) return
  // 捕获当前 flow id，await 后校验一致性，避免切换 flow 期间旧请求覆盖新数据
  const targetId = props.flow.id
  techLoading.value = true
  try {
    const res: any = await api.getTechFingerprint(targetId)
    // 已切换到其他 flow，丢弃过期结果
    if (props.flow.id !== targetId) return
    techItems.value = (res.items || []) as TechFingerprint[]
    techLoaded.value = targetId
  } catch (e: any) {
    if (props.flow.id !== targetId) return
    techItems.value = []
  } finally {
    techLoading.value = false
  }
}

watch(activeTab, (v) => {
  if (v === 'tech') loadTech()
})

// 按类别分组识别结果
const techGroups = computed(() => {
  const groups: Record<string, TechFingerprint[]> = {}
  for (const t of techItems.value) {
    if (!groups[t.category]) groups[t.category] = []
    groups[t.category].push(t)
  }
  return groups
})

const TECH_CATEGORY_LABEL = computed<Record<string, string>>(() => ({
  server: t('inspector.techServer'),
  language: t('inspector.techLanguage'),
  framework: t('inspector.techFramework'),
  frontend: t('inspector.techFrontend'),
  cms: t('inspector.techCms'),
  cdn_waf: t('inspector.techCdnWaf'),
  analytics: t('inspector.techAnalytics'),
  build_tool: t('inspector.techBuildTool'),
}))

function confidenceColor(c: string): 'success' | 'warning' | 'info' {
  if (c === 'high') return 'success'
  if (c === 'medium') return 'warning'
  return 'info'
}
function confidenceLabel(c: string): string {
  if (c === 'high') return t('inspector.confidenceHigh')
  if (c === 'medium') return t('inspector.confidenceMedium')
  return t('inspector.confidenceLow')
}
</script>

<template>
  <div class="inspector-pane full flex flex-col">
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
        <el-tab-pane label="Tech" name="tech" lazy>
          <div class="tech-view">
            <div v-if="techLoading" class="tech-loading text-dim">
              <el-icon class="is-loading"><Loading /></el-icon>&nbsp;{{ t('inspector.techLoading') }}
            </div>
            <div v-else-if="!techItems.length" class="tech-empty text-dim">
              <el-icon :size="28"><Cpu /></el-icon>
              <div style="margin-top: 6px">{{ t('inspector.techEmpty') }}</div>
              <div class="text-muted" style="font-size: 11px; margin-top: 4px">
                {{ t('inspector.techEmptyHint') }}
              </div>
            </div>
            <div v-else class="tech-groups">
              <div v-for="(items, cat) in techGroups" :key="cat" class="tech-group">
                <div class="tech-cat">{{ TECH_CATEGORY_LABEL[cat] || cat }}</div>
                <div class="tech-chips">
                  <span v-for="(t, i) in items" :key="i" class="tech-chip">
                    <span class="tech-name">{{ t.name }}</span>
                    <span v-if="t.version" class="tech-ver">{{ t.version }}</span>
                    <el-tag size="small" :type="confidenceColor(t.confidence)" effect="plain" class="tech-conf">
                      {{ confidenceLabel(t.confidence) }}
                    </el-tag>
                  </span>
                </div>
              </div>
            </div>
          </div>
        </el-tab-pane>
        <el-tab-pane v-if="enabledTabs.includes('cache')" label="Cache" name="cache" lazy>
          <div class="cache-view overflow-auto">
            <table class="kv-table mono" v-if="cacheRows.length">
              <thead><tr><th>{{ t('common.name') }}</th><th>{{ t('common.value') }}</th></tr></thead>
              <tbody>
                <tr v-for="(c, i) in cacheRows" :key="i"><td>{{ c.key }}</td><td>{{ c.value }}</td></tr>
              </tbody>
            </table>
            <div v-else class="empty-text text-dim">{{ t('inspector.noCacheHeaders') }}</div>
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
.pane-meta { color: var(--on-text-muted); white-space: nowrap; }
.insp-tabs { padding: 0 10px; display: flex; flex-direction: column; height: 100%; }
/* el-tabs__content 不滚动，让各 tab 内部组件自己处理滚动，确保 TextSearch 等组件的悬浮窗定位正确 */
.insp-tabs :deep(.el-tabs__content) { flex: 1; overflow: hidden; }
.insp-tabs :deep(.el-tab-pane) { height: 100%; }
.kv-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.kv-table th { text-align: left; padding: 6px 8px; color: var(--on-text-muted); border-bottom: 1px solid var(--on-border-light); }
.kv-table td { padding: 5px 8px; border-bottom: 1px solid var(--on-border-light); word-break: break-all; }
.empty-text { text-align: center; padding: 18px; }
.cache-view { padding: 8px; }
/* 技术栈识别视图 */
.tech-view { padding: 12px; height: 100%; overflow: auto; }
.tech-loading { padding: 18px; text-align: center; }
.tech-empty { padding: 32px 12px; text-align: center; }
.tech-empty .text-muted { color: var(--on-text-muted); }
.tech-groups { display: flex; flex-direction: column; gap: 14px; }
.tech-group { display: flex; flex-direction: column; gap: 6px; }
.tech-cat {
  font-size: 12px; color: var(--on-text-muted);
  font-weight: 600; padding-bottom: 4px;
  border-bottom: 1px solid var(--on-border-light);
}
.tech-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.tech-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 8px; border-radius: 4px;
  background: var(--on-bg); border: 1px solid var(--on-border-light);
  font-size: 12px;
}
.tech-name { font-weight: 600; }
.tech-ver {
  font-size: 11px; color: var(--on-text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.tech-conf { transform: scale(0.9); transform-origin: center; }
</style>
