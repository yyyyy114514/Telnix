<script setup lang="ts">
import { ref, watch, computed, reactive, nextTick, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import type { AutoReplyRule, ModifyRule, ScriptTestResult } from '../api/client'
import { api } from '../api/client'
import CodeEditor from './CodeEditor.vue'
import MonacoEditor from './MonacoEditor.vue'

// 自动修改规则编辑器对话框
const props = defineProps<{
  modelValue: boolean
  rule: AutoReplyRule | null
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  save: [rule: AutoReplyRule]
}>()

const form = ref<AutoReplyRule>(emptyRule())

// Python 脚本默认模板（用户新建 script 规则时填充，引导上手）
const SCRIPT_TEMPLATE = `# Telnix Python 脚本
# 可用钩子：on_request（转发前）/ on_response（返回客户端前）
# ctx 属性：host / path / method / url / scheme / pid / process_name
#           request_headers(dict) / request_body(bytes)
#           on_response 额外有：status_code / response_headers / response_body
# 修改方法：ctx.set_request_header / set_request_body / set_response_header / set_response_body / set_status_code
# 返回 None：应用修改后继续；{"drop": True}：拒绝；{"mock": True, "status": 200, "headers": {}, "body": b""}：伪造响应

def on_request(ctx):
    # 例：给所有请求加自定义头
    ctx.set_request_header("X-Custom", "telnix")
    # 例：根据路径修改请求体
    # if ctx.path.startswith("/api/login"):
    #     import json
    #     data = json.loads(ctx.request_body)
    #     data["source"] = "telnix"
    #     ctx.set_request_body(json.dumps(data).encode("utf-8"))
    return None

def on_response(ctx):
    # 例：把响应中的 status 改成 200
    # import json
    # data = json.loads(ctx.response_body)
    # if data.get("code") == -1:
    #     data["code"] = 0
    #     ctx.set_response_body(json.dumps(data).encode("utf-8"))
    return None
`

function emptyRule(): AutoReplyRule {
  return {
    enabled: true,
    match_mode: 'wildcard',
    pattern: '',
    action: 'modify_response',
    mock_status: null as unknown as number,
    mock_headers: '{"Content-Type": "application/json"}',
    mock_body: '',
    modify_rules: [],
    note: '',
    mock_method: 'GET',
    mock_url: '',
  }
}

watch(
  () => [props.modelValue, props.rule],
  () => {
    if (props.modelValue) {
      form.value = props.rule ? JSON.parse(JSON.stringify(props.rule)) : emptyRule()
      if (!form.value.modify_rules) form.value.modify_rules = []
      // script action 时 modify_rules 应为 string（Python 脚本源码）
      if (form.value.action === 'script' && typeof form.value.modify_rules !== 'string') {
        form.value.modify_rules = SCRIPT_TEMPLATE
      }
      // mock_headers 从后端返回的是 dict，编辑时转成字符串
      if (form.value.mock_headers && typeof form.value.mock_headers === 'object') {
        form.value.mock_headers = JSON.stringify(form.value.mock_headers, null, 2)
      }
      // 重置测试面板状态
      testPanelVisible.value = false
      userWantsTestPanel.value = false
    }
  },
  { deep: false }
)

// action 切换时初始化对应字段
watch(() => form.value.action, (newAction, oldAction) => {
  if (newAction === 'script') {
    // 切到 script：若 modify_rules 不是字符串则填模板
    if (typeof form.value.modify_rules !== 'string' || !form.value.modify_rules.trim()) {
      form.value.modify_rules = SCRIPT_TEMPLATE
    }
    // 如果用户之前展开过测试面板，切回 script 时自动展开
    if (userWantsTestPanel.value) {
      testPanelVisible.value = true
    }
  } else if (oldAction === 'script') {
    // 从 script 切走：modify_rules 置空 list
    if (typeof form.value.modify_rules === 'string') {
      form.value.modify_rules = []
    }
    // 切走时收起测试面板（dialog 宽度恢复正常）
    testPanelVisible.value = false
  }
})

const isMock = computed(() => form.value.action === 'mock')
const isMockReq = computed(() => form.value.action === 'mock_request')
const isModifyResp = computed(() => form.value.action === 'modify_response')
const isModifyReq = computed(() => form.value.action === 'modify_request')
const isScript = computed(() => form.value.action === 'script')

const HTTP_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']

// 响应体字段替换规则
const bodyRules = computed(() =>
  ((form.value.modify_rules as ModifyRule[]) || []).filter(
    (m) => m.target === 'response_body' && m.op !== 'append'
  )
)
// 响应头规则
const headerRules = computed(() =>
  ((form.value.modify_rules as ModifyRule[]) || []).filter((m) => m.target === 'response_header')
)
// 请求体字段替换规则
const reqBodyRules = computed(() =>
  ((form.value.modify_rules as ModifyRule[]) || []).filter(
    (m) => m.target === 'request_body' && m.op !== 'append'
  )
)
// 请求头规则
const reqHeaderRules = computed(() =>
  ((form.value.modify_rules as ModifyRule[]) || []).filter((m) => m.target === 'request_header')
)

function addBodyRule() {
  ;(form.value.modify_rules as ModifyRule[]).push({
    target: 'response_body',
    op: 'replace',
    key: '',
    value: '',
  })
}
function addHeaderRule() {
  ;(form.value.modify_rules as ModifyRule[]).push({
    target: 'response_header',
    op: 'replace',
    key: '',
    value: '',
  })
}
function addReqBodyRule() {
  ;(form.value.modify_rules as ModifyRule[]).push({
    target: 'request_body',
    op: 'replace',
    key: '',
    value: '',
  })
}
function addReqHeaderRule() {
  ;(form.value.modify_rules as ModifyRule[]).push({
    target: 'request_header',
    op: 'replace',
    key: '',
    value: '',
  })
}
function removeModify(i: number) {
  ;(form.value.modify_rules as ModifyRule[]).splice(i, 1)
}
// 根据 modify_rules 中的实际位置删除
function removeModifyByIdx(list: ModifyRule[], idx: number) {
  const target = list[idx]
  if (!target) return
  const realIdx = (form.value.modify_rules as ModifyRule[]).indexOf(target)
  if (realIdx >= 0) (form.value.modify_rules as ModifyRule[]).splice(realIdx, 1)
}

function save() {
  if (!form.value.pattern.trim()) {
    ElMessage.warning('请填写 URL 模式')
    return
  }
  // 脚本规则：检查 modify_rules 是否有内容
  if (form.value.action === 'script') {
    const script = form.value.modify_rules
    if (typeof script !== 'string' || !script.trim()) {
      ElMessage.warning('请填写脚本内容')
      return
    }
  }
  // 提交前把 mock_headers 字符串解析为 dict
  const payload = { ...form.value }
  if (typeof payload.mock_headers === 'string') {
    try {
      payload.mock_headers = JSON.parse(payload.mock_headers)
    } catch {
      payload.mock_headers = {}
    }
  }
  emit('save', payload)
  emit('update:modelValue', false)
}

function close() {
  emit('update:modelValue', false)
}

const opOptions = [
  { label: '替换', value: 'replace' },
  { label: '删除', value: 'remove' },
]

// ---------- Python 脚本测试面板 ----------
const testPanelVisible = ref(false)
const userWantsTestPanel = ref(false) // 记住用户是否主动展开过测试面板
const testRunning = ref(false)
const testResult = ref<ScriptTestResult | null>(null)

// Monaco 编辑器高度跟随测试面板内容高度（ResizeObserver 精确测量，避免 stretch 循环引用）
const testPanelSideRef = ref<HTMLElement | null>(null)
const editorHeight = ref('220px')
let resizeObserver: ResizeObserver | null = null

function updateEditorHeight() {
  if (testPanelSideRef.value && testPanelVisible.value) {
    // 用 scrollHeight 拿到内容真实高度（含 padding，不含 border）
    const h = testPanelSideRef.value.scrollHeight
    editorHeight.value = `${h}px`
  } else {
    editorHeight.value = '220px'
  }
}

watch(testPanelVisible, (visible) => {
  if (visible) {
    nextTick(() => {
      if (testPanelSideRef.value) {
        if (!resizeObserver) {
          resizeObserver = new ResizeObserver(updateEditorHeight)
          resizeObserver.observe(testPanelSideRef.value)
        }
        updateEditorHeight()
      }
    })
  } else {
    editorHeight.value = '220px'
    if (resizeObserver) {
      resizeObserver.disconnect()
      resizeObserver = null
    }
  }
})

// mode 切换时也需要重新测量（testMock.mode 变化导致内容块显隐）
// 注意：watch 必须在 testMock 声明之后，否则触发 TDZ 错误（Cannot access 'testMock' before initialization）
// watch 已移至 testMock 声明之后

onBeforeUnmount(() => {
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
})

// 切换测试面板可见性（记录用户偏好，切换 action 时自动恢复）
function toggleTestPanel(force?: boolean) {
  const next = force !== undefined ? force : !testPanelVisible.value
  testPanelVisible.value = next
  userWantsTestPanel.value = next
}

// ---------- Python 文件选择（便于 agent / 用户从本地 .py 加载脚本）----------
const pyFileInputRef = ref<HTMLInputElement | null>(null)

function pickPyFile() {
  // 触发原生文件选择对话框
  pyFileInputRef.value?.click()
}

function onPyFilePicked(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  // 限制 1MB，避免加载过大文件
  if (file.size > 1024 * 1024) {
    ElMessage.warning('文件过大（>1MB），请选择较小的 Python 脚本文件')
    input.value = ''
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    const content = reader.result as string
    form.value.modify_rules = content
    ElMessage.success(`已加载 ${file.name}（${file.size} 字节）`)
    input.value = ''  // 重置，允许再次选择同一文件
  }
  reader.onerror = () => {
    ElMessage.error('读取文件失败')
    input.value = ''
  }
  reader.readAsText(file)
}

// 默认预设（方便用户上手）
const testMock = reactive({
  host: 'api.example.com',
  path: '/v1/user',
  method: 'GET',
  scheme: 'https',
  httpVersion: 'HTTP/1.1',
  // headers 用 JSON 字符串编辑，便于多行/复制
  headers: '{\n  "User-Agent": "telnix-test/1.0",\n  "Accept": "application/json"\n}',
  body: '',
  // 模拟响应（可选；enableResp 控制是否启用）
  enableResp: false,
  respStatus: 200,
  respHeaders: '{\n  "Content-Type": "application/json"\n}',
  respBody: '{"code": 0, "data": {"id": 1}}',
  // 测试模式：request 只请求 / response 只响应 / both 请求+响应
  mode: 'both' as 'request' | 'response' | 'both',
})

// mode 切换时也需要重新测量（testMock.mode 变化导致内容块显隐）
watch(() => testMock.mode, () => {
  if (testPanelVisible.value) {
    nextTick(updateEditorHeight)
  }
})

function resetTestResult() {
  testResult.value = null
}

function parseHeadersJson(s: string): Record<string, string> | null {
  if (!s.trim()) return {}
  try {
    const obj = JSON.parse(s)
    if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
      ElMessage.warning('Headers 必须是 JSON 对象')
      return null
    }
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(obj)) {
      out[String(k)] = String(v)
    }
    return out
  } catch {
    ElMessage.warning('Headers JSON 解析失败')
    return null
  }
}

async function runTest() {
  if (!testPanelVisible.value) return
  const script = (form.value.modify_rules as string) || ''
  if (!script.trim()) {
    ElMessage.warning('请先填写脚本内容')
    return
  }

  const reqHeaders = parseHeadersJson(testMock.headers)
  if (reqHeaders === null) return

  // mode 决定是否需要 mock_response：
  // - request : 不需要
  // - response: 需要（用户若未填 respBody 也允许，给默认空响应）
  // - both    : 需要（同时调用 on_request + on_response）
  const mode = testMock.mode
  let mockResp = null
  if (mode !== 'request') {
    const respHeaders = parseHeadersJson(testMock.respHeaders)
    if (respHeaders === null) return
    mockResp = {
      status_code: Number(testMock.respStatus) || 200,
      headers: respHeaders,
      body: testMock.respBody || '',
    }
  }

  testRunning.value = true
  resetTestResult()
  try {
    const result = await api.testScript({
      script,
      mock_request: {
        host: testMock.host,
        path: testMock.path,
        method: testMock.method,
        scheme: testMock.scheme,
        http_version: testMock.httpVersion,
        headers: reqHeaders,
        body: testMock.body || '',
      },
      mock_response: mockResp,
      mode,
    })
    testResult.value = result
  } catch (e: any) {
    testResult.value = {
      ok: false,
      duration_ms: 0,
      error: e?.message || String(e),
      traceback: '',
      request_phase: null,
      response_phase: null,
    }
  } finally {
    testRunning.value = false
  }
}

// 弹窗宽度：展开测试面板时变宽以容纳左右分栏
const dialogWidth = computed(() => testPanelVisible.value ? '1080px' : '720px')

// 阶段 action -> el-tag type
function phaseActionTagType(action: string): 'success' | 'danger' | 'warning' | 'info' {
  if (action === 'drop') return 'danger'
  if (action === 'mock') return 'warning'
  if (action === 'continue') return 'success'
  return 'info'
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    @update:model-value="close"
    :title="rule?.id ? '编辑自动修改规则' : '新建自动修改规则'"
    :width="dialogWidth"
    :close-on-click-modal="false"
    align-center
    :append-to-body="true"
    :lock-scroll="true"
    class="rule-editor-dialog"
    :class="{ 'test-panel-open': testPanelVisible && isScript, 'script-mode': isScript }"
  >
    <!-- 顶部说明 -->
    <el-alert
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <template #title>
        URL 匹配后自动改写请求或响应。字段路径不含 <code>.</code> 时全局替换同名键；含 <code>.</code> 时按路径精确定位。
      </template>
    </el-alert>

    <el-form :model="form" label-width="96px" size="default">
      <el-form-item label="启用">
        <el-switch v-model="form.enabled" />
      </el-form-item>

      <el-form-item label="备注">
        <el-input
          v-model="form.note"
          placeholder="可选，规则说明"
          maxlength="100"
          show-word-limit
        />
      </el-form-item>

      <el-form-item label="URL 模式">
        <el-input
          v-model="form.pattern"
          placeholder="如 *example.com*"
          class="mono"
        />
        <div class="field-hint">
          匹配完整 URL（含 https://）。<code>*</code> 任意串，<code>?</code> 单字符。
        </div>
      </el-form-item>

      <el-form-item label="匹配方式">
        <el-radio-group v-model="form.match_mode">
          <el-radio value="wildcard">通配符（推荐）</el-radio>
          <el-radio value="exact">精确匹配</el-radio>
          <el-radio value="regex">正则表达式</el-radio>
        </el-radio-group>
      </el-form-item>

      <el-form-item label="动作">
        <el-select v-model="form.action" style="width: 220px">
          <el-option label="响应字段修改" value="modify_response" />
          <el-option label="请求字段修改" value="modify_request" />
          <el-option label="响应 Mock" value="mock" />
          <el-option label="请求 Mock" value="mock_request" />
          <el-option label="Python 脚本" value="script" />
        </el-select>
        <div class="field-hint">
          <code>响应字段修改</code> 服务器返回后改字段；
          <code>请求字段修改</code> 转发前改请求头/体；
          <code>响应 Mock</code> 不请求服务器，直接返回预设响应；
          <code>请求 Mock</code> 用预设请求转发到目标服务器，返回真实响应；
          <code>Python 脚本</code> 写 Python 代码处理复杂逻辑（on_request/on_response）。
        </div>
      </el-form-item>

      <!-- 响应 Mock 配置 -->
      <template v-if="isMock">
        <el-form-item label="状态码">
          <el-input-number
            v-model="form.mock_status"
            :min="100"
            :max="599"
            controls-position="right"
            :value-on-clear="null"
            placeholder="默认 200"
          />
          <span class="field-hint" style="margin-left: 8px">留空为 200</span>
        </el-form-item>
        <el-form-item label="响应头">
          <CodeEditor v-model="form.mock_headers as string" language="json" :min-height="'80px'" />
        </el-form-item>
        <el-form-item label="响应体">
          <CodeEditor v-model="form.mock_body" language="plaintext" :min-height="'120px'" placeholder="预设响应内容" />
        </el-form-item>
      </template>

      <!-- 请求 Mock 配置：用预设请求转发到目标服务器 -->
      <template v-if="isMockReq">
        <el-form-item label="请求方法">
          <el-select v-model="form.mock_method" style="width: 160px">
            <el-option v-for="m in HTTP_METHODS" :key="m" :label="m" :value="m" />
          </el-select>
        </el-form-item>
        <el-form-item label="请求 URL">
          <el-input
            v-model="form.mock_url"
            placeholder="如 https://api.example.com/v1/user"
            class="mono"
          />
          <div class="field-hint">
            匹配的请求会被替换为向此 URL 发送的新请求。
          </div>
        </el-form-item>
        <el-form-item label="请求头">
          <CodeEditor v-model="form.mock_headers as string" language="json" :min-height="'100px'" placeholder='JSON，如 {"Content-Type": "application/json"}' />
        </el-form-item>
        <el-form-item label="请求体">
          <CodeEditor v-model="form.mock_body" language="plaintext" :min-height="'120px'" placeholder="POST/PUT 的 body" />
        </el-form-item>
      </template>

      <!-- 响应字段修改 -->
      <template v-if="isModifyResp">
        <!-- 响应体字段替换 -->
        <el-form-item label="响应体字段">
          <div class="modify-section">
            <div class="section-hint">
              修改 JSON 响应中的字段。两种填法：
              <br />1. <b>只填字段名</b>（不含 <code>.</code>）：全局替换同名键，如 <code class="mono">count</code>。
              <br />2. <b>填完整路径</b>（含 <code>.</code>）：精确定位，如 <code class="mono">data.list[0].count</code>。
              <br />值自动识别数字 / 布尔 / null。
            </div>
            <div v-for="(m, i) in bodyRules" :key="i" class="modify-row">
              <el-select v-model="m.op" size="small" style="width: 80px">
                <el-option v-for="o in opOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-input
                v-model="m.key"
                size="small"
                placeholder="字段路径，如 data.list[0].count"
                class="mono"
                style="flex: 1.4"
              />
              <el-input
                v-if="m.op === 'replace'"
                v-model="m.value"
                size="small"
                placeholder="新值"
                class="mono"
                style="flex: 1"
              />
              <el-button link type="danger" size="small" @click="removeModifyByIdx(bodyRules, i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-button size="small" @click="addBodyRule">
              <el-icon><Plus /></el-icon>&nbsp;添加响应体字段规则
            </el-button>
          </div>
        </el-form-item>

        <!-- 响应头 -->
        <el-form-item label="响应头">
          <div class="modify-section">
            <div class="section-hint">
              修改响应头，如 <code class="mono">Content-Type</code>。
            </div>
            <div v-for="(m, i) in headerRules" :key="i" class="modify-row">
              <el-select v-model="m.op" size="small" style="width: 80px">
                <el-option v-for="o in opOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-input
                v-model="m.key"
                size="small"
                placeholder="头名，如 Content-Type"
                class="mono"
                style="flex: 1.2"
              />
              <el-input
                v-if="m.op === 'replace'"
                v-model="m.value"
                size="small"
                placeholder="新值"
                class="mono"
                style="flex: 1"
              />
              <el-button link type="danger" size="small" @click="removeModifyByIdx(headerRules, i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-button size="small" @click="addHeaderRule">
              <el-icon><Plus /></el-icon>&nbsp;添加响应头规则
            </el-button>
          </div>
        </el-form-item>
      </template>

      <!-- 请求字段修改 -->
      <template v-if="isModifyReq">
        <!-- 请求体字段替换 -->
        <el-form-item label="请求体字段">
          <div class="modify-section">
            <div class="section-hint">
              修改 JSON 请求体中的字段（转发前）。填法同上：
              <br />1. <b>只填字段名</b>：全局替换同名键。
              <br />2. <b>填完整路径</b>：如 <code class="mono">data.user_id</code>、<code class="mono">list[0].count</code>。
              <br />值自动识别数字 / 布尔 / null。
            </div>
            <div v-for="(m, i) in reqBodyRules" :key="i" class="modify-row">
              <el-select v-model="m.op" size="small" style="width: 80px">
                <el-option v-for="o in opOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-input
                v-model="m.key"
                size="small"
                placeholder="字段路径，如 data.user_id"
                class="mono"
                style="flex: 1.4"
              />
              <el-input
                v-if="m.op === 'replace'"
                v-model="m.value"
                size="small"
                placeholder="新值"
                class="mono"
                style="flex: 1"
              />
              <el-button link type="danger" size="small" @click="removeModifyByIdx(reqBodyRules, i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-button size="small" @click="addReqBodyRule">
              <el-icon><Plus /></el-icon>&nbsp;添加请求体字段规则
            </el-button>
          </div>
        </el-form-item>

        <!-- 请求头 -->
        <el-form-item label="请求头">
          <div class="modify-section">
            <div class="section-hint">
              修改请求头，例如改 <code class="mono">Authorization</code>、<code class="mono">User-Agent</code>、添加自定义头等。
            </div>
            <div v-for="(m, i) in reqHeaderRules" :key="i" class="modify-row">
              <el-select v-model="m.op" size="small" style="width: 80px">
                <el-option v-for="o in opOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-input
                v-model="m.key"
                size="small"
                placeholder="头名，如 Authorization"
                class="mono"
                style="flex: 1.2"
              />
              <el-input
                v-if="m.op === 'replace'"
                v-model="m.value"
                size="small"
                placeholder="新值"
                class="mono"
                style="flex: 1"
              />
              <el-button link type="danger" size="small" @click="removeModifyByIdx(reqHeaderRules, i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-button size="small" @click="addReqHeaderRule">
              <el-icon><Plus /></el-icon>&nbsp;添加请求头规则
            </el-button>
          </div>
        </el-form-item>
      </template>

      <!-- Python 脚本配置 -->
      <template v-if="isScript">
        <el-form-item label="脚本" class="script-form-item">
          <div class="script-section">
            <div class="section-hint">
              定义 <code>on_request(ctx)</code> / <code>on_response(ctx)</code> 函数处理请求/响应。
              <br />脚本在独立 worker 子进程运行，可 <code>import json/re/...</code>，单次调用超时 5 秒。
              <br />返回 <code>{"drop": True}</code> 拒绝请求；返回 <code>{"mock": True, "status": 200, "headers": {}, "body": b""}</code> 伪造响应。
            </div>

            <div class="script-test-toolbar">
              <el-button
                size="small"
                :type="testPanelVisible ? 'success' : 'primary'"
                plain
                @click="toggleTestPanel()"
              >
                <el-icon><VideoPlay v-if="!testPanelVisible" /><VideoPause v-else /></el-icon>
                &nbsp;{{ testPanelVisible ? '收起测试面板' : '展开测试面板' }}
              </el-button>
              <el-button size="small" plain @click="pickPyFile">
                <el-icon><Upload /></el-icon>&nbsp;从 .py 文件加载
              </el-button>
              <input
                ref="pyFileInputRef"
                type="file"
                accept=".py,text/x-python"
                style="display:none"
                @change="onPyFilePicked"
              />
            </div>

            <div class="script-with-test" :class="{ expanded: testPanelVisible }">
              <!-- 左侧 70%：Monaco 编辑器（不展开时单列高度 220px，展开时高度跟随内容） -->
              <div class="script-editor-side">
                <MonacoEditor
                  :model-value="(form.modify_rules as string) || ''"
                  @update:model-value="(v: string) => (form.modify_rules = v)"
                  language="python"
                  :height="editorHeight"
                >
                  <!-- 全屏放大时使用的测试面板内容 -->
                  <template #test-panel>
                    <div class="test-panel-header">
                      <span class="test-panel-title">
                        <el-icon><Cpu /></el-icon>&nbsp;测试面板
                      </span>
                    </div>
                    <div class="test-panel-body">
                      <!-- 测试模式：只请求 / 只响应 / 请求响应 -->
                      <div class="test-block">
                        <div class="test-block-title">测试模式</div>
                        <el-radio-group v-model="testMock.mode" size="small">
                          <el-radio-button value="request">只请求</el-radio-button>
                          <el-radio-button value="response">只响应</el-radio-button>
                          <el-radio-button value="both">请求+响应</el-radio-button>
                        </el-radio-group>
                        <div class="test-mode-hint">
                          <span v-if="testMock.mode === 'request'">仅调用 <code>on_request</code>，跳过 <code>on_response</code></span>
                          <span v-else-if="testMock.mode === 'response'">仅调用 <code>on_response</code>，跳过 <code>on_request</code></span>
                          <span v-else>先调用 <code>on_request</code>，再调用 <code>on_response</code></span>
                        </div>
                      </div>

                      <!-- 模拟请求（只在 mode != response 时需要填写） -->
                      <div v-if="testMock.mode !== 'response'" class="test-block">
                        <div class="test-block-title">模拟请求</div>
                        <div class="test-row">
                          <el-input v-model="testMock.host" size="small" placeholder="host" class="mono" />
                        </div>
                        <div class="test-row">
                          <el-input v-model="testMock.path" size="small" placeholder="path" class="mono" />
                        </div>
                        <div class="test-row-2">
                          <el-select v-model="testMock.method" size="small" style="width: 100px">
                            <el-option v-for="m in HTTP_METHODS" :key="m" :label="m" :value="m" />
                          </el-select>
                          <el-select v-model="testMock.scheme" size="small" style="width: 90px">
                            <el-option label="https" value="https" />
                            <el-option label="http" value="http" />
                          </el-select>
                          <el-select v-model="testMock.httpVersion" size="small" style="flex: 1">
                            <el-option label="HTTP/1.1" value="HTTP/1.1" />
                            <el-option label="HTTP/2" value="HTTP/2" />
                          </el-select>
                        </div>
                        <div class="test-label">Headers (JSON)</div>
                        <el-input
                          v-model="testMock.headers"
                          type="textarea"
                          :rows="4"
                          size="small"
                          class="mono"
                          placeholder='{"Content-Type": "application/json"}'
                        />
                        <div class="test-label">Body</div>
                        <el-input
                          v-model="testMock.body"
                          type="textarea"
                          :rows="3"
                          size="small"
                          class="mono"
                          placeholder="请求体（可为空）"
                        />
                      </div>

                      <!-- 模拟响应（只在 mode != request 时需要填写） -->
                      <div v-if="testMock.mode !== 'request'" class="test-block">
                        <div class="test-block-title">模拟响应</div>
                        <div class="test-row-2">
                          <span class="test-label-inline">状态码</span>
                          <el-input-number
                            v-model="testMock.respStatus"
                            size="small"
                            :min="100"
                            :max="599"
                            controls-position="right"
                            style="width: 110px"
                          />
                        </div>
                        <div class="test-label">Headers (JSON)</div>
                        <el-input
                          v-model="testMock.respHeaders"
                          type="textarea"
                          :rows="3"
                          size="small"
                          class="mono"
                        />
                        <div class="test-label">Body</div>
                        <el-input
                          v-model="testMock.respBody"
                          type="textarea"
                          :rows="3"
                          size="small"
                          class="mono"
                        />
                      </div>

                      <!-- 运行按钮 -->
                      <div class="test-run-row">
                        <el-button
                          type="primary"
                          size="small"
                          :loading="testRunning"
                          @click="runTest"
                        >
                          <el-icon><VideoPlay /></el-icon>&nbsp;运行测试
                        </el-button>
                        <el-button
                          v-if="testResult"
                          size="small"
                          @click="resetTestResult"
                        >清空结果</el-button>
                      </div>

                      <!-- 结果显示 -->
                      <div v-if="testResult" class="test-result">
                        <div class="test-result-meta">
                          <el-tag :type="testResult.ok ? 'success' : 'danger'" size="small">
                            {{ testResult.ok ? '成功' : '失败' }}
                          </el-tag>
                          <span class="test-duration">耗时 {{ testResult.duration_ms }}ms</span>
                        </div>

                        <div v-if="testResult.error" class="test-error">
                          <div class="test-error-msg">{{ testResult.error }}</div>
                          <pre v-if="testResult.traceback" class="test-traceback">{{ testResult.traceback }}</pre>
                        </div>

                        <div v-if="testResult.request_phase" class="test-phase">
                          <div class="test-phase-title">
                            on_request 阶段
                            <el-tag size="small" :type="phaseActionTagType(testResult.request_phase.action)">
                              {{ testResult.request_phase.action }}
                            </el-tag>
                            <el-tag v-if="!testResult.request_phase.available" size="small" type="info">未定义</el-tag>
                            <el-tag v-else-if="testResult.request_phase.modified" size="small" type="warning">已修改</el-tag>
                          </div>
                          <div v-if="testResult.request_phase.error" class="test-phase-error">
                            {{ testResult.request_phase.error }}
                          </div>
                          <template v-else-if="testResult.request_phase.available">
                            <div v-if="testResult.request_phase.action === 'mock'" class="test-mock-info">
                              Mock 响应：{{ testResult.request_phase.mock_status }}
                            </div>
                            <div class="test-sub-label">Headers</div>
                            <pre class="test-pre">{{ JSON.stringify(testResult.request_phase.headers, null, 2) }}</pre>
                            <div class="test-sub-label">Body</div>
                            <pre class="test-pre">{{ testResult.request_phase.body || '(空)' }}</pre>
                          </template>
                        </div>

                        <div v-if="testResult.response_phase" class="test-phase">
                          <div class="test-phase-title">
                            on_response 阶段
                            <el-tag size="small" :type="phaseActionTagType(testResult.response_phase.action)">
                              {{ testResult.response_phase.action }}
                            </el-tag>
                            <el-tag v-if="!testResult.response_phase.available" size="small" type="info">未定义</el-tag>
                            <el-tag v-else-if="testResult.response_phase.modified" size="small" type="warning">已修改</el-tag>
                          </div>
                          <div v-if="testResult.response_phase.error" class="test-phase-error">
                            {{ testResult.response_phase.error }}
                          </div>
                          <template v-else-if="testResult.response_phase.available">
                            <div class="test-sub-label">Status: {{ testResult.response_phase.status_code }}</div>
                            <div class="test-sub-label">Headers</div>
                            <pre class="test-pre">{{ JSON.stringify(testResult.response_phase.headers, null, 2) }}</pre>
                            <div class="test-sub-label">Body</div>
                            <pre class="test-pre">{{ testResult.response_phase.body || '(空)' }}</pre>
                          </template>
                        </div>
                      </div>
                    </div>
                  </template>
                </MonacoEditor>
              </div>

              <!-- 右侧 30%：测试面板（dialog 内左右分栏时显示） -->
              <transition name="test-slide">
                <div v-if="testPanelVisible" ref="testPanelSideRef" class="test-panel-side">
                  <div class="test-panel-header">
                    <span class="test-panel-title">
                      <el-icon><Cpu /></el-icon>&nbsp;测试面板
                    </span>
                    <el-button link size="small" @click="toggleTestPanel(false)">收起</el-button>
                  </div>

                  <div class="test-panel-body">
                    <!-- 测试模式：只请求 / 只响应 / 请求响应 -->
                    <div class="test-block">
                      <div class="test-block-title">测试模式</div>
                      <el-radio-group v-model="testMock.mode" size="small">
                        <el-radio-button value="request">只请求</el-radio-button>
                        <el-radio-button value="response">只响应</el-radio-button>
                        <el-radio-button value="both">请求+响应</el-radio-button>
                      </el-radio-group>
                      <div class="test-mode-hint">
                        <span v-if="testMock.mode === 'request'">仅调用 <code>on_request</code>，跳过 <code>on_response</code></span>
                        <span v-else-if="testMock.mode === 'response'">仅调用 <code>on_response</code>，跳过 <code>on_request</code></span>
                        <span v-else>先调用 <code>on_request</code>，再调用 <code>on_response</code></span>
                      </div>
                    </div>

                    <!-- 模拟请求（只在 mode != response 时需要填写） -->
                    <div v-if="testMock.mode !== 'response'" class="test-block">
                      <div class="test-block-title">模拟请求</div>
                      <div class="test-row">
                        <el-input v-model="testMock.host" size="small" placeholder="host" class="mono" />
                      </div>
                      <div class="test-row">
                        <el-input v-model="testMock.path" size="small" placeholder="path" class="mono" />
                      </div>
                      <div class="test-row-2">
                        <el-select v-model="testMock.method" size="small" style="width: 100px">
                          <el-option v-for="m in HTTP_METHODS" :key="m" :label="m" :value="m" />
                        </el-select>
                        <el-select v-model="testMock.scheme" size="small" style="width: 90px">
                          <el-option label="https" value="https" />
                          <el-option label="http" value="http" />
                        </el-select>
                        <el-select v-model="testMock.httpVersion" size="small" style="flex: 1">
                          <el-option label="HTTP/1.1" value="HTTP/1.1" />
                          <el-option label="HTTP/2" value="HTTP/2" />
                        </el-select>
                      </div>
                      <div class="test-label">Headers (JSON)</div>
                      <el-input
                        v-model="testMock.headers"
                        type="textarea"
                        :rows="4"
                        size="small"
                        class="mono"
                        placeholder='{"Content-Type": "application/json"}'
                      />
                      <div class="test-label">Body</div>
                      <el-input
                        v-model="testMock.body"
                        type="textarea"
                        :rows="3"
                        size="small"
                        class="mono"
                        placeholder="请求体（可为空）"
                      />
                    </div>

                    <!-- 模拟响应（只在 mode != request 时需要填写） -->
                    <div v-if="testMock.mode !== 'request'" class="test-block">
                      <div class="test-block-title">模拟响应</div>
                      <div class="test-row-2">
                        <span class="test-label-inline">状态码</span>
                        <el-input-number
                          v-model="testMock.respStatus"
                          size="small"
                          :min="100"
                          :max="599"
                          controls-position="right"
                          style="width: 110px"
                        />
                      </div>
                      <div class="test-label">Headers (JSON)</div>
                      <el-input
                        v-model="testMock.respHeaders"
                        type="textarea"
                        :rows="3"
                        size="small"
                        class="mono"
                      />
                      <div class="test-label">Body</div>
                      <el-input
                        v-model="testMock.respBody"
                        type="textarea"
                        :rows="3"
                        size="small"
                        class="mono"
                      />
                    </div>

                    <!-- 运行按钮 -->
                    <div class="test-run-row">
                      <el-button
                        type="primary"
                        size="small"
                        :loading="testRunning"
                        @click="runTest"
                      >
                        <el-icon><VideoPlay /></el-icon>&nbsp;运行测试
                      </el-button>
                      <el-button
                        v-if="testResult"
                        size="small"
                        @click="resetTestResult"
                      >清空结果</el-button>
                    </div>

                    <!-- 结果显示 -->
                    <div v-if="testResult" class="test-result">
                      <div class="test-result-meta">
                        <el-tag :type="testResult.ok ? 'success' : 'danger'" size="small">
                          {{ testResult.ok ? '成功' : '失败' }}
                        </el-tag>
                        <span class="test-duration">耗时 {{ testResult.duration_ms }}ms</span>
                      </div>

                      <div v-if="testResult.error" class="test-error">
                        <div class="test-error-msg">{{ testResult.error }}</div>
                        <pre v-if="testResult.traceback" class="test-traceback">{{ testResult.traceback }}</pre>
                      </div>

                      <!-- 请求阶段结果 -->
                      <div v-if="testResult.request_phase" class="test-phase">
                        <div class="test-phase-title">
                          on_request 阶段
                          <el-tag size="small" :type="phaseActionTagType(testResult.request_phase.action)">
                            {{ testResult.request_phase.action }}
                          </el-tag>
                          <el-tag v-if="!testResult.request_phase.available" size="small" type="info">未定义</el-tag>
                          <el-tag v-else-if="testResult.request_phase.modified" size="small" type="warning">已修改</el-tag>
                        </div>
                        <div v-if="testResult.request_phase.error" class="test-phase-error">
                          {{ testResult.request_phase.error }}
                        </div>
                        <template v-else-if="testResult.request_phase.available">
                          <div v-if="testResult.request_phase.action === 'mock'" class="test-mock-info">
                            Mock 响应：{{ testResult.request_phase.mock_status }}
                          </div>
                          <div class="test-sub-label">Headers</div>
                          <pre class="test-pre">{{ JSON.stringify(testResult.request_phase.headers, null, 2) }}</pre>
                          <div class="test-sub-label">Body</div>
                          <pre class="test-pre">{{ testResult.request_phase.body || '(空)' }}</pre>
                        </template>
                      </div>

                      <!-- 响应阶段结果 -->
                      <div v-if="testResult.response_phase" class="test-phase">
                        <div class="test-phase-title">
                          on_response 阶段
                          <el-tag size="small" :type="phaseActionTagType(testResult.response_phase.action)">
                            {{ testResult.response_phase.action }}
                          </el-tag>
                          <el-tag v-if="!testResult.response_phase.available" size="small" type="info">未定义</el-tag>
                          <el-tag v-else-if="testResult.response_phase.modified" size="small" type="warning">已修改</el-tag>
                        </div>
                        <div v-if="testResult.response_phase.error" class="test-phase-error">
                          {{ testResult.response_phase.error }}
                        </div>
                        <template v-else-if="testResult.response_phase.available">
                          <div class="test-sub-label">Status: {{ testResult.response_phase.status_code }}</div>
                          <div class="test-sub-label">Headers</div>
                          <pre class="test-pre">{{ JSON.stringify(testResult.response_phase.headers, null, 2) }}</pre>
                          <div class="test-sub-label">Body</div>
                          <pre class="test-pre">{{ testResult.response_phase.body || '(空)' }}</pre>
                        </template>
                      </div>
                    </div>
                  </div>
                </div>
              </transition>
            </div>
          </div>
        </el-form-item>
      </template>
    </el-form>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.modify-section { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.modify-row { display: flex; align-items: center; gap: 6px; }
.modify-row .el-input { flex: 1; }
.section-hint {
  font-size: 12px; color: var(--on-text-muted);
  background: var(--on-bg-hover, #1e1e2e);
  padding: 6px 10px; border-radius: 4px;
  line-height: 1.6;
}
/* code 标签使用全局 main.css 样式（亮/暗模式均正常），
   不再覆盖颜色，避免亮色模式白字+淡灰底不可读 */
.field-hint {
  font-size: 12px; color: var(--on-text-muted);
  margin-top: 2px; line-height: 1.5;
}
.mono { font-family: 'Consolas', 'Monaco', monospace; }

/* ---------- Python 脚本测试面板 ---------- */
.script-section { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.script-test-toolbar {
  display: flex; align-items: center; gap: 10px;
  padding: 4px 0;
  flex-wrap: wrap;
}

.script-with-test {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}
.script-with-test.expanded {
  /* 展开时左右分栏：68% 编辑器 + 30% 测试面板（间距 2%） */
  flex-direction: row;
  align-items: flex-start;  /* 顶部对齐，两侧各自按内容高度，MonacoEditor 高度由 JS 测量同步 */
  gap: 10px;
}
/* 默认（不展开）：编辑器全宽 */
.script-editor-side {
  width: 100%;
}
/* 展开时：编辑器左 68% */
.script-with-test.expanded .script-editor-side {
  flex: 0 0 68%;
  min-width: 0;
}
.test-panel-side {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--on-border, #333);
  border-radius: 6px;
  background: var(--on-bg-elevated, var(--on-bg, #fff));
  /* 不再设固定高度/overflow:hidden，让 dialog body 滚动 */
}
.test-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  background: var(--on-bg-hover, #f6f8fa);
  border-bottom: 1px solid var(--on-border, #d0d7de);
  font-size: 13px;
  font-weight: 600;
  color: var(--on-text, #1f2328);
  flex: 0 0 auto;
}
.test-panel-title { display: inline-flex; align-items: center; }
.test-panel-body {
  /* 不再 flex:1 + overflow-y:auto，让面板自然高度，dialog body 统一滚动 */
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.test-mode-hint {
  font-size: 11px;
  color: var(--on-text-muted);
  margin-top: 4px;
  line-height: 1.4;
}

.test-block {
  border: 1px solid var(--on-border-light, #e1e4e8);
  border-radius: 4px;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: var(--on-bg, #fff);
}
.test-block-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--on-text, #1f2328);
  margin-bottom: 2px;
}
.test-row { display: flex; gap: 6px; }
.test-row-2 { display: flex; gap: 6px; align-items: center; }
.test-label {
  font-size: 11px;
  color: var(--on-text-muted);
  margin-top: 4px;
  margin-bottom: 2px;
}
.test-label-inline {
  font-size: 11px;
  color: var(--on-text-muted);
  white-space: nowrap;
}
.test-run-row {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 0;
}

.test-result {
  border: 1px solid var(--on-border-light, #e1e4e8);
  border-radius: 4px;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--on-bg-hover, #f6f8fa);
}
.test-result-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}
.test-duration {
  font-size: 12px;
  color: var(--on-text-muted);
}
.test-error {
  background: rgba(207, 34, 46, 0.08);
  border: 1px solid var(--on-error, #cf222e);
  border-radius: 4px;
  padding: 6px 8px;
}
.test-error-msg {
  color: var(--on-error, #cf222e);
  font-size: 12px;
  font-weight: 600;
  white-space: pre-wrap;
  word-break: break-all;
}
.test-traceback {
  margin-top: 6px;
  font-size: 11px;
  color: var(--on-text-muted);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 140px;
  overflow-y: auto;
  font-family: 'Consolas', 'Monaco', monospace;
}
.test-phase {
  border-left: 2px solid var(--on-accent, #0d9488);
  padding: 4px 8px;
  background: var(--on-bg, #fff);
  border-radius: 0 4px 4px 0;
}
.test-phase-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--on-text, #1f2328);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.test-phase-error {
  font-size: 11px;
  color: var(--on-error, #cf222e);
  padding: 2px 4px;
}
.test-mock-info {
  font-size: 11px;
  color: var(--on-warn, #9a6700);
  margin: 2px 0;
}
.test-sub-label {
  font-size: 11px;
  color: var(--on-text-muted);
  margin-top: 4px;
  margin-bottom: 2px;
}
.test-pre {
  margin: 0;
  font-size: 11px;
  font-family: 'Consolas', 'Monaco', monospace;
  background: var(--on-bg-hover, #f6f8fa);
  border: 1px solid var(--on-border-light, #e1e4e8);
  border-radius: 3px;
  padding: 4px 6px;
  color: var(--on-text, #1f2328);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 140px;
  overflow-y: auto;
}

/* 展开测试面板的滑入动画 */
.test-slide-enter-active, .test-slide-leave-active {
  transition: all 0.2s ease;
  overflow: hidden;
}
.test-slide-enter-from, .test-slide-leave-to {
  opacity: 0;
  transform: translateX(20px);
  max-width: 0;
}
</style>

<!-- 非 scoped：append-to-body 后弹窗在 body 下，scoped 样式无法触达 -->
<style>
.rule-editor-dialog.el-dialog {
  /* 弹窗整体不超过视口高度，留出标题/底部按钮空间 */
  max-height: 92vh;
  display: flex;
  flex-direction: column;
  margin: 0 auto !important;
  transition: width 0.2s ease;
}
.rule-editor-dialog .el-dialog__body {
  /* body 内部滚动，标题和底部按钮始终可见。
     不再因 test-panel-open 切换 overflow（避免左右两栏独立滚动） */
  flex: 1;
  overflow-y: auto;
  max-height: calc(92vh - 110px); /* 减去标题(~55px) + 底部按钮(~55px) */
}
</style>
