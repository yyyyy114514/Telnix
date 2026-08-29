<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useFlowsStore } from '../stores/flows'

interface RecordScript {
  id: string
  name: string
  created_at: string
  flow_ids: number[]
  note: string
  flow_count?: number
}

interface ReplayStats {
  total: number
  replayed: number
  skipped: number
  success: number
  fail: number
  status_distribution: Record<string, number>
  results: { flow_id: number; url: string; method: string; status_code?: number; duration_ms: number; ok: boolean; error?: string; assertions?: AssertionResult[] }[]
  assertions_passed?: number
  assertions_failed?: number
}

interface RecordingStatus {
  active: boolean
  recording_id: string | null
  session_start: string | null
  flow_count: number
  flow_ids: number[]
}

// ===== 变量系统 =====
interface ScriptVariable {
  name: string
  description?: string
  type: 'path' | 'query' | 'header' | 'body' | 'json_path' | 'regex'
  extraction_rule: string
  sample_values: string[]
  from_flow_id: number
}

interface RecordScriptVariables {
  script_id: string
  variables: ScriptVariable[]
  flows: Array<{
    flow_id: number
    url: string
    extracted_variables: Record<string, string>
  }>
}

// ===== 多环境配置 =====
interface ReplayEnvironment {
  id?: string
  name: string
  description?: string
  variables: Record<string, string>
  is_default?: boolean
}

// ===== 条件执行规则 =====
interface ReplayConditionRule {
  id?: string
  name: string
  condition_type: 'status_code' | 'response_body' | 'response_header' | 'request_header'
  operator: 'equals' | 'contains' | 'regex' | 'greater' | 'less'
  value: string
  action: 'skip' | 'delay' | 'mock' | 'abort'
  action_params?: Record<string, any>
}

// ===== 响应断言 =====
interface AssertionRule {
  id?: string
  script_id?: string
  flow_id: number
  field: 'status_code' | 'response_body' | 'response_header' | 'response_time'
  operator: 'equals' | 'not_equals' | 'contains' | 'not_contains' | 'regex' | 'greater' | 'less' | 'exists' | 'not_exists'
  expected_value: string
  enabled: boolean
}

interface AssertionResult {
  rule_id: string
  field: string
  operator: string
  expected: string
  actual: string
  passed: boolean
  error?: string
}

const { t } = useI18n()
const router = useRouter()
const flows = useFlowsStore()
const scripts = ref<RecordScript[]>([])
const loading = ref(false)
const recording = ref<RecordingStatus>({ active: false, recording_id: null, session_start: null, flow_count: 0, flow_ids: [] })
const togglingRecord = ref(false)

// Tab 切换
type RecordTabType = 'scripts' | 'variables' | 'environments' | 'assertions'
const activeTab = ref<RecordTabType>('scripts')

// 新建脚本对话框
const createDialogVisible = ref(false)
const createSubmitting = ref(false)
const createForm = ref({ name: '', flowIdsText: '', note: '' })

// 回放对话框
const replayDialogVisible = ref(false)
const replayTarget = ref<RecordScript | null>(null)
const replaying = ref(false)
const replayStats = ref<ReplayStats | null>(null)

// 详情对话框
const detailDialogVisible = ref(false)
const detailScript = ref<RecordScript | null>(null)

// ===== 变量管理 =====
const scriptVariables = ref<RecordScriptVariables[]>([])
const variablesDialogVisible = ref(false)
const variablesScript = ref<RecordScript | null>(null)

// ===== 环境管理 =====
const environments = ref<ReplayEnvironment[]>([])
const envDialogVisible = ref(false)
const editingEnv = ref<ReplayEnvironment | null>(null)
const envForm = ref<ReplayEnvironment>({ name: '', description: '', variables: {}, is_default: false })
const envVariablesText = ref('{}')

// ===== 断言管理 =====
const assertionRules = ref<AssertionRule[]>([])
const assertionDialogVisible = ref(false)
const assertionScript = ref<RecordScript | null>(null)
const assertionForm = ref<AssertionRule>({
  flow_id: 0,
  field: 'status_code',
  operator: 'equals',
  expected_value: '',
  enabled: true,
})

// ===== 参数化回放配置 =====
const parameterizedReplayVisible = ref(false)
const parameterizedForm = ref({
  script_id: '',
  environment_id: '',
  iterations: 1,
  delay_ms: 0,
  fail_fast: true,
})

let statusTimer: number | null = null

function unwrap<T>(res: any, fallback: T): T {
  return res?.data?.code === 0 ? (res.data.data as T) : fallback
}

// ===== 变量管理函数 =====
async function loadVariables() {
  try {
    const res = await axios.get('/api/record-scripts/variables')
    scriptVariables.value = unwrap<RecordScriptVariables[]>(res, [])
  } catch (e: any) {
    ElMessage.error(t('record.loadVarsFailed') + (e?.message || e))
  }
}

async function openVariablesDialog(script: RecordScript) {
  variablesScript.value = script
  try {
    const res = await axios.get(`/api/record-scripts/${script.id}/variables`)
    const vars = unwrap<RecordScriptVariables>(res, { script_id: script.id, variables: [], flows: [] })
    scriptVariables.value = scriptVariables.value.filter(v => v.script_id !== script.id)
    scriptVariables.value.push(vars)
  } catch (e: any) {
    ElMessage.error(t('record.loadVarsFailed') + (e?.message || e))
  }
  variablesDialogVisible.value = true
}

async function extractVariables(script: RecordScript) {
  try {
    await axios.post(`/api/record-scripts/${script.id}/extract-variables`)
    ElMessage.success(t('record.extractVarsSuccess'))
    await openVariablesDialog(script)
  } catch (e: any) {
    ElMessage.error(t('record.extractVarsFailed') + (e?.message || e))
  }
}

// ===== 环境管理函数 =====
async function loadEnvironments() {
  try {
    const res = await axios.get('/api/record-scripts/environments')
    environments.value = unwrap<ReplayEnvironment[]>(res, [])
  } catch (e: any) {
    ElMessage.error(t('record.loadEnvFailed') + (e?.message || e))
  }
}

function openNewEnv() {
  editingEnv.value = null
  envForm.value = { name: '', description: '', variables: {}, is_default: false }
  envVariablesText.value = '{}'
  envDialogVisible.value = true
}

function openEditEnv(env: ReplayEnvironment) {
  editingEnv.value = env
  envForm.value = { ...env, variables: { ...env.variables } }
  try {
    envVariablesText.value = JSON.stringify(env.variables || {}, null, 2)
  } catch {
    envVariablesText.value = '{}'
  }
  envDialogVisible.value = true
}

async function submitEnv() {
  if (!envForm.value.name.trim()) {
    ElMessage.warning(t('record.envNameRequired'))
    return
  }
  // 解析变量 JSON
  try {
    envForm.value.variables = JSON.parse(envVariablesText.value || '{}')
  } catch {
    ElMessage.warning(t('record.envVarsInvalid'))
    return
  }
  try {
    if (editingEnv.value?.id) {
      const res = await axios.put(`/api/record-scripts/environments/${editingEnv.value.id}`, envForm.value)
      const updated = unwrap<ReplayEnvironment>(res, envForm.value)
      const idx = environments.value.findIndex(e => e.id === editingEnv.value!.id)
      if (idx >= 0) environments.value[idx] = updated
    } else {
      const res = await axios.post('/api/record-scripts/environments', envForm.value)
      const created = unwrap<ReplayEnvironment>(res, envForm.value)
      environments.value.push(created)
    }
    ElMessage.success(t('common.success'))
    envDialogVisible.value = false
  } catch (e: any) {
    ElMessage.error(t('common.failed') + (e?.message || e))
  }
}

async function deleteEnv(env: ReplayEnvironment) {
  try {
    await ElMessageBox.confirm(t('record.deleteEnvConfirm'), t('common.confirm'), {
      confirmButtonText: t('common.delete'),
      cancelButtonText: t('common.cancel'),
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await axios.delete(`/api/record-scripts/environments/${env.id}`)
    environments.value = environments.value.filter(e => e.id !== env.id)
    ElMessage.success(t('common.success'))
  } catch (e: any) {
    ElMessage.error(t('common.failed') + (e?.message || e))
  }
}

// ===== 断言管理函数 =====
async function loadAssertions() {
  try {
    const res = await axios.get('/api/record-scripts/assertions')
    assertionRules.value = unwrap<AssertionRule[]>(res, [])
  } catch (e: any) {
    ElMessage.error(t('record.loadAssertionsFailed') + (e?.message || e))
  }
}

async function openAssertionsDialog(script: RecordScript) {
  assertionScript.value = script
  try {
    const res = await axios.get(`/api/record-scripts/${script.id}/assertions`)
    assertionRules.value = unwrap<AssertionRule[]>(res, [])
  } catch (e: any) {
    ElMessage.error(t('record.loadAssertionsFailed') + (e?.message || e))
  }
  assertionDialogVisible.value = true
}

function openNewAssertion() {
  assertionForm.value = {
    flow_id: 0,
    field: 'status_code',
    operator: 'equals',
    expected_value: '',
    enabled: true,
  }
}

async function submitAssertion() {
  if (!assertionScript.value?.id) return
  try {
    const res = await axios.post(`/api/record-scripts/${assertionScript.value.id}/assertions`, assertionForm.value)
    const created = unwrap<AssertionRule>(res, assertionForm.value)
    assertionRules.value.push(created)
    ElMessage.success(t('common.success'))
  } catch (e: any) {
    ElMessage.error(t('common.failed') + (e?.message || e))
  }
}

async function deleteAssertion(rule: AssertionRule) {
  if (!rule.id) return
  try {
    await axios.delete(`/api/record-scripts/assertions/${rule.id}`)
    assertionRules.value = assertionRules.value.filter(r => r.id !== rule.id)
  } catch (e: any) {
    ElMessage.error(t('common.failed') + (e?.message || e))
  }
}

// ===== 参数化回放函数 =====
async function openParameterizedReplay(script: RecordScript) {
  parameterizedForm.value.script_id = script.id
  parameterizedForm.value.iterations = 1
  parameterizedForm.value.delay_ms = 0
  parameterizedForm.value.fail_fast = true
  parameterizedForm.value.environment_id = ''
  parameterizedReplayVisible.value = true
}

async function doParameterizedReplay() {
  try {
    replaying.value = true
    const res = await axios.post(`/api/record-scripts/${parameterizedForm.value.script_id}/replay`, {
      environment_id: parameterizedForm.value.environment_id || undefined,
      iterations: parameterizedForm.value.iterations,
      delay_ms: parameterizedForm.value.delay_ms,
      fail_fast: parameterizedForm.value.fail_fast,
    })
    replayStats.value = unwrap<ReplayStats>(res, {} as ReplayStats)
    parameterizedReplayVisible.value = false
    replayDialogVisible.value = true
    ElMessage.success(t('record.replayDone'))
  } catch (e: any) {
    ElMessage.error(t('record.replayFailed') + (e?.message || e))
  } finally {
    replaying.value = false
  }
}

// ===== 转换到 Mock =====
async function convertToMock(script: RecordScript) {
  try {
    await axios.post(`/api/record-scripts/${script.id}/convert-to-mock`)
    ElMessage.success(t('record.convertToMockSuccess'))
    router.push('/mock')
  } catch (e: any) {
    ElMessage.error(t('record.convertToMockFailed') + (e?.message || e))
  }
}

async function loadScripts() {
  loading.value = true
  try {
    const res = await axios.get('/api/record-scripts')
    scripts.value = unwrap<RecordScript[]>(res, [])
  } catch (e: any) {
    ElMessage.error(t('record.loadFailed') + (e?.message || e))
  } finally {
    loading.value = false
  }
}

async function loadRecordingStatus() {
  try {
    const res = await axios.get('/api/record-scripts/recording-status')
    recording.value = unwrap<RecordingStatus>(res, recording.value)
  } catch {
    // 静默：状态轮询失败不打扰用户
  }
}

async function startRecording() {
  togglingRecord.value = true
  try {
    const res = await axios.post('/api/record-scripts/start-recording')
    const data = unwrap<{ recording_id: string; status: RecordingStatus }>(res, { recording_id: '', status: recording.value })
    recording.value = data.status
    ElMessage.success(t('record.recordingStarted'))
  } catch (e: any) {
    ElMessage.error(t('record.startFailed') + (e?.message || e))
  } finally {
    togglingRecord.value = false
  }
}

async function stopRecording() {
  try {
    await ElMessageBox.confirm(t('record.stopConfirm'), t('record.stopTitle'), {
      confirmButtonText: t('record.stopButton'),
      cancelButtonText: t('record.cancelButton'),
      type: 'warning',
    })
  } catch {
    return
  }
  togglingRecord.value = true
  try {
    const res = await axios.post('/api/record-scripts/stop-recording')
    const created = unwrap<RecordScript>(res, {} as RecordScript)
    ElMessage.success(t('record.recordingStopped'))
    recording.value = { active: false, recording_id: null, session_start: null, flow_count: 0, flow_ids: [] }
    await loadScripts()
    if (created?.id) {
      // 停止后高亮提示新脚本
    }
  } catch (e: any) {
    ElMessage.error(t('record.stopFailed') + (e?.message || e))
  } finally {
    togglingRecord.value = false
  }
}

function openCreate() {
  createForm.value = { name: '', flowIdsText: '', note: '' }
  createDialogVisible.value = true
}

async function submitCreate() {
  if (!createForm.value.name.trim()) {
    ElMessage.warning(t('record.nameRequired'))
    return
  }
  const flow_ids = createForm.value.flowIdsText
    .split(/[\s,;\n]+/)
    .map(s => parseInt(s.trim(), 10))
    .filter(n => !Number.isNaN(n) && n > 0)
  if (!flow_ids.length) {
    ElMessage.warning(t('record.flowIdsRequired'))
    return
  }
  createSubmitting.value = true
  try {
    await axios.post('/api/record-scripts', {
      name: createForm.value.name,
      flow_ids,
      note: createForm.value.note,
    })
    ElMessage.success(t('record.createSuccess'))
    createDialogVisible.value = false
    await loadScripts()
  } catch (e: any) {
    ElMessage.error(t('record.createFailed') + (e?.message || e))
  } finally {
    createSubmitting.value = false
  }
}

async function viewScript(r: RecordScript) {
  try {
    const res = await axios.get(`/api/record-scripts/${r.id}`)
    const full = unwrap<RecordScript & { flows: any[] }>(res, {} as any)
    detailScript.value = full
    detailDialogVisible.value = true
  } catch (e: any) {
    ElMessage.error(t('record.loadDetailFailed') + (e?.message || e))
  }
}

function openReplay(r: RecordScript) {
  replayTarget.value = r
  replayStats.value = null
  replayDialogVisible.value = true
}

async function doReplay() {
  if (!replayTarget.value) return
  replaying.value = true
  replayStats.value = null
  try {
    const res = await axios.post(`/api/record-scripts/${replayTarget.value.id}/replay`)
    replayStats.value = unwrap<ReplayStats>(res, {} as ReplayStats)
    ElMessage.success(t('record.replayDone'))
  } catch (e: any) {
    ElMessage.error(t('record.replayFailed') + (e?.message || e))
  } finally {
    replaying.value = false
  }
}

async function deleteScript(r: RecordScript) {
  try {
    await ElMessageBox.confirm(t('record.deleteConfirm'), t('record.deleteTitle'), {
      confirmButtonText: t('record.deleteButton'),
      cancelButtonText: t('record.cancelButton'),
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await axios.delete(`/api/record-scripts/${r.id}`)
    scripts.value = scripts.value.filter(x => x.id !== r.id)
    ElMessage.success(t('record.deleted'))
  } catch (e: any) {
    ElMessage.error(t('record.deleteFailed') + (e?.message || e))
  }
}

const statusDistEntries = computed(() => {
  const dist = replayStats.value?.status_distribution || {}
  return Object.entries(dist).sort((a, b) => a[0].localeCompare(b[0]))
})

onMounted(() => {
  loadScripts()
  loadRecordingStatus()
  // 录制中时轮询状态更新已录制数量
  statusTimer = window.setInterval(() => {
    if (recording.value.active) loadRecordingStatus()
  }, 2000)
})
onUnmounted(() => {
  if (statusTimer !== null) clearInterval(statusTimer)
})
</script>

<template>
  <div class="record-view full flex flex-col">
    <div class="page-header">
      <div class="header-back">
        <el-button size="small" @click="() => router.push(flows.lastPage || '/capture')" :title="t('common.back')">
          <el-icon><ArrowLeft /></el-icon>&nbsp;{{ t('common.back') }}
        </el-button>
      </div>
      <div class="page-title no-select">
        <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('record.pageTitle') }}
      </div>
      <div class="header-actions">
        <el-tag v-if="recording.active" type="danger" size="small" effect="dark" class="pulse-dot no-select">
          {{ t('record.recording') }} · {{ recording.flow_count }}
        </el-tag>
        <el-tag v-else type="info" size="small" class="no-select">{{ t('record.stopped') }}</el-tag>
        <el-button
          v-if="!recording.active"
          type="danger"
          size="small"
          :loading="togglingRecord"
          @click="startRecording"
        >
          <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('record.startRecording') }}
        </el-button>
        <el-button
          v-else
          type="warning"
          size="small"
          :loading="togglingRecord"
          @click="stopRecording"
        >
          <el-icon><VideoPause /></el-icon>&nbsp;{{ t('record.stopRecording') }}
        </el-button>
        <el-button type="primary" size="small" @click="openCreate">
          <el-icon><Plus /></el-icon>&nbsp;{{ t('record.newScript') }}
        </el-button>
      </div>
    </div>

    <!-- Tab 切换 -->
    <div class="tab-bar">
      <div class="tab-item" :class="{ active: activeTab === 'scripts' }" @click="activeTab = 'scripts'; loadScripts()">
        <el-icon><Document /></el-icon>&nbsp;{{ t('record.scripts') }}
      </div>
      <div class="tab-item" :class="{ active: activeTab === 'variables' }" @click="activeTab = 'variables'; loadVariables()">
        <el-icon><Key /></el-icon>&nbsp;{{ t('record.variables') }}
      </div>
      <div class="tab-item" :class="{ active: activeTab === 'environments' }" @click="activeTab = 'environments'; loadEnvironments()">
        <el-icon><Setting /></el-icon>&nbsp;{{ t('record.environments') }}
      </div>
      <div class="tab-item" :class="{ active: activeTab === 'assertions' }" @click="activeTab = 'assertions'; loadAssertions()">
        <el-icon><CircleCheck /></el-icon>&nbsp;{{ t('record.assertions') }}
      </div>
    </div>

    <!-- 脚本列表 Tab -->
    <div v-show="activeTab === 'scripts'" class="tab-content flex-1 overflow-auto" style="padding: 0 16px 16px">
      <el-table :data="scripts" size="small" border stripe class="no-select">
        <el-table-column :label="t('record.name')" prop="name" min-width="160" />
        <el-table-column :label="t('record.createdAt')" width="180">
          <template #default="{ row }">{{ row.created_at }}</template>
        </el-table-column>
        <el-table-column :label="t('record.flowCount')" width="100">
          <template #default="{ row }">{{ row.flow_count ?? (row.flow_ids?.length || 0) }}</template>
        </el-table-column>
        <el-table-column :label="t('record.note')" min-width="140">
          <template #default="{ row }">
            <span v-if="row.note" :title="row.note">{{ row.note }}</span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('record.operations')" width="320">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="viewScript(row as RecordScript)">{{ t('record.view') }}</el-button>
            <el-button link type="success" size="small" @click="openReplay(row as RecordScript)">{{ t('record.replay') }}</el-button>
            <el-button link type="warning" size="small" @click="openParameterizedReplay(row as RecordScript)" :title="t('record.parameterizedReplay')">{{ t('record.paramReplay') }}</el-button>
            <el-button link type="info" size="small" @click="openVariablesDialog(row as RecordScript)">{{ t('record.variables') }}</el-button>
            <el-button link type="warning" size="small" @click="openAssertionsDialog(row as RecordScript)">{{ t('record.assertions') }}</el-button>
            <el-button link type="primary" size="small" @click="convertToMock(row as RecordScript)" :title="t('record.convertToMock')">{{ t('record.toMock') }}</el-button>
            <el-button link type="danger" size="small" @click="deleteScript(row as RecordScript)">{{ t('record.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('record.emptyHint') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 变量管理 Tab -->
    <div v-show="activeTab === 'variables'" class="tab-content flex-1 overflow-auto" style="padding: 16px">
      <div class="section-header">
        <div class="section-title">{{ t('record.variablesTitle') }}</div>
      </div>
      <el-alert :title="t('record.variablesHint')" type="info" :closable="false" style="margin-bottom: 16px" />
      <el-table :data="scriptVariables" size="small" border>
        <el-table-column :label="t('record.scriptName')" prop="script_id" min-width="160" />
        <el-table-column :label="t('record.varCount')" width="100">
          <template #default="{ row }">{{ (row.variables || []).length }}</template>
        </el-table-column>
        <el-table-column :label="t('record.variables')" min-width="300">
          <template #default="{ row }">
            <el-tag v-for="v in (row.variables || [])" :key="v.name" size="small" style="margin: 2px">
              {{ v.name }}: {{ v.type }}
            </el-tag>
            <span v-if="!(row.variables || []).length" class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('record.operations')" width="150">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="extractVariables(row as any)">{{ t('record.extractVars') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('record.variablesEmpty') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 环境管理 Tab -->
    <div v-show="activeTab === 'environments'" class="tab-content flex-1 overflow-auto" style="padding: 16px">
      <div class="section-header">
        <div class="section-title">{{ t('record.environmentsTitle') }}</div>
        <el-button type="primary" size="small" @click="openNewEnv">
          <el-icon><Plus /></el-icon>&nbsp;{{ t('record.newEnv') }}
        </el-button>
      </div>
      <el-table :data="environments" size="small" border stripe>
        <el-table-column :label="t('record.envName')" prop="name" min-width="160" />
        <el-table-column :label="t('record.description')" prop="description" min-width="160" show-overflow-tooltip />
        <el-table-column :label="t('record.envVars')" min-width="200">
          <template #default="{ row }">
            <span v-if="Object.keys(row.variables || {}).length" class="mono text-dim">
              {{ JSON.stringify(row.variables).slice(0, 60) }}...
            </span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('record.isDefault')" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.is_default" type="success" size="small">{{ t('common.yes') }}</el-tag>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('record.operations')" width="130">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEditEnv(row as ReplayEnvironment)">{{ t('common.edit') }}</el-button>
            <el-button link type="danger" size="small" @click="deleteEnv(row as ReplayEnvironment)">{{ t('common.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('record.envsEmpty') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 断言管理 Tab -->
    <div v-show="activeTab === 'assertions'" class="tab-content flex-1 overflow-auto" style="padding: 16px">
      <div class="section-header">
        <div class="section-title">{{ t('record.assertionsTitle') }}</div>
      </div>
      <el-table :data="assertionRules" size="small" border stripe>
        <el-table-column :label="t('record.enabled')" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('record.flowId')" prop="flow_id" width="80" />
        <el-table-column :label="t('record.assertField')" prop="field" width="140" />
        <el-table-column :label="t('record.assertOperator')" prop="operator" width="100" />
        <el-table-column :label="t('record.assertExpected')" prop="expected_value" min-width="120" />
        <el-table-column :label="t('record.operations')" width="100">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="deleteAssertion(row as AssertionRule)">{{ t('common.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">{{ t('record.assertionsEmpty') }}</div>
        </template>
      </el-table>
    </div>

    <!-- 新建脚本对话框 -->
    <el-dialog
      v-model="createDialogVisible"
      :title="t('record.newTitle')"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form :model="createForm" label-width="100px" size="default">
        <el-form-item :label="t('record.name')">
          <el-input v-model="createForm.name" :placeholder="t('record.namePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('record.flowIds')">
          <el-input
            v-model="createForm.flowIdsText"
            type="textarea"
            :rows="4"
            :placeholder="t('record.flowIdsPlaceholder')"
          />
        </el-form-item>
        <el-form-item :label="t('record.note')">
          <el-input v-model="createForm.note" :placeholder="t('record.notePlaceholder')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">{{ t('record.cancelButton') }}</el-button>
        <el-button type="primary" :loading="createSubmitting" @click="submitCreate">{{ t('record.createButton') }}</el-button>
      </template>
    </el-dialog>

    <!-- 回放对话框 -->
    <el-dialog
      v-model="replayDialogVisible"
      :title="t('record.replayTitle')"
      width="560px"
      :close-on-click-modal="false"
    >
      <div v-if="replayTarget" class="replay-info">
        <div><span class="info-label">{{ t('record.name') }}:</span> {{ replayTarget.name }}</div>
        <div><span class="info-label">{{ t('record.flowCount') }}:</span> {{ replayTarget.flow_count ?? (replayTarget.flow_ids?.length || 0) }}</div>
      </div>
      <el-alert
        :title="t('record.replayAlert')"
        type="warning"
        :closable="false"
        style="margin: 12px 0"
      />
      <div v-if="replayStats" class="replay-stats">
        <div class="stats-row">
          <el-tag type="success" size="large">{{ t('record.success') }}: {{ replayStats.success }}</el-tag>
          <el-tag type="danger" size="large">{{ t('record.fail') }}: {{ replayStats.fail }}</el-tag>
          <el-tag type="info" size="large">{{ t('record.replayed') }}: {{ replayStats.replayed }}</el-tag>
          <el-tag v-if="replayStats.skipped" type="warning" size="large">{{ t('record.skipped') }}: {{ replayStats.skipped }}</el-tag>
        </div>
        <div class="stats-title">{{ t('record.statusDistribution') }}</div>
        <div class="stats-dist">
          <el-tag v-for="[code, cnt] in statusDistEntries" :key="code" size="default" style="margin: 2px 6px 2px 0">
            {{ code }}: {{ cnt }}
          </el-tag>
          <span v-if="!statusDistEntries.length" class="text-dim">-</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="replayDialogVisible = false">{{ t('record.closeButton') }}</el-button>
        <el-button type="success" :loading="replaying" @click="doReplay">{{ t('record.replayButton') }}</el-button>
      </template>
    </el-dialog>

    <!-- 详情对话框 -->
    <el-dialog
      v-model="detailDialogVisible"
      :title="t('record.detailTitle')"
      width="640px"
      :close-on-click-modal="true"
    >
      <div v-if="detailScript" class="detail-content">
        <div><span class="info-label">{{ t('record.name') }}:</span> {{ detailScript.name }}</div>
        <div><span class="info-label">{{ t('record.createdAt') }}:</span> {{ detailScript.created_at }}</div>
        <div><span class="info-label">{{ t('record.note') }}:</span> {{ detailScript.note || '-' }}</div>
        <div><span class="info-label">{{ t('record.flowCount') }}:</span> {{ detailScript.flow_count ?? (detailScript.flow_ids?.length || 0) }}</div>
        <div class="info-label" style="margin-top: 8px">{{ t('record.flowsList') }}:</div>
        <el-table :data="(detailScript as any).flows || []" size="small" border max-height="320" style="margin-top: 4px">
          <el-table-column label="#" prop="id" width="70" />
          <el-table-column :label="t('record.method')" prop="method" width="90" />
          <el-table-column :label="t('record.url')" prop="url" min-width="200" show-overflow-tooltip />
          <el-table-column :label="t('record.status')" prop="status_code" width="80" />
        </el-table>
      </div>
      <template #footer>
        <el-button @click="detailDialogVisible = false">{{ t('record.closeButton') }}</el-button>
      </template>
    </el-dialog>

    <!-- 变量对话框 -->
    <el-dialog
      v-model="variablesDialogVisible"
      :title="t('record.variablesTitle')"
      width="700px"
      :close-on-click-modal="false"
    >
      <div v-if="scriptVariables.find(v => v.script_id === variablesScript?.id)" class="variables-content">
        <el-alert :title="t('record.variablesHint')" type="info" :closable="false" style="margin-bottom: 16px" />
        <div v-for="sv in scriptVariables.filter(v => v.script_id === variablesScript?.id)" :key="sv.script_id">
          <div class="section-title">{{ t('record.variables') }} ({{ (sv.variables || []).length }})</div>
          <el-table :data="sv.variables || []" size="small" border style="margin: 12px 0">
            <el-table-column :label="t('record.varName')" prop="name" width="120" />
            <el-table-column :label="t('record.varType')" prop="type" width="100" />
            <el-table-column :label="t('record.varRule')" prop="extraction_rule" min-width="160" />
            <el-table-column :label="t('record.varSamples')" min-width="160">
              <template #default="{ row }">
                <span v-if="row.sample_values?.length" class="mono text-dim">
                  {{ row.sample_values.slice(0, 2).join(', ') }}...
                </span>
                <span v-else class="text-dim">-</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
      <template #footer>
        <el-button @click="variablesDialogVisible = false">{{ t('record.closeButton') }}</el-button>
        <el-button type="primary" @click="extractVariables(variablesScript as RecordScript)">{{ t('record.extractVars') }}</el-button>
      </template>
    </el-dialog>

    <!-- 环境编辑对话框 -->
    <el-dialog
      v-model="envDialogVisible"
      :title="editingEnv ? t('record.editEnv') : t('record.newEnv')"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form label-width="100px" size="default">
        <el-form-item :label="t('record.envName')">
          <el-input v-model="envForm.name" :placeholder="t('record.envNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('record.description')">
          <el-input v-model="envForm.description" :placeholder="t('record.envDescPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('record.envVars')">
          <el-input
            v-model="envVariablesText"
            type="textarea"
            :rows="5"
            :placeholder="t('record.envVarsPlaceholder')"
            class="mono"
          />
        </el-form-item>
        <el-form-item :label="t('record.isDefault')">
          <el-switch v-model="envForm.is_default" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="envDialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" @click="submitEnv">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 参数化回放对话框 -->
    <el-dialog
      v-model="parameterizedReplayVisible"
      :title="t('record.parameterizedReplay')"
      width="480px"
      :close-on-click-modal="false"
    >
      <el-form label-width="120px" size="default">
        <el-form-item :label="t('record.envName')">
          <el-select v-model="parameterizedForm.environment_id" :placeholder="t('record.selectEnv')" clearable style="width: 100%">
            <el-option v-for="env in environments" :key="env.id" :label="env.name" :value="env.id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('record.iterations')">
          <el-input-number v-model="parameterizedForm.iterations" :min="1" :max="1000" style="width: 100%" />
        </el-form-item>
        <el-form-item :label="t('record.delayMs')">
          <el-input-number v-model="parameterizedForm.delay_ms" :min="0" :max="60000" :step="100" style="width: 100%" />
        </el-form-item>
        <el-form-item :label="t('record.failFast')">
          <el-switch v-model="parameterizedForm.fail_fast" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="parameterizedReplayVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="success" :loading="replaying" @click="doParameterizedReplay">{{ t('record.replayButton') }}</el-button>
      </template>
    </el-dialog>

    <!-- 断言对话框 -->
    <el-dialog
      v-model="assertionDialogVisible"
      :title="t('record.assertionsTitle')"
      width="700px"
      :close-on-click-modal="false"
    >
      <el-alert :title="t('record.assertionsHint')" type="info" :closable="false" style="margin-bottom: 16px" />
      <el-table :data="assertionRules" size="small" border style="margin-bottom: 16px">
        <el-table-column :label="t('record.enabled')" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('record.flowId')" prop="flow_id" width="80" />
        <el-table-column :label="t('record.assertField')" prop="field" width="140" />
        <el-table-column :label="t('record.assertOperator')" prop="operator" width="100" />
        <el-table-column :label="t('record.assertExpected')" prop="expected_value" min-width="120" />
        <el-table-column :label="t('record.operations')" width="100">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="deleteAssertion(row as AssertionRule)">{{ t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-divider />
      <div class="section-title">{{ t('record.addAssertion') }}</div>
      <el-form :model="assertionForm" inline size="small" style="margin-top: 12px">
        <el-form-item :label="t('record.flowId')">
          <el-input-number v-model="assertionForm.flow_id" :min="0" controls-position="right" />
        </el-form-item>
        <el-form-item :label="t('record.assertField')">
          <el-select v-model="assertionForm.field" style="width: 140px">
            <el-option label="Status Code" value="status_code" />
            <el-option label="Response Body" value="response_body" />
            <el-option label="Response Header" value="response_header" />
            <el-option label="Response Time" value="response_time" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('record.assertOperator')">
          <el-select v-model="assertionForm.operator" style="width: 100px">
            <el-option label="Equals" value="equals" />
            <el-option label="Contains" value="contains" />
            <el-option label="Regex" value="regex" />
            <el-option label="Greater" value="greater" />
            <el-option label="Less" value="less" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('record.assertExpected')">
          <el-input v-model="assertionForm.expected_value" style="width: 140px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="submitAssertion">{{ t('common.add') }}</el-button>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assertionDialogVisible = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.record-view { background: var(--on-bg); }
.record-view .page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 16px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.record-view .header-back { flex-shrink: 0; }
.record-view .page-title { font-size: 15px; font-weight: 600; color: var(--on-text); display: flex; align-items: center; }
.record-view .header-actions { display: flex; gap: 6px; }

/* Tab 样式 */
.tab-bar {
  display: flex; align-items: center; gap: 4px;
  padding: 8px 16px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.tab-item {
  display: flex; align-items: center; gap: 4px;
  padding: 6px 14px; border-radius: var(--on-radius-md);
  cursor: pointer; font-size: 13px; color: var(--on-text-secondary);
  transition: all 0.2s;
}
.tab-item:hover { background: var(--on-bg-hover); color: var(--on-text); }
.tab-item.active { background: var(--on-accent); color: white; font-weight: 500; }
.tab-content { background: var(--on-bg); }

/* 通用样式 */
.empty-text { padding: 30px; }
.replay-info { font-size: 13px; line-height: 1.8; }
.info-label { color: var(--on-text-secondary, #999); margin-right: 4px; }
.replay-stats { margin-top: 8px; }
.stats-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.stats-title { font-size: 13px; color: var(--on-text-secondary, #999); margin-bottom: 6px; }
.stats-dist { display: flex; flex-wrap: wrap; }
.detail-content { font-size: 13px; line-height: 1.8; }
.variables-content { max-height: 400px; overflow-y: auto; }
.section-title { font-size: 13px; font-weight: 600; margin: 8px 0; color: var(--on-text-secondary); }
.section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
</style>
