<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import { api, type ProcessInfo, type Flow, type AutoReplyRule } from '../api/client'
import FlowList from '../components/FlowList.vue'
import Inspector from '../components/Inspector.vue'
import BreakpointBar from '../components/BreakpointBar.vue'
import ReplayDialog from '../components/ReplayDialog.vue'
import ImportButton from '../components/ImportButton.vue'
import RuleEditor from '../components/RuleEditor.vue'

const capture = useCaptureStore()
const flows = useFlowsStore()

const processes = ref<ProcessInfo[]>([])
const enabledTabs = ref<string[]>([])
// 响应切换流量是否自动回到 preview（从设置读取，默认 true）
const autoSwitchPreview = ref(true)
const leftRatio = ref(0.4)
const replayVisible = ref(false)
const flowColumns = ref<string[]>([])
const multiSelectBarDelay = ref(1)
const ruleEditorVisible = ref(false)
const editingRule = ref<AutoReplyRule | null>(null)

let pollTimer: number | null = null

const selectedFlow = computed<Flow | null>(() => flows.selectedFlow)

// 加载进程列表
async function loadProcesses() {
  try {
    processes.value = await api.getProcesses()
  } catch {
    /* ignore */
  }
}

// 加载检查器标签设置：先用 localStorage 立即恢复，再异步从后端刷新
async function loadSettings() {
  // 本地缓存立即恢复（避免切换页面时列延迟显示）
  try {
    const cached = localStorage.getItem('telnix_settings_cache')
    if (cached) {
      const s = JSON.parse(cached)
      if (Array.isArray(s.flow_columns)) flowColumns.value = s.flow_columns
      if (Array.isArray(s.inspector_tabs)) enabledTabs.value = s.inspector_tabs
      if (s.auto_switch_preview !== undefined) autoSwitchPreview.value = s.auto_switch_preview !== false && s.auto_switch_preview !== '0'
      if (s.multi_select_bar_delay !== undefined) {
        multiSelectBarDelay.value = Number(s.multi_select_bar_delay) || 1
      }
      // autoScroll/autoScrollDelay 由 store 自己从 localStorage 持久化，不从后端覆盖
    }
  } catch { /* ignore */ }
  // 异步从后端刷新最新值
  try {
    const s = await api.getSettings()
    enabledTabs.value = s.inspector_tabs || []
    flowColumns.value = s.flow_columns || []
    if (s.auto_switch_preview !== undefined) autoSwitchPreview.value = s.auto_switch_preview !== false && s.auto_switch_preview !== '0'
    if (s.multi_select_bar_delay !== undefined) {
      multiSelectBarDelay.value = Number(s.multi_select_bar_delay) || 1
    }
    // autoScroll/autoScrollDelay 由 store 自己持久化，不从后端覆盖（避免切换页面时被旧后端值重置）
    // 更新本地缓存
    localStorage.setItem('telnix_settings_cache', JSON.stringify(s))
  } catch {
    /* ignore */
  }
}

// 抓包时轮询流量 + 进程列表
// 流量数据持久化在磁盘，开始抓包接着以前的包：
// - 进入页面先 loadAllFlows 加载历史
// - 抓包中用 pollNewFlows 增量拉取新流量插入顶部
let lastProcLoadTime = 0
async function pollFlows() {
  if (capture.status.capturing) {
    // 增量轮询新流量；若 maxFlowId 为 0（无历史），先全量加载
    if (flows.maxFlowId) {
      const n = await flows.pollNewFlows()
      // 有新流量时触发自动滚动（由 FlowList watch flows 处理）
      if (n > 0 && flows.autoScroll && !flows.autoScrollPaused) {
        // autoScroll 逻辑由 FlowList 内部处理
      }
    } else {
      await flows.loadAllFlows()
    }
    // 进程列表每 3 秒刷新一次（后端也有 5 秒缓存），且不阻塞流量刷新
    const now = Date.now()
    if (now - lastProcLoadTime > 3000) {
      lastProcLoadTime = now
      loadProcesses()  // 不 await，避免阻塞流量轮询
    }
  }
}

// 流量变更（删除后刷新）：重新全量加载
async function onFlowsChanged() {
  await flows.loadAllFlows()
}

function startPolling() {
  if (pollTimer !== null) return
  // SSE 推送：新 flow 立即推送到前端，UI 延迟 <50ms（替代 500ms 轮询）
  flows.startSSE()
  // 兜底轮询：5 秒一次，防止 SSE 异常断开未重连时漏掉流量
  // 也用于触发 loadProcesses（进程列表每 3 秒刷新）
  pollTimer = window.setInterval(pollFlows, 5000)
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  flows.stopSSE()
}

async function onToggleCapture() {
  try {
    if (capture.status.capturing) {
      // 停止抓包：fire-and-forget（store 内部已乐观更新 capturing=false）
      capture.toggleCapture().catch((e: any) =>
        ElMessage.error('停止抓包失败：' + (e?.message || e))
      )
      stopPolling()
    } else {
      // 开始抓包：fire-and-forget（store 内部已乐观更新 capturing=true）
      // 不 await，按钮立即响应；轮询立即启动，乐观状态由后续 fetchStatus 校正
      capture.toggleCapture().catch((e: any) =>
        ElMessage.error('开始抓包失败：' + (e?.message || e))
      )
      // 乐观更新已设置 capturing=true，立即启动轮询
      startPolling()
      loadProcesses()
      pollFlows()
    }
  } catch (e: any) {
    ElMessage.error('操作失败：' + (e?.message || e))
  }
}

async function onClear() {
  try {
    await ElMessageBox.confirm(
      '确定清空流量？有活动会话只清当前会话，无活动会话清空全部。',
      '清空',
      { confirmButtonText: '清空', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  try {
    const res = await capture.clearSessions()
    // 清空本地 store 并重置 maxFlowId
    flows.clear()
    ElMessage.success(res?.scope === 'all' ? '已清空全部流量' : '已清空当前会话')
  } catch (e: any) {
    ElMessage.error('清空失败：' + (e?.message || e))
  }
}

function onReplay() {
  if (!flows.selectedId) {
    ElMessage.warning('请先选中一条流量')
    return
  }
  replayVisible.value = true
}

// 右键菜单触发的重放
function onCtxReplay(flowId: number) {
  flows.select(flowId)
  replayVisible.value = true
}

// 右键菜单触发的 AI 分析
function onCtxAI(flowId: number) {
  flows.select(flowId)
  flows.aiFlowIds = [flowId]
  window.location.hash = '#/ai'
}

// 批量 AI 分析
function onBatchAI(flowIds: number[]) {
  flows.aiFlowIds = flowIds
  window.location.hash = '#/ai'
}

// 右键「在全局分析查看」：注入 flow 到 store 并跳转全局分析页，传 host query 自动展开对应分组
function onCtxViewInAnalyze(flowId: number) {
  const f = flows.flows.find(x => x.id === flowId)
  if (f) {
    flows.selectFlow(f)
    const host = f.host || ''
    window.location.hash = host ? `#/analyze?host=${encodeURIComponent(host)}` : '#/analyze'
  } else {
    window.location.hash = '#/analyze'
  }
}

// 右键「自动修改」子菜单：
//   mode='request'  → 写死请求（mock_request）：用预设请求转发到目标服务器，返回真实响应
//   mode='response' → 写死响应（mock）：不走服务器，直接返回预设响应
function onCtxAutoModify(flow: any, mode: 'request' | 'response') {
  flows.select(flow.id)
  const url = flow.url || ''
  const method = flow.method || 'GET'
  const host = flow.host || ''
  const path = flow.path || ''
  const defaultHeaders = '{"Content-Type": "application/json"}'

  if (mode === 'request') {
    // 写死请求：用原请求的 method/url/headers/body 预填 mock_request 规则
    const reqHeaders = flow.request_headers && flow.request_headers !== '{}'
      ? flow.request_headers
      : defaultHeaders
    editingRule.value = {
      enabled: true,
      match_mode: 'wildcard',
      pattern: url,
      action: 'mock_request',
      mock_method: method,
      mock_url: url,
      mock_headers: reqHeaders,
      mock_body: flow.request_body || '',
      modify_rules: [],
      note: `写死请求：${method} ${host}${path}`.slice(0, 100),
    }
  } else {
    // 写死响应：基于该响应创建 mock 规则，预填原响应 headers 和 body（固化当前响应）
    const respHeaders = flow.response_headers && flow.response_headers !== '{}'
      ? flow.response_headers
      : defaultHeaders
    editingRule.value = {
      enabled: true,
      match_mode: 'wildcard',
      pattern: url,
      action: 'mock',
      mock_status: flow.status_code || 200,
      mock_headers: respHeaders,
      mock_body: flow.response_body || '',
      modify_rules: [],
      note: `写死响应：${method} ${host}${path}`.slice(0, 100),
    }
  }
  ruleEditorVisible.value = true
}

async function saveRule(r: AutoReplyRule) {
  try {
    // 提交前转换 mock_headers
    const payload = { ...r }
    if (typeof payload.mock_headers === 'string') {
      try { payload.mock_headers = JSON.parse(payload.mock_headers) } catch { payload.mock_headers = {} }
    }
    await api.createRule(payload)
    ElMessage.success('规则已保存')
  } catch (e: any) {
    ElMessage.error('保存失败：' + (e?.message || e))
  }
}

async function doReplay(id: number, override?: any) {
  try {
    if (override && Object.keys(override).length) {
      await api.replayFlowOverride(id, override)
    } else {
      await api.replayFlow(id)
    }
    ElMessage.success('重放请求已发送')
  } catch (e: any) {
    ElMessage.error('重放失败：' + (e?.message || e))
  }
}

async function onIgnoreProcess(proc: ProcessInfo) {
  try {
    await api.ignoreProcess(proc)
    ElMessage.success(`已忽略进程 ${proc.name}（对新连接生效，已有连接需重启后端）`)
    await loadProcesses()
  } catch (e: any) {
    ElMessage.error('忽略失败：' + (e?.message || e))
  }
}

async function onIgnoreHost(host: string) {
  try {
    await api.ignoreHost(host)
    ElMessage.success(`已忽略 Host ${host}（对新连接生效，已有连接需重启后端）`)
  } catch (e: any) {
    ElMessage.error('忽略失败：' + (e?.message || e))
  }
}

// 放行/丢弃断点流量
async function onRelease(action: 'release' | 'drop', modified: any) {
  if (!selectedFlow.value) return
  try {
    if (action === 'release' && Object.keys(modified).length) {
      await api.patchFlow(selectedFlow.value.id, modified)
    }
    await api.releaseFlow(selectedFlow.value.id, { action })
    // 放行后刷新流量（不依赖抓包状态）
    await onFlowsChanged()
  } catch (e: any) {
    ElMessage.error('操作失败：' + (e?.message || e))
  }
}

// 导出：所有格式统一通过浏览器下载
async function onExport(format: string) {
  if (!capture.status.session_id) {
    ElMessage.warning('当前无活动会话')
    return
  }
  try {
    const res: any = await api.exportSession(capture.status.session_id, format)
    // 根据格式确定文件扩展名和 MIME
    const extMap: Record<string, string> = {
      'har': 'har',
      'json': 'json',
      'python-requests': 'py',
      'postman': 'json',
      'curl': 'sh',
    }
    const mimeMap: Record<string, string> = {
      'har': 'application/json',
      'json': 'application/json',
      'python-requests': 'text/x-python',
      'postman': 'application/json',
      'curl': 'application/x-sh',
    }
    const ext = extMap[format] || 'txt'
    const mime = mimeMap[format] || 'text/plain'
    // content 可能是字符串（python-requests/curl）或对象（har/json/postman）
    const content = res?.content ?? res?.data
    if (content === undefined) {
      ElMessage.warning('导出结果为空')
      return
    }
    const text = typeof content === 'string' ? content : JSON.stringify(content, null, 2)
    const blob = new Blob([text], { type: mime })
    const url = URL.createObjectURL(blob)
    const ts = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    const fname = `telnix_${ts.getFullYear()}${pad(ts.getMonth()+1)}${pad(ts.getDate())}_${pad(ts.getHours())}${pad(ts.getMinutes())}${pad(ts.getSeconds())}.${ext}`
    const a = document.createElement('a')
    a.href = url
    a.download = fname
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出：${fname}`)
  } catch (e: any) {
    ElMessage.error('导出失败：' + (e?.message || e))
  }
}

// 跳转 AI 分析
function goAI() {
  if (!flows.selectedId) {
    ElMessage.warning('请先选中流量')
    return
  }
  flows.aiFlowIds = flows.selectedId ? [flows.selectedId] : []
  window.location.hash = '#/ai'
}

// 左右拖拽分隔
const dragging = ref(false)
const wrapRef = ref<HTMLElement | null>(null)
function onSplitDown(e: MouseEvent) {
  e.preventDefault()
  dragging.value = true
  window.addEventListener('mousemove', onSplitMove)
  window.addEventListener('mouseup', onSplitUp)
}
function onSplitMove(e: MouseEvent) {
  const el = wrapRef.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  const r = (e.clientX - rect.left) / rect.width
  leftRatio.value = Math.min(0.7, Math.max(0.2, r))
}
function onSplitUp() {
  dragging.value = false
  window.removeEventListener('mousemove', onSplitMove)
  window.removeEventListener('mouseup', onSplitUp)
}

onMounted(() => {
  capture.startPolling()
  loadSettings()
  loadProcesses()
  // 初始加载历史流量（不依赖活动会话，进入抓包页面即可看到以前的包）
  // 等待加载完成后再触发滚动检查，确保跨页跳转选中的行已渲染
  flows.loadAllFlows().finally(() => {
    // loadAllFlows 会覆盖 flows.value，可能导致 selectFlow 注入的 flow 丢失。
    // 如果 selectedId 有值但不在新列表中，重新注入 flow 对象，保证高亮 + 滚动到位
    if (flows.selectedId != null && flows.selectedFlow) {
      const f = flows.selectedFlow
      if (!flows.flows.some(x => x.id === f.id)) {
        flows.selectFlow(f)
      }
    }
  })
  startPolling()
})
onUnmounted(() => {
  capture.stopPolling()
  stopPolling()
})
</script>

<template>
  <div class="capture-view full flex flex-col">
    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <el-button
        :type="capture.status.capturing ? 'danger' : 'primary'"
        size="small"
        @click="onToggleCapture"
      >
        <el-icon><component :is="capture.status.capturing ? 'VideoPause' : 'VideoPlay'" /></el-icon>
        &nbsp;{{ capture.status.capturing ? '停止抓包' : '开始抓包' }}
      </el-button>
      <el-button size="small" @click="onClear">
        <el-icon><Delete /></el-icon>&nbsp;清空
      </el-button>
      <el-button size="small" :disabled="!flows.selectedId" @click="onReplay">
        <el-icon><RefreshRight /></el-icon>&nbsp;重放
      </el-button>
      <el-button size="small" :disabled="!flows.selectedId" @click="goAI">
        <el-icon><MagicStick /></el-icon>&nbsp;发送到AI
      </el-button>
      <div class="toolbar-sep"></div>
      <BreakpointBar />
      <div class="flex-1"></div>
      <el-dropdown @command="onExport" size="small">
        <el-button size="small">
          <el-icon><Download /></el-icon>&nbsp;导出<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="har">HAR 格式</el-dropdown-item>
            <el-dropdown-item command="json">JSON 格式</el-dropdown-item>
            <el-dropdown-item command="python-requests">Python 脚本</el-dropdown-item>
            <el-dropdown-item command="postman">Postman Collection</el-dropdown-item>
            <el-dropdown-item command="curl">cURL 脚本</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <ImportButton />
    </div>

    <!-- 主体：左右分栏 -->
    <div ref="wrapRef" class="flex-1 flex overflow-hidden">
      <div :style="{ width: `${leftRatio * 100}%` }" class="left-pane">
        <FlowList
          :processes="processes"
          :flow-columns="flowColumns"
          :multi-select-bar-delay="multiSelectBarDelay"
          actions-right
          @ignore-process="onIgnoreProcess"
          @ignore-host="onIgnoreHost"
          @replay="onCtxReplay"
          @ai-analyze="onCtxAI"
          @ai-analyze-batch="onBatchAI"
          @view-in-analyze="onCtxViewInAnalyze"
          @auto-modify="onCtxAutoModify"
          @flows-changed="onFlowsChanged"
        />
      </div>
      <div class="splitter-v" :class="{ active: dragging }" @mousedown="onSplitDown"></div>
      <div :style="{ width: `${(1 - leftRatio) * 100}%` }" class="right-pane">
        <Inspector :flow="selectedFlow" :enabled-tabs="enabledTabs" :auto-switch-preview="autoSwitchPreview" @release="onRelease" />
      </div>
    </div>

    <ReplayDialog v-model="replayVisible" :flow="selectedFlow" @replay="doReplay" />
    <RuleEditor v-model="ruleEditorVisible" :rule="editingRule" @save="saveRule" />
  </div>
</template>

<style scoped>
.capture-view { background: var(--on-bg); }
.toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.toolbar-sep { width: 1px; height: 20px; background: var(--on-border); margin: 0 4px; }
.left-pane, .right-pane { overflow: hidden; min-width: 0; }
</style>
