<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, type Flow } from '../api/client'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import PreviewView from '../components/PreviewView.vue'
import TextSearch from '../components/TextSearch.vue'
import ImportButton from '../components/ImportButton.vue'

// 跨流量搜索页：
// - 搜索结果单击右侧显示响应 Preview 或 请求 Raw（带高亮和搜索），双击跳转抓包页面
// - 右侧详情右上角有关闭按钮
const router = useRouter()
const capture = useCaptureStore()
const flowsStore = useFlowsStore()

const bodyRegex = ref('')
const binaryHex = ref('')
const searchAllSessions = ref(true) // 默认跨会话
const searchResults = ref<Flow[]>([])
const searching = ref(false)

// 详情相关
type DetailMode = 'response' | 'request'
const detailMode = ref<DetailMode>('response')
const selectedFlowId = ref<number | null>(null)
const selectedFlow = computed(() => searchResults.value.find(f => f.id === selectedFlowId.value) || null)

// 搜索请求令牌：丢弃过期结果，避免快速多次搜索时旧请求覆盖新结果
let searchToken = 0

async function onSearch() {
  if (!bodyRegex.value.trim() && !binaryHex.value.trim()) {
    ElMessage.warning('请输入正则或二进制 hex')
    return
  }
  // 跨会话模式用 0，否则用当前会话 id
  const sid = searchAllSessions.value ? 0 : (capture.status.session_id || 0)
  if (!searchAllSessions.value && !sid) {
    ElMessage.warning('当前无活动会话，可勾选「跨所有会话」')
    return
  }
  const myToken = ++searchToken
  searching.value = true
  selectedFlowId.value = null
  try {
    const r = await api.searchFlows({
      session_id: sid,
      body_regex: bodyRegex.value.trim() || undefined,
      binary_hex: binaryHex.value.trim() || undefined,
      limit: 500,
    })
    // 已发起新搜索，丢弃过期结果
    if (myToken !== searchToken) return
    searchResults.value = r.matches || []
    ElMessage.success(`找到 ${r.count} 条匹配`)
  } catch (e: any) {
    if (myToken !== searchToken) return
    ElMessage.error('搜索失败：' + (e?.message || e))
  } finally {
    if (myToken === searchToken) {
      searching.value = false
    }
  }
}

function formatTime(ts: string): string {
  if (!ts) return ''
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
}

// 单击：选中显示详情
function onResultClick(f: Flow) {
  selectedFlowId.value = f.id
}

// 双击：跳转抓包页面并选中（用 selectFlow 注入 flow 对象，不依赖 store 列表查找）
function onResultDblClick(f: Flow) {
  flowsStore.selectFlow(f)
  flowsStore.aiFlowIds = []
  router.push('/capture')
}

// 关闭详情面板
function closeDetail() {
  selectedFlowId.value = null
}

// 从 response_headers 提取 Content-Type
function extractContentType(f: Flow | null): string {
  if (!f) return ''
  try {
    const obj = JSON.parse(f.response_headers || '{}')
    for (const k of Object.keys(obj)) {
      if (k.toLowerCase() === 'content-type') return String(obj[k])
    }
  } catch { /* ignore */ }
  return ''
}

// Content-Type 简短显示（如 application/json → json，text/html → html）
function contentTypeShort(f: Flow): string {
  const ct = extractContentType(f)
  if (!ct) return '-'
  // 取分号前的主类型，再取 / 后的子类型
  const main = ct.split(';')[0].trim()
  const slashIdx = main.indexOf('/')
  return slashIdx >= 0 ? main.substring(slashIdx + 1) : main
}

// 请求 Raw 文本
const requestRaw = computed(() => {
  const f = selectedFlow.value
  if (!f) return ''
  const headers = f.request_headers || '{}'
  let headerStr = ''
  try {
    const obj = JSON.parse(headers)
    headerStr = Object.entries(obj).map(([k, v]) => `${k}: ${v}`).join('\n')
  } catch {
    headerStr = headers
  }
  const line = `${f.method} ${f.url} HTTP/1.1`
  const body = f.request_body || ''
  return body ? `${line}\n${headerStr}\n\n${body}` : `${line}\n${headerStr}`
})

const responseContentType = computed(() => extractContentType(selectedFlow.value))

onMounted(() => {
  // 进入页面即可输入搜索
})
</script>

<template>
  <div class="search-view-page full flex flex-col">
    <div class="search-form">
      <el-input
        v-model="bodyRegex"
        placeholder="正则表达式（搜索 url/path/request_body/response_body）"
        size="small"
        clearable
        style="flex: 1; min-width: 260px"
        v-code-assist
        @keyup.enter="onSearch"
      />
      <el-input
        v-model="binaryHex"
        placeholder="二进制 hex（如 ef bb bf）"
        size="small"
        style="width: 220px"
        clearable
        @keyup.enter="onSearch"
      />
      <el-checkbox v-model="searchAllSessions" size="small">跨所有会话</el-checkbox>
      <el-button type="primary" size="small" :loading="searching" @click="onSearch">
        <el-icon><Search /></el-icon>&nbsp;搜索
      </el-button>
      <div class="flex-1"></div>
      <ImportButton />
    </div>
    <!-- 主体：左侧结果列表 + 右侧详情 -->
    <div class="search-body flex overflow-hidden">
      <div class="result-list">
        <div v-if="!searchResults.length" class="empty-text text-dim">
          输入正则或 hex 后点击搜索，结果将显示在此<br />
          <span style="font-size: 11px">单击查看详情，双击跳转抓包页面</span>
        </div>
        <div
          v-for="f in searchResults"
          :key="f.id"
          class="sr-row mono"
          :class="{ selected: f.id === selectedFlowId }"
          @click="onResultClick(f)"
          @dblclick="onResultDblClick(f)"
        >
          <span class="sr-id text-dim">#{{ f.id }}</span>
          <span class="sr-method" :class="'m-' + (f.method || '').toLowerCase()">{{ f.method }}</span>
          <span class="sr-host text-truncate">{{ f.host }}{{ f.path }}</span>
          <span class="sr-ctype text-dim text-truncate">{{ contentTypeShort(f) }}</span>
          <span class="sr-code" :class="f.status_code && f.status_code < 400 ? 'ok' : 'err'">{{ f.status_code }}</span>
          <span class="sr-time text-muted">{{ formatTime(f.timestamp) }}</span>
        </div>
      </div>

      <div class="detail-pane">
        <div v-if="!selectedFlow" class="empty-text text-dim">
          （单击左侧流量查看详情，双击跳转抓包页面）
        </div>
        <template v-else>
          <!-- 顶部条：模式切换 + 标题 + 关闭按钮 -->
          <div class="detail-header">
            <el-radio-group v-model="detailMode" size="small">
              <el-radio-button value="response">响应 Preview</el-radio-button>
              <el-radio-button value="request">请求 Raw</el-radio-button>
            </el-radio-group>
            <span class="detail-title text-dim">
              #{{ selectedFlow.id }} {{ selectedFlow.method }} {{ selectedFlow.host }}{{ selectedFlow.path }}
            </span>
            <div class="flex-1"></div>
            <el-button size="small" circle plain @click="closeDetail" title="关闭">
              <el-icon><Close /></el-icon>
            </el-button>
          </div>
          <!-- 请求 Raw -->
          <div v-if="detailMode === 'request'" class="detail-content">
            <TextSearch :text="requestRaw" searchable language="http" class="detail-code-wrap" />
          </div>
          <!-- 响应 Preview -->
          <div v-else class="detail-content">
            <PreviewView :body="selectedFlow.response_body || ''" :content-type="responseContentType" />
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.search-view-page { background: var(--on-bg); padding: 12px; }
.search-form {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding-bottom: 10px; border-bottom: 1px solid var(--on-border-light);
}
.search-body { flex: 1; min-height: 0; padding-top: 8px; }
.result-list {
  width: 50%; min-width: 320px; overflow-y: auto;
  border-right: 1px solid var(--on-border-light);
}
.sr-row {
  display: grid;
  grid-template-columns: 50px 54px 1fr 70px 44px 64px;
  gap: 6px; padding: 5px 8px; font-size: 12px;
  border-bottom: 1px solid var(--on-border-light);
  cursor: pointer;
  transition: background .1s ease;
}
.sr-row:hover { background: var(--on-bg-hover); }
.sr-row.selected { background: rgba(64, 158, 255, 0.18); }
.sr-method { font-weight: 700; }
.m-get { color: var(--on-ok); }
.m-post { color: var(--on-redirect); }
.m-put { color: var(--on-warn); }
.m-delete { color: var(--on-error); }
.sr-ctype { font-size: 11px; }
.sr-code.ok { color: var(--on-ok); }
.sr-code.err { color: var(--on-error); }
.text-truncate { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.detail-pane {
  flex: 1; min-width: 0; overflow: hidden;
  display: flex; flex-direction: column;
}
.detail-header {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.detail-title {
  font-size: 12px; font-family: var(--on-font-mono);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.detail-content { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.detail-code-wrap {
  flex: 1; height: 0;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
}

.empty-text { text-align: center; padding: 40px; line-height: 1.8; }
</style>
