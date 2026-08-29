<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, onUnmounted, ref, watch } from 'vue'
import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import { api, type ProcessInfo, type Flow, type AutoReplyRule, type RepeatResponse, type ReplayOverride } from '../api/client'
import FlowList from '../components/FlowList.vue'
import Inspector from '../components/Inspector.vue'
import BreakpointBar from '../components/BreakpointBar.vue'
import ReplayDialog from '../components/ReplayDialog.vue'
import RepeatResultsDialog from '../components/RepeatResultsDialog.vue'
import ImportButton from '../components/ImportButton.vue'
import RuleEditor from '../components/RuleEditor.vue'
import QuickExec from '../components/QuickExec.vue'
import { useSettingsStore } from '../stores/settings'

const capture = useCaptureStore()
const flows = useFlowsStore()
const { t } = useI18n()

const processes = ref<ProcessInfo[]>([])
const enabledTabs = ref<string[]>([])
// 响应切换流量是否自动回到 preview（从设置读取，默认 true）
const autoSwitchPreview = ref(true)

// 触发式捕获是否启用（从设置读取，默认 false，不显示按钮）
const triggerCaptureVisible = ref(false)

const leftRatio = ref(0.5)
// 紧凑模式：列表列多或容器窄时隐藏右侧详情栏，全屏显示包列表
const containerWidth = ref(0)
const compactMode = computed(() => {
  // 列过多（>7）或容器较窄（<960px）时启用紧凑模式
  if (flowColumns.value.length > 7) return true
  if (containerWidth.value > 0 && containerWidth.value < 720) return true
  if (containerWidth.value > 0 && containerWidth.value < 960 && flowColumns.value.length > 5) return true
  return false
})
const slideVisible = ref(false)
const replayVisible = ref(false)
// Repeat Advanced 结果对话框
const repeatResultsVisible = ref(false)
const repeatResult = ref<RepeatResponse | null>(null)
const repeatLoading = ref(false)
const flowColumns = ref<string[]>([])
const multiSelectBarDelay = ref(1)
const ruleEditorVisible = ref(false)
const editingRule = ref<AutoReplyRule | null>(null)

// 录制回放状态（从 RecordView 移植，按钮放在工具栏 AI 和断点之间）
const recording = ref(false)
const recordingFlowCount = ref(0)
const togglingRecord = ref(false)

// 会话名称和颜色（用于工具栏显示）
const sessionName = ref('')
const sessionColor = ref('')

async function loadSessionInfo() {
  if (!capture.status.session_id) return
  try {
    const res: any = await api.getSession(capture.status.session_id)
    sessionName.value = res?.data?.name || ''
    sessionColor.value = res?.data?.color || ''
  } catch { /* 静默 */ }
}

watch(() => capture.status.session_id, () => {
  loadSessionInfo()
})

// 触发式捕获状态
const triggerEnabled = ref(false)
const triggerTriggered = ref(false)
const triggerDsl = ref('')
const triggerLoading = ref(false)

// 触发条件配置（筛选按钮风格）
interface TriggerCondition {
  field: 'host' | 'status' | 'method' | 'path' | 'process'
  op: 'contains' | 'equals' | 'startsWith' | 'regex'
  value: string
}
const triggerConditions = ref<TriggerCondition[]>([])

function addTriggerCondition() {
  triggerConditions.value.push({ field: 'host', op: 'contains', value: '' })
}
function removeTriggerCondition(idx: number) {
  triggerConditions.value.splice(idx, 1)
}
function buildTriggerDsl(): string {
  return triggerConditions.value
    .filter(c => c.value.trim())
    .map(c => {
      if (c.op === 'contains') return `${c.field}=*${c.value}*`
      if (c.op === 'startsWith') return `${c.field}=${c.value}*`
      if (c.op === 'regex') return `${c.field}~${c.value}`
      return `${c.field}=${c.value}`
    }).join(' & ')
}

async function loadTrigger() {
  try {
    const res: any = await api.getTrigger()
    triggerEnabled.value = !!res?.enabled
    triggerTriggered.value = !!res?.triggered
    const conds = res?.conditions || []
    if (conds.length) {
      triggerDsl.value = conds.map((c: any) => `${c.field}=${c.value}`).join(' & ')
      // 同步解析到 triggerConditions
      triggerConditions.value = conds.map((c: any) => {
        let op: TriggerCondition['op'] = 'contains'
        let value = c.value
        if (typeof c.value === 'string') {
          if (c.value.startsWith('*') && c.value.endsWith('*')) {
            op = 'contains'; value = c.value.slice(1, -1)
          } else if (c.value.endsWith('*')) {
            op = 'startsWith'; value = c.value.slice(0, -1)
          } else if (c.value.startsWith('~')) {
            op = 'regex'; value = c.value.slice(1)
          } else {
            op = 'equals'
          }
        }
        return { field: c.field as TriggerCondition['field'], op, value }
      })
    }
  } catch { /* 静默 */ }
}

async function saveTrigger() {
  triggerLoading.value = true
  try {
    triggerDsl.value = buildTriggerDsl()
    const res: any = await api.setTrigger({ dsl: triggerDsl.value })
    triggerEnabled.value = !!res?.enabled
    triggerTriggered.value = !!res?.triggered
    ElMessage.success(triggerEnabled.value ? t('capture.triggerEnabled') : t('capture.triggerClear'))
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    triggerLoading.value = false
  }
}

async function clearTrigger() {
  triggerDsl.value = ''
  triggerConditions.value = []
  triggerLoading.value = true
  try {
    await api.setTrigger({ dsl: '' })
    triggerEnabled.value = false
    triggerTriggered.value = false
    ElMessage.success(t('capture.triggerClear'))
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    triggerLoading.value = false
  }
}

async function resetTrigger() {
  try {
    const res: any = await api.resetTrigger()
    triggerTriggered.value = !!res?.triggered
    ElMessage.success(t('capture.triggerReset'))
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

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
      if (s.trigger_capture_enabled !== undefined) triggerCaptureVisible.value = s.trigger_capture_enabled === true
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
    if (s.trigger_capture_enabled !== undefined) triggerCaptureVisible.value = s.trigger_capture_enabled === true
    // autoScroll/autoScrollDelay 由 store 自己持久化，不从后端覆盖（避免切换页面时被旧后端值重置）
    // 更新本地缓存
    localStorage.setItem('telnix_settings_cache', JSON.stringify(s))
  } catch {
    /* ignore */
  }
}

// 抓包页流量刷新 + 进程列表刷新
// 实时更新策略：
// - 正常情况：SSE 推送新流量（<50ms 延迟），此处不轮询流量，只定期刷新进程列表；
// - 兜底情况：当 SSE 未连接「或」SSE 已连接但疑似假死（超过 20s 无任何消息，
//   例如后端生成器异常退出、连接静默断开但浏览器未触发 onerror）时，
//   用增量轮询（since_id）补齐新流量，保证新包实时显示，
//   不再需要刷新页面才更新（修复「抓包页来新包不实时更新」）。
// 不再依赖 capture.status.capturing 作为轮询开关：只要本页打开就保持流量最新，
// 即使后端 capturing 状态同步有抖动也不影响实时性。
let lastProcLoadTime = 0
let lastFullLoadTime = 0
async function pollFlows() {
  // SSE 健康 = 已连接 且 近期有消息（init/flow/ping），否则视为需要兜底轮询
  const sseHealthy = flows.sseActive && !flows.sseStale()
  if (!sseHealthy) {
    // SSE 失活/假死：增量轮询补齐新流量；本地无历史时先全量加载基线
    if (flows.maxFlowId) {
      flows.pollNewFlows()  // 不 await，避免阻塞下一个 pollTimer tick
    } else {
      // 避免频繁全量加载：至少间隔 5 秒才再次 loadAllFlows
      // initialLoading 为 true 时跳过 5 秒限制：让 loadAllFlows 触发 polling 锁分支
      // (initialLoading=false)，提前结束转圈（最多转 1.5s 而非 3s）
      const now = Date.now()
      if (flows.initialLoading || now - lastFullLoadTime > 5000) {
        lastFullLoadTime = now
        flows.loadAllFlows()  // 不 await
      }
    }
  }
  // 进程列表每 5 秒刷新一次（与后端 list_processes 5s TTL 缓存对齐，命中率 100%）
  const now = Date.now()
  if (now - lastProcLoadTime > 5000) {
    lastProcLoadTime = now
    loadProcesses()  // 不 await，避免阻塞流量轮询
  }
  // 每次 poll 都刷新录制状态（最多 1 秒一次），确保录制数字实时增长，
  // 也支持从其他页面开始/停止录制后状态同步。
  if (now - lastRecordStatusTime > 1000) {
    lastRecordStatusTime = now
    loadRecordingStatus()
  }
}
let lastRecordStatusTime = 0

// 流量变更（删除后刷新）：重新全量加载
async function onFlowsChanged() {
  await flows.loadAllFlows()
}

function startPolling() {
  if (pollTimer !== null) return
  // SSE 推送：新 flow 立即推送到前端，UI 延迟 <50ms（替代 500ms 轮询）
  flows.startSSE()
  // 兜底轮询：1.5 秒一次，防止 SSE 异常断开未重连时漏掉流量
  // 性能优化：从 5s 降到 1.5s，SSE 异常时延迟从 5s 降到 1.5s
  // 也用于触发 loadProcesses（进程列表每 5 秒刷新，与后端 5s TTL 缓存对齐）
  pollTimer = window.setInterval(pollFlows, 1500)
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
        ElMessage.error(t('capture.stopCaptureFailed') + (e?.message || e))
      )
      stopPolling()
    } else {
      // 开始抓包：fire-and-forget（store 内部已乐观更新 capturing=true）
      // 不 await，按钮立即响应；轮询立即启动，乐观状态由后续 fetchStatus 校正
      capture.toggleCapture().catch((e: any) =>
        ElMessage.error(t('capture.startCaptureFailed') + (e?.message || e))
      )
      // 乐观更新已设置 capturing=true，立即启动 SSE + 轮询
      // 不调用 pollFlows()：SSE 会推送新流量，避免触发不必要的 loadAllFlows
      startPolling()
      loadProcesses()
    }
  } catch (e: any) {
    ElMessage.error(t('capture.operationFailed') + (e?.message || e))
  }
}

async function onClear() {
  let clearAll = true
  try {
    await ElMessageBox.confirm(
      t('capture.clearScopeMessage'),
      t('capture.clearConfirmTitle'),
      {
        confirmButtonText: t('capture.clearAllSessions'),
        cancelButtonText: t('capture.clearCurrentSession'),
        type: 'warning',
      }
    )
    clearAll = true
  } catch {
    // 用户取消默认清空当前会话
    clearAll = false
  }

  // 乐观清空：先立即清空 UI + 断开 SSE（用户无延迟感知）。
  // 注意：此时不能立刻重连 SSE——否则新 SSE 的 init 会在后端删除完成前返回旧的
  // max_id，触发 pollNewFlows(since_id=0) 把还没删除的旧包重新拉回来。
  flows.clearLocal()
  let ok = false
  try {
    // 清空范围由用户选择：当前会话或所有会话
    await api.clearAllFlows(clearAll ? 'all' : 'current')
    ok = true
    ElMessage.success(t('capture.clearAllSuccess'))
  } catch (e: any) {
    ElMessage.error(t('capture.clearFailed') + (e?.message || e))
  } finally {
    // 后端清空（或失败恢复）后才重连 SSE：此时 init 的 max_id=0，不会把旧包拉回
    flows.reconnectSSE()
    if (!ok) flows.loadAllFlows()
  }
}

function onReplay() {
  if (!flows.selectedId) {
    ElMessage.warning(t('capture.selectFlowFirst'))
    return
  }
  replayVisible.value = true
}

// 批量重放：弹出 QPS 输入框，依次重放选中流量
// QPS=0 表示不限制（并发只受浏览器/服务器限制），默认 5 QPS（每 200ms 一条）
let batchReplaying = false
async function onBatchReplay(ids: number[]) {
  if (!ids.length) return
  if (batchReplaying) {
    ElMessage.warning(t('capture.batchReplayInProgress'))
    return
  }
  let qps = 5
  try {
    const { value } = await ElMessageBox.prompt(
      t('capture.batchReplayPrompt', { n: ids.length }),
      t('capture.batchReplayTitle'),
      {
        confirmButtonText: t('capture.startReplayButton'),
        cancelButtonText: t('capture.cancelButton'),
        inputValue: '5',
        inputValidator: (v: string) => {
          const n = Number(v)
          if (!isFinite(n) || n < 0 || !Number.isInteger(n)) return t('capture.batchReplayInputError')
          return true
        },
      }
    )
    qps = Number(value)
  } catch {
    return
  }
  batchReplaying = true
  const delay = qps > 0 ? Math.ceil(1000 / qps) : 0
  const total = ids.length
  let ok = 0
  let fail = 0
  let startMsg = ElMessage({
    message: t('capture.batchReplaying', { current: 0, total }),
    type: 'info',
    duration: 0,
  })
  const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))
  for (let i = 0; i < total; i++) {
    const id = ids[i]
    try {
      await api.replayFlow(id)
      ok++
    } catch {
      fail++
    }
    // 每 5 条更新一次进度（直接修改 ElMessage 实例属性不会触发重渲染，需关闭后重建）
    if ((i + 1) % 5 === 0 || i === total - 1) {
      startMsg.close()
      startMsg = ElMessage({
        message: t('capture.batchReplayingDetail', { current: i + 1, total, ok, fail }),
        type: 'info',
        duration: 0,
      })
    }
    if (delay > 0 && i < total - 1) await sleep(delay)
  }
  startMsg.close()
  ElMessage.success(t('capture.batchReplayComplete', { total, ok, fail }))
  batchReplaying = false
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
      note: t('capture.mockRequestNote', { method, host, path }).slice(0, 100),
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
      note: t('capture.mockResponseNote', { method, host, path }).slice(0, 100),
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
    ElMessage.success(t('capture.ruleSaved'))
  } catch (e: any) {
    ElMessage.error(t('capture.saveFailed') + (e?.message || e))
  }
}

// 从 FlowList 右键菜单添加延迟规则
function onAddDelayRule(flow: any, phase: 'request' | 'response') {
  const url = flow.url || ''
  const host = flow.host || ''
  const path = flow.path || ''
  // 跳转到延迟规则页并预填
  router.push({
    path: '/delay',
    query: {
      pattern: path || url,
      host: host,
      phase: phase,
    },
  })
}

// 从 FlowList 右键菜单添加到 Mock
function onAddToMock(flow: any) {
  const url = flow.url || ''
  const host = flow.host || ''
  // 跳转到 Mock 页并预填
  router.push({
    path: '/mock',
    query: {
      flowId: String(flow.id),
    },
  })
}

// 从 FlowList 右键菜单添加到录制
function onAddToRecord(flow: any) {
  // 跳转到录制页，添加到当前录制会话
  router.push({
    path: '/record',
    query: {
      addFlowId: String(flow.id),
    },
  })
}

async function doReplay(id: number, override?: any) {
  try {
    if (override && Object.keys(override).length) {
      await api.replayFlowOverride(id, override)
    } else {
      await api.replayFlow(id)
    }
    ElMessage.success(t('capture.replaySent'))
  } catch (e: any) {
    ElMessage.error(t('capture.replayFailed') + (e?.message || e))
  }
}

// Repeat Advanced：批量并发重放
async function doRepeat(
  id: number,
  count: number,
  concurrency: number,
  intervalMs: number,
  override?: ReplayOverride,
) {
  repeatLoading.value = true
  repeatResult.value = null
  repeatResultsVisible.value = true
  try {
    const res = await api.repeatFlow(id, count, concurrency, intervalMs, override)
    repeatResult.value = res
  } catch (e: any) {
    ElMessage.error(t('capture.replayFailed') + (e?.message || e))
    repeatResultsVisible.value = false
  } finally {
    repeatLoading.value = false
  }
}

async function onIgnoreProcess(proc: ProcessInfo) {
  try {
    await api.ignoreProcess(proc)
    ElMessage.success(t('capture.ignoredProcess', { name: proc.name }))
    await loadProcesses()
  } catch (e: any) {
    ElMessage.error(t('capture.ignoreFailed') + (e?.message || e))
  }
}

async function onIgnoreHost(host: string) {
  try {
    await api.ignoreHost(host)
    ElMessage.success(t('capture.ignoredHost', { host }))
  } catch (e: any) {
    ElMessage.error(t('capture.ignoreFailed') + (e?.message || e))
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
    // 性能优化：断点放行后不执行慢的全量 reloadAllFlows，
    // 改为触发 flows store 立即刷新（dispatch 事件让 SSE 推送 + 增量轮询补漏）
    window.dispatchEvent(new CustomEvent('telnix:breakpoint-released', { detail: { id: selectedFlow.value.id } }))
    // 立即在本地清除断点状态，避免等待 SSE 推送的延迟
    flows.patchFlows([selectedFlow.value.id], (f) => { f.breakpoint_status = null })
    // API 成功后提示（原本由 Inspector 在 emit 前提示，存在失败时误显示成功的问题）
    if (action === 'release') {
      ElMessage.success(Object.keys(modified).length ? t('inspector.modifiedAndReleased') : t('inspector.released'))
    } else {
      ElMessage.info(t('inspector.dropped'))
    }
  } catch (e: any) {
    ElMessage.error(t('capture.operationFailed') + (e?.message || e))
  }
}

// 导出：所有格式统一通过浏览器下载
async function onExport(format: string) {
  if (!capture.status.session_id) {
    ElMessage.warning(t('capture.noActiveSession'))
    return
  }
  try {
    const res: any = await api.exportSession(capture.status.session_id, format)
    // 根据格式确定文件扩展名和 MIME
    const extMap: Record<string, string> = {
      'har': 'har',
      'json': 'json',
      'csv': 'csv',
      'python-requests': 'py',
      'postman': 'json',
      'curl': 'sh',
      'pcap': 'pcap',
    }
    const mimeMap: Record<string, string> = {
      'har': 'application/json',
      'json': 'application/json',
      'csv': 'text/csv',
      'python-requests': 'text/x-python',
      'postman': 'application/json',
      'curl': 'application/x-sh',
      'pcap': 'application/vnd.tcpdump.pcap',
    }
    const ext = extMap[format] || 'txt'
    const mime = mimeMap[format] || 'text/plain'
    // content 可能是字符串（python-requests/curl）或对象（har/json/postman）
    // pcap 格式：后端返回 base64 编码的二进制，需解码为 Blob
    const content = res?.content ?? res?.data
    if (content === undefined) {
      ElMessage.warning(t('capture.exportEmpty'))
      return
    }
    let blob: Blob
    if (format === 'pcap' && typeof content === 'string') {
      // base64 → 二进制 Blob
      const binary = atob(content)
      const bytes = new Uint8Array(binary.length)
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
      blob = new Blob([bytes], { type: mime })
    } else {
      const text = typeof content === 'string' ? content : JSON.stringify(content, null, 2)
      blob = new Blob([text], { type: mime })
    }
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
    ElMessage.success(t('capture.exportedFile', { name: fname }))
  } catch (e: any) {
    ElMessage.error(t('capture.exportFailed') + (e?.message || e))
  }
}

// 跳转 AI 分析
function goAI() {
  if (!flows.selectedId) {
    ElMessage.warning(t('capture.selectFlowFirstShort'))
    return
  }
  flows.aiFlowIds = flows.selectedId ? [flows.selectedId] : []
  window.location.hash = '#/ai'
}

// ---------- 录制回放 ----------
function goRecord() {
  window.location.hash = '#/record'
}
function goRecordFromCapture() {
  flows.rememberPage('/capture')
  goRecord()
}

async function loadRecordingStatus() {
  try {
    const res = await axios.get('/api/record-scripts/recording-status')
    const data = res?.data?.code === 0 ? res.data.data : null
    if (data) {
      recording.value = !!data.active
      recordingFlowCount.value = data.flow_count || 0
    }
  } catch { /* 静默 */ }
}

async function toggleRecording() {
  togglingRecord.value = true
  try {
    if (!recording.value) {
      await axios.post('/api/record-scripts/start-recording')
      recording.value = true
      recordingFlowCount.value = 0
      ElMessage.success(t('record.recordingStarted'))
    } else {
      await ElMessageBox.confirm(t('record.stopConfirm'), t('record.stopTitle'), {
        confirmButtonText: t('record.stopButton'),
        cancelButtonText: t('record.cancelButton'),
        type: 'warning',
      })
      await axios.post('/api/record-scripts/stop-recording')
      recording.value = false
      recordingFlowCount.value = 0
      ElMessage.success(t('record.recordingStopped'))
    }
  } catch (e: any) {
    if (e === 'cancel' || e?.message === 'cancel') return
    ElMessage.error((recording.value ? t('record.stopFailed') : t('record.startFailed')) + (e?.message || e))
  } finally {
    togglingRecord.value = false
  }
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
  // 限制分割范围：包列表至少 32%，详情栏最多 68%，保证 FlowList 头部按钮不被遮挡
  leftRatio.value = Math.min(0.68, Math.max(0.32, r))
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
  loadRecordingStatus()
  loadTrigger()
  loadSessionInfo()
  // 初始加载历史流量（不阻塞 UI渲染），SSE 会推送新流量
  // 页面显示后异步加载，避免3秒阻塞白屏
  requestAnimationFrame(() => {
    flows.loadAllFlows().then(() => {
      // flows 加载完成后注入选中的 flow 对象，保证高亮 + 滚动到位
      if (flows.selectedId != null && flows.selectedFlow) {
        const f = flows.selectedFlow
        if (!flows.hasFlow(f.id)) {
          flows.selectFlow(f)
        }
      }
    })
  })
  startPolling()
  // 监听容器宽度变化，用于自动切换紧凑模式
  if (wrapRef.value && typeof ResizeObserver !== 'undefined') {
    const updateWidth = () => {
      containerWidth.value = wrapRef.value?.clientWidth || 0
    }
    updateWidth()
    const ro = new ResizeObserver(updateWidth)
    ro.observe(wrapRef.value)
    onUnmounted(() => ro.disconnect())
  }
})
onUnmounted(() => {
  capture.stopPolling()
  stopPolling()
  // 清理 SSE：关闭 EventSource + 移除 window 事件监听器
  flows.cleanup()
  // 拖拽中卸载（如切换路由）时清理 window 监听器，避免泄漏
  if (dragging.value) {
    window.removeEventListener('mousemove', onSplitMove)
    window.removeEventListener('mouseup', onSplitUp)
    dragging.value = false
  }
})

// 紧凑模式下选中流量时从右侧滑出详情面板
watch(() => flows.selectedId, (id) => {
  if (compactMode.value && id != null) {
    slideVisible.value = true
  }
})

function closeSlide() {
  slideVisible.value = false
  // 关闭面板时取消选中，方便再次点击同一行重新打开
  flows.select(null)
}
</script>

<template>
    <div class="capture-view full flex flex-col">
    <!-- 顶部工具栏 -->
    <div class="toolbar-row">
      <el-button
        :type="capture.status.capturing ? 'danger' : 'primary'"
        size="small"
        @click="onToggleCapture"
      >
        <el-icon><component :is="capture.status.capturing ? 'VideoPause' : 'VideoPlay'" /></el-icon>
        &nbsp;{{ capture.status.capturing ? t('capture.stopCapture') : t('capture.startCapture') }}
      </el-button>
      <div class="session-info no-select" v-if="capture.status.session_id">
        <span class="session-dot" :style="{ background: sessionColor || 'var(--on-accent)' }"></span>
        <span class="session-name text-dim">{{ sessionName || `Session #${capture.status.session_id}` }}</span>
      </div>
      <el-button size="small" @click="onClear">
        <el-icon><Delete /></el-icon>&nbsp;{{ t('capture.clear') }}
      </el-button>
      <el-button size="small" :disabled="!flows.selectedId" @click="onReplay">
        <el-icon><RefreshRight /></el-icon>&nbsp;{{ t('capture.replay') }}
      </el-button>
      <el-button size="small" :disabled="!flows.selectedId" @click="goAI">
        <el-icon><MagicStick /></el-icon>&nbsp;{{ t('capture.sendToAI') }}
      </el-button>
      <div class="record-btn-group">
        <el-button
          size="small"
          :type="recording ? 'danger' : 'default'"
          :loading="togglingRecord"
          @click="toggleRecording"
          :title="recording ? t('record.stopRecording') : t('record.startRecording')"
        >
          <el-icon><component :is="recording ? 'VideoPause' : 'VideoPlay'" /></el-icon>
          <template v-if="recording">&nbsp;{{ t('record.recording') }} · {{ recordingFlowCount }}</template>
          <template v-else>&nbsp;{{ t('record.startRecording') }}</template>
        </el-button>
        <el-button size="small" class="record-go-btn" :title="t('record.gotoRecordPage')" @click="goRecordFromCapture">
          <el-icon><ArrowRight /></el-icon>
        </el-button>
      </div>
      <div class="toolbar-sep"></div>
      <BreakpointBar />
      <el-popover v-if="triggerCaptureVisible" :width="420" placement="bottom" trigger="click">
        <template #reference>
          <el-button size="small" :type="triggerEnabled ? (triggerTriggered ? 'success' : 'warning') : 'default'">
            <el-icon><Aim /></el-icon>&nbsp;{{ t('capture.triggerCapture') }}
            <el-tag v-if="triggerEnabled" size="small" :type="triggerTriggered ? 'success' : 'warning'" style="margin-left: 4px">
              {{ triggerTriggered ? t('capture.triggerTriggered') : t('capture.triggerWaiting') }}
            </el-tag>
          </el-button>
        </template>
        <div style="padding: 8px; min-width: 400px" @click.stop>
          <div style="font-weight: 600; margin-bottom: 8px">{{ t('capture.triggerConfig') }}</div>
          <div class="trigger-conds">
            <div v-for="(cond, idx) in triggerConditions" :key="idx" class="trigger-cond-row">
              <el-select v-model="cond.field" size="small" style="width: 90px" placeholder="field">
                <el-option label="Host" value="host" />
                <el-option label="Status" value="status" />
                <el-option label="Method" value="method" />
                <el-option label="Path" value="path" />
                <el-option label="Process" value="process" />
              </el-select>
              <el-select v-model="cond.op" size="small" style="width: 100px" placeholder="op">
                <el-option label="包含" value="contains" />
                <el-option label="等于" value="equals" />
                <el-option label="开头是" value="startsWith" />
                <el-option label="正则" value="regex" />
              </el-select>
              <el-input v-model="cond.value" size="small" clearable style="flex: 1" placeholder="值" />
              <el-button size="small" text type="danger" style="color: white" @click="removeTriggerCondition(idx)">
                <el-icon><Close /></el-icon>
              </el-button>
            </div>
          </div>
          <el-button size="small" text type="primary" style="color: white; margin: 4px 0 8px" @click="addTriggerCondition">
            <el-icon><Plus /></el-icon>&nbsp;添加条件
          </el-button>
          <div class="trigger-dsl-preview" v-if="triggerConditions.some(c => c.value)">
            <span style="font-size: 11px; color: var(--on-text-muted)">DSL: </span>
            <code class="mono" style="font-size: 11px; color: var(--on-accent)">{{ buildTriggerDsl() || '-' }}</code>
          </div>
          <div style="display: flex; gap: 8px; margin-top: 8px">
            <el-button size="small" type="primary" :loading="triggerLoading" @click="saveTrigger">{{ t('capture.triggerSave') }}</el-button>
            <el-button size="small" :disabled="!triggerEnabled" @click="resetTrigger">{{ t('capture.triggerReset') }}</el-button>
            <el-button size="small" @click="clearTrigger">{{ t('capture.triggerClear') }}</el-button>
          </div>
        </div>
      </el-popover>
      <div class="flex-1"></div>
      <el-dropdown @command="onExport" size="small">
        <el-button size="small">
          <el-icon><Download /></el-icon>&nbsp;{{ t('capture.export') }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="har">{{ t('capture.exportHarFormat') }}</el-dropdown-item>
            <el-dropdown-item command="json">{{ t('capture.exportJsonFormat') }}</el-dropdown-item>
            <el-dropdown-item command="csv">{{ t('capture.exportCsvFormat') }}</el-dropdown-item>
            <el-dropdown-item command="python-requests">{{ t('capture.exportPythonScript') }}</el-dropdown-item>
            <el-dropdown-item command="postman">{{ t('capture.exportPostman') }}</el-dropdown-item>
            <el-dropdown-item command="curl">{{ t('capture.exportCurlScript') }}</el-dropdown-item>
            <el-dropdown-item command="pcap">{{ t('capture.exportPcapFormat') }}</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <ImportButton />
    </div>

    <!-- QuickExec 命令栏 -->
    <QuickExec />

    <!-- 主体：左右分栏 / 紧凑模式全屏列表 + 右侧滑出详情 -->
    <div ref="wrapRef" class="flex-1 flex overflow-hidden main-wrap">
      <div :style="{ width: compactMode ? '100%' : `${leftRatio * 100}%` }" class="left-pane">
        <FlowList
          :processes="processes"
          :flow-columns="flowColumns"
          :multi-select-bar-delay="multiSelectBarDelay"
          :compact-mode="compactMode"
          actions-right
          @ignore-process="onIgnoreProcess"
          @ignore-host="onIgnoreHost"
          @replay="onCtxReplay"
          @replay-batch="onBatchReplay"
          @ai-analyze="onCtxAI"
          @ai-analyze-batch="onBatchAI"
          @view-in-analyze="onCtxViewInAnalyze"
          @auto-modify="onCtxAutoModify"
          @flows-changed="onFlowsChanged"
          @add-delay-rule="onAddDelayRule"
          @add-to-mock="onAddToMock"
          @add-to-record="onAddToRecord"
        />
      </div>
      <div v-if="!compactMode" class="splitter-v" :class="{ active: dragging }" @mousedown="onSplitDown"></div>
      <div v-if="!compactMode" :style="{ width: `${(1 - leftRatio) * 100}%` }" class="right-pane">
        <Inspector :flow="selectedFlow" :enabled-tabs="enabledTabs" :auto-switch-preview="autoSwitchPreview" @release="onRelease" />
      </div>

      <!-- 紧凑模式：右侧滑出详情面板 -->
      <div v-if="compactMode" class="slide-overlay" :class="{ visible: slideVisible }" @click.self="closeSlide">
        <div class="slide-panel">
          <div class="slide-header">
            <span class="slide-title">{{ t('capture.flowDetail') }}</span>
            <el-button size="small" text @click="closeSlide">
              <el-icon><Close /></el-icon>
            </el-button>
          </div>
          <div class="slide-body">
            <Inspector :flow="selectedFlow" :enabled-tabs="enabledTabs" :auto-switch-preview="autoSwitchPreview" @release="onRelease" />
          </div>
        </div>
      </div>
    </div>

    <ReplayDialog v-model="replayVisible" :flow="selectedFlow" @replay="doReplay" @repeat="doRepeat" />
    <RepeatResultsDialog v-model="repeatResultsVisible" :result="repeatResult" :loading="repeatLoading" />
    <RuleEditor v-model="ruleEditorVisible" :rule="editingRule" @save="saveRule" />
  </div>
</template>

<style scoped>
.capture-view { background: var(--on-bg); }

.left-pane, .right-pane { overflow: hidden; min-width: 0; }

/* 紧凑模式：右侧滑出详情面板 */
.slide-overlay {
  position: absolute; inset: 0;
  background: rgba(0, 0, 0, 0.2);
  opacity: 0; pointer-events: none;
  transition: opacity 0.2s ease;
  z-index: 50;
}
.slide-overlay.visible {
  opacity: 1; pointer-events: auto;
}
.slide-panel {
  position: absolute; top: 0; right: 0; bottom: 0;
  width: min(560px, 90%);
  background: var(--on-bg-elevated);
  border-left: 1px solid var(--on-border-light);
  box-shadow: -2px 0 12px rgba(0,0,0,0.15);
  transform: translateX(100%);
  transition: transform 0.25s ease;
  display: flex; flex-direction: column;
}
.slide-overlay.visible .slide-panel {
  transform: translateX(0);
}
.slide-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
}
.slide-title { font-weight: 500; font-size: 14px; }
.slide-body { flex: 1; overflow: hidden; min-width: 0; }

/* 录制按钮组：箭头按钮紧贴开始录制按钮 */
.record-btn-group {
  display: inline-flex;
  align-items: stretch;
}
.record-btn-group .el-button {
  border-radius: 0;
  margin: 0;
}
.record-btn-group > .el-button:first-child {
  border-radius: var(--el-border-radius-base) 0 0 var(--el-border-radius-base);
}
.record-btn-group > .el-button:last-child {
  border-radius: 0 var(--el-border-radius-base) var(--el-border-radius-base) 0;
  padding: 0 8px;
}
.record-btn-group > .el-button:not(:first-child):not(:last-child) {
  border-radius: 0;
}
/* 会话名称和颜色指示器 */
.session-info {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px;
  font-size: 12px;
  max-width: 200px;
  overflow: hidden;
}
.session-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.session-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 触发式捕获条件配置 */
.trigger-conds { display: flex; flex-direction: column; gap: 6px; max-height: 240px; overflow-y: auto; }
.trigger-cond-row { display: flex; align-items: center; gap: 6px; }
.trigger-dsl-preview { margin-top: 4px; padding: 4px 8px; background: var(--on-bg); border-radius: 4px; }
</style>
