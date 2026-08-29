<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useFlowsStore } from '../stores/flows'
import { api, type AiChat, type AiMessage, type Flow, type Settings, type AiUsageStats } from '../api/client'
import { saveFlowSnapshot, getFlowSnapshot } from '../utils/aiFlowSnapshot'

const router = useRouter()

// AI 分析视图：左侧聊天记录列表 + 中间聊天界面 + 右侧包信息面板
const flowsStore = useFlowsStore()
const { t } = useI18n()

const chats = ref<AiChat[]>([])
const currentChat = ref<AiChat | null>(null)
const messages = ref<AiMessage[]>([])
const inputText = ref('')
const analyzing = ref(false)
const chatting = ref(false)
const settings = ref<Settings>({})
const targetFlows = ref<Flow[]>([])
const chatBodyRef = ref<HTMLElement | null>(null)

// 当前对话关联的流量详情（用于右侧包信息面板显示）
const chatFlows = ref<Flow[]>([])
const chatFlowsLoading = ref(false)
// 右侧面板展开的流量 id（默认展开第一条）
const expandedFlowId = ref<number | null>(null)
// 右侧面板是否可见（用户可折叠）
const contextPanelVisible = ref(true)

// AI 使用统计
const aiUsage = ref<AiUsageStats>({
  all_time: { requests: 0, input_tokens: 0, output_tokens: 0, total_cost: 0 },
  month: { requests: 0, input_tokens: 0, output_tokens: 0, total_cost: 0 },
  today: { requests: 0, input_tokens: 0, output_tokens: 0, total_cost: 0 },
  by_service: [],
})
const usageStatsVisible = ref(false)  // 侧边栏使用统计可见性

// 流式响应状态
const streamingContent = ref('')  // 当前流式响应的累积内容
const streamingMessageId = ref<number | null>(null)  // 当前流式消息 ID

const md = new MarkdownIt({
  html: false,
  breaks: true,
  highlight(str: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang }).value}</code></pre>`
      } catch {
        /* ignore */
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
  },
})
// 安全：仅允许安全协议链接，阻断 javascript:/data:/vbscript: 等，防止 AI 输出恶意链接点击后 XSS
md.validateLink = (url: string): boolean => {
  const s = url.trim().toLowerCase()
  // 允许相对/锚点/查询链接与安全协议
  if (/^(#|\/|\.\/|\.\.\/|\?)/.test(s) || !/^[a-z][a-z0-9+.-]*:/.test(s)) return true
  return /^(https?|mailto|tel):/.test(s)
}

const hasSelection = computed(() => flowsStore.aiFlowIds.length > 0)

// 待分析面板：从抓包页跳转过来时显示选中流量，等用户确认
const pendingFlows = ref<Flow[]>([])

async function loadSettings() {
  try {
    settings.value = await api.getSettings()
  } catch {
    /* ignore */
  }
}

async function loadAiUsage() {
  try {
    aiUsage.value = await api.aiGetUsage()
  } catch {
    /* ignore */
  }
}

function formatCost(cost: number): string {
  if (cost < 0.0001) return '$0.0000'
  return '$' + cost.toFixed(4)
}

async function loadChats() {
  try {
    chats.value = await api.aiListChats()
  } catch {
    /* ignore */
  }
}

async function loadPendingFlows() {
  const ids = flowsStore.aiFlowIds
  if (!ids.length) {
    pendingFlows.value = []
    return
  }
  const list: Flow[] = []
  for (const id of ids) {
    try {
      const flow = await api.getFlow(id)
      list.push(flow)
      // 同步存快照：发送到 AI 之前先备份，避免后端重启后丢失
      saveFlowSnapshot(flow)
    } catch {
      /* skip */
    }
  }
  pendingFlows.value = list
}

// 加载当前对话关联的流量详情（用于右侧包信息面板）
async function loadChatFlows(flowIds: number[]) {
  if (!flowIds.length) {
    chatFlows.value = []
    expandedFlowId.value = null
    return
  }
  chatFlowsLoading.value = true
  const list: Flow[] = []
  for (const id of flowIds) {
    // 优先调后端 API；失败时（如后端重启后 flow 表被清空）回退到本地快照
    let flow: Flow | null = null
    try {
      flow = await api.getFlow(id)
    } catch {
      /* 后端拿不到，尝试本地快照 */
      flow = await getFlowSnapshot(id)
    }
    if (flow) list.push(flow)
  }
  chatFlows.value = list
  // 默认展开第一条
  expandedFlowId.value = list[0]?.id ?? null
  chatFlowsLoading.value = false
}

async function startAnalyze() {
  if (!hasAiKey()) {
    ElMessage.warning(t('ai.pleaseConfigureApiKey'))
    return
  }
  analyzing.value = true
  // 立即提示，避免像卡死
  const waitMsg = ElMessage({
    message: t('ai.requestSentWaiting'),
    type: 'info',
    duration: 0,
  })
  try {
    const flowIds = flowsStore.aiFlowIds
    const res = await api.aiAnalyze({ flow_ids: flowIds })
    await loadChats()
    await openChat(res.chat_id)
    // 清除选中状态
    flowsStore.aiFlowIds = []
    pendingFlows.value = []
    waitMsg?.close()
    ElMessage.success(t('ai.analysisComplete'))
    // 刷新使用统计
    loadAiUsage()
  } catch (e: any) {
    waitMsg?.close()
    const msg = e?.message || String(e)
    if (msg === 'not found') {
      ElMessage.error(t('ai.backendNotFound'))
    } else {
      ElMessage.error(t('ai.analysisFailed', { msg }))
    }
  } finally {
    analyzing.value = false
  }
}

function hasAiKey(): boolean {
  const service = settings.value.ai_service || 'deepseek'
  switch (service) {
    case 'deepseek': return !!settings.value.deepseek_api_key
    case 'anthropic': return !!settings.value.anthropic_api_key
    case 'openai': return !!settings.value.openai_api_key
    case 'gemini': return !!settings.value.gemini_api_key
    case 'ollama': return !!settings.value.ollama_endpoint
    default: return !!settings.value.deepseek_api_key
  }
}

function handleUsageDropdown() {
  // 点击空白区域时刷新统计
  loadAiUsage()
}

async function onModelChange(val: string) {
  try {
    await api.saveSettings({ ai_service: val } as Settings)
    ElMessage.success(t('settings.aiServiceChanged'))
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e?.message }))
  }
}

// 无流量直接开始自由对话（不调 API，只创建空聊天记录，等用户先发消息）
async function startFreeChat() {
  if (!hasAiKey()) {
    ElMessage.warning(t('ai.pleaseConfigureApiKey'))
    return
  }
  analyzing.value = true
  try {
    const res = await api.aiAnalyze({ flow_ids: [] })
    await loadChats()
    await openChat(res.chat_id)
    ElMessage.success(t('ai.freeChatCreated'))
  } catch (e: any) {
    const msg = e?.message || String(e)
    if (msg === 'not found') {
      ElMessage.error(t('ai.backendNotFound'))
    } else {
      ElMessage.error(t('ai.createFailed', { msg }))
    }
  } finally {
    analyzing.value = false
  }
}

async function openChat(chatId: number) {
  try {
    const res = await api.aiGetChat(chatId)
    currentChat.value = res.chat
    messages.value = res.messages
    // 清除待分析面板
    flowsStore.aiFlowIds = []
    pendingFlows.value = []
    await nextTick()
    scrollToBottom()
    // 加载关联流量详情（用于右侧包信息面板）
    await loadChatFlows(res.chat.flow_ids || [])
  } catch (e: any) {
    ElMessage.error(t('ai.loadFailed', { msg: e?.message || e }))
  }
}

// 发送到已有对话：打开选中的 chat，并在输入框预填引用待分析流量的消息
const sendToExistingChatId = ref<number | null>(null)
// 下一次 sendMessage 时要带上的流量 ID（用于"发送到已有会话"场景）
const pendingQuoteFlowIds = ref<number[]>([])
async function sendToExistingChat() {
  if (!sendToExistingChatId.value) {
    ElMessage.warning(t('ai.pleaseSelectExistingChat'))
    return
  }
  const ids = pendingFlows.value.map(f => f.id)
  // 打开该对话
  await openChat(sendToExistingChatId.value)
  // 在输入框预填引用待分析流量的消息，用户可编辑后发送
  if (ids.length) {
    inputText.value = t('ai.analyzeFlowsPrompt', { ids: ids.map(id => `#${id}`).join(' ') }) + '\n'
    // 记下要带上的流量 ID，sendMessage 时一起发给后端
    pendingQuoteFlowIds.value = ids
  }
  sendToExistingChatId.value = null
  ElMessage.success(t('ai.chatOpenedPrefilled'))
}

async function sendMessage() {
  if (!currentChat.value || !inputText.value.trim()) return
  const msg = inputText.value.trim()
  inputText.value = ''
  // 用唯一 tempId 标记临时消息，错误回滚时仅按 id 删除，避免误删同内容的历史消息
  const tempId = -Date.now()
  const userMsgId = -Date.now() - 1
  messages.value.push({
    id: userMsgId,
    chat_id: currentChat.value.id,
    role: 'user',
    content: msg,
    created_at: new Date().toISOString(),
  })
  await nextTick()
  scrollToBottom()

  chatting.value = true
  // 取出待追加的流量 ID（从"发送到已有会话"预填而来），用完即清空
  const quoteIds = pendingQuoteFlowIds.value.slice()
  pendingQuoteFlowIds.value = []
  
  // 创建助手消息占位（用于流式更新）
  const assistantMsgId = -Date.now() - 2
  const assistantMsg: AiMessage = {
    id: assistantMsgId,
    chat_id: currentChat.value.id,
    role: 'assistant',
    content: '',
    created_at: new Date().toISOString(),
  }
  messages.value.push(assistantMsg)
  streamingContent.value = ''
  streamingMessageId.value = assistantMsgId
  
  try {
    // 尝试流式响应
    const streaming = await api.aiChatStream({
      chat_id: currentChat.value.id,
      message: msg,
      flow_ids: quoteIds.length ? quoteIds : undefined,
    }, (chunk) => {
      // 逐步累积内容
      streamingContent.value += chunk
      assistantMsg.content = streamingContent.value
      // 渐进渲染：只更新累积的 Markdown
      nextTick(() => scrollToBottom())
    })
    
    // 流式完成后更新消息
    assistantMsg.content = streamingContent.value
    await nextTick()
    scrollToBottom()
    // 刷新使用统计
    loadAiUsage()
  } catch (e: any) {
    // 流式失败时删除占位消息
    messages.value = messages.value.filter((m) => m.id !== assistantMsgId)
    ElMessage.error(t('ai.replyFailed', { msg: e?.message || e }))
    // 仅删除本次创建的临时消息，避免误删历史消息
    messages.value = messages.value.filter((m) => m.id !== tempId)
    inputText.value = msg
    // 失败时还原流量引用，下次发送再带上
    pendingQuoteFlowIds.value = quoteIds
  } finally {
    chatting.value = false
    streamingContent.value = ''
    streamingMessageId.value = null
  }
}

function scrollToBottom() {
  const el = chatBodyRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function deleteChat(chatId: number) {
  try {
    await ElMessageBox.confirm(t('ai.confirmDeleteRecord'), t('ai.delete'), {
      confirmButtonText: t('ai.delete'), cancelButtonText: t('ai.cancel'), type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.aiDeleteChat(chatId)
    if (currentChat.value?.id === chatId) {
      currentChat.value = null
      messages.value = []
      chatFlows.value = []
    }
    await loadChats()
    ElMessage.success(t('ai.deleted'))
  } catch (e: any) {
    ElMessage.error(t('ai.deleteFailed', { msg: e?.message || e }))
  }
}

// 重命名对话
async function renameChat(chatId: number, oldTitle: string) {
  try {
    const { value } = await ElMessageBox.prompt(t('ai.pleaseEnterNewName'), t('ai.rename'), {
      confirmButtonText: t('ai.save'),
      cancelButtonText: t('ai.cancel'),
      inputValue: oldTitle,
      inputValidator: (v: string) => !!v?.trim() || t('ai.nameCannotBeEmpty'),
    })
    const title = value.trim()
    if (title === oldTitle) return
    await api.aiUpdateTitle(chatId, title)
    // 更新本地列表和当前对话
    const c = chats.value.find(x => x.id === chatId)
    if (c) c.title = title
    if (currentChat.value?.id === chatId) currentChat.value.title = title
    ElMessage.success(t('ai.renamed'))
  } catch (e: any) {
    if (e === 'cancel' || e?.toString?.().includes('cancel')) return
    ElMessage.error(t('ai.renameFailed', { msg: e?.message || e }))
  }
}

function formatTime(s: string): string {
  if (!s) return ''
  const d = new Date(s)
  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`
}

function renderMarkdown(content: string): string {
  return md.render(content)
}

function onKeydown(e: Event) {
  const ke = e as KeyboardEvent
  if (ke.key === 'Enter' && !ke.shiftKey) {
    ke.preventDefault()
    sendMessage()
  }
}

function clearPending() {
  flowsStore.aiFlowIds = []
  pendingFlows.value = []
}

// ===== 包信息面板辅助函数 =====
// HTTP method 转换为 method-badge 类名
function methodClass(m: string): string {
  const map: Record<string, string> = {
    GET: 'method-get', POST: 'method-post', PUT: 'method-put',
    DELETE: 'method-del', PATCH: 'method-patch',
    HEAD: 'method-other', OPTIONS: 'method-other', TRACE: 'method-other',
  }
  return map[(m || '').toUpperCase()] || 'method-other'
}

// 状态码分级
function statusClass(code: number | null): string {
  if (!code) return 'status-null'
  if (code < 300) return 'status-2xx'
  if (code < 400) return 'status-3xx'
  if (code < 500) return 'status-4xx'
  return 'status-5xx'
}

// 解析 headers JSON 为 key-value 数组（仅显示重要的头，避免太长）
function parseHeaders(raw: string | null, max = 30): { key: string; value: string }[] {
  if (!raw) return []
  try {
    const obj = JSON.parse(raw)
    if (obj && typeof obj === 'object') {
      return Object.entries(obj)
        .map(([k, v]) => ({ key: k, value: String(v) }))
        .slice(0, max)
    }
  } catch { /* ignore */ }
  return []
}

// 安全 JSON 解析
function safeJson(s: string | null): any {
  if (!s) return null
  try { return JSON.parse(s) } catch { return null }
}

// 格式化字节大小
function formatSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

// 计算 body 大小（从字符串长度估算）
function bodySize(body: string | null): number {
  return body ? body.length : 0
}

// 提取 host 主名（去掉 TLD 显示更短）
function shortHost(host: string): string {
  if (!host) return ''
  return host
}

// 提取 Content-Type 主类型
function contentType(f: Flow): string {
  const headers = safeJson(f.response_headers) || {}
  for (const k of Object.keys(headers)) {
    if (k.toLowerCase() === 'content-type') {
      return String(headers[k]).split(';')[0].trim()
    }
  }
  return ''
}

// 切换流量展开
function toggleFlowExpand(id: number) {
  expandedFlowId.value = expandedFlowId.value === id ? null : id
}

// 在抓包页查看此流量（跳转到 /capture 并选中）
function viewInCapture(flowId: number) {
  flowsStore.select(flowId)
  router.push('/capture')
}

// 引用流量到输入框
function quoteFlowInInput(f: Flow) {
  inputText.value = (inputText.value || '') + t('ai.quoteFlowPrompt', { id: f.id, method: f.method, host: f.host, path: f.path })
}

onMounted(() => {
  loadSettings()
  loadChats()
  loadPendingFlows()
  loadAiUsage()
})
watch(() => flowsStore.aiFlowIds, loadPendingFlows)
</script>

<template>
  <div class="ai-view full flex">
    <!-- 左侧：聊天记录列表 -->
    <div class="chat-sidebar">
      <div class="sidebar-header">
        <div class="sidebar-title-row">
          <span class="sidebar-title">
            <el-icon class="sidebar-title-icon"><MagicStick /></el-icon>
            {{ t('ai.analysisRecords') }}
          </span>
        </div>
        <el-button type="primary" size="small" :loading="analyzing" @click="startFreeChat" class="new-chat-btn">
          <el-icon><ChatDotRound /></el-icon>&nbsp;{{ t('ai.freeChat') }}
        </el-button>
      </div>
      <div class="chat-list flex-1 overflow-auto">
        <div
          v-for="c in chats"
          :key="c.id"
          class="chat-item"
          :class="{ active: currentChat?.id === c.id }"
          @click="openChat(c.id)"
        >
          <div class="chat-item-main">
            <div class="chat-item-title" @dblclick.stop="renameChat(c.id, c.title)">{{ c.title }}</div>
            <div class="chat-item-meta">
              <span class="chat-item-time text-dim">{{ formatTime(c.updated_at) }}</span>
              <span v-if="c.flow_ids.length" class="chat-item-flows">
                <el-icon><Connection /></el-icon>
                {{ c.flow_ids.length }}
              </span>
              <span v-else class="chat-item-free">{{ t('ai.free') }}</span>
            </div>
          </div>
          <div class="chat-item-actions">
            <el-button
              link
              size="small"
              class="chat-item-rename"
              :title="t('ai.rename')"
              @click.stop="renameChat(c.id, c.title)"
            >
              <el-icon><Edit /></el-icon>
            </el-button>
            <el-button
              link
              type="danger"
              size="small"
              class="chat-item-del"
              @click.stop="deleteChat(c.id)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
        <div v-if="!chats.length" class="empty-text text-dim">
          <el-icon :size="32" class="empty-icon"><ChatDotRound /></el-icon>
          <div>{{ t('ai.noRecords') }}</div>
          <div class="empty-hint">{{ t('ai.selectFlowHint') }}</div>
        </div>
      </div>
    </div>

    <!-- 中间：聊天界面 -->
    <div class="chat-main flex-1 flex flex-col">
      <!-- 待分析面板：从抓包页选中流量跳转过来 -->
      <div v-if="pendingFlows.length && !currentChat" class="pending-panel">
        <div class="pending-header">
          <div class="pending-header-icon">
            <el-icon><MagicStick /></el-icon>
          </div>
          <div class="pending-header-text">
            <div class="pending-header-title">{{ t('ai.flowsSelected', { n: pendingFlows.length }) }}</div>
            <div class="pending-header-sub text-dim">{{ t('ai.clickNewChatAnalysisHint') }}</div>
          </div>
        </div>
        <div class="pending-flows">
          <div v-for="f in pendingFlows" :key="f.id" class="pending-flow-item">
            <span class="mono text-dim flow-id">#{{ f.id }}</span>
            <span :class="['method-badge', methodClass(f.method)]">{{ f.method }}</span>
            <span v-if="f.status_code" :class="['flow-status', statusClass(f.status_code)]">{{ f.status_code }}</span>
            <span class="pending-flow-url">{{ f.url }}</span>
          </div>
        </div>
        <div class="pending-actions">
          <el-button type="primary" :loading="analyzing" @click="startAnalyze">
            <el-icon><Cpu /></el-icon>&nbsp;{{ t('ai.newChatAnalysis') }}
          </el-button>
          <el-button @click="clearPending">{{ t('ai.cancel') }}</el-button>
        </div>
        <!-- 发送到已有对话：选择一个已有对话，预填引用消息 -->
        <div v-if="chats.length" class="pending-existing">
          <div class="text-dim" style="font-size: 12px; margin-bottom: 6px">{{ t('ai.orSendToExisting') }}</div>
          <div class="existing-row">
            <el-select
              v-model="sendToExistingChatId"
              size="small"
              filterable
              clearable
              :placeholder="t('ai.selectExistingChatPlaceholder')"
              style="flex: 1; min-width: 200px"
            >
              <el-option
                v-for="c in chats"
                :key="c.id"
                :label="c.title"
                :value="c.id"
              />
            </el-select>
            <el-button size="small" :disabled="!sendToExistingChatId" @click="sendToExistingChat">
              {{ t('ai.send') }}
            </el-button>
          </div>
        </div>
      </div>

      <!-- 顶部信息 -->
      <div v-if="currentChat" class="chat-header">
        <div class="chat-header-icon icon-badge icon-badge-purple">
          <el-icon><MagicStick /></el-icon>
        </div>
        <div class="chat-header-info">
          <span class="chat-title">{{ currentChat.title }}</span>
          <span class="chat-header-meta">
            <span v-if="currentChat.flow_ids.length" class="meta-chip meta-chip-purple">
              <el-icon><Connection /></el-icon>
              {{ t('ai.flowsCount', { n: currentChat.flow_ids.length }) }}
            </span>
            <span v-else class="meta-chip meta-chip-neutral">
              <el-icon><ChatDotRound /></el-icon>
              {{ t('ai.freeChat') }}
            </span>
            <span class="meta-chip meta-chip-neutral">
              <el-icon><Clock /></el-icon>
              {{ formatTime(currentChat.updated_at) }}
            </span>
          </span>
        </div>
        <div class="chat-header-actions">
          <!-- 使用统计下拉 -->
          <el-dropdown trigger="click" @command="handleUsageDropdown">
            <el-button size="small" :title="t('ai.usageStats')">
              <el-icon><DataLine /></el-icon>
              <span class="usage-total">{{ formatCost(aiUsage.all_time.total_cost) }}</span>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu style="width: 320px; padding: 12px">
                <div class="usage-dropdown">
                  <div class="usage-section">
                    <div class="usage-section-title">
                      <el-icon><Coin /></el-icon>
                      {{ t('ai.todayUsage') }}
                    </div>
                    <div class="usage-stat-row">
                      <span class="usage-stat-label">{{ t('ai.requests') }}</span>
                      <span class="usage-stat-value">{{ aiUsage.today.requests }}</span>
                    </div>
                    <div class="usage-stat-row">
                      <span class="usage-stat-label">{{ t('ai.inputTokens') }}</span>
                      <span class="usage-stat-value">{{ aiUsage.today.input_tokens.toLocaleString() }}</span>
                    </div>
                    <div class="usage-stat-row">
                      <span class="usage-stat-label">{{ t('ai.outputTokens') }}</span>
                      <span class="usage-stat-value">{{ aiUsage.today.output_tokens.toLocaleString() }}</span>
                    </div>
                    <div class="usage-stat-row">
                      <span class="usage-stat-label">{{ t('ai.cost') }}</span>
                      <span class="usage-stat-value">{{ formatCost(aiUsage.today.total_cost) }}</span>
                    </div>
                  </div>
                  <div class="usage-section">
                    <div class="usage-section-title">
                      <el-icon><Calendar /></el-icon>
                      {{ t('ai.monthUsage') }}
                    </div>
                    <div class="usage-stat-row">
                      <span class="usage-stat-label">{{ t('ai.requests') }}</span>
                      <span class="usage-stat-value">{{ aiUsage.month.requests }}</span>
                    </div>
                    <div class="usage-stat-row">
                      <span class="usage-stat-label">{{ t('ai.cost') }}</span>
                      <span class="usage-stat-value">{{ formatCost(aiUsage.month.total_cost) }}</span>
                    </div>
                  </div>
                  <div class="usage-total-card">
                    <div class="usage-total-amount">{{ formatCost(aiUsage.all_time.total_cost) }}</div>
                    <div class="usage-total-label">{{ t('ai.totalCost') }}</div>
                  </div>
                </div>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button
            size="small"
            :class="{ 'panel-toggle-active': contextPanelVisible }"
            @click="contextPanelVisible = !contextPanelVisible"
            v-if="chatFlows.length"
            :title="t('ai.togglePacketPanel')"
          >
            <el-icon><Document /></el-icon>&nbsp;{{ t('ai.packetInfo') }}
          </el-button>
        </div>
      </div>
      <div v-else-if="!pendingFlows.length" class="chat-header">
        <div class="chat-header-icon icon-badge icon-badge-purple">
          <el-icon><MagicStick /></el-icon>
        </div>
        <!-- 模型选择器 -->
        <el-select v-model="settings.ai_service" size="small" style="width: 130px" @change="onModelChange" :placeholder="t('ai.selectModel')">
          <el-option value="claude" label="Claude" />
          <el-option value="openai" label="GPT-4o" />
          <el-option value="gemini" label="Gemini" />
          <el-option value="ollama" label="Ollama" />
        </el-select>
        <span class="text-dim" style="margin-left: 12px">{{ t('ai.selectRecordOrStart') }}</span>
        <div class="chat-header-actions">
          <el-button type="primary" size="small" :loading="analyzing" @click="startFreeChat">
            <el-icon><ChatDotRound /></el-icon>&nbsp;{{ t('ai.freeChat') }}
          </el-button>
        </div>
      </div>

      <!-- 消息列表 -->
      <div ref="chatBodyRef" class="chat-body flex-1 overflow-auto">
        <div v-if="!currentChat && !pendingFlows.length" class="empty-chat">
          <div class="empty-chat-icon icon-badge icon-badge-purple" style="width: 56px; height: 56px;">
            <el-icon :size="28"><ChatDotRound /></el-icon>
          </div>
          <div class="empty-chat-title">{{ t('ai.aiAssistantTitle') }}</div>
          <p class="text-dim">{{ t('ai.autoJumpHint') }}</p>
          <p class="text-dim">{{ t('ai.orClickFreeChat') }}</p>
          <div class="empty-chat-hint">
            <div class="hint-row">
              <el-icon class="hint-icon"><Cpu /></el-icon>
              <span>{{ t('ai.hintAnalyzeFields') }}</span>
            </div>
            <div class="hint-row">
              <el-icon class="hint-icon"><SetUp /></el-icon>
              <span>{{ t('ai.hintSetAutoModify') }}</span>
            </div>
            <div class="hint-row">
              <el-icon class="hint-icon"><Delete /></el-icon>
              <span>{{ t('ai.hintManageRules') }}</span>
            </div>
          </div>
        </div>
        <div
          v-for="m in messages"
          :key="m.id"
          class="msg-row"
          :class="m.role === 'user' ? 'msg-user' : 'msg-ai'"
        >
          <div v-if="m.role === 'assistant'" class="msg-avatar avatar-ai">
            <el-icon><MagicStick /></el-icon>
          </div>
          <div class="msg-content">
            <div v-if="m.role === 'user'" class="msg-text">{{ m.content }}</div>
            <div v-else class="markdown-body" v-html="renderMarkdown(m.content)"></div>
          </div>
        </div>
        <div v-if="chatting" class="msg-row msg-ai">
          <div class="msg-avatar avatar-ai">
            <el-icon class="is-loading"><Loading /></el-icon>
          </div>
          <div class="msg-content">
            <div class="msg-thinking">
              <span class="thinking-dot"></span>
              <span class="thinking-dot"></span>
              <span class="thinking-dot"></span>
              <span class="text-dim" style="margin-left: 6px">{{ t('ai.thinking') }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 输入框 -->
      <div v-if="currentChat" class="chat-input-bar">
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="2"
          :placeholder="t('ai.inputPlaceholder')"
          @keydown="onKeydown"
          :disabled="chatting"
        />
        <el-button type="primary" @click="sendMessage" :loading="chatting" :disabled="!inputText.trim()" class="send-btn">
          <el-icon><Promotion /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- 右侧：包信息面板（仅在当前对话有关联流量时显示） -->
    <div v-if="currentChat && chatFlows.length && contextPanelVisible" class="context-panel">
      <div class="context-header">
        <div class="context-title">
          <el-icon class="context-title-icon"><Document /></el-icon>
          <span>{{ t('ai.packetInfo') }}</span>
          <span class="context-count">{{ chatFlows.length }}</span>
        </div>
        <el-button link size="small" @click="contextPanelVisible = false" :title="t('ai.collapse')">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>
      <div class="context-body flex-1 overflow-auto">
        <div v-if="chatFlowsLoading" class="context-loading">
          <el-icon class="is-loading"><Loading /></el-icon>
          <span style="margin-left: 6px">{{ t('ai.loadingFlowDetails') }}</span>
        </div>
        <div
          v-for="(f, idx) in chatFlows"
          :key="f.id"
          class="flow-card"
          :class="{ expanded: expandedFlowId === f.id }"
        >
          <div class="flow-card-head" @click="toggleFlowExpand(f.id)">
            <span class="flow-card-index" :style="{ background: `var(--on-gradient-${['accent','indigo','purple','rose','blue','emerald','amber','cyan'][idx % 8]})` }">{{ idx + 1 }}</span>
            <span :class="['method-badge', methodClass(f.method)]">{{ f.method }}</span>
            <span v-if="f.status_code" :class="['flow-status', statusClass(f.status_code)]">{{ f.status_code }}</span>
            <span class="flow-card-host mono">{{ f.host || '-' }}</span>
            <el-icon class="flow-card-arrow"><ArrowDown /></el-icon>
          </div>
          <div v-if="expandedFlowId === f.id" class="flow-card-body">
            <!-- 摘要信息 -->
            <div class="flow-section">
              <div class="flow-section-title">{{ t('ai.summary') }}</div>
              <div class="flow-kv-grid">
                <div class="flow-kv">
                  <span class="kv-key">ID</span>
                  <span class="kv-val mono">#{{ f.id }}</span>
                </div>
                <div class="flow-kv">
                  <span class="kv-key">{{ t('ai.protocol') }}</span>
                  <span class="kv-val">{{ f.protocol || f.scheme || '-' }}<span v-if="f.http_version" class="kv-sub"> · {{ f.http_version }}</span></span>
                </div>
                <div class="flow-kv">
                  <span class="kv-key">{{ t('ai.duration') }}</span>
                  <span class="kv-val">{{ f.duration_ms != null ? f.duration_ms + ' ms' : '-' }}</span>
                </div>
                <div class="flow-kv">
                  <span class="kv-key">{{ t('ai.size') }}</span>
                  <span class="kv-val">{{ formatSize(f.size) }}</span>
                </div>
                <div v-if="f.process_name" class="flow-kv">
                  <span class="kv-key">{{ t('ai.process') }}</span>
                  <span class="kv-val">{{ f.process_name }}<span v-if="f.pid" class="kv-sub"> · PID {{ f.pid }}</span></span>
                </div>
                <div v-if="f.ip_region" class="flow-kv">
                  <span class="kv-key">{{ t('ai.region') }}</span>
                  <span class="kv-val">{{ f.ip_region }}</span>
                </div>
                <div v-if="f.remote_ip" class="flow-kv">
                  <span class="kv-key">{{ t('ai.remoteIp') }}</span>
                  <span class="kv-val mono">{{ f.remote_ip }}<span v-if="f.dst_port" class="kv-sub">:{{ f.dst_port }}</span></span>
                </div>
                <div v-if="contentType(f)" class="flow-kv">
                  <span class="kv-key">{{ t('ai.contentType') }}</span>
                  <span class="kv-val">{{ contentType(f) }}</span>
                </div>
              </div>
            </div>

            <!-- URL -->
            <div class="flow-section">
              <div class="flow-section-title">URL</div>
              <div class="flow-url mono">{{ f.url }}</div>
            </div>

            <!-- 请求头 -->
            <div v-if="parseHeaders(f.request_headers).length" class="flow-section">
              <div class="flow-section-title">
                {{ t('ai.requestHeaders') }}
                <span class="flow-section-count">{{ parseHeaders(f.request_headers).length }}</span>
              </div>
              <div class="flow-headers">
                <div v-for="h in parseHeaders(f.request_headers)" :key="h.key" class="flow-header-row">
                  <span class="flow-header-key mono">{{ h.key }}</span>
                  <span class="flow-header-val mono">{{ h.value }}</span>
                </div>
              </div>
            </div>

            <!-- 请求体大小 -->
            <div v-if="f.request_body" class="flow-section">
              <div class="flow-section-title">
                {{ t('ai.requestBody') }}
                <span class="flow-section-count">{{ formatSize(bodySize(f.request_body)) }}</span>
              </div>
              <div class="flow-body mono">{{ f.request_body.slice(0, 500) }}<span v-if="f.request_body.length > 500" class="body-trunc">…</span></div>
            </div>

            <!-- 响应头 -->
            <div v-if="parseHeaders(f.response_headers).length" class="flow-section">
              <div class="flow-section-title">
                {{ t('ai.responseHeaders') }}
                <span class="flow-section-count">{{ parseHeaders(f.response_headers).length }}</span>
              </div>
              <div class="flow-headers">
                <div v-for="h in parseHeaders(f.response_headers)" :key="h.key" class="flow-header-row">
                  <span class="flow-header-key mono">{{ h.key }}</span>
                  <span class="flow-header-val mono">{{ h.value }}</span>
                </div>
              </div>
            </div>

            <!-- 响应体大小 -->
            <div v-if="f.response_body" class="flow-section">
              <div class="flow-section-title">
                {{ t('ai.responseBody') }}
                <span class="flow-section-count">{{ formatSize(bodySize(f.response_body)) }}</span>
              </div>
              <div class="flow-body mono">{{ f.response_body.slice(0, 500) }}<span v-if="f.response_body.length > 500" class="body-trunc">…</span></div>
            </div>

            <!-- 操作 -->
            <div class="flow-actions">
              <el-button size="small" @click="quoteFlowInInput(f)" :title="t('ai.quoteFlowTitle')">
                <el-icon><ChatLineSquare /></el-icon>&nbsp;{{ t('ai.quote') }}
              </el-button>
              <el-button size="small" @click="viewInCapture(f.id)" :title="t('ai.viewInCaptureTitle')">
                <el-icon><Aim /></el-icon>&nbsp;{{ t('ai.view') }}
              </el-button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ai-view {
  background: var(--on-bg);
  background-image:
    radial-gradient(circle at 0% 0%, var(--on-purple-glow) 0%, transparent 35%),
    radial-gradient(circle at 100% 100%, var(--on-indigo-glow) 0%, transparent 35%),
    radial-gradient(circle at 50% 100%, rgba(255,255,255,0.015) 0%, transparent 50%);
  background-attachment: fixed;
}

/* ============ 左侧 ============ */
.chat-sidebar {
  width: 260px;
  border-right: 1px solid var(--on-border-light);
  display: flex;
  flex-direction: column;
  background: var(--on-bg-elevated);
  position: relative;
  box-shadow: inset -1px 0 0 rgba(255,255,255,0.02), 4px 0 16px rgba(0, 0, 0, 0.03);
}
.chat-sidebar::after {
  content: '';
  position: absolute;
  top: 0;
  right: -1px;
  bottom: 0;
  width: 1px;
  background: linear-gradient(
    180deg,
    transparent 0%,
    var(--on-purple-glow) 12%,
    var(--on-border-light) 40%,
    var(--on-border-light) 60%,
    var(--on-purple-glow) 88%,
    transparent 100%
  );
  opacity: 0.5;
}
.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--on-border-light);
  gap: 8px;
  background: linear-gradient(180deg, rgba(255,255,255,0.025) 0%, transparent 100%);
}
.sidebar-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.sidebar-title {
  font-size: 13px; font-weight: 700;
  background: var(--on-gradient-purple);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; color: transparent;
  display: inline-flex; align-items: center; gap: 6px;
  letter-spacing: .3px;
}
.sidebar-title-icon { color: var(--on-purple); -webkit-text-fill-color: var(--on-purple); }
.new-chat-btn { flex-shrink: 0; }

.chat-list { padding: 6px; }
.chat-item {
  position: relative; padding: 10px 12px; cursor: pointer;
  border-radius: var(--on-radius-md); margin-bottom: 3px;
  transition: all .18s cubic-bezier(0.4, 0, 0.2, 1);
  border: 1px solid transparent;
}
.chat-item:hover {
  background: var(--on-bg-hover);
  transform: translateX(2px);
}
.chat-item.active {
  background: var(--on-purple-glow);
  border-color: var(--on-purple-glow);
  box-shadow: inset 3px 0 0 var(--on-purple), 0 2px 8px var(--on-purple-glow);
}
.chat-item.active .chat-item-title { color: var(--on-purple); font-weight: 600; }
.chat-item-main { padding-right: 48px; }
.chat-item-title {
  font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--on-text); transition: color .18s;
}
.chat-item-meta {
  display: flex; align-items: center; gap: 8px; margin-top: 4px;
}
.chat-item-time { font-size: 11px; }
.chat-item-flows {
  display: inline-flex; align-items: center; gap: 3px;
  font-size: 10.5px; padding: 1px 6px; border-radius: var(--on-radius-full);
  background: var(--on-purple-glow); color: var(--on-purple);
  font-weight: 600;
}
.chat-item-free {
  font-size: 10.5px; padding: 1px 6px; border-radius: var(--on-radius-full);
  background: var(--on-bg-hover); color: var(--on-text-dim);
}
.chat-item-actions {
  position: absolute; top: 8px; right: 6px;
  display: flex; gap: 2px; opacity: 0;
  transition: opacity .15s;
}
.chat-item:hover .chat-item-actions { opacity: 1; }

.empty-text {
  text-align: center; padding: 36px 12px; font-size: 12.5px; line-height: 1.8;
  display: flex; flex-direction: column; align-items: center; gap: 6px;
}
.empty-icon { color: var(--on-purple); margin-bottom: 8px; opacity: 0.6; }
.empty-hint { font-size: 11px; color: var(--on-text-dim); }

/* ============ 中间聊天区 ============ */
.chat-main {
  min-width: 0;
  background: var(--on-bg);
  position: relative;
}

/* 待分析面板 */
.pending-panel {
  padding: 18px 20px; border-bottom: 1px solid var(--on-border-light);
  background: linear-gradient(180deg, var(--on-bg-elevated) 0%, var(--on-bg) 100%);
}
.pending-header {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 14px;
}
.pending-header-icon {
  width: 40px; height: 40px; border-radius: var(--on-radius-lg);
  background: var(--on-gradient-purple);
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 20px;
  box-shadow: 0 4px 14px var(--on-purple-glow);
  flex-shrink: 0;
}
.pending-header-title { font-size: 15px; font-weight: 700; color: var(--on-text); }
.pending-header-sub { font-size: 12px; margin-top: 2px; }
.pending-flows {
  max-height: 240px; overflow-y: auto; margin-bottom: 14px;
  border: 1px solid var(--on-border-light); border-radius: var(--on-radius-md);
  background: var(--on-bg);
}
.pending-flow-item {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  font-size: 12px;
  transition: background .15s;
}
.pending-flow-item:hover { background: var(--on-bg-hover); }
.pending-flow-item:last-child { border-bottom: none; }
.flow-id { font-size: 11px; min-width: 36px; }
.flow-status {
  font-size: 11px; font-weight: 700; padding: 1px 6px;
  border-radius: var(--on-radius-sm); min-width: 32px; text-align: center;
}
.pending-flow-url {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
  color: var(--on-text-muted); font-family: var(--on-font-mono); font-size: 11.5px;
}
.pending-actions {
  display: flex; gap: 10px; margin-bottom: 0;
}
.pending-existing {
  margin-top: 12px; padding-top: 12px;
  border-top: 1px dashed var(--on-border-light);
}
.existing-row { display: flex; gap: 8px; align-items: center; }

/* 顶部信息 */
.chat-header {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 18px; border-bottom: 1px solid var(--on-border-light);
  background: linear-gradient(180deg, var(--on-bg-elevated) 0%, var(--on-bg) 100%);
}
.chat-header-icon { width: 36px; height: 36px; font-size: 18px; }
.chat-header-info {
  display: flex; flex-direction: column; gap: 4px;
  flex: 1; min-width: 0;
}
.chat-title {
  font-size: 14px; font-weight: 700; color: var(--on-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.chat-header-meta { display: flex; align-items: center; gap: 6px; }
.meta-chip {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; padding: 2px 8px; border-radius: var(--on-radius-full);
  font-weight: 500;
}
.meta-chip-purple {
  background: var(--on-purple-glow); color: var(--on-purple);
}
.meta-chip-neutral {
  background: var(--on-bg-hover); color: var(--on-text-muted);
}
.chat-header-actions { margin-left: auto; display: flex; gap: 8px; }
.panel-toggle-active {
  border-color: var(--on-purple) !important;
  color: var(--on-purple) !important;
  background: var(--on-purple-glow) !important;
}
.usage-total {
  margin-left: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--on-accent);
}
.el-dropdown-menu {
  padding: 0 !important;
}
.usage-dropdown {
  min-width: 300px;
  padding: 12px;
}
.usage-section {
  margin-bottom: 12px;
}
.usage-section:last-child {
  margin-bottom: 0;
}
.usage-section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--on-text);
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.usage-stat-row {
  display: flex;
  justify-content: space-between;
  padding: 4px 0;
  font-size: 12px;
}
.usage-stat-label {
  color: var(--on-text-dim);
}
.usage-stat-value {
  color: var(--on-text);
  font-weight: 500;
  font-family: var(--on-font-mono);
}
.usage-total-card {
  background: var(--on-bg-hover);
  border-radius: var(--on-radius-md);
  padding: 12px;
  margin-top: 8px;
  text-align: center;
}
.usage-total-amount {
  font-size: 24px;
  font-weight: 700;
  color: var(--on-accent);
  font-family: var(--on-font-mono);
}
.usage-total-label {
  font-size: 11px;
  color: var(--on-text-dim);
  margin-top: 4px;
}
.panel-toggle-active {
  border-color: var(--on-purple) !important;
  color: var(--on-purple) !important;
  background: var(--on-purple-glow) !important;
}

/* 消息列表 */
.chat-body { padding: 20px 24px; }
.empty-chat {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  height: 100%; gap: 8px; text-align: center;
  padding: 40px 20px;
}
.empty-chat-icon {
  margin-bottom: 8px;
  animation: empty-float 3.6s ease-in-out infinite;
}
@keyframes empty-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-6px); }
}
.empty-chat-title {
  font-size: 18px; font-weight: 700;
  background: var(--on-gradient-purple);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; color: transparent;
  margin-bottom: 6px;
}
.empty-chat-hint {
  margin-top: 16px; padding: 14px 18px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-lg);
  display: flex; flex-direction: column; gap: 8px;
  max-width: 420px;
  text-align: left;
}
.hint-row {
  display: flex; align-items: center; gap: 10px;
  font-size: 12.5px; color: var(--on-text-muted);
}
.hint-icon { color: var(--on-purple); font-size: 14px; flex-shrink: 0; }

/* 消息行：用户右对齐，AI 左对齐（参考 __ai_ref/pages/AI 分析.html）*/
.msg-row {
  display: flex; gap: 12px; margin-bottom: 20px;
  animation: msg-in .3s cubic-bezier(0.4, 0, 0.2, 1);
}
.msg-user { flex-direction: row-reverse; }
@keyframes msg-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
.msg-avatar {
  width: 28px; height: 28px; border-radius: var(--on-radius-full);
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; flex-shrink: 0; margin-top: 2px;
}
.avatar-ai {
  background: linear-gradient(135deg, var(--on-accent), var(--on-purple));
  color: #fff;
  box-shadow: 0 2px 8px var(--on-purple-glow);
}
/* msg-content 不设 flex:1，让 max-width 生效 */
.msg-content { min-width: 0; }
.msg-user .msg-content { display: flex; justify-content: flex-end; max-width: 85%; }

/* 用户消息：蓝色气泡，右对齐，右下小圆角 */
/* width: fit-content 让短文本不撑满，max-width 限制超长文本换行 */
.msg-text {
  background: var(--on-accent); color: #fff;
  padding: 12px 16px;
  border-radius: var(--on-radius-xl) var(--on-radius-xl) var(--on-radius-sm) var(--on-radius-xl);
  font-size: 14px; line-height: 1.5;
  width: fit-content; max-width: 100%;
  word-break: break-word; white-space: pre-wrap;
  flex: 0 0 auto;
  font-family: var(--on-font-sans), 'Apple Color Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol', 'Noto Color Emoji', sans-serif;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
}
.msg-thinking {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 12px 16px;
  background: var(--on-bg-elevated);
  border-radius: var(--on-radius-xl) var(--on-radius-xl) var(--on-radius-xl) var(--on-radius-sm);
  border: 1px solid var(--on-border-light);
  max-width: 75%;
}
.thinking-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--on-purple);
  animation: thinking-bounce 1.4s ease-in-out infinite;
}
.thinking-dot:nth-child(2) { animation-delay: 0.2s; }
.thinking-dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes thinking-bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
  30% { transform: translateY(-4px); opacity: 1; }
}

/* AI 消息：surface 卡片，左对齐，右下小圆角，带 padding 防止贴边 */
/* width: fit-content 让短回复不撑满，max-width 限制超长内容换行 */
.markdown-body {
  font-size: 14px; line-height: 1.625; color: var(--on-text);
  background: var(--on-bg-elevated);
  padding: 16px;
  border-radius: var(--on-radius-xl) var(--on-radius-xl) var(--on-radius-xl) var(--on-radius-sm);
  border: 1px solid var(--on-border-light);
  width: fit-content; max-width: 75%; word-break: break-word;
  font-family: var(--on-font-sans), 'Apple Color Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol', 'Noto Color Emoji', sans-serif;
}
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3) {
  color: var(--on-text); font-weight: 600;
  margin: 16px 0 10px;
}
.markdown-body :deep(h1) { font-size: 18px; }
.markdown-body :deep(h2) { font-size: 16px; }
.markdown-body :deep(h3) { font-size: 14.5px; }
.markdown-body :deep(p) { margin: 8px 0; }
.markdown-body :deep(p:first-child) { margin-top: 0; }
.markdown-body :deep(p:last-child) { margin-bottom: 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 22px; margin: 8px 0; }
.markdown-body :deep(li) { margin: 4px 0; }
.markdown-body :deep(code) {
  font-family: var(--on-font-mono); background: var(--on-bg);
  padding: 2px 6px; border-radius: var(--on-radius-sm); font-size: 13px;
  color: var(--on-purple); border: 1px solid var(--on-border-light);
}
.markdown-body :deep(pre) {
  background: var(--on-bg) !important;
  border-radius: var(--on-radius-md); padding: 14px 16px; overflow: auto;
  border: 1px solid var(--on-border-light);
  margin: 10px 0;
}
.markdown-body :deep(pre code) {
  background: transparent; border: none; padding: 0; color: var(--on-text);
  font-size: 13px;
}
.markdown-body :deep(table) {
  border-collapse: collapse; width: 100%; margin: 10px 0;
  border-radius: var(--on-radius-sm); overflow: hidden;
}
.markdown-body :deep(th) {
  background: var(--on-bg-hover); font-weight: 600;
}
.markdown-body :deep(th), .markdown-body :deep(td) {
  border: 1px solid var(--on-border); padding: 8px 12px; font-size: 13px;
}
.markdown-body :deep(blockquote) {
  border-left: 3px solid var(--on-purple);
  padding: 4px 12px; margin: 8px 0;
  background: var(--on-purple-glow);
  border-radius: 0 var(--on-radius-sm) var(--on-radius-sm) 0;
  color: var(--on-text-muted);
}
.markdown-body :deep(a) { color: var(--on-purple); text-decoration: none; }
.markdown-body :deep(a:hover) { text-decoration: underline; }
.markdown-body :deep(strong) { color: var(--on-text); font-weight: 700; }

/* 输入栏：圆角胶囊样式 */
.chat-input-bar {
  display: flex; gap: 10px; align-items: flex-end;
  padding: 12px 18px; border-top: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.chat-input-bar .el-input { flex: 1; }
.send-btn {
  height: 56px !important;
  padding: 0 18px !important;
  font-size: 18px !important;
}

/* ============ 右侧包信息面板 ============ */
.context-panel {
  width: 340px; flex-shrink: 0;
  border-left: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
  display: flex; flex-direction: column;
  position: relative;
}
.context-panel::before {
  content: ''; position: absolute; top: 0; left: 0; bottom: 0; width: 1px;
  background: linear-gradient(180deg, transparent 0%, var(--on-purple-glow) 30%, var(--on-purple-glow) 70%, transparent 100%);
}
.context-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.context-title {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; font-weight: 700;
  color: var(--on-text);
}
.context-title-icon { color: var(--on-purple); font-size: 16px; }
.context-count {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; height: 18px; padding: 0 6px;
  border-radius: var(--on-radius-full);
  background: var(--on-purple-glow); color: var(--on-purple);
  font-size: 11px; font-weight: 700;
}
.context-body { padding: 8px; }
.context-loading {
  padding: 24px 12px; display: flex; align-items: center; justify-content: center;
  color: var(--on-text-muted); font-size: 12px;
}

.flow-card {
  margin-bottom: 6px;
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-md);
  background: var(--on-bg);
  overflow: hidden;
  transition: all .18s;
}
.flow-card:hover { border-color: var(--on-purple-glow); }
.flow-card.expanded {
  border-color: var(--on-purple);
  box-shadow: 0 0 0 1px var(--on-purple-glow), var(--on-shadow-sm);
}
.flow-card-head {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; cursor: pointer;
  transition: background .15s;
}
.flow-card-head:hover { background: var(--on-bg-hover); }
.flow-card-index {
  width: 22px; height: 22px; border-radius: var(--on-radius-full);
  display: inline-flex; align-items: center; justify-content: center;
  color: #fff; font-size: 11px; font-weight: 700;
  flex-shrink: 0;
  box-shadow: 0 2px 4px rgba(0,0,0,0.15);
}
.flow-card-host {
  flex: 1; min-width: 0;
  font-size: 11.5px; color: var(--on-text-muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.flow-card-arrow {
  font-size: 12px; color: var(--on-text-dim);
  transition: transform .18s;
}
.flow-card.expanded .flow-card-arrow { transform: rotate(180deg); }

.flow-card-body { padding: 12px; border-top: 1px solid var(--on-border-light); }

.flow-section { margin-bottom: 14px; }
.flow-section:last-child { margin-bottom: 0; }
.flow-section-title {
  font-size: 11px; font-weight: 700; color: var(--on-text-muted);
  letter-spacing: .5px; text-transform: uppercase;
  margin-bottom: 6px;
  display: flex; align-items: center; gap: 6px;
}
.flow-section-count {
  font-weight: 500; text-transform: none; letter-spacing: 0;
  padding: 0 6px; height: 16px; line-height: 16px;
  border-radius: var(--on-radius-full);
  background: var(--on-bg-hover); color: var(--on-text-dim);
  font-size: 10px;
}

.flow-kv-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
}
.flow-kv {
  display: flex; flex-direction: column; gap: 1px;
  padding: 6px 8px;
  background: var(--on-bg-elevated);
  border-radius: var(--on-radius-sm);
  border: 1px solid var(--on-border-light);
}
.kv-key {
  font-size: 10px; color: var(--on-text-dim);
  text-transform: uppercase; letter-spacing: .3px;
}
.kv-val {
  font-size: 11.5px; color: var(--on-text); font-weight: 500;
  word-break: break-all;
}
.kv-sub { color: var(--on-text-dim); font-weight: 400; font-size: 10.5px; }

.flow-url {
  font-size: 11px; color: var(--on-text-muted);
  padding: 6px 8px; background: var(--on-bg-elevated);
  border-radius: var(--on-radius-sm);
  border: 1px solid var(--on-border-light);
  word-break: break-all;
  max-height: 80px; overflow-y: auto;
}

.flow-headers {
  display: flex; flex-direction: column; gap: 2px;
  max-height: 160px; overflow-y: auto;
}
.flow-header-row {
  display: grid; grid-template-columns: 130px 1fr; gap: 8px;
  padding: 4px 8px; border-radius: var(--on-radius-sm);
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  font-size: 11px;
  transition: background .12s;
}
.flow-header-row:hover { background: var(--on-bg-hover); }
.flow-header-key {
  color: var(--on-purple); font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.flow-header-val {
  color: var(--on-text-muted);
  word-break: break-all;
}

.flow-body {
  font-size: 11px; color: var(--on-text-muted);
  padding: 8px 10px; background: var(--on-bg-elevated);
  border-radius: var(--on-radius-sm);
  border: 1px solid var(--on-border-light);
  max-height: 140px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-all;
  line-height: 1.5;
}
.body-trunc { color: var(--on-text-dim); font-weight: 700; }

.flow-actions {
  display: flex; gap: 6px; padding-top: 10px;
  border-top: 1px dashed var(--on-border-light);
  margin-top: 6px;
}
.flow-actions .el-button { flex: 1; }
</style>
