<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import type { MockRule, MockLog, MockStatus, MockTemplate, TemplateVariable, MockMultiMatchRule, MockMatchCondition } from '../api/client'

const router = useRouter()

// Props: 支持从 FlowList 右键跳转
const props = defineProps<{
  prefilledFlowId?: number
}>()

// Mock 预览结果
interface MockPreviewResult {
  rendered_body: string
  rendered_headers: Record<string, string>
  errors: string[]
}

const { t } = useI18n()
const rules = ref<MockRule[]>([])
const logs = ref<MockLog[]>([])
const status = ref<MockStatus>({ running: false, port: 18902, request_count: 0 })
const loading = ref(false)
const dialogVisible = ref(false)
const editing = ref<MockRule | null>(null)
const submitting = ref(false)
const portInput = ref(18902)
const importFlowId = ref<number | null>(null)
const importDialogVisible = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

// Tab 切换
type TabType = 'rules' | 'templates' | 'multiMatch' | 'logs'
const activeTab = ref<TabType>('rules')

// 动态响应模板
const templates = ref<MockTemplate[]>([])
const templateDialogVisible = ref(false)
const editingTemplate = ref<MockTemplate | null>(null)
const templateForm = ref<MockTemplate>({
  name: '',
  description: '',
  variables: [],
  body_template: '',
  headers_template: '',
  status_code: 200,
  content_type: 'application/json',
  delay_ms: 0,
})
const templatePreviewVisible = ref(false)
const templatePreviewResult = ref<MockPreviewResult | null>(null)
const previewVariables = ref<Record<string, string>>({})

// 多条件匹配规则
const multiMatchRules = ref<MockMultiMatchRule[]>([])
const multiMatchDialogVisible = ref(false)
const editingMultiMatch = ref<MockMultiMatchRule | null>(null)
const multiMatchForm = ref<MockMultiMatchRule>({
  enabled: true,
  name: '',
  conditions: [],
  mock_response: {
    status_code: 200,
    headers: { 'Content-Type': 'application/json' },
    body: '',
    delay_ms: 0,
  },
  priority: 0,
  note: '',
})

const matchOptions = computed<{ label: string; value: string }[]>(() => [
  { label: t('mock.matchExact'), value: 'exact' },
  { label: t('mock.matchPrefix'), value: 'prefix' },
  { label: t('mock.matchRegex'), value: 'regex' },
])
const methodOptions = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']
const matchLabel = computed<Record<string, string>>(() => ({
  exact: t('mock.matchExact'),
  prefix: t('mock.matchPrefix'),
  regex: t('mock.matchRegex'),
}))

function unwrap<T>(res: any, fallback: T): T {
  return res?.data?.code === 0 ? (res.data.data as T) : fallback
}

async function loadRules() {
  loading.value = true
  try {
    const res = await axios.get('/api/mock/rules')
    rules.value = unwrap<MockRule[]>(res, [])
  } catch (e: any) {
    ElMessage.error(t('mock.loadFailed') + (e?.message || e))
  } finally {
    loading.value = false
  }
}

async function loadStatus() {
  try {
    const res = await axios.get('/api/mock/status')
    status.value = unwrap<MockStatus>(res, status.value)
    portInput.value = status.value.port
  } catch (e: any) {
    // 静默：状态轮询失败不打扰用户
  }
}

async function loadLogs() {
  try {
    const res = await axios.get('/api/mock/logs')
    logs.value = unwrap<MockLog[]>(res, [])
  } catch (e: any) {
    // 静默
  }
}

async function startServer() {
  try {
    const res = await axios.post('/api/mock/start', { port: portInput.value })
    status.value = unwrap<MockStatus>(res, status.value)
    ElMessage.success(t('mock.startSuccess'))
  } catch (e: any) {
    ElMessage.error(t('mock.startFailed') + (e?.message || e))
  }
}

async function stopServer() {
  try {
    const res = await axios.post('/api/mock/stop')
    status.value = unwrap<MockStatus>(res, status.value)
    ElMessage.success(t('mock.stopSuccess'))
  } catch (e: any) {
    ElMessage.error(t('mock.stopFailed') + (e?.message || e))
  }
}

function openNew() {
  editing.value = null
  form.value = {
    enabled: true,
    method: 'GET',
    path: '/',
    match_mode: 'exact',
    status_code: 200,
    headers: {},
    body: '',
    content_type: 'application/json',
    delay_ms: 0,
    note: '',
  }
  headersText.value = '{\n  "Content-Type": "application/json"\n}'
  dialogVisible.value = true
}

function openEdit(r: MockRule) {
  editing.value = r
  form.value = {
    enabled: r.enabled,
    method: r.method,
    path: r.path,
    match_mode: r.match_mode,
    status_code: r.status_code,
    headers: { ...(r.headers || {}) },
    body: r.body,
    content_type: r.content_type,
    delay_ms: r.delay_ms,
    note: r.note,
  }
  try {
    headersText.value = JSON.stringify(r.headers || {}, null, 2)
  } catch (e) {
    headersText.value = '{}'
  }
  dialogVisible.value = true
}

const form = ref<MockRule>({
  enabled: true,
  method: 'GET',
  path: '/',
  match_mode: 'exact',
  status_code: 200,
  headers: {},
  body: '',
  content_type: 'application/json',
  delay_ms: 0,
  note: '',
})
const headersText = ref('{}')

// 多条件匹配规则的 headers 编辑（JSON 字符串）
const multiMatchHeadersText = computed({
  get() {
    try {
      return JSON.stringify(multiMatchForm.value.mock_response.headers || {}, null, 2)
    } catch {
      return '{}'
    }
  },
  set(val: string) {
    try {
      multiMatchForm.value.mock_response.headers = JSON.parse(val)
    } catch { /* ignore */ }
  },
})

async function submit() {
  if (!form.value.path.trim()) {
    ElMessage.warning(t('mock.pathRequired'))
    return
  }
  // 解析 headers 文本
  let parsedHeaders: Record<string, string> = {}
  const raw = headersText.value.trim()
  if (raw) {
    try {
      const obj = JSON.parse(raw)
      if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
        parsedHeaders = obj
      } else {
        ElMessage.warning(t('mock.headersInvalid'))
        return
      }
    } catch (e) {
      ElMessage.warning(t('mock.headersInvalid'))
      return
    }
  }
  form.value.headers = parsedHeaders
  submitting.value = true
  try {
    if (editing.value && editing.value.id) {
      const res = await axios.put(`/api/mock/rules/${editing.value.id}`, form.value)
      const updated = unwrap<MockRule>(res, form.value)
      const idx = rules.value.findIndex(r => r.id === editing.value!.id)
      if (idx >= 0) rules.value[idx] = updated
      ElMessage.success(t('mock.saveSuccess'))
    } else {
      const res = await axios.post('/api/mock/rules', form.value)
      const created = unwrap<MockRule>(res, form.value)
      rules.value.push(created)
      ElMessage.success(t('mock.saveSuccess'))
    }
    dialogVisible.value = false
  } catch (e: any) {
    ElMessage.error(t('mock.saveFailed') + (e?.message || e))
  } finally {
    submitting.value = false
  }
}

async function deleteRule(r: MockRule) {
  try {
    await ElMessageBox.confirm(t('mock.deleteConfirm'), t('mock.deleteTitle'), {
      confirmButtonText: t('mock.deleteButton'),
      cancelButtonText: t('mock.cancelButton'),
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await axios.delete(`/api/mock/rules/${r.id}`)
    rules.value = rules.value.filter(x => x.id !== r.id)
    ElMessage.success(t('mock.deleted'))
  } catch (e: any) {
    ElMessage.error(t('mock.deleteFailed') + (e?.message || e))
  }
}

async function toggleRule(r: MockRule) {
  const prev = r.enabled
  try {
    const res = await axios.post(`/api/mock/rules/${r.id}/toggle`)
    const updated = unwrap<MockRule>(res, r)
    r.enabled = updated.enabled
  } catch (e: any) {
    r.enabled = prev
    ElMessage.error(t('mock.toggleFailed') + (e?.message || e))
  }
}

function openImport() {
  importFlowId.value = null
  importDialogVisible.value = true
}

async function submitImport() {
  if (!importFlowId.value) {
    ElMessage.warning(t('mock.flowIdRequired'))
    return
  }
  try {
    const res = await axios.post('/api/mock/import-flow', { flow_id: importFlowId.value })
    const created = unwrap<MockRule>(res, {} as MockRule)
    rules.value.push(created)
    ElMessage.success(t('mock.importSuccess'))
    importDialogVisible.value = false
  } catch (e: any) {
    ElMessage.error(t('mock.importFailed') + (e?.message || e))
  }
}

async function clearLogs() {
  try {
    await axios.delete('/api/mock/logs')
    logs.value = []
    ElMessage.success(t('mock.logsCleared'))
  } catch (e: any) {
    ElMessage.error(t('mock.clearLogsFailed') + (e?.message || e))
  }
}

function refreshAll() {
  loadStatus()
  loadLogs()
}

// ===== 动态响应模板 =====
async function loadTemplates() {
  try {
    const res = await axios.get('/api/mock/templates')
    templates.value = unwrap<MockTemplate[]>(res, [])
  } catch (e: any) {
    ElMessage.error(t('mock.loadTemplatesFailed') + (e?.message || e))
  }
}

function openNewTemplate() {
  editingTemplate.value = null
  templateForm.value = {
    name: '',
    description: '',
    variables: [],
    body_template: '',
    headers_template: '',
    status_code: 200,
    content_type: 'application/json',
    delay_ms: 0,
  }
  templateDialogVisible.value = true
}

function openEditTemplate(tpl: MockTemplate) {
  editingTemplate.value = tpl
  templateForm.value = {
    id: tpl.id,
    name: tpl.name,
    description: tpl.description || '',
    variables: [...(tpl.variables || [])],
    body_template: tpl.body_template,
    headers_template: tpl.headers_template || '',
    status_code: tpl.status_code || 200,
    content_type: tpl.content_type || 'application/json',
    delay_ms: tpl.delay_ms || 0,
  }
  templateDialogVisible.value = true
}

function addTemplateVariable() {
  templateForm.value.variables.push({
    name: '',
    description: '',
    type: 'string',
    default_value: '',
    extraction_pattern: '',
    from_flow_field: '',
  })
}

function removeTemplateVariable(index: number) {
  templateForm.value.variables.splice(index, 1)
}

async function submitTemplate() {
  if (!templateForm.value.name.trim()) {
    ElMessage.warning(t('mock.templateNameRequired'))
    return
  }
  submitting.value = true
  try {
    if (editingTemplate.value && editingTemplate.value.id) {
      const res = await axios.put(`/api/mock/templates/${editingTemplate.value.id}`, templateForm.value)
      const updated = unwrap<MockTemplate>(res, templateForm.value)
      const idx = templates.value.findIndex(t => t.id === editingTemplate.value!.id)
      if (idx >= 0) templates.value[idx] = updated
      ElMessage.success(t('mock.saveSuccess'))
    } else {
      const res = await axios.post('/api/mock/templates', templateForm.value)
      const created = unwrap<MockTemplate>(res, templateForm.value)
      templates.value.push(created)
      ElMessage.success(t('mock.saveSuccess'))
    }
    templateDialogVisible.value = false
  } catch (e: any) {
    ElMessage.error(t('mock.saveFailed') + (e?.message || e))
  } finally {
    submitting.value = false
  }
}

async function deleteTemplate(tpl: MockTemplate) {
  try {
    await ElMessageBox.confirm(t('mock.deleteTemplateConfirm'), t('mock.deleteTitle'), {
      confirmButtonText: t('mock.deleteButton'),
      cancelButtonText: t('mock.cancelButton'),
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await axios.delete(`/api/mock/templates/${tpl.id}`)
    templates.value = templates.value.filter(x => x.id !== tpl.id)
    ElMessage.success(t('mock.deleted'))
  } catch (e: any) {
    ElMessage.error(t('mock.deleteFailed') + (e?.message || e))
  }
}

function openTemplatePreview(tpl: MockTemplate) {
  editingTemplate.value = tpl
  previewVariables.value = {}
  // 初始化变量默认值
  for (const v of (tpl.variables || [])) {
    previewVariables.value[v.name] = v.default_value || ''
  }
  templatePreviewResult.value = null
  templatePreviewVisible.value = true
}

async function doTemplatePreview() {
  try {
    const res = await axios.post(`/api/mock/templates/${editingTemplate.value!.id}/preview`, {
      variables: previewVariables.value,
    })
    templatePreviewResult.value = unwrap(res, null)
  } catch (e: any) {
    ElMessage.error(t('mock.previewFailed') + (e?.message || e))
  }
}

// ===== 多条件匹配规则 =====
async function loadMultiMatchRules() {
  try {
    const res = await axios.get('/api/mock/multi-match-rules')
    multiMatchRules.value = unwrap<MockMultiMatchRule[]>(res, [])
  } catch (e: any) {
    ElMessage.error(t('mock.loadMultiMatchFailed') + (e?.message || e))
  }
}

function openNewMultiMatch() {
  editingMultiMatch.value = null
  multiMatchForm.value = {
    enabled: true,
    name: '',
    conditions: [],
    mock_response: {
      status_code: 200,
      headers: { 'Content-Type': 'application/json' },
      body: '',
      delay_ms: 0,
    },
    priority: 0,
    note: '',
  }
  multiMatchDialogVisible.value = true
}

function openEditMultiMatch(rule: MockMultiMatchRule) {
  editingMultiMatch.value = rule
  multiMatchForm.value = {
    id: rule.id,
    enabled: rule.enabled,
    name: rule.name,
    conditions: [...(rule.conditions || [])],
    mock_response: { ...rule.mock_response },
    priority: rule.priority || 0,
    note: rule.note || '',
  }
  multiMatchDialogVisible.value = true
}

function addMultiMatchCondition() {
  multiMatchForm.value.conditions.push({
    field: 'method',
    operator: 'equals',
    value: '',
  })
}

function removeMultiMatchCondition(index: number) {
  multiMatchForm.value.conditions.splice(index, 1)
}

async function submitMultiMatch() {
  if (!multiMatchForm.value.name.trim()) {
    ElMessage.warning(t('mock.multiMatchNameRequired'))
    return
  }
  if (!multiMatchForm.value.conditions.length) {
    ElMessage.warning(t('mock.multiMatchConditionsRequired'))
    return
  }
  submitting.value = true
  try {
    if (editingMultiMatch.value && editingMultiMatch.value.id) {
      const res = await axios.put(`/api/mock/multi-match-rules/${editingMultiMatch.value.id}`, multiMatchForm.value)
      const updated = unwrap<MockMultiMatchRule>(res, multiMatchForm.value)
      const idx = multiMatchRules.value.findIndex(r => r.id === editingMultiMatch.value!.id)
      if (idx >= 0) multiMatchRules.value[idx] = updated
      ElMessage.success(t('mock.saveSuccess'))
    } else {
      const res = await axios.post('/api/mock/multi-match-rules', multiMatchForm.value)
      const created = unwrap<MockMultiMatchRule>(res, multiMatchForm.value)
      multiMatchRules.value.push(created)
      ElMessage.success(t('mock.saveSuccess'))
    }
    multiMatchDialogVisible.value = false
  } catch (e: any) {
    ElMessage.error(t('mock.saveFailed') + (e?.message || e))
  } finally {
    submitting.value = false
  }
}

async function deleteMultiMatch(rule: MockMultiMatchRule) {
  try {
    await ElMessageBox.confirm(t('mock.deleteMultiMatchConfirm'), t('mock.deleteTitle'), {
      confirmButtonText: t('mock.deleteButton'),
      cancelButtonText: t('mock.cancelButton'),
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await axios.delete(`/api/mock/multi-match-rules/${rule.id}`)
    multiMatchRules.value = multiMatchRules.value.filter(x => x.id !== rule.id)
    ElMessage.success(t('mock.deleted'))
  } catch (e: any) {
    ElMessage.error(t('mock.deleteFailed') + (e?.message || e))
  }
}

async function toggleMultiMatch(rule: MockMultiMatchRule) {
  const prev = rule.enabled
  try {
    const res = await axios.post(`/api/mock/multi-match-rules/${rule.id}/toggle`)
    const updated = unwrap<MockMultiMatchRule>(res, rule)
    rule.enabled = updated.enabled
  } catch (e: any) {
    rule.enabled = prev
    ElMessage.error(t('mock.toggleFailed') + (e?.message || e))
  }
}

onMounted(() => {
  loadRules()
  loadStatus()
  loadLogs()
  loadTemplates()
  loadMultiMatchRules()
  // 性能优化：页面不可见时跳过轮询，避免后台无谓请求
  pollTimer = setInterval(() => {
    if (document.hidden) return
    refreshAll()
  }, 3000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="mock-view full flex flex-col">
    <div class="page-header">
      <div style="display: flex; align-items: center; gap: 8px">
        <el-button size="small" @click="router.push('/auto-reply')" :title="t('common.back')">
          <el-icon><ArrowLeft /></el-icon>&nbsp;{{ t('common.back') }}
        </el-button>
        <div class="page-title">
          <el-icon><Connection /></el-icon>&nbsp;{{ t('mock.pageTitle') }}
        </div>
      </div>
    </div>

    <!-- 服务器状态卡片 -->
    <div class="status-bar">
      <div class="status-card">
        <span class="status-label">{{ t('mock.status') }}</span>
        <el-tag :type="status.running ? 'success' : 'info'" size="small">
          {{ status.running ? t('mock.running') : t('mock.stopped') }}
        </el-tag>
      </div>
      <div class="status-card">
        <span class="status-label">{{ t('mock.port') }}</span>
        <span class="status-value">{{ status.port }}</span>
      </div>
      <div class="status-card">
        <span class="status-label">{{ t('mock.requestCount') }}</span>
        <span class="status-value">{{ status.request_count }}</span>
      </div>
      <div class="status-actions">
        <el-input-number v-model="portInput" :min="1" :max="65535" size="small" controls-position="right" style="width: 110px" />
        <el-button v-if="!status.running" type="success" size="small" @click="startServer">
          <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('mock.start') }}
        </el-button>
        <el-button v-else type="danger" size="small" @click="stopServer">
          <el-icon><VideoPause /></el-icon>&nbsp;{{ t('mock.stop') }}
        </el-button>
        <el-button size="small" @click="openImport">
          <el-icon><Download /></el-icon>&nbsp;{{ t('mock.importFromFlow') }}
        </el-button>
        <el-button type="primary" size="small" @click="openNew">
          <el-icon><Plus /></el-icon>&nbsp;{{ t('mock.newRule') }}
        </el-button>
      </div>
    </div>

    <!-- Tab 切换 -->
    <el-tabs v-model="activeTab" class="mock-tabs">
      <el-tab-pane :label="t('mock.rulesTitle')" name="rules">
        <template #label>
          <el-icon><List /></el-icon>&nbsp;{{ t('mock.rulesTitle') }}
        </template>
      </el-tab-pane>
      <el-tab-pane name="templates">
        <template #label>
          <el-icon><Document /></el-icon>&nbsp;{{ t('mock.templatesTitle') }}
        </template>
      </el-tab-pane>
      <el-tab-pane name="multiMatch">
        <template #label>
          <el-icon><Setting /></el-icon>&nbsp;{{ t('mock.multiMatchTitle') }}
        </template>
      </el-tab-pane>
      <el-tab-pane name="logs">
        <template #label>
          <el-icon><List /></el-icon>&nbsp;{{ t('mock.logsTitle') }}
        </template>
      </el-tab-pane>
    </el-tabs>

    <!-- 规则列表 (rules Tab) -->
    <div v-show="activeTab === 'rules'" class="flex-1 overflow-auto section-block">
      <div class="section-title">{{ t('mock.rulesTitle') }}</div>
      <el-table :data="rules" size="small" border stripe>
        <el-table-column :label="t('mock.enabled')" width="70">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" @change="toggleRule(row as MockRule)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('mock.method')" prop="method" width="80" class-name="mono" />
        <el-table-column :label="t('mock.path')" prop="path" class-name="mono" min-width="160" show-overflow-tooltip />
        <el-table-column :label="t('mock.matchMode')" width="90">
          <template #default="{ row }">{{ matchLabel[row.match_mode] || row.match_mode }}</template>
        </el-table-column>
        <el-table-column :label="t('mock.statusCode')" prop="status_code" width="90" />
        <el-table-column :label="t('mock.contentType')" prop="content_type" width="150" show-overflow-tooltip />
        <el-table-column :label="t('mock.delayMs')" width="90">
          <template #default="{ row }">{{ row.delay_ms }} {{ t('common.ms') }}</template>
        </el-table-column>
        <el-table-column :label="t('mock.note')" min-width="100">
          <template #default="{ row }">
            <span v-if="row.note" :title="row.note">{{ row.note }}</span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('mock.operations')" width="130">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row as MockRule)">{{ t('mock.edit') }}</el-button>
            <el-button link type="danger" size="small" @click="deleteRule(row as MockRule)">{{ t('mock.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('mock.emptyHint') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 请求日志面板 (logs Tab) -->
    <div v-show="activeTab === 'logs'" class="section-block logs-block">
      <div class="section-title-row">
        <span class="section-title">{{ t('mock.logsTitle') }}</span>
        <el-button link type="primary" size="small" @click="clearLogs">{{ t('mock.clearLogs') }}</el-button>
      </div>
      <el-table :data="logs" size="small" border stripe max-height="240">
        <el-table-column :label="t('mock.logTime')" prop="timestamp" width="180" />
        <el-table-column :label="t('mock.method')" prop="method" width="70" class-name="mono" />
        <el-table-column :label="t('mock.path')" prop="path" class-name="mono" min-width="160" show-overflow-tooltip />
        <el-table-column :label="t('mock.matchedRule')" min-width="120">
          <template #default="{ row }">
            <span v-if="row.matched_rule_id" class="mono">{{ row.matched_rule_id }}</span>
            <span v-else class="text-dim">{{ t('mock.noMatch') }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('mock.statusCode')" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status_code < 400 ? 'success' : 'danger'" size="small">{{ row.status_code }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('mock.duration')" width="90">
          <template #default="{ row }">{{ row.duration_ms }} ms</template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('mock.logsEmpty') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 新增/编辑规则对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editing ? t('mock.editTitle') : t('mock.newTitle')"
      width="620px"
      :close-on-click-modal="false"
    >
      <el-form :model="form" label-width="110px" size="default">
        <el-form-item :label="t('mock.method')">
          <el-select v-model="form.method" style="width: 100%">
            <el-option v-for="m in methodOptions" :key="m" :label="m" :value="m" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('mock.path')">
          <el-input v-model="form.path" :placeholder="t('mock.pathPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('mock.matchMode')">
          <el-select v-model="form.match_mode" style="width: 100%">
            <el-option v-for="o in matchOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('mock.statusCode')">
          <el-input-number v-model="form.status_code" :min="100" :max="599" style="width: 100%" />
        </el-form-item>
        <el-form-item :label="t('mock.contentType')">
          <el-input v-model="form.content_type" :placeholder="t('mock.contentTypePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('mock.headers')">
          <el-input v-model="headersText" type="textarea" :rows="4" :placeholder="t('mock.headersPlaceholder')" class="mono" />
        </el-form-item>
        <el-form-item :label="t('mock.body')">
          <el-input v-model="form.body" type="textarea" :rows="5" :placeholder="t('mock.bodyPlaceholder')" class="mono" />
        </el-form-item>
        <el-form-item :label="t('mock.delayMs')">
          <el-input-number v-model="form.delay_ms" :min="0" :max="60000" :step="100" style="width: 100%" />
        </el-form-item>
        <el-form-item :label="t('mock.note')">
          <el-input v-model="form.note" :placeholder="t('mock.notePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('mock.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('mock.cancelButton') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">{{ t('mock.saveButton') }}</el-button>
      </template>
    </el-dialog>

    <!-- 模板列表 (templates Tab) -->
    <div v-show="activeTab === 'templates'" class="flex-1 overflow-auto section-block">
      <div class="section-header">
        <div class="section-title">{{ t('mock.templatesTitle') }}</div>
        <el-button type="primary" size="small" @click="openNewTemplate">
          <el-icon><Plus /></el-icon>&nbsp;{{ t('mock.newTemplate') }}
        </el-button>
      </div>
      <el-table :data="templates" size="small" border stripe>
        <el-table-column :label="t('mock.templateName')" prop="name" min-width="160" />
        <el-table-column :label="t('mock.description')" prop="description" min-width="160" show-overflow-tooltip />
        <el-table-column :label="t('mock.variables')" width="80">
          <template #default="{ row }">{{ (row.variables || []).length }}</template>
        </el-table-column>
        <el-table-column :label="t('mock.statusCode')" prop="status_code" width="90" />
        <el-table-column :label="t('mock.contentType')" prop="content_type" width="150" show-overflow-tooltip />
        <el-table-column :label="t('mock.delayMs')" width="90">
          <template #default="{ row }">{{ row.delay_ms || 0 }} ms</template>
        </el-table-column>
        <el-table-column :label="t('mock.operations')" width="220">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openTemplatePreview(row as MockTemplate)">{{ t('mock.preview') }}</el-button>
            <el-button link type="primary" size="small" @click="openEditTemplate(row as MockTemplate)">{{ t('mock.edit') }}</el-button>
            <el-button link type="danger" size="small" @click="deleteTemplate(row as MockTemplate)">{{ t('mock.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('mock.templatesEmpty') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 多条件匹配规则列表 (multiMatch Tab) -->
    <div v-show="activeTab === 'multiMatch'" class="flex-1 overflow-auto section-block">
      <div class="section-header">
        <div class="section-title">{{ t('mock.multiMatchTitle') }}</div>
        <el-button type="primary" size="small" @click="openNewMultiMatch">
          <el-icon><Plus /></el-icon>&nbsp;{{ t('mock.newMultiMatch') }}
        </el-button>
      </div>
      <el-table :data="multiMatchRules" size="small" border stripe>
        <el-table-column :label="t('mock.enabled')" width="70">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" @change="toggleMultiMatch(row as MockMultiMatchRule)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('mock.multiMatchName')" prop="name" min-width="160" />
        <el-table-column :label="t('mock.conditions')" min-width="200">
          <template #default="{ row }">
            <span class="conditions-preview">
              <el-tag v-for="(cond, idx) in (row.conditions || []).slice(0, 3)" :key="idx" size="small" style="margin: 2px">
                {{ cond.field }} {{ cond.operator }} {{ cond.value }}
              </el-tag>
              <span v-if="(row.conditions || []).length > 3" class="text-dim">...</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('mock.priority')" prop="priority" width="80" />
        <el-table-column :label="t('mock.note')" min-width="100">
          <template #default="{ row }">
            <span v-if="row.note" :title="row.note">{{ row.note }}</span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('mock.operations')" width="130">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEditMultiMatch(row as MockMultiMatchRule)">{{ t('mock.edit') }}</el-button>
            <el-button link type="danger" size="small" @click="deleteMultiMatch(row as MockMultiMatchRule)">{{ t('mock.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('mock.multiMatchEmpty') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 新增/编辑模板对话框 -->
    <el-dialog
      v-model="templateDialogVisible"
      :title="editingTemplate ? t('mock.editTemplate') : t('mock.newTemplate')"
      width="680px"
      :close-on-click-modal="false"
    >
      <el-form :model="templateForm" label-width="110px" size="default">
        <el-form-item :label="t('mock.templateName')" required>
          <el-input v-model="templateForm.name" :placeholder="t('mock.templateNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('mock.description')">
          <el-input v-model="templateForm.description" :placeholder="t('mock.templateDescPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('mock.statusCode')">
          <el-input-number v-model="templateForm.status_code" :min="100" :max="599" />
        </el-form-item>
        <el-form-item :label="t('mock.contentType')">
          <el-input v-model="templateForm.content_type" :placeholder="t('mock.contentTypePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('mock.delayMs')">
          <el-input-number v-model="templateForm.delay_ms" :min="0" :max="60000" :step="100" />
        </el-form-item>
        <el-form-item :label="t('mock.headersTemplate')">
          <el-input v-model="templateForm.headers_template" type="textarea" :rows="3" :placeholder="t('mock.headersTemplatePlaceholder')" class="mono" />
        </el-form-item>
        <el-form-item :label="t('mock.bodyTemplate')">
          <el-input v-model="templateForm.body_template" type="textarea" :rows="6" :placeholder="t('mock.bodyTemplatePlaceholder')" class="mono" />
        </el-form-item>
        <el-form-item :label="t('mock.variables')">
          <div class="variables-list">
            <div v-for="(v, idx) in templateForm.variables" :key="idx" class="variable-item">
              <el-input v-model="v.name" :placeholder="t('mock.varName')" style="width: 100px" />
              <el-select v-model="v.type" style="width: 90px">
                <el-option :label="t('mock.varTypeString')" value="string" />
                <el-option :label="t('mock.varTypeNumber')" value="number" />
                <el-option :label="t('mock.varTypeBoolean')" value="boolean" />
                <el-option :label="t('mock.varTypeJson')" value="json" />
                <el-option :label="t('mock.varTypeRegex')" value="regex" />
              </el-select>
              <el-input v-model="v.default_value" :placeholder="t('mock.varDefault')" style="width: 100px" />
              <el-input v-model="v.extraction_pattern" :placeholder="t('mock.varExtract')" style="width: 120px" />
              <el-button link type="danger" size="small" @click="removeTemplateVariable(idx)">{{ t('mock.delete') }}</el-button>
            </div>
            <el-button link type="primary" size="small" @click="addTemplateVariable">{{ t('mock.addVariable') }}</el-button>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="templateDialogVisible = false">{{ t('mock.cancelButton') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submitTemplate">{{ t('mock.saveButton') }}</el-button>
      </template>
    </el-dialog>

    <!-- 模板预览对话框 -->
    <el-dialog
      v-model="templatePreviewVisible"
      :title="t('mock.templatePreview')"
      width="700px"
      :close-on-click-modal="false"
    >
      <div v-if="editingTemplate" class="preview-section">
        <div class="preview-title">{{ t('mock.previewVariables') }}</div>
        <div class="preview-vars">
          <div v-for="v in (editingTemplate.variables || [])" :key="v.name" class="preview-var-row">
            <span class="preview-var-label">{{ v.name }}</span>
            <el-input v-model="previewVariables[v.name]" :placeholder="v.default_value || ''" style="width: 300px" />
          </div>
        </div>
        <el-button type="primary" size="small" @click="doTemplatePreview">{{ t('mock.renderPreview') }}</el-button>
      </div>
      <div v-if="templatePreviewResult" class="preview-result">
        <div v-if="templatePreviewResult.errors?.length" class="preview-errors">
          <el-alert v-for="(err, idx) in templatePreviewResult.errors" :key="idx" :title="err" type="error" show-icon :closable="false" />
        </div>
        <div v-if="templatePreviewResult.rendered_headers" class="preview-headers">
          <div class="preview-title">{{ t('mock.renderedHeaders') }}</div>
          <pre class="mono">{{ JSON.stringify(templatePreviewResult.rendered_headers, null, 2) }}</pre>
        </div>
        <div v-if="templatePreviewResult.rendered_body" class="preview-body">
          <div class="preview-title">{{ t('mock.renderedBody') }}</div>
          <pre class="mono preview-body-content">{{ templatePreviewResult.rendered_body }}</pre>
        </div>
      </div>
      <template #footer>
        <el-button @click="templatePreviewVisible = false">{{ t('mock.closeButton') }}</el-button>
      </template>
    </el-dialog>

    <!-- 新增/编辑多条件匹配规则对话框 -->
    <el-dialog
      v-model="multiMatchDialogVisible"
      :title="editingMultiMatch ? t('mock.editMultiMatch') : t('mock.newMultiMatch')"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form :model="multiMatchForm" label-width="110px" size="default">
        <el-form-item :label="t('mock.multiMatchName')" required>
          <el-input v-model="multiMatchForm.name" :placeholder="t('mock.multiMatchNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('mock.priority')">
          <el-input-number v-model="multiMatchForm.priority" :min="0" :max="999" />
        </el-form-item>
        <el-form-item :label="t('mock.note')">
          <el-input v-model="multiMatchForm.note" :placeholder="t('mock.notePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('mock.enabled')">
          <el-switch v-model="multiMatchForm.enabled" />
        </el-form-item>
        <el-divider content-position="left">{{ t('mock.conditions') }}</el-divider>
        <div class="conditions-list">
          <div v-for="(cond, idx) in multiMatchForm.conditions" :key="idx" class="condition-item">
            <el-select v-model="cond.field" style="width: 120px">
              <el-option :label="t('mock.condFieldMethod')" value="method" />
              <el-option :label="t('mock.condFieldPath')" value="path" />
              <el-option :label="t('mock.condFieldHost')" value="host" />
              <el-option :label="t('mock.condFieldHeader')" value="header" />
              <el-option :label="t('mock.condFieldBody')" value="body" />
              <el-option :label="t('mock.condFieldQuery')" value="query" />
              <el-option :label="t('mock.condFieldStatus')" value="status" />
            </el-select>
            <el-input v-if="cond.field === 'header'" v-model="cond.header_name" :placeholder="t('mock.headerName')" style="width: 100px" />
            <el-select v-model="cond.operator" style="width: 100px">
              <el-option :label="t('mock.condOpEquals')" value="equals" />
              <el-option :label="t('mock.condOpContains')" value="contains" />
              <el-option :label="t('mock.condOpStartsWith')" value="startsWith" />
              <el-option :label="t('mock.condOpEndsWith')" value="endsWith" />
              <el-option :label="t('mock.condOpRegex')" value="regex" />
              <el-option :label="t('mock.condOpExists')" value="exists" />
              <el-option :label="t('mock.condOpNotExists')" value="notExists" />
            </el-select>
            <el-input v-if="!['exists', 'notExists'].includes(cond.operator)" v-model="cond.value" :placeholder="t('mock.condValue')" style="flex: 1" />
            <el-button link type="danger" size="small" @click="removeMultiMatchCondition(idx)">{{ t('mock.delete') }}</el-button>
          </div>
          <el-button link type="primary" size="small" @click="addMultiMatchCondition">{{ t('mock.addCondition') }}</el-button>
        </div>
        <el-divider content-position="left">{{ t('mock.mockResponse') }}</el-divider>
        <el-form-item :label="t('mock.statusCode')">
          <el-input-number v-model="multiMatchForm.mock_response.status_code" :min="100" :max="599" />
        </el-form-item>
        <el-form-item :label="t('mock.headers')">
          <el-input v-model="multiMatchHeadersText" type="textarea" :rows="3" :placeholder="t('mock.headersPlaceholder')" class="mono" />
        </el-form-item>
        <el-form-item :label="t('mock.body')">
          <el-input v-model="multiMatchForm.mock_response.body" type="textarea" :rows="5" :placeholder="t('mock.bodyPlaceholder')" class="mono" />
        </el-form-item>
        <el-form-item :label="t('mock.delayMs')">
          <el-input-number v-model="multiMatchForm.mock_response.delay_ms" :min="0" :max="60000" :step="100" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="multiMatchDialogVisible = false">{{ t('mock.cancelButton') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submitMultiMatch">{{ t('mock.saveButton') }}</el-button>
      </template>
    </el-dialog>

    <!-- 从 flow 导入对话框 -->
    <el-dialog
      v-model="importDialogVisible"
      :title="t('mock.importTitle')"
      width="420px"
      :close-on-click-modal="false"
    >
      <el-form label-width="100px" size="default">
        <el-form-item :label="t('mock.flowId')">
          <el-input-number v-model="importFlowId" :min="1" controls-position="right" style="width: 100%" />
        </el-form-item>
      </el-form>
      <div class="text-dim import-hint">{{ t('mock.importHint') }}</div>
      <template #footer>
        <el-button @click="importDialogVisible = false">{{ t('mock.cancelButton') }}</el-button>
        <el-button type="primary" @click="submitImport">{{ t('mock.importButton') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mock-view { background: var(--on-bg); }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.page-title { font-size: 15px; font-weight: 600; display: flex; align-items: center; }
.status-bar {
  display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.status-card { display: flex; align-items: center; gap: 8px; }
.status-label { color: var(--on-text-secondary); font-size: 13px; }
.status-value { font-weight: 600; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.status-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.mock-tabs { padding: 0 16px; }
.section-block { padding: 8px 16px; }
.section-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.section-title { font-size: 13px; font-weight: 600; color: var(--on-text-secondary); }
.section-title-row { display: flex; align-items: center; justify-content: space-between; }
.logs-block { border-top: 1px solid var(--on-border-light); }
.empty-text { padding: 20px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.import-hint { font-size: 12px; padding: 4px 0 0 100px; }

/* 变量列表 */
.variables-list { display: flex; flex-direction: column; gap: 8px; }
.variable-item { display: flex; align-items: center; gap: 8px; }

/* 条件列表 */
.conditions-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
.condition-item { display: flex; align-items: center; gap: 8px; }
.conditions-preview { display: flex; flex-wrap: wrap; gap: 4px; }

/* 模板预览 */
.preview-section { margin-bottom: 16px; }
.preview-title { font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--on-text-secondary); }
.preview-vars { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.preview-var-row { display: flex; align-items: center; gap: 12px; }
.preview-var-label { font-weight: 500; min-width: 80px; color: var(--on-text); }
.preview-result { margin-top: 16px; }
.preview-errors { margin-bottom: 12px; }
.preview-headers { margin-bottom: 12px; }
.preview-headers pre { padding: 8px; background: var(--on-bg); border-radius: 4px; font-size: 12px; max-height: 120px; overflow: auto; }
.preview-body pre { padding: 8px; background: var(--on-bg); border-radius: 4px; font-size: 12px; max-height: 200px; overflow: auto; }
</style>
