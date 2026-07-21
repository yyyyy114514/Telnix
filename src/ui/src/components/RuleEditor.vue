<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import type { AutoReplyRule, ModifyRule } from '../api/client'
import CodeEditor from './CodeEditor.vue'

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
      // mock_headers 从后端返回的是 dict，编辑时转成字符串
      if (form.value.mock_headers && typeof form.value.mock_headers === 'object') {
        form.value.mock_headers = JSON.stringify(form.value.mock_headers, null, 2)
      }
    }
  },
  { deep: false }
)

const isMock = computed(() => form.value.action === 'mock')
const isMockReq = computed(() => form.value.action === 'mock_request')
const isModifyResp = computed(() => form.value.action === 'modify_response')
const isModifyReq = computed(() => form.value.action === 'modify_request')

const HTTP_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']

// 响应体字段替换规则
const bodyRules = computed(() =>
  (form.value.modify_rules || []).filter(
    (m) => m.target === 'response_body' && m.op !== 'append'
  )
)
// 响应头规则
const headerRules = computed(() =>
  (form.value.modify_rules || []).filter((m) => m.target === 'response_header')
)
// 请求体字段替换规则
const reqBodyRules = computed(() =>
  (form.value.modify_rules || []).filter(
    (m) => m.target === 'request_body' && m.op !== 'append'
  )
)
// 请求头规则
const reqHeaderRules = computed(() =>
  (form.value.modify_rules || []).filter((m) => m.target === 'request_header')
)

function addBodyRule() {
  form.value.modify_rules!.push({
    target: 'response_body',
    op: 'replace',
    key: '',
    value: '',
  })
}
function addHeaderRule() {
  form.value.modify_rules!.push({
    target: 'response_header',
    op: 'replace',
    key: '',
    value: '',
  })
}
function addReqBodyRule() {
  form.value.modify_rules!.push({
    target: 'request_body',
    op: 'replace',
    key: '',
    value: '',
  })
}
function addReqHeaderRule() {
  form.value.modify_rules!.push({
    target: 'request_header',
    op: 'replace',
    key: '',
    value: '',
  })
}
function removeModify(i: number) {
  form.value.modify_rules!.splice(i, 1)
}
// 根据 modify_rules 中的实际位置删除
function removeModifyByIdx(list: ModifyRule[], idx: number) {
  const target = list[idx]
  if (!target) return
  const realIdx = form.value.modify_rules!.indexOf(target)
  if (realIdx >= 0) form.value.modify_rules!.splice(realIdx, 1)
}

function save() {
  if (!form.value.pattern.trim()) {
    return
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
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    @update:model-value="close"
    :title="rule?.id ? '编辑自动修改规则' : '新建自动修改规则'"
    width="720px"
    :close-on-click-modal="false"
    align-center
    :append-to-body="true"
    :lock-scroll="true"
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
        </el-select>
        <div class="field-hint">
          <code>响应字段修改</code> 服务器返回后改字段；
          <code>请求字段修改</code> 转发前改请求头/体；
          <code>响应 Mock</code> 不请求服务器，直接返回预设响应；
          <code>请求 Mock</code> 用预设请求转发到目标服务器，返回真实响应。
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
.section-hint code {
  background: rgba(0,0,0,0.3);
  color: #ffffff;
  padding: 1px 5px; border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
}
.field-hint {
  font-size: 12px; color: var(--on-text-muted);
  margin-top: 2px; line-height: 1.5;
}
.field-hint code {
  background: rgba(0,0,0,0.3);
  color: #ffffff;
  padding: 1px 5px; border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
}
.mono { font-family: 'Consolas', 'Monaco', monospace; }
</style>
