<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'
import CodeEditor from '../components/CodeEditor.vue'

// 发包页：从零构造 HTTP 请求发送（Composer 功能）
type HistoryItem = {
  id: number
  method: string
  url: string
  headers: Record<string, string>
  body: string
  status_code: number | null
  duration_ms: number | null
  sent_at: number  // 时间戳
}

const METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']

const method = ref('GET')
const url = ref('')
const headerText = ref('')   // 请求头文本：每行 K:V
const body = ref('')
const bodyType = ref('text') // text | json
const timeout = ref(30)
const sending = ref(false)

// 响应
const response = ref<any>(null)

// ---------- 表单状态持久化（切换页面不丢失） ----------
const FORM_KEY = 'telnix_send_form'
let formPersistTimer: number | null = null

function loadForm() {
  try {
    const s = localStorage.getItem(FORM_KEY)
    if (!s) return
    const f = JSON.parse(s)
    if (f.method) method.value = f.method
    if (typeof f.url === 'string') url.value = f.url
    if (typeof f.headerText === 'string') headerText.value = f.headerText
    if (typeof f.body === 'string') body.value = f.body
    if (f.bodyType === 'json' || f.bodyType === 'text') bodyType.value = f.bodyType
    if (typeof f.timeout === 'number' && f.timeout > 0) timeout.value = f.timeout
  } catch { /* ignore */ }
}

function persistForm() {
  if (formPersistTimer !== null) clearTimeout(formPersistTimer)
  formPersistTimer = window.setTimeout(() => {
    try {
      localStorage.setItem(FORM_KEY, JSON.stringify({
        method: method.value,
        url: url.value,
        headerText: headerText.value,
        body: body.value,
        bodyType: bodyType.value,
        timeout: timeout.value,
      }))
    } catch { /* ignore */ }
  }, 300)
}

// 监听表单变化持久化
watch([method, url, headerText, body, bodyType, timeout], persistForm)

// 清空所有文本框
function clearAll() {
  method.value = 'GET'
  url.value = ''
  headerText.value = ''
  body.value = ''
  bodyType.value = 'text'
  timeout.value = 30
  response.value = null
  ElMessage.success('已清空')
}

// 历史记录
const history = ref<HistoryItem[]>([])
const HISTORY_KEY = 'telnix_send_history'
const HISTORY_MAX = 50
let historyIdCounter = 1

// 收藏的请求模板
const templates = ref<any[]>([])
const TEMPLATE_KEY = 'telnix_send_templates'

function loadHistory() {
  try {
    const s = localStorage.getItem(HISTORY_KEY)
    if (s) {
      const arr = JSON.parse(s)
      if (Array.isArray(arr)) {
        history.value = arr
        historyIdCounter = arr.length ? Math.max(...arr.map((x: HistoryItem) => x.id)) + 1 : 1
      }
    }
  } catch { /* ignore */ }
}

function persistHistory() {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.value.slice(0, HISTORY_MAX)))
  } catch { /* ignore */ }
}

function loadTemplates() {
  try {
    const s = localStorage.getItem(TEMPLATE_KEY)
    if (s) {
      const arr = JSON.parse(s)
      if (Array.isArray(arr)) templates.value = arr
    }
  } catch { /* ignore */ }
}

function persistTemplates() {
  try {
    localStorage.setItem(TEMPLATE_KEY, JSON.stringify(templates.value))
  } catch { /* ignore */ }
}

// 解析 headerText 为 dict
function parseHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  if (!text) return out
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const idx = trimmed.indexOf(':')
    if (idx <= 0) continue
    const k = trimmed.slice(0, idx).trim()
    const v = trimmed.slice(idx + 1).trim()
    if (k) out[k] = v
  }
  return out
}

function headersToText(h: Record<string, string>): string {
  return Object.entries(h).map(([k, v]) => `${k}: ${v}`).join('\n')
}

// 发送请求
async function send() {
  const u = url.value.trim()
  if (!u) {
    ElMessage.warning('请输入 URL')
    return
  }
  if (!/^https?:\/\//i.test(u)) {
    ElMessage.warning('URL 必须以 http:// 或 https:// 开头')
    return
  }
  const headers = parseHeaders(headerText.value)
  // body 为空时不发送
  const sendBody = body.value ? body.value : undefined
  sending.value = true
  response.value = null
  const t0 = Date.now()
  try {
    const r: any = await api.sendRequest({
      method: method.value,
      url: u,
      headers,
      body: sendBody,
      timeout: timeout.value,
    })
    response.value = r
    // 入历史
    const item: HistoryItem = {
      id: historyIdCounter++,
      method: method.value,
      url: u,
      headers,
      body: sendBody || '',
      status_code: r.status_code,
      duration_ms: r.duration_ms,
      sent_at: t0,
    }
    history.value.unshift(item)
    if (history.value.length > HISTORY_MAX) {
      history.value = history.value.slice(0, HISTORY_MAX)
    }
    persistHistory()
  } catch (e: any) {
    const msg = e?.message || String(e)
    response.value = { error: msg }
    ElMessage.error('请求失败：' + msg)
  } finally {
    sending.value = false
  }
}

// 从历史记录恢复
function restoreHistory(h: HistoryItem) {
  method.value = h.method
  url.value = h.url
  headerText.value = headersToText(h.headers || {})
  body.value = h.body || ''
  bodyType.value = h.body ? (h.body.trim().startsWith('{') || h.body.trim().startsWith('[') ? 'json' : 'text') : 'text'
}

// 清空历史
async function clearHistory() {
  try {
    await ElMessageBox.confirm('确定清空所有发送历史？', '清空历史', {
      confirmButtonText: '清空', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  history.value = []
  persistHistory()
  ElMessage.success('已清空')
}

// 删除单条历史
function removeHistory(id: number) {
  history.value = history.value.filter(h => h.id !== id)
  persistHistory()
}

// 保存为模板
async function saveAsTemplate() {
  try {
    const { value: name } = await ElMessageBox.prompt('请输入模板名称', '保存模板', {
      confirmButtonText: '保存', cancelButtonText: '取消',
    })
    if (!name || !name.trim()) return
    templates.value.unshift({
      name: name.trim(),
      method: method.value,
      url: url.value,
      headerText: headerText.value,
      body: body.value,
      bodyType: bodyType.value,
    })
    persistTemplates()
    ElMessage.success('模板已保存')
  } catch { /* cancel */ }
}

function applyTemplate(t: any) {
  method.value = t.method || 'GET'
  url.value = t.url || ''
  headerText.value = t.headerText || ''
  body.value = t.body || ''
  bodyType.value = t.bodyType || 'text'
}

async function deleteTemplate(idx: number) {
  try {
    await ElMessageBox.confirm(`确定删除模板「${templates.value[idx].name}」？`, '删除模板', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  templates.value.splice(idx, 1)
  persistTemplates()
}

// cURL 导入
const curlDialogVisible = ref(false)
const curlText = ref('')

function showCurlDialog() {
  curlText.value = ''
  curlDialogVisible.value = true
}

function importCurl() {
  const text = curlText.value.trim()
  if (!text) {
    ElMessage.warning('请粘贴 cURL 命令')
    return
  }
  try {
    // 简单 cURL 解析：支持 -X, -H, -d, --data, --data-raw, URL
    const tokens = tokenizeCurl(text)
    let m = 'GET'
    let u = ''
    const headers: Record<string, string> = {}
    let bodyData = ''
    for (let i = 0; i < tokens.length; i++) {
      const t = tokens[i]
      if (t === '-X' || t === '--request') {
        if (tokens[i + 1]) m = tokens[i + 1].toUpperCase()
        i++
      } else if (t === '-H' || t === '--header') {
        if (tokens[i + 1]) {
          const h = tokens[i + 1]
          const idx = h.indexOf(':')
          if (idx > 0) headers[h.slice(0, idx).trim()] = h.slice(idx + 1).trim()
        }
        i++
      } else if (t === '-d' || t === '--data' || t === '--data-raw' || t === '--data-binary') {
        if (tokens[i + 1]) bodyData = tokens[i + 1]
        i++
        if (m === 'GET') m = 'POST'
      } else if (t === '--compressed' || t === '-k' || t === '--insecure' || t === '-s' || t === '--silent' || t === '-S' || t === '--show-error' || t === '-L' || t === '--location') {
        // 忽略这些 flag
      } else if (!t.startsWith('-') && !u) {
        u = t
      }
    }
    if (!u) {
      ElMessage.error('未找到 URL')
      return
    }
    method.value = m
    url.value = u
    headerText.value = headersToText(headers)
    body.value = bodyData
    bodyType.value = bodyData ? (bodyData.trim().startsWith('{') || bodyData.trim().startsWith('[') ? 'json' : 'text') : 'text'
    curlDialogVisible.value = false
    ElMessage.success('cURL 已导入')
  } catch (e: any) {
    ElMessage.error('cURL 解析失败：' + (e?.message || e))
  }
}

// 简单 cURL 分词（支持单双引号）
function tokenizeCurl(text: string): string[] {
  const tokens: string[] = []
  let i = 0
  while (i < text.length) {
    while (i < text.length && /\s/.test(text[i])) i++
    if (i >= text.length) break
    const ch = text[i]
    if (ch === '"' || ch === "'") {
      const quote = ch
      i++
      let s = ''
      while (i < text.length && text[i] !== quote) {
        if (text[i] === '\\' && i + 1 < text.length) {
          s += text[i + 1]
          i += 2
        } else {
          s += text[i]
          i++
        }
      }
      i++  // 跳过结束引号
      tokens.push(s)
    } else {
      let s = ''
      while (i < text.length && !/\s/.test(text[i])) {
        s += text[i]
        i++
      }
      tokens.push(s)
    }
  }
  return tokens
}

// 导出为 cURL
function exportCurl() {
  const u = url.value.trim()
  if (!u) {
    ElMessage.warning('请先填写 URL')
    return
  }
  const headers = parseHeaders(headerText.value)
  const parts: string[] = ['curl', '-X', method.value]
  for (const [k, v] of Object.entries(headers)) {
    parts.push('-H', `"${k}: ${v}"`)
  }
  if (body.value) {
    parts.push('--data-raw', `'${body.value.replace(/'/g, "'\\''")}'`)
  }
  parts.push(`"${u}"`)
  const curl = parts.join(' ')
  navigator.clipboard.writeText(curl).then(() => {
    ElMessage.success('cURL 已复制到剪贴板')
  }).catch(() => {
    ElMessage.warning('复制失败，请手动复制控制台输出')
    console.log(curl)
  })
}

// 响应内容自动格式化
const responsePreview = computed(() => {
  if (!response.value) return ''
  if (response.value.error) return `错误：${response.value.error}`
  const headers = response.value.response_headers || {}
  const ct = (headers['Content-Type'] || headers['content-type'] || '').toString()
  const body = response.value.response_body || ''
  // JSON 自动格式化
  if (ct.includes('json') || body.trim().startsWith('{') || body.trim().startsWith('[')) {
    try {
      return JSON.stringify(JSON.parse(body), null, 2)
    } catch {
      return body
    }
  }
  return body
})

const responseHeadersText = computed(() => {
  if (!response.value || !response.value.response_headers) return ''
  return Object.entries(response.value.response_headers)
    .map(([k, v]) => `${k}: ${v}`)
    .join('\n')
})

const statusColor = computed(() => {
  if (!response.value || response.value.status_code == null) return ''
  const s = response.value.status_code
  if (s >= 200 && s < 300) return 'text-success'
  if (s >= 300 && s < 400) return 'text-warning'
  if (s >= 400) return 'text-danger'
  return ''
})

// 格式化 JSON body
function formatJson() {
  if (!body.value) return
  try {
    body.value = JSON.stringify(JSON.parse(body.value), null, 2)
    ElMessage.success('已格式化')
  } catch (e: any) {
    ElMessage.error('JSON 解析失败：' + (e?.message || e))
  }
}

function formatTime(ts: number): string {
  const d = new Date(ts)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

// ---------- 底部历史/模板区域可拖动改变高度 ----------
const BOTTOM_HEIGHT_KEY = 'telnix_send_bottom_height'
const bottomHeight = ref(220)  // 默认 220px
const isResizing = ref(false)
let resizeStartY = 0
let resizeStartH = 0

function loadBottomHeight() {
  try {
    const s = localStorage.getItem(BOTTOM_HEIGHT_KEY)
    if (s) {
      const n = parseInt(s, 10)
      if (!isNaN(n) && n >= 80 && n <= 600) bottomHeight.value = n
    }
  } catch { /* ignore */ }
}

function onResizeDown(e: MouseEvent) {
  isResizing.value = true
  resizeStartY = e.clientY
  resizeStartH = bottomHeight.value
  document.body.style.cursor = 'row-resize'
  document.body.style.userSelect = 'none'
  e.preventDefault()
}

function onResizeMove(e: MouseEvent) {
  if (!isResizing.value) return
  // 向上拖增大高度
  const delta = resizeStartY - e.clientY
  const newH = Math.max(80, Math.min(600, resizeStartH + delta))
  bottomHeight.value = newH
}

function onResizeUp() {
  if (!isResizing.value) return
  isResizing.value = false
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
  try {
    localStorage.setItem(BOTTOM_HEIGHT_KEY, String(bottomHeight.value))
  } catch { /* ignore */ }
}

onUnmounted(() => {
  window.removeEventListener('mousemove', onResizeMove)
  window.removeEventListener('mouseup', onResizeUp)
})

onMounted(() => {
  loadForm()
  loadHistory()
  loadTemplates()
  loadBottomHeight()
  window.addEventListener('mousemove', onResizeMove)
  window.addEventListener('mouseup', onResizeUp)
})
</script>

<template>
  <div class="send-view full flex flex-col">
    <div class="page-header">
      <div class="page-title"><el-icon><Promotion /></el-icon>&nbsp;发包</div>
      <div class="header-actions">
        <el-button size="small" @click="clearAll">
          <el-icon><Delete /></el-icon>&nbsp;清空
        </el-button>
        <el-button size="small" @click="showCurlDialog">
          <el-icon><Download /></el-icon>&nbsp;从 cURL 导入
        </el-button>
        <el-button size="small" @click="exportCurl">
          <el-icon><Upload /></el-icon>&nbsp;复制为 cURL
        </el-button>
        <el-button size="small" @click="saveAsTemplate">
          <el-icon><Star /></el-icon>&nbsp;存为模板
        </el-button>
      </div>
    </div>

    <div class="send-body flex-1 overflow-hidden flex">
      <!-- 左侧：请求构造 + 历史 + 模板 -->
      <div class="send-left flex flex-col">
        <!-- 请求行：方法 + URL + 发送 -->
        <div class="req-line">
          <el-select v-model="method" size="default" style="width: 110px">
            <el-option v-for="m in METHODS" :key="m" :value="m" :label="m" />
          </el-select>
          <el-input
            v-model="url"
            size="default"
            placeholder="https://example.com/api/v1/users?page=1"
            class="url-input"
            @keyup.enter="send"
          />
          <el-input-number v-model="timeout" :min="1" :max="300" :step="5" size="default" style="width: 110px" />
          <span class="text-dim" style="font-size: 12px">秒</span>
          <el-button type="primary" size="default" :loading="sending" @click="send">
            <el-icon><Promotion /></el-icon>&nbsp;发送
          </el-button>
        </div>

        <!-- 请求头 + 请求体 -->
        <div class="req-editors flex-1 overflow-hidden flex flex-col">
          <div class="editor-block flex-1">
            <div class="block-header">
              <span>请求头</span>
              <span class="text-dim" style="font-size: 11px">每行 Key: Value</span>
            </div>
            <CodeEditor
              v-model="headerText"
              language="plaintext"
              placeholder="Content-Type: application/json&#10;Authorization: Bearer ..."
              min-height="80px"
              class="editor-area"
            />
          </div>
          <div class="editor-block flex-1">
            <div class="block-header">
              <span>请求体</span>
              <el-radio-group v-model="bodyType" size="small" style="margin-left: auto">
                <el-radio-button value="text">文本</el-radio-button>
                <el-radio-button value="json">JSON</el-radio-button>
              </el-radio-group>
              <el-button v-if="bodyType === 'json'" link size="small" @click="formatJson">格式化</el-button>
            </div>
            <CodeEditor
              v-model="body"
              :language="bodyType === 'json' ? 'json' : 'plaintext'"
              placeholder="请求体内容（留空表示无请求体）"
              min-height="100px"
              class="editor-area"
            />
          </div>
        </div>

        <!-- 历史记录 + 模板（Tab） -->
        <div class="bottom-tabs" :style="{ height: bottomHeight + 'px' }">
          <div class="bottom-resizer" @mousedown="onResizeDown">
            <span class="resizer-grip"></span>
          </div>
          <el-tabs>
            <el-tab-pane label="历史">
              <div class="tab-toolbar">
                <span class="text-dim" style="font-size: 12px">{{ history.length }} 条</span>
                <el-button size="small" link @click="clearHistory" :disabled="!history.length">清空</el-button>
              </div>
              <div class="list-scroll">
                <div
                  v-for="h in history"
                  :key="h.id"
                  class="list-item"
                  @click="restoreHistory(h)"
                >
                  <div class="li-main">
                    <span class="li-method" :class="'m-' + h.method.toLowerCase()">{{ h.method }}</span>
                    <span class="li-url" :title="h.url">{{ h.url }}</span>
                  </div>
                  <div class="li-meta">
                    <span :class="h.status_code && h.status_code < 400 ? 'text-success' : 'text-danger'">
                      {{ h.status_code ?? '—' }}
                    </span>
                    <span class="text-dim">{{ h.duration_ms ?? '—' }}ms</span>
                    <span class="text-dim">{{ formatTime(h.sent_at) }}</span>
                    <el-button link size="small" @click.stop="removeHistory(h.id)">
                      <el-icon><Delete /></el-icon>
                    </el-button>
                  </div>
                </div>
                <div v-if="!history.length" class="empty-text text-dim">暂无历史</div>
              </div>
            </el-tab-pane>
            <el-tab-pane label="模板">
              <div class="list-scroll">
                <div
                  v-for="(t, idx) in templates"
                  :key="idx"
                  class="list-item"
                  @click="applyTemplate(t)"
                >
                  <div class="li-main">
                    <span class="li-method" :class="'m-' + (t.method || 'get').toLowerCase()">{{ t.method || 'GET' }}</span>
                    <span class="li-url" :title="t.url">{{ t.name }}</span>
                  </div>
                  <div class="li-meta">
                    <span class="text-dim" :title="t.url">{{ t.url }}</span>
                    <el-button link size="small" @click.stop="deleteTemplate(idx)">
                      <el-icon><Delete /></el-icon>
                    </el-button>
                  </div>
                </div>
                <div v-if="!templates.length" class="empty-text text-dim">暂无模板，点击右上角「存为模板」</div>
              </div>
            </el-tab-pane>
          </el-tabs>
        </div>
      </div>

      <!-- 右侧：响应展示 -->
      <div class="send-right flex flex-col">
        <div class="resp-header">
          <span class="resp-title">响应</span>
          <template v-if="response && !response.error">
            <span :class="statusColor" class="resp-status">{{ response.status_code }} {{ response.reason }}</span>
            <span class="text-dim">{{ response.size }} bytes</span>
            <span class="text-dim">{{ response.duration_ms }}ms</span>
          </template>
        </div>
        <div v-if="!response" class="empty-text text-dim resp-empty">
          <el-icon style="font-size: 28px"><Promotion /></el-icon>
          <div style="margin-top: 8px">填写请求后点击「发送」</div>
        </div>
        <div v-else-if="response.error" class="empty-text text-danger resp-empty">
          <el-icon style="font-size: 28px"><WarningFilled /></el-icon>
          <div style="margin-top: 8px">{{ response.error }}</div>
        </div>
        <div v-else class="resp-content flex-1 overflow-hidden flex flex-col">
          <el-tabs class="resp-tabs flex-1">
            <el-tab-pane label="Preview">
              <div class="resp-preview mono">{{ responsePreview }}</div>
            </el-tab-pane>
            <el-tab-pane label="Headers">
              <div class="resp-headers mono">{{ responseHeadersText }}</div>
            </el-tab-pane>
          </el-tabs>
        </div>
      </div>
    </div>

    <!-- cURL 导入对话框 -->
    <el-dialog v-model="curlDialogVisible" title="从 cURL 导入" width="640px">
      <div class="curl-hint text-dim">
        粘贴 cURL 命令，支持 -X / -H / -d / --data-raw / URL 等参数
      </div>
      <el-input
        v-model="curlText"
        type="textarea"
        :rows="8"
        placeholder="curl -X POST 'https://example.com/api' -H 'Content-Type: application/json' -d '{&quot;key&quot;:&quot;value&quot;}'"
        class="mono"
      />
      <template #footer>
        <el-button @click="curlDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="importCurl">导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.send-view { background: var(--on-bg); }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.header-actions { display: flex; gap: 8px; align-items: center; }
.page-title { font-size: 15px; font-weight: 600; display: flex; align-items: center; }

.send-body { padding: 0; }

/* 左侧：请求构造区 */
.send-left {
  width: 55%; min-width: 480px;
  border-right: 1px solid var(--on-border-light);
  overflow: hidden;
}
.req-line {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--on-border-light);
}
.url-input { flex: 1; }

.req-editors { padding: 8px 12px; gap: 8px; overflow: hidden; }
.editor-block {
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
  display: flex; flex-direction: column;
  min-height: 120px;
  overflow: hidden;
}
.block-header {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 10px;
  background: var(--on-bg-elevated);
  border-bottom: 1px solid var(--on-border-light);
  font-size: 12px; font-weight: 500;
}
.editor-area { flex: 1; }

/* 底部历史/模板 tab */
.bottom-tabs {
  border-top: 1px solid var(--on-border-light);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.bottom-resizer {
  height: 6px;
  cursor: row-resize;
  background: var(--on-bg-elevated);
  border-bottom: 1px solid var(--on-border-light);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background 0.15s;
}
.bottom-resizer:hover { background: var(--on-border-light, #444); }
.resizer-grip {
  display: inline-block;
  width: 36px;
  height: 2px;
  background: var(--on-text-dim, #666);
  border-radius: 1px;
  opacity: 0.5;
}
.bottom-resizer:hover .resizer-grip { opacity: 1; }
.bottom-tabs :deep(.el-tabs) { display: flex; flex-direction: column; flex: 1; overflow: hidden; }
.bottom-tabs :deep(.el-tabs__header) { margin: 0 12px; flex-shrink: 0; }
.bottom-tabs :deep(.el-tabs__content) { padding: 0 12px; flex: 1; overflow: hidden; }
.tab-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 4px 0;
}
.list-scroll {
  flex: 1; overflow-y: auto; min-height: 0;
}
.list-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.list-item:hover { background: var(--on-bg-elevated); }
.li-main {
  display: flex; align-items: center; gap: 8px;
  flex: 1; min-width: 0;
}
.li-method {
  display: inline-block; min-width: 50px;
  font-weight: 600; font-size: 11px;
  padding: 1px 6px; border-radius: 3px;
  background: var(--on-bg-elevated);
}
.li-method.m-get { color: var(--on-ok); }
.li-method.m-post { color: var(--on-warn); }
.li-method.m-put { color: var(--on-redirect); }
.li-method.m-delete { color: var(--on-error); }
.li-method.m-patch { color: var(--on-purple); }
.li-method.m-head, .li-method.m-options { color: var(--on-text-dim); }
.li-url {
  flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--on-text);
}
.li-meta {
  display: flex; align-items: center; gap: 10px;
  font-size: 11px;
}

/* 右侧：响应展示 */
.send-right {
  flex: 1; padding: 0;
  overflow: hidden;
}
.resp-header {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--on-border-light);
  font-size: 13px;
}
.resp-title { font-weight: 600; }
.resp-status { font-weight: 600; }
.text-success { color: var(--on-ok); }
.text-warning { color: var(--on-warn); }
.text-danger { color: var(--on-error); }
.resp-empty {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  font-size: 13px;
}
.resp-content { padding: 8px 12px; overflow: hidden; }
.resp-tabs { height: 100%; }
.resp-tabs :deep(.el-tabs__content) { height: calc(100% - 36px); overflow: hidden; }
.resp-preview, .resp-headers {
  height: 100%; overflow: auto;
  padding: 8px 10px;
  background: var(--on-bg-elevated);
  border-radius: 4px;
  white-space: pre-wrap; word-break: break-all;
  font-size: 12px;
}

.empty-text { padding: 20px; text-align: center; }
.curl-hint { font-size: 12px; margin-bottom: 8px; }
</style>
