<script setup lang="ts">
import { ref, watch, computed, reactive, onBeforeUnmount, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import type { AutoReplyRule, ModifyRule, ScriptTestResult } from '../api/client'
import { api } from '../api/client'
import CodeEditor from './CodeEditor.vue'
import MonacoEditor from './MonacoEditor.vue'

const { t } = useI18n()

const props = defineProps<{
  modelValue: boolean
  rule: AutoReplyRule | null
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  save: [rule: AutoReplyRule]
}>()

const form = ref<AutoReplyRule>(emptyRule())

const SCRIPT_TEMPLATE = `# Telnix Python 脚本
# 可用钩子：on_request（转发前）/ on_response（返回客户端前）
# ctx 属性：host / path / method / url / scheme / pid / process_name
#           request_headers(dict) / request_body(bytes)
#           on_response 额外有：status_code / response_headers / response_body
# 修改方法：ctx.set_request_header / set_request_body / set_response_header / set_response_body / set_status_code
# 返回 None：应用修改后继续；{"drop": True}：拒绝；{"mock": True, "status": 200, "headers": {}, "body": b""}：伪造响应
# === 调试功能 ===
# ctx.log(msg) 记录日志，支持格式化：ctx.log("user_id =", user_id)
# ctx.set_var("key", value) / ctx.get_var("key") 操作中间变量
# print() 输出自动捕获到 ctx.logs

def on_request(ctx):
    # 例：记录日志
    ctx.log("Processing request:", ctx.path)
    ctx.log(f"Method: {ctx.method}, Body length: {len(ctx.request_body)}")
    # 例：存储中间变量
    import json
    if ctx.request_body:
        data = json.loads(ctx.request_body)
        ctx.set_var("user_id", data.get("id"))
        ctx.log("Extracted user_id:", data.get("id"))
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
    # 例：记录响应信息
    ctx.log(f"Response status: {ctx.status_code}, body size: {len(ctx.response_body)}")
    # 例：读取请求阶段存储的变量
    user_id = ctx.get_var("user_id")
    if user_id:
        ctx.log("User ID from request:", user_id)
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
      if (form.value.action === 'script' && typeof form.value.modify_rules !== 'string') {
        form.value.modify_rules = SCRIPT_TEMPLATE
      }
      if (form.value.mock_headers && typeof form.value.mock_headers === 'object') {
        form.value.mock_headers = JSON.stringify(form.value.mock_headers, null, 2)
      }
      testPanelVisible.value = false
      userWantsTestPanel.value = false
    }
  },
  { deep: false }
)

watch(() => form.value.action, (newAction, oldAction) => {
  if (newAction === 'script') {
    if (typeof form.value.modify_rules !== 'string' || !form.value.modify_rules.trim()) {
      form.value.modify_rules = SCRIPT_TEMPLATE
    }
    if (userWantsTestPanel.value) {
      testPanelVisible.value = true
    }
  } else if (oldAction === 'script') {
    if (typeof form.value.modify_rules === 'string') {
      form.value.modify_rules = []
    }
    testPanelVisible.value = false
  }
})

const isMock = computed(() => form.value.action === 'mock')
const isMockReq = computed(() => form.value.action === 'mock_request')
const isModifyResp = computed(() => form.value.action === 'modify_response')
const isModifyReq = computed(() => form.value.action === 'modify_request')
const isScript = computed(() => form.value.action === 'script')

const HTTP_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']

const rulesArray = computed<ModifyRule[]>(() =>
  Array.isArray(form.value.modify_rules) ? (form.value.modify_rules as ModifyRule[]) : []
)
const bodyRules = computed(() =>
  rulesArray.value.filter((m) => m.target === 'response_body' && m.op !== 'append')
)
const headerRules = computed(() =>
  rulesArray.value.filter((m) => m.target === 'response_header')
)
const reqBodyRules = computed(() =>
  rulesArray.value.filter((m) => m.target === 'request_body' && m.op !== 'append')
)
const reqHeaderRules = computed(() =>
  rulesArray.value.filter((m) => m.target === 'request_header')
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
function removeModifyByIdx(list: ModifyRule[], idx: number) {
  const target = list[idx]
  if (!target) return
  const realIdx = (form.value.modify_rules as ModifyRule[]).indexOf(target)
  if (realIdx >= 0) (form.value.modify_rules as ModifyRule[]).splice(realIdx, 1)
}

function save() {
  if (!form.value.pattern.trim()) {
    ElMessage.warning(t('ruleEditor.urlPatternRequired'))
    return
  }
  if (form.value.action === 'script') {
    const script = form.value.modify_rules
    if (typeof script !== 'string' || !script.trim()) {
      ElMessage.warning(t('ruleEditor.scriptRequired'))
      return
    }
  }
  const payload = { ...form.value }
  if (typeof payload.mock_headers === 'string') {
    try {
      payload.mock_headers = JSON.parse(payload.mock_headers)
    } catch {
      payload.mock_headers = {}
    }
  }
  // P0: 分离 group_id 和 tags 到单独字段
  const { tags, group_id, ...rest } = payload as any
  emit('save', rest as AutoReplyRule)
  emit('update:modelValue', false)
}

function close() {
  emit('update:modelValue', false)
}

const opOptions = computed(() => [
  { label: t('ruleEditor.opReplace'), value: 'replace' },
  { label: t('ruleEditor.opRemove'), value: 'remove' },
])

const testPanelVisible = ref(false)
const userWantsTestPanel = ref(false)
const testRunning = ref(false)
const testResult = ref<ScriptTestResult | null>(null)
const testResultVisible = ref(false)

const editorHeight = computed(() => testPanelVisible.value ? '100%' : '200px')

function toggleTestPanel(force?: boolean) {
  const next = force !== undefined ? force : !testPanelVisible.value
  testPanelVisible.value = next
  userWantsTestPanel.value = next
}

const pyFileInputRef = ref<HTMLInputElement | null>(null)

function pickPyFile() {
  pyFileInputRef.value?.click()
}

function onPyFilePicked(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 1024 * 1024) {
    ElMessage.warning(t('ruleEditor.fileTooLarge'))
    input.value = ''
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    const content = reader.result as string
    form.value.modify_rules = content
    ElMessage.success(t('ruleEditor.fileLoaded', { name: file.name, size: file.size }))
    input.value = ''
  }
  reader.onerror = () => {
    ElMessage.error(t('ruleEditor.readFileFailed'))
    input.value = ''
  }
  reader.readAsText(file)
}

const testMock = reactive({
  host: 'api.example.com',
  path: '/v1/user',
  method: 'GET',
  scheme: 'https',
  httpVersion: 'HTTP/1.1',
  headers: '{\n  "User-Agent": "telnix-test/1.0",\n  "Accept": "application/json"\n}',
  body: '',
  enableResp: false,
  respStatus: 200,
  respHeaders: '{\n  "Content-Type": "application/json"\n}',
  respBody: '{"code": 0, "data": {"id": 1}}',
  mode: 'both' as 'request' | 'response' | 'both',
})

function resetTestResult() {
  testResult.value = null
}

function parseHeadersJson(s: string): Record<string, string> | null {
  if (!s.trim()) return {}
  try {
    const obj = JSON.parse(s)
    if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
      ElMessage.warning(t('ruleEditor.headersNotObject'))
      return null
    }
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(obj)) {
      out[String(k)] = String(v)
    }
    return out
  } catch {
    ElMessage.warning(t('ruleEditor.headersJsonFailed'))
    return null
  }
}

async function runTest() {
  const script = (form.value.modify_rules as string) || ''
  if (!script.trim()) {
    ElMessage.warning(t('ruleEditor.scriptRequired'))
    return
  }

  const reqHeaders = parseHeadersJson(testMock.headers)
  if (reqHeaders === null) return

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
    testResultVisible.value = true
  } catch (e: any) {
    testResult.value = {
      ok: false,
      duration_ms: 0,
      error: e?.message || String(e),
      traceback: '',
      request_phase: null,
      response_phase: null,
    }
    testResultVisible.value = true
  } finally {
    testRunning.value = false
  }
}

const dialogWidth = computed(() => {
  if (testPanelVisible.value && isScript.value) return '1100px'
  if (isScript.value) return '900px'
  if (isMock.value || isMockReq.value) return '680px'
  return '640px'
})

const dialogBodyMaxHeight = computed(() => {
  if (testPanelVisible.value && isScript.value) return 'calc(100vh - 160px)'
  return 'none'
})

function phaseActionTagType(action: string): 'success' | 'danger' | 'warning' | 'info' {
  if (action === 'drop') return 'danger'
  if (action === 'mock') return 'warning'
  if (action === 'continue') return 'success'
  return 'info'
}

// ========== P0 功能增强：预览匹配 ==========
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewResult = ref<{
  total: number
  samples: Array<{ flow_id: number; method: string; host: string; path: string; url: string }>
} | null>(null)

async function openPreview() {
  if (!form.value.pattern.trim()) {
    ElMessage.warning(t('ruleEditor.urlPatternRequired'))
    return
  }
  previewVisible.value = true
  previewLoading.value = true
  previewResult.value = null
  try {
    const result = await api.previewMatch({
      pattern: form.value.pattern,
      match_mode: form.value.match_mode,
      method_filter: (form.value as any).method_filter || '',
      status_filter: (form.value as any).status_filter || '',
      pid_filter: (form.value as any).pid_filter || '',
      process_filter: (form.value as any).process_filter || '',
      limit: 20,
    })
    previewResult.value = result
  } catch (e: any) {
    ElMessage.error(t('autoReply.previewMatchFailed') + (e?.message || e))
  } finally {
    previewLoading.value = false
  }
}

// ========== P0 功能增强：标签与分组 ==========
const groups = ref<Array<{ id: number; name: string; enabled: boolean; rule_count?: number }>>([])

async function loadGroups() {
  try {
    groups.value = await api.getRuleGroups()
  } catch {
    // 静默失败
  }
}

// 初始化时加载分组
onMounted(() => {
  loadGroups()
})
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    @update:model-value="close"
    :title="rule?.id ? t('ruleEditor.editTitle') : t('ruleEditor.newTitle')"
    :width="dialogWidth"
    :close-on-click-modal="false"
    :append-to-body="true"
    :lock-scroll="true"
    class="rule-editor-dialog"
    :class="{ 'test-panel-open': testPanelVisible && isScript }"
    destroy-on-close
  >
    <el-alert type="info" :closable="false" show-icon class="editor-alert">
      <template #title>{{ t('ruleEditor.alertHint') }}</template>
    </el-alert>

    <el-form :model="form" label-width="110px" label-position="left" size="small" class="rule-form">
      <el-form-item :label="t('ruleEditor.labelEnabled')">
        <el-switch v-model="form.enabled" />
      </el-form-item>

      <el-form-item :label="t('ruleEditor.labelNote')">
        <el-input
          v-model="form.note"
          :placeholder="t('ruleEditor.notePlaceholder')"
          maxlength="100"
          show-word-limit
        />
      </el-form-item>

      <!-- P0: 标签与分组 -->
      <el-form-item :label="t('autoReply.ruleEditorTags')">
        <el-input
          v-model="(form as any).tags"
          :placeholder="t('autoReply.tagsPlaceholder')"
          maxlength="200"
          style="width: 300px"
        />
        <div class="hint-text">{{ t('autoReply.tagsHint') }}</div>
      </el-form-item>

      <el-form-item :label="t('autoReply.ruleEditorGroup')">
        <el-select
          v-model="(form as any).group_id"
          clearable
          :placeholder="t('autoReply.groupNoRules')"
          style="width: 200px"
        >
          <el-option
            v-for="g in groups"
            :key="g.id"
            :label="g.name + (g.rule_count ? ` (${g.rule_count})` : '')"
            :value="g.id"
          />
        </el-select>
      </el-form-item>

      <el-form-item :label="t('ruleEditor.labelUrlPattern')">
        <el-input
          v-model="form.pattern"
          :placeholder="t('ruleEditor.patternPlaceholder')"
          class="mono"
          style="flex: 1"
        />
        <el-button
          size="small"
          type="primary"
          plain
          :loading="previewLoading"
          @click="openPreview"
          style="margin-left: 8px; flex-shrink: 0;"
        >
          <el-icon><Search /></el-icon>&nbsp;{{ t('autoReply.previewMatch') }}
        </el-button>
        <div class="hint-text" v-html="t('ruleEditor.patternHint')"></div>
      </el-form-item>

      <el-form-item :label="t('ruleEditor.labelMatchMode')">
        <el-radio-group v-model="form.match_mode" size="default">
          <el-radio value="wildcard">{{ t('ruleEditor.matchWildcard') }}</el-radio>
          <el-radio value="exact">{{ t('ruleEditor.matchExact') }}</el-radio>
          <el-radio value="regex">{{ t('ruleEditor.matchRegex') }}</el-radio>
        </el-radio-group>
      </el-form-item>

      <el-form-item :label="t('ruleEditor.labelAction')">
        <el-select v-model="form.action" style="width: 200px">
          <el-option :label="t('ruleEditor.actionModifyResponse')" value="modify_response" />
          <el-option :label="t('ruleEditor.actionModifyRequest')" value="modify_request" />
          <el-option :label="t('ruleEditor.actionMockResponse')" value="mock" />
          <el-option :label="t('ruleEditor.actionMockRequest')" value="mock_request" />
          <el-option :label="t('ruleEditor.actionScript')" value="script" />
        </el-select>
        <div class="hint-text" v-html="t('ruleEditor.actionHint')"></div>
      </el-form-item>

      <template v-if="isMock">
        <el-form-item :label="t('ruleEditor.labelStatusCode')">
          <el-input-number
            v-model="form.mock_status"
            :min="100"
            :max="599"
            controls-position="right"
            :value-on-clear="null"
            :placeholder="t('ruleEditor.statusPlaceholder')"
          />
          <span class="hint-inline">{{ t('ruleEditor.statusHint') }}</span>
        </el-form-item>
        <el-form-item :label="t('ruleEditor.labelRespHeaders')">
          <CodeEditor v-model="form.mock_headers as string" language="json" :min-height="'100px'" />
        </el-form-item>
        <el-form-item :label="t('ruleEditor.labelRespBody')">
          <CodeEditor v-model="form.mock_body" language="plaintext" :min-height="'140px'" :placeholder="t('ruleEditor.mockBodyPlaceholder')" />
        </el-form-item>
      </template>

      <template v-if="isMockReq">
        <el-form-item :label="t('ruleEditor.labelReqMethod')">
          <el-select v-model="form.mock_method" style="width: 140px">
            <el-option v-for="m in HTTP_METHODS" :key="m" :label="m" :value="m" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('ruleEditor.labelReqUrl')">
          <el-input
            v-model="form.mock_url"
            :placeholder="t('ruleEditor.mockUrlPlaceholder')"
            class="mono"
          />
          <div class="hint-text">{{ t('ruleEditor.mockUrlHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('ruleEditor.labelReqHeaders')">
          <CodeEditor v-model="form.mock_headers as string" language="json" :min-height="'100px'" :placeholder="t('ruleEditor.headersJsonPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('ruleEditor.labelReqBody')">
          <CodeEditor v-model="form.mock_body" language="plaintext" :min-height="'140px'" :placeholder="t('ruleEditor.mockReqBodyPlaceholder')" />
        </el-form-item>
      </template>

      <template v-if="isModifyResp">
        <el-form-item :label="t('ruleEditor.labelRespBodyField')">
          <div class="rule-section">
            <div class="section-hint" v-html="t('ruleEditor.respBodyFieldHint')"></div>
            <div v-for="(m, i) in bodyRules" :key="i" class="rule-row">
              <el-select v-model="m.op" style="width: 80px">
                <el-option v-for="o in opOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-input
                v-model="m.key"
                :placeholder="t('ruleEditor.fieldPathPlaceholder')"
                class="mono flex-1"
              />
              <el-input
                v-if="m.op === 'replace'"
                v-model="m.value"
                :placeholder="t('ruleEditor.newValuePlaceholder')"
                class="mono flex-1"
              />
              <el-button link type="danger" @click="removeModifyByIdx(bodyRules, i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-button @click="addBodyRule" type="primary" plain>
              <el-icon><Plus /></el-icon>&nbsp;{{ t('ruleEditor.addBodyRule') }}
            </el-button>
          </div>
        </el-form-item>

        <el-form-item :label="t('ruleEditor.labelRespHeaders')">
          <div class="rule-section">
            <div class="section-hint" v-html="t('ruleEditor.respHeaderHint')"></div>
            <div v-for="(m, i) in headerRules" :key="i" class="rule-row">
              <el-select v-model="m.op" style="width: 80px">
                <el-option v-for="o in opOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-input
                v-model="m.key"
                :placeholder="t('ruleEditor.headerNamePlaceholder')"
                class="mono flex-1"
              />
              <el-input
                v-if="m.op === 'replace'"
                v-model="m.value"
                :placeholder="t('ruleEditor.newValuePlaceholder')"
                class="mono flex-1"
              />
              <el-button link type="danger" @click="removeModifyByIdx(headerRules, i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-button @click="addHeaderRule" type="primary" plain>
              <el-icon><Plus /></el-icon>&nbsp;{{ t('ruleEditor.addHeaderRule') }}
            </el-button>
          </div>
        </el-form-item>
      </template>

      <template v-if="isModifyReq">
        <el-form-item :label="t('ruleEditor.labelReqBodyField')">
          <div class="rule-section">
            <div class="section-hint" v-html="t('ruleEditor.reqBodyFieldHint')"></div>
            <div v-for="(m, i) in reqBodyRules" :key="i" class="rule-row">
              <el-select v-model="m.op" style="width: 80px">
                <el-option v-for="o in opOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-input
                v-model="m.key"
                :placeholder="t('ruleEditor.reqFieldPathPlaceholder')"
                class="mono flex-1"
              />
              <el-input
                v-if="m.op === 'replace'"
                v-model="m.value"
                :placeholder="t('ruleEditor.newValuePlaceholder')"
                class="mono flex-1"
              />
              <el-button link type="danger" @click="removeModifyByIdx(reqBodyRules, i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-button @click="addReqBodyRule" type="primary" plain>
              <el-icon><Plus /></el-icon>&nbsp;{{ t('ruleEditor.addReqBodyRule') }}
            </el-button>
          </div>
        </el-form-item>

        <el-form-item :label="t('ruleEditor.labelReqHeaders')">
          <div class="rule-section">
            <div class="section-hint" v-html="t('ruleEditor.reqHeaderHint')"></div>
            <div v-for="(m, i) in reqHeaderRules" :key="i" class="rule-row">
              <el-select v-model="m.op" style="width: 80px">
                <el-option v-for="o in opOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-input
                v-model="m.key"
                :placeholder="t('ruleEditor.reqHeaderNamePlaceholder')"
                class="mono flex-1"
              />
              <el-input
                v-if="m.op === 'replace'"
                v-model="m.value"
                :placeholder="t('ruleEditor.newValuePlaceholder')"
                class="mono flex-1"
              />
              <el-button link type="danger" @click="removeModifyByIdx(reqHeaderRules, i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-button @click="addReqHeaderRule" type="primary" plain>
              <el-icon><Plus /></el-icon>&nbsp;{{ t('ruleEditor.addReqHeaderRule') }}
            </el-button>
          </div>
        </el-form-item>
      </template>

      <template v-if="isScript">
        <el-form-item :label="t('ruleEditor.labelScript')" class="script-form-item">
          <div class="script-container">
            <div class="section-hint" v-html="t('ruleEditor.scriptHint')"></div>

            <div class="script-toolbar">
              <el-button
                :type="testPanelVisible ? 'success' : 'primary'"
                plain
                @click="toggleTestPanel()"
              >
                <el-icon><VideoPlay v-if="!testPanelVisible" /><VideoPause v-else /></el-icon>
                &nbsp;{{ testPanelVisible ? t('ruleEditor.collapseTestPanel') : t('ruleEditor.expandTestPanel') }}
              </el-button>
              <el-button plain @click="pickPyFile">
                <el-icon><Upload /></el-icon>&nbsp;{{ t('ruleEditor.loadFromPyFile') }}
              </el-button>
              <input
                ref="pyFileInputRef"
                type="file"
                accept=".py,text/x-python"
                style="display:none"
                @change="onPyFilePicked"
              />
            </div>

            <div class="script-layout" :class="{ 'with-test': testPanelVisible }">
              <div class="script-editor-area">
                <MonacoEditor
                  :model-value="(form.modify_rules as string) || ''"
                  @update:model-value="(v: string) => (form.modify_rules = v)"
                  language="python"
                  :height="testPanelVisible ? '100%' : editorHeight"
                  :show-collapse-button="false"
                >
                  <template #test-panel>
                    <div class="test-panel-header">
                      <span class="test-panel-title">
                        <el-icon><Cpu /></el-icon>&nbsp;{{ t('ruleEditor.testPanel') }}
                      </span>
                    </div>
                    <div class="test-panel-content">
                      <div class="test-section">
                        <div class="test-section-title">{{ t('ruleEditor.testMode') }}</div>
                        <el-radio-group v-model="testMock.mode" size="small">
                          <el-radio-button value="request">{{ t('ruleEditor.modeRequestOnly') }}</el-radio-button>
                          <el-radio-button value="response">{{ t('ruleEditor.modeResponseOnly') }}</el-radio-button>
                          <el-radio-button value="both">{{ t('ruleEditor.modeBoth') }}</el-radio-button>
                        </el-radio-group>
                        <div class="mode-hint">
                          <span v-if="testMock.mode === 'request'" v-html="t('ruleEditor.modeHintRequest')"></span>
                          <span v-else-if="testMock.mode === 'response'" v-html="t('ruleEditor.modeHintResponse')"></span>
                          <span v-else v-html="t('ruleEditor.modeHintBoth')"></span>
                        </div>
                      </div>

                      <div v-if="testMock.mode !== 'response'" class="test-section">
                        <div class="test-section-title">{{ t('ruleEditor.mockRequest') }}</div>
                        <div class="test-row">
                          <el-input v-model="testMock.host" size="small" placeholder="host" class="mono" />
                        </div>
                        <div class="test-row">
                          <el-input v-model="testMock.path" size="small" placeholder="path" class="mono" />
                        </div>
                        <div class="test-row-group">
                          <el-select v-model="testMock.method" size="small" style="width: 90px">
                            <el-option v-for="m in HTTP_METHODS" :key="m" :label="m" :value="m" />
                          </el-select>
                          <el-select v-model="testMock.scheme" size="small" style="width: 80px">
                            <el-option label="https" value="https" />
                            <el-option label="http" value="http" />
                          </el-select>
                          <el-select v-model="testMock.httpVersion" size="small" style="flex: 1">
                            <el-option label="HTTP/1.1" value="HTTP/1.1" />
                            <el-option label="HTTP/2" value="HTTP/2" />
                          </el-select>
                        </div>
                        <div class="test-label">{{ t('ruleEditor.headersJson') }}</div>
                        <el-input v-model="testMock.headers" type="textarea" :rows="3" size="small" class="mono" />
                        <div class="test-label">{{ t('ruleEditor.body') }}</div>
                        <el-input v-model="testMock.body" type="textarea" :rows="2" size="small" class="mono" />
                      </div>

                      <div v-if="testMock.mode !== 'request'" class="test-section">
                        <div class="test-section-title">{{ t('ruleEditor.mockResponse') }}</div>
                        <div class="test-row-group">
                          <span class="test-label-inline">{{ t('ruleEditor.statusCode') }}</span>
                          <el-input-number v-model="testMock.respStatus" size="small" :min="100" :max="599" controls-position="right" style="width: 100px" />
                        </div>
                        <div class="test-label">{{ t('ruleEditor.headersJson') }}</div>
                        <el-input v-model="testMock.respHeaders" type="textarea" :rows="2" size="small" class="mono" />
                        <div class="test-label">{{ t('ruleEditor.body') }}</div>
                        <el-input v-model="testMock.respBody" type="textarea" :rows="2" size="small" class="mono" />
                      </div>

                      <div class="test-run-section">
                        <el-button type="primary" :loading="testRunning" @click="runTest">
                          <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('ruleEditor.runTest') }}
                        </el-button>
                      </div>
                    </div>
                  </template>
                </MonacoEditor>
              </div>

              <div v-if="testPanelVisible" class="test-panel-area">
                <div class="test-panel-header">
                  <span class="test-panel-title">
                    <el-icon><Cpu /></el-icon>&nbsp;{{ t('ruleEditor.testPanel') }}
                  </span>
                  <el-button link size="small" @click="toggleTestPanel(false)">
                    {{ t('ruleEditor.collapseTestPanel') }}
                  </el-button>
                </div>
                <div class="test-panel-content">
                  <div class="test-section">
                    <div class="test-section-title">{{ t('ruleEditor.testMode') }}</div>
                    <el-radio-group v-model="testMock.mode" size="small">
                      <el-radio-button value="request">{{ t('ruleEditor.modeRequestOnly') }}</el-radio-button>
                      <el-radio-button value="response">{{ t('ruleEditor.modeResponseOnly') }}</el-radio-button>
                      <el-radio-button value="both">{{ t('ruleEditor.modeBoth') }}</el-radio-button>
                    </el-radio-group>
                    <div class="mode-hint">
                      <span v-if="testMock.mode === 'request'" v-html="t('ruleEditor.modeHintRequest')"></span>
                      <span v-else-if="testMock.mode === 'response'" v-html="t('ruleEditor.modeHintResponse')"></span>
                      <span v-else v-html="t('ruleEditor.modeHintBoth')"></span>
                    </div>
                  </div>

                  <div v-if="testMock.mode !== 'response'" class="test-section">
                    <div class="test-section-title">{{ t('ruleEditor.mockRequest') }}</div>
                    <div class="test-row">
                      <el-input v-model="testMock.host" size="small" placeholder="host" class="mono" />
                    </div>
                    <div class="test-row">
                      <el-input v-model="testMock.path" size="small" placeholder="path" class="mono" />
                    </div>
                    <div class="test-row-group">
                      <el-select v-model="testMock.method" size="small" style="width: 90px">
                        <el-option v-for="m in HTTP_METHODS" :key="m" :label="m" :value="m" />
                      </el-select>
                      <el-select v-model="testMock.scheme" size="small" style="width: 80px">
                        <el-option label="https" value="https" />
                        <el-option label="http" value="http" />
                      </el-select>
                      <el-select v-model="testMock.httpVersion" size="small" style="flex: 1">
                        <el-option label="HTTP/1.1" value="HTTP/1.1" />
                        <el-option label="HTTP/2" value="HTTP/2" />
                      </el-select>
                    </div>
                    <div class="test-label">{{ t('ruleEditor.headersJson') }}</div>
                    <el-input v-model="testMock.headers" type="textarea" :rows="3" size="small" class="mono" />
                    <div class="test-label">{{ t('ruleEditor.body') }}</div>
                    <el-input v-model="testMock.body" type="textarea" :rows="2" size="small" class="mono" />
                  </div>

                  <div v-if="testMock.mode !== 'request'" class="test-section">
                    <div class="test-section-title">{{ t('ruleEditor.mockResponse') }}</div>
                    <div class="test-row-group">
                      <span class="test-label-inline">{{ t('ruleEditor.statusCode') }}</span>
                      <el-input-number v-model="testMock.respStatus" size="small" :min="100" :max="599" controls-position="right" style="width: 100px" />
                    </div>
                    <div class="test-label">{{ t('ruleEditor.headersJson') }}</div>
                    <el-input v-model="testMock.respHeaders" type="textarea" :rows="2" size="small" class="mono" />
                    <div class="test-label">{{ t('ruleEditor.body') }}</div>
                    <el-input v-model="testMock.respBody" type="textarea" :rows="2" size="small" class="mono" />
                  </div>

                  <div class="test-run-section">
                    <el-button type="primary" :loading="testRunning" @click="runTest">
                      <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('ruleEditor.runTest') }}
                    </el-button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </el-form-item>
      </template>
    </el-form>

    <template #footer>
      <el-button @click="close">{{ t('common.cancel') }}</el-button>
      <el-button type="primary" @click="save">{{ t('common.save') }}</el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="testResultVisible"
    :title="t('ruleEditor.testResultTitle')"
    width="700px"
    :append-to-body="true"
    class="test-result-dialog"
  >
    <div v-if="testResult" class="test-result-content">
      <div class="result-meta">
        <el-tag :type="testResult.ok ? 'success' : 'danger'" size="default">
          {{ testResult.ok ? t('ruleEditor.success') : t('ruleEditor.failed') }}
        </el-tag>
        <span class="duration">{{ t('ruleEditor.duration', { ms: testResult.duration_ms }) }}</span>
      </div>

      <div v-if="testResult.error" class="result-error">
        <div class="error-msg">{{ testResult.error }}</div>
        <pre v-if="testResult.traceback" class="traceback">{{ testResult.traceback }}</pre>
      </div>

      <div v-if="testResult.request_phase" class="result-phase">
        <div class="phase-title">
          {{ t('ruleEditor.onRequestPhase') }}
          <el-tag size="small" :type="phaseActionTagType(testResult.request_phase.action)">
            {{ testResult.request_phase.action }}
          </el-tag>
          <el-tag v-if="!testResult.request_phase.available" size="small" type="info">{{ t('ruleEditor.undefined') }}</el-tag>
          <el-tag v-else-if="testResult.request_phase.modified" size="small" type="warning">{{ t('ruleEditor.modified') }}</el-tag>
        </div>
        <div v-if="testResult.request_phase.error" class="phase-error">{{ testResult.request_phase.error }}</div>
        <template v-else-if="testResult.request_phase.available">
          <div v-if="testResult.request_phase.action === 'mock'" class="mock-info">
            {{ t('ruleEditor.mockResponseStatus', { status: testResult.request_phase.mock_status }) }}
          </div>
          <div class="result-sub-label">{{ t('ruleEditor.headersJson') }}</div>
          <pre class="result-pre">{{ JSON.stringify(testResult.request_phase.headers, null, 2) }}</pre>
          <div class="result-sub-label">{{ t('ruleEditor.body') }}</div>
          <pre class="result-pre">{{ testResult.request_phase.body || t('ruleEditor.empty') }}</pre>
        </template>
      </div>

      <div v-if="testResult.response_phase" class="result-phase">
        <div class="phase-title">
          {{ t('ruleEditor.onResponsePhase') }}
          <el-tag size="small" :type="phaseActionTagType(testResult.response_phase.action)">
            {{ testResult.response_phase.action }}
          </el-tag>
          <el-tag v-if="!testResult.response_phase.available" size="small" type="info">{{ t('ruleEditor.undefined') }}</el-tag>
          <el-tag v-else-if="testResult.response_phase.modified" size="small" type="warning">{{ t('ruleEditor.modified') }}</el-tag>
        </div>
        <div v-if="testResult.response_phase.error" class="phase-error">{{ testResult.response_phase.error }}</div>
        <template v-else-if="testResult.response_phase.available">
          <div class="result-sub-label">{{ t('common.status') }}: {{ testResult.response_phase.status_code }}</div>
          <div class="result-sub-label">{{ t('ruleEditor.headersJson') }}</div>
          <pre class="result-pre">{{ JSON.stringify(testResult.response_phase.headers, null, 2) }}</pre>
          <div class="result-sub-label">{{ t('ruleEditor.body') }}</div>
          <pre class="result-pre">{{ testResult.response_phase.body || t('ruleEditor.empty') }}</pre>
        </template>
        <!-- === 调试增强：日志 === -->
        <div v-if="testResult.response_phase.logs?.length" class="debug-section">
          <div class="debug-title">
            <el-icon><Tickets /></el-icon>&nbsp;{{ t('ruleEditor.debugLogs') }}
          </div>
          <pre class="debug-output">{{ testResult.response_phase.logs.join('') }}</pre>
        </div>
        <!-- === 调试增强：中间变量 === -->
        <div v-if="testResult.response_phase.variables && Object.keys(testResult.response_phase.variables).length > 0" class="debug-section">
          <div class="debug-title">
            <el-icon><Collection /></el-icon>&nbsp;{{ t('ruleEditor.debugVariables') }}
          </div>
          <pre class="debug-output">{{ JSON.stringify(testResult.response_phase.variables, null, 2) }}</pre>
        </div>
      </div>
      <!-- === 调试增强：请求阶段日志和变量 === -->
      <div v-if="testResult.request_phase">
        <!-- 请求阶段日志 -->
        <div v-if="testResult.request_phase.logs?.length" class="debug-section">
          <div class="debug-title">
            <el-icon><Tickets /></el-icon>&nbsp;{{ t('ruleEditor.debugLogs') }} ({{ t('ruleEditor.onRequestPhase') }})
          </div>
          <pre class="debug-output">{{ testResult.request_phase.logs.join('') }}</pre>
        </div>
        <!-- 请求阶段中间变量 -->
        <div v-if="testResult.request_phase.variables && Object.keys(testResult.request_phase.variables).length > 0" class="debug-section">
          <div class="debug-title">
            <el-icon><Collection /></el-icon>&nbsp;{{ t('ruleEditor.debugVariables') }} ({{ t('ruleEditor.onRequestPhase') }})
          </div>
          <pre class="debug-output">{{ JSON.stringify(testResult.request_phase.variables, null, 2) }}</pre>
        </div>
      </div>
    </div>
    <template #footer>
      <el-button @click="resetTestResult">{{ t('ruleEditor.clearResult') }}</el-button>
      <el-button type="primary" @click="testResultVisible = false">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>

  <!-- P0: 预览匹配结果对话框 -->
  <el-dialog
    v-model="previewVisible"
    :title="t('autoReply.previewMatchTitle')"
    width="600px"
    :append-to-body="true"
    class="preview-match-dialog"
  >
    <div v-if="previewLoading" class="preview-loading">
      <el-icon class="is-loading"><Loading /></el-icon>&nbsp;{{ t('autoReply.previewMatchLoading') }}
    </div>
    <template v-else-if="previewResult">
      <div class="preview-summary">
        <el-tag :type="previewResult.total > 0 ? 'success' : 'info'" size="large">
          {{ t('autoReply.previewMatchCount', { n: previewResult.total }) }}
        </el-tag>
      </div>
      <div v-if="previewResult.samples.length > 0" class="preview-samples">
        <div class="preview-samples-title">{{ t('autoReply.previewMatchSample') }}</div>
        <div class="preview-samples-list">
          <div
            v-for="sample in previewResult.samples"
            :key="sample.flow_id"
            class="preview-sample-item"
          >
            <el-tag size="small" type="primary">{{ sample.method }}</el-tag>
            <span class="preview-sample-url" :title="sample.url">{{ sample.host }}{{ sample.path }}</span>
          </div>
        </div>
      </div>
      <el-empty v-else :description="t('autoReply.previewMatchNoFlow')" />
    </template>
    <template #footer>
      <el-button type="primary" @click="previewVisible = false">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.rule-form {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.rule-form :deep(.el-form-item) {
  margin-bottom: 10px;
}

.rule-form :deep(.el-form-item__label) {
  font-size: 13px;
  font-weight: 500;
  padding-bottom: 6px !important;
}

.rule-form :deep(.el-input__inner) {
  font-size: 13px;
}

.rule-form :deep(.el-select) {
  --el-select-input-focus-border-color: var(--on-accent);
}

.rule-form :deep(.el-radio) {
  margin-right: 12px;
}

.editor-alert {
  margin-bottom: 16px;
}

.hint-text {
  font-size: 12px;
  color: var(--on-text-dim);
  margin-top: 4px;
  line-height: 1.5;
}

.hint-inline {
  font-size: 12px;
  color: var(--on-text-dim);
  margin-left: 10px;
}

.mono {
  font-family: 'Consolas', 'Monaco', monospace;
}

.flex-1 {
  flex: 1;
}

.rule-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.section-hint {
  font-size: 12px;
  color: var(--on-text-dim);
  background: var(--on-bg-hover);
  padding: 6px 10px;
  border-radius: 4px;
  line-height: 1.6;
}

.rule-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rule-row .el-select,
.rule-row .el-input {
  flex-shrink: 0;
}

.script-container {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
  overflow: hidden;
}

/* 测试面板展开时：撑满 dialog body，内部各自滚动，避免外部 body 滚动 */
.test-panel-open .el-dialog__body .script-container {
  flex: 1;
  min-height: 0;
}

.script-toolbar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 8px;
  background: var(--on-bg-elevated);
  border-radius: 4px;
  flex-shrink: 0;
}

.script-toolbar :deep(.el-button) {
  padding: 4px 10px;
  font-size: 12px;
  background: var(--on-bg-hover) !important;
  border-color: var(--on-border-light) !important;
  color: var(--on-text) !important;
}

.script-toolbar :deep(.el-button:hover) {
  background: var(--on-bg-hover) !important;
  border-color: var(--on-border-light) !important;
  color: var(--on-text) !important;
}

.script-toolbar :deep(.el-button .el-icon) {
  font-size: 12px;
}

.script-layout {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
  max-height: 420px;
  overflow: hidden;
  flex-shrink: 0;
}

.script-layout.with-test {
  flex-direction: row;
  align-items: stretch;
  max-height: 100%;
  overflow: hidden;
  flex-shrink: 0;
}

.script-editor-area {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 180px;
  max-height: 100%;
}

.script-layout.with-test .script-editor-area {
  flex: 0 0 65%;
  max-height: 100%;
}

.test-panel-area {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--on-border-light);
  border-radius: 6px;
  background: var(--on-bg-card);
  display: flex;
  flex-direction: column;
  max-height: 100%;
  min-height: 200px;
}

.script-layout.with-test .test-panel-area {
  min-height: 200px;
}

.test-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: var(--on-bg-hover);
  border-bottom: 1px solid var(--on-border-light);
  border-radius: 6px 6px 0 0;
}

.test-panel-title {
  display: inline-flex;
  align-items: center;
  font-size: 14px;
  font-weight: 600;
}

.test-panel-content {
  padding: 10px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.test-section {
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.test-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--on-text);
}

.mode-hint {
  font-size: 11px;
  color: var(--on-text-dim);
  line-height: 1.4;
}

.test-row {
  display: flex;
  gap: 6px;
}

.test-row-group {
  display: flex;
  gap: 6px;
  align-items: center;
}

.test-label {
  font-size: 11px;
  color: var(--on-text-dim);
  margin-top: 4px;
}

.test-label-inline {
  font-size: 11px;
  color: var(--on-text-dim);
  white-space: nowrap;
}

.test-run-section {
  display: flex;
  gap: 8px;
  align-items: center;
  padding-top: 4px;
}

.test-result-section {
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--on-bg-elevated);
}

.result-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.duration {
  font-size: 12px;
  color: var(--on-text-dim);
}

.result-error {
  background: rgba(245, 108, 108, 0.1);
  border: 1px solid var(--on-error);
  border-radius: 4px;
  padding: 6px 8px;
}

.error-msg {
  color: var(--on-error);
  font-size: 12px;
  font-weight: 500;
  white-space: pre-wrap;
  word-break: break-all;
}

.traceback {
  margin-top: 6px;
  font-size: 11px;
  color: var(--on-text-dim);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 120px;
  overflow-y: auto;
  font-family: 'Consolas', 'Monaco', monospace;
}

.result-phase {
  border-left: 3px solid var(--on-accent);
  padding: 6px 8px;
  background: var(--on-bg-card);
  border-radius: 0 4px 4px 0;
}

.phase-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--on-text);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.phase-error {
  font-size: 11px;
  color: var(--on-error);
  padding: 2px 4px;
}

.mock-info {
  font-size: 11px;
  color: var(--on-warn);
  margin: 2px 0;
}

.result-sub-label {
  font-size: 11px;
  color: var(--on-text-dim);
  margin-top: 4px;
  margin-bottom: 2px;
}

.result-pre {
  margin: 0;
  font-size: 11px;
  font-family: 'Consolas', 'Monaco', monospace;
  background: var(--on-bg-hover);
  border: 1px solid var(--on-border-light);
  border-radius: 3px;
  padding: 4px 6px;
  color: var(--on-text);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 100px;
  overflow-y: auto;
}

.slide-in-enter-active,
.slide-in-leave-active {
  transition: all 0.2s ease;
}

.slide-in-enter-from,
.slide-in-leave-to {
  opacity: 0;
  transform: translateX(20px);
}

.test-result-dialog .test-result-content {
  max-height: 60vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.test-result-dialog .result-phase {
  margin-top: 4px;
}

.test-result-dialog .result-pre {
  max-height: 150px;
}

/* === 调试增强样式 === */
.debug-section {
  margin-top: 8px;
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
  overflow: hidden;
}

.debug-title {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--on-text);
  padding: 4px 8px;
  background: var(--on-bg-hover);
  border-bottom: 1px solid var(--on-border-light);
}

.debug-output {
  margin: 0;
  font-size: 11px;
  font-family: 'Consolas', 'Monaco', monospace;
  background: var(--on-bg-elevated);
  padding: 6px 8px;
  color: var(--on-text);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 120px;
  overflow-y: auto;
}

/* P0: 预览匹配对话框 */
.preview-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
  color: var(--on-text-dim);
}

.preview-summary {
  margin-bottom: 16px;
}

.preview-samples {
  margin-top: 12px;
}

.preview-samples-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--on-text);
}

.preview-samples-list {
  max-height: 300px;
  overflow-y: auto;
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
}

.preview-sample-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--on-border-light);
  font-size: 12px;
}

.preview-sample-item:last-child {
  border-bottom: none;
}

.preview-sample-url {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: 'Consolas', 'Monaco', monospace;
  color: var(--on-text);
}
</style>

<style>
.rule-editor-dialog {
  display: flex !important;
  flex-direction: column !important;
  align-items: stretch !important;
}

.rule-editor-dialog .el-dialog__header {
  padding: 12px 20px 8px !important;
  margin-right: 0 !important;
}

.rule-editor-dialog .el-dialog__body {
  padding: 12px 20px !important;
  flex: none !important;
  overflow-y: auto !important;
  max-height: calc(100vh - 200px) !important;
}

.rule-editor-dialog .el-dialog__footer {
  padding: 12px 20px 16px !important;
  text-align: center !important;
  border-top: 1px solid var(--on-border-light) !important;
}

.rule-editor-dialog .el-dialog__footer .el-button {
  margin: 0 8px;
  padding: 6px 14px;
  font-size: 13px;
}

.rule-editor-dialog .el-form-item {
  margin-bottom: 12px !important;
}

.rule-editor-dialog .el-form-item:last-child {
  margin-bottom: 0 !important;
}
</style>
