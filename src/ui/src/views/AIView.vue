<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useFlowsStore } from '../stores/flows'
import { api, type AiChat, type AiMessage, type Flow, type Settings } from '../api/client'

// AI 分析视图：左侧聊天记录列表 + 右侧聊天界面
const flowsStore = useFlowsStore()

const chats = ref<AiChat[]>([])
const currentChat = ref<AiChat | null>(null)
const messages = ref<AiMessage[]>([])
const inputText = ref('')
const analyzing = ref(false)
const chatting = ref(false)
const settings = ref<Settings>({})
const targetFlows = ref<Flow[]>([])
const chatBodyRef = ref<HTMLElement | null>(null)

const md = new MarkdownIt({
  html: true,
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
      list.push(await api.getFlow(id))
    } catch {
      /* skip */
    }
  }
  pendingFlows.value = list
}

async function startAnalyze() {
  if (!settings.value.deepseek_api_key) {
    ElMessage.warning('请先在设置页配置 DeepSeek API Key')
    return
  }
  analyzing.value = true
  // 立即提示，避免像卡死
  const waitMsg = ElMessage({
    message: '已发送请求，等待响应...',
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
    ElMessage.success('分析完成')
  } catch (e: any) {
    waitMsg?.close()
    const msg = e?.message || String(e)
    if (msg === 'not found') {
      ElMessage.error('后端未找到 AI 分析接口，请重启后端（python -m opennet）后再试')
    } else {
      ElMessage.error('分析失败：' + msg)
    }
  } finally {
    analyzing.value = false
  }
}

// 无流量直接开始自由对话（不调 API，只创建空聊天记录，等用户先发消息）
async function startFreeChat() {
  if (!settings.value.deepseek_api_key) {
    ElMessage.warning('请先在设置页配置 DeepSeek API Key')
    return
  }
  analyzing.value = true
  try {
    const res = await api.aiAnalyze({ flow_ids: [] })
    await loadChats()
    await openChat(res.chat_id)
    ElMessage.success('已创建自由对话，请直接输入问题')
  } catch (e: any) {
    const msg = e?.message || String(e)
    if (msg === 'not found') {
      ElMessage.error('后端未找到 AI 分析接口，请重启后端（python -m opennet）后再试')
    } else {
      ElMessage.error('创建失败：' + msg)
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
  } catch (e: any) {
    ElMessage.error('加载失败：' + (e?.message || e))
  }
}

// 发送到已有对话：打开选中的 chat，并在输入框预填引用待分析流量的消息
const sendToExistingChatId = ref<number | null>(null)
async function sendToExistingChat() {
  if (!sendToExistingChatId.value) {
    ElMessage.warning('请先选择一个已有对话')
    return
  }
  const ids = pendingFlows.value.map(f => f.id)
  // 打开该对话
  await openChat(sendToExistingChatId.value)
  // 在输入框预填引用待分析流量的消息，用户可编辑后发送
  if (ids.length) {
    inputText.value = `请分析以下流量：${ids.map(id => `#${id}`).join(' ')}\n`
  }
  sendToExistingChatId.value = null
  ElMessage.success('已打开对话并预填流量引用，编辑后发送即可')
}

async function sendMessage() {
  if (!currentChat.value || !inputText.value.trim()) return
  const msg = inputText.value.trim()
  inputText.value = ''
  messages.value.push({
    id: -Date.now(),
    chat_id: currentChat.value.id,
    role: 'user',
    content: msg,
    created_at: new Date().toISOString(),
  })
  await nextTick()
  scrollToBottom()

  chatting.value = true
  try {
    const res = await api.aiChat({ chat_id: currentChat.value.id, message: msg })
    messages.value.push({
      id: -Date.now() - 1,
      chat_id: currentChat.value.id,
      role: 'assistant',
      content: res.result,
      created_at: new Date().toISOString(),
    })
    await nextTick()
    scrollToBottom()
  } catch (e: any) {
    ElMessage.error('回复失败：' + (e?.message || e))
    messages.value = messages.value.filter((m) => m.content !== msg || m.role !== 'user')
    inputText.value = msg
  } finally {
    chatting.value = false
  }
}

function scrollToBottom() {
  const el = chatBodyRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function deleteChat(chatId: number) {
  try {
    await ElMessageBox.confirm('确定删除这条分析记录？', '删除', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.aiDeleteChat(chatId)
    if (currentChat.value?.id === chatId) {
      currentChat.value = null
      messages.value = []
    }
    await loadChats()
    ElMessage.success('已删除')
  } catch (e: any) {
    ElMessage.error('删除失败：' + (e?.message || e))
  }
}

// 重命名对话
async function renameChat(chatId: number, oldTitle: string) {
  try {
    const { value } = await ElMessageBox.prompt('请输入新的对话名称', '重命名', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValue: oldTitle,
      inputValidator: (v: string) => !!v?.trim() || '名称不能为空',
    })
    const title = value.trim()
    if (title === oldTitle) return
    await api.aiUpdateTitle(chatId, title)
    // 更新本地列表和当前对话
    const c = chats.value.find(x => x.id === chatId)
    if (c) c.title = title
    if (currentChat.value?.id === chatId) currentChat.value.title = title
    ElMessage.success('已重命名')
  } catch (e: any) {
    if (e === 'cancel' || e?.toString?.().includes('cancel')) return
    ElMessage.error('重命名失败：' + (e?.message || e))
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

onMounted(() => {
  loadSettings()
  loadChats()
  loadPendingFlows()
})
watch(() => flowsStore.aiFlowIds, loadPendingFlows)
</script>

<template>
  <div class="ai-view full flex">
    <!-- 左侧：聊天记录列表 -->
    <div class="chat-sidebar">
      <div class="sidebar-header">
        <span class="sidebar-title">分析记录</span>
        <el-button type="primary" size="small" :loading="analyzing" @click="startFreeChat">
          <el-icon><ChatDotRound /></el-icon>&nbsp;自由对话
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
          <div class="chat-item-title" @dblclick.stop="renameChat(c.id, c.title)">{{ c.title }}</div>
          <div class="chat-item-time text-dim">{{ formatTime(c.updated_at) }}</div>
          <div class="chat-item-actions">
            <el-button
              link
              size="small"
              class="chat-item-rename"
              title="重命名"
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
          暂无分析记录
          <br />选中流量后点击「新分析」
        </div>
      </div>
    </div>

    <!-- 右侧：聊天界面 -->
    <div class="chat-main flex-1 flex flex-col">
      <!-- 待分析面板：从抓包页选中流量跳转过来 -->
      <div v-if="pendingFlows.length && !currentChat" class="pending-panel">
        <div class="pending-header">
          <el-icon><MagicStick /></el-icon>
          <span>已选中 {{ pendingFlows.length }} 条流量，点击开始分析</span>
        </div>
        <div class="pending-flows">
          <div v-for="f in pendingFlows" :key="f.id" class="pending-flow-item">
            <span class="mono text-dim">#{{ f.id }}</span>
            <span class="mono">{{ f.method }}</span>
            <span class="pending-flow-url">{{ f.url }}</span>
          </div>
        </div>
        <div class="pending-actions">
          <el-button type="primary" size="small" :loading="analyzing" @click="startAnalyze">
            <el-icon><Cpu /></el-icon>&nbsp;新对话分析
          </el-button>
          <el-button size="small" @click="clearPending">取消</el-button>
        </div>
        <!-- 发送到已有对话：选择一个已有对话，预填引用消息 -->
        <div v-if="chats.length" class="pending-existing">
          <div class="text-dim" style="font-size: 12px; margin-bottom: 4px">或发送到已有对话：</div>
          <div class="existing-row">
            <el-select
              v-model="sendToExistingChatId"
              size="small"
              filterable
              clearable
              placeholder="选择一个已有对话"
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
              发送
            </el-button>
          </div>
        </div>
      </div>

      <!-- 顶部信息 -->
      <div v-if="currentChat" class="chat-header">
        <el-icon><MagicStick /></el-icon>
        <span class="chat-title">{{ currentChat.title }}</span>
        <span class="text-dim" style="margin-left: auto; font-size: 12px">
          流量 ID: {{ currentChat.flow_ids.length ? currentChat.flow_ids.join(', ') : '无（自由对话）' }}
        </span>
      </div>
      <div v-else-if="!pendingFlows.length" class="chat-header">
        <span class="text-dim">请从左侧选择记录，或开始新的分析</span>
        <div style="margin-left: auto; display: flex; gap: 8px">
          <el-button type="primary" size="small" :loading="analyzing" @click="startFreeChat">
            <el-icon><ChatDotRound /></el-icon>&nbsp;自由对话
          </el-button>
        </div>
      </div>

      <!-- 消息列表 -->
      <div ref="chatBodyRef" class="chat-body flex-1 overflow-auto">
        <div v-if="!currentChat && !pendingFlows.length" class="empty-chat text-dim">
          <el-icon :size="40"><ChatDotRound /></el-icon>
          <p>选中流量后自动跳转到这里</p>
          <p>或点击上方「自由对话」直接聊天</p>
          <p style="margin-top: 12px; font-size: 12px">
            提示：你可以对 AI 说「帮我设置自动修改把 xxx 改成 yyy」或「改请求里的 xxx 为 yyy」
          </p>
        </div>
        <div
          v-for="m in messages"
          :key="m.id"
          class="msg-row"
          :class="m.role === 'user' ? 'msg-user' : 'msg-ai'"
        >
          <div class="msg-avatar">
            <el-icon v-if="m.role === 'user'"><User /></el-icon>
            <el-icon v-else><Cpu /></el-icon>
          </div>
          <div class="msg-content">
            <div v-if="m.role === 'user'" class="msg-text">{{ m.content }}</div>
            <div v-else class="markdown-body" v-html="renderMarkdown(m.content)"></div>
          </div>
        </div>
        <div v-if="chatting" class="msg-row msg-ai">
          <div class="msg-avatar"><el-icon class="is-loading"><Loading /></el-icon></div>
          <div class="msg-content"><span class="text-dim">正在思考...</span></div>
        </div>
      </div>

      <!-- 输入框 -->
      <div v-if="currentChat" class="chat-input-bar">
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="2"
          placeholder="输入问题，如「这个 remainingUses 字段是什么意思？」（Enter 发送，Shift+Enter 换行）"
          @keydown="onKeydown"
          :disabled="chatting"
        />
        <el-button type="primary" @click="sendMessage" :loading="chatting" :disabled="!inputText.trim()">
          <el-icon><Promotion /></el-icon>
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ai-view { background: var(--on-bg); }

/* 左侧 */
.chat-sidebar {
  width: 280px; border-right: 1px solid var(--on-border-light);
  display: flex; flex-direction: column; background: var(--on-bg-elevated);
}
.sidebar-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 12px; border-bottom: 1px solid var(--on-border-light);
}
.sidebar-title { font-size: 13px; font-weight: 600; }
.chat-list { padding: 4px; }
.chat-item {
  position: relative; padding: 10px 12px; cursor: pointer;
  border-radius: 4px; margin-bottom: 2px; transition: background 0.15s;
}
.chat-item:hover { background: var(--on-bg-hover); }
.chat-item.active { background: var(--on-accent-glow); border-left: 3px solid var(--on-accent); }
.chat-item-title {
  font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding-right: 48px;
}
.chat-item-time { font-size: 11px; margin-top: 2px; }
.chat-item-actions {
  position: absolute; top: 8px; right: 4px;
  display: flex; gap: 2px; opacity: 0;
}
.chat-item:hover .chat-item-actions { opacity: 1; }

/* 右侧 */
.chat-header {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 16px; border-bottom: 1px solid var(--on-border-light);
  font-size: 14px; font-weight: 600;
}
.chat-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-body { padding: 16px; }
.empty-chat {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  height: 100%; gap: 8px; text-align: center;
}
.msg-row {
  display: flex; gap: 10px; margin-bottom: 16px;
}
.msg-avatar {
  width: 32px; height: 32px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 16px; flex-shrink: 0;
}
.msg-user .msg-avatar { background: var(--on-accent-glow); color: var(--on-accent); }
.msg-ai .msg-avatar { background: rgba(88,166,255,0.15); color: #58a6ff; }
.msg-content {
  flex: 1; min-width: 0;
}
.msg-text {
  background: var(--on-bg-elevated); padding: 10px 14px; border-radius: 8px;
  font-size: 13px; line-height: 1.6; display: inline-block; max-width: 100%;
}
.msg-ai .msg-content { padding-top: 4px; }

.markdown-body {
  font-size: 13.5px; line-height: 1.7; color: var(--on-text);
}
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3) {
  color: var(--on-accent); margin: 16px 0 8px;
}
.markdown-body :deep(code) {
  font-family: 'Consolas', 'Monaco', monospace; background: var(--on-bg);
  padding: 2px 5px; border-radius: 3px; font-size: 12.5px;
}
.markdown-body :deep(pre) {
  background: var(--on-bg) !important; border-radius: 4px; padding: 10px; overflow: auto;
}
.markdown-body :deep(table) { border-collapse: collapse; width: 100%; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid var(--on-border); padding: 6px 10px; }

.chat-input-bar {
  display: flex; gap: 8px; align-items: flex-end;
  padding: 10px 16px; border-top: 1px solid var(--on-border-light);
}
.chat-input-bar .el-input { flex: 1; }

/* 待分析面板 */
.pending-panel {
  padding: 16px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.pending-header {
  display: flex; align-items: center; gap: 8px;
  font-size: 14px; font-weight: 600; margin-bottom: 10px;
}
.pending-flows {
  max-height: 200px; overflow-y: auto; margin-bottom: 12px;
  border: 1px solid var(--on-border-light); border-radius: 4px;
}
.pending-flow-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-bottom: 1px solid var(--on-border-light);
  font-size: 12px;
}
.pending-flow-item:last-child { border-bottom: none; }
.pending-flow-url {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
  color: var(--on-text-muted);
}
.pending-actions { display: flex; gap: 8px; }
.pending-existing { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--on-border-light); }
.existing-row { display: flex; gap: 6px; align-items: center; }

.empty-text { text-align: center; padding: 24px 12px; font-size: 12px; line-height: 1.8; }
</style>
