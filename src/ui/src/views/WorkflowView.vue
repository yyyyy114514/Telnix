<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import {
  Refresh, Plus, Delete, CopyDocument, VideoPlay, VideoPause,
  RefreshRight, Download, PriceTag, Clock, Timer, Edit, DocumentCopy,
  ArrowRight, Folder, Files, Setting, List, Grid, UploadFilled
} from '@element-plus/icons-vue'
import { useWorkflowStore, type Workflow, type WorkflowStep, type WorkflowStepAction } from '../stores/workflow'
import { useFlowsStore } from '../stores/flows'

const { t } = useI18n()
const workflowStore = useWorkflowStore()
const flowsStore = useFlowsStore()

// ============ UI 状态 ============
const viewMode = ref<'list' | 'designer'>('list')
const createDialogVisible = ref(false)
const editStepDialogVisible = ref(false)
const executeDialogVisible = ref(false)
const importDialogVisible = ref(false)
const importFile = ref<File | null>(null)
const creating = ref(false)
const importing = ref(false)

// 新建工作流表单
const newWorkflowName = ref('')
const newWorkflowDesc = ref('')
const newWorkflowFromTemplate = ref('')

// 编辑步骤
const editingStep = ref<WorkflowStep | null>(null)
const editingStepParams = ref<Record<string, any>>({})

// 执行选中的流量
const executeFlowIdsInput = ref('')
function parseFlowIds(input: string): number[] {
  if (!input.trim()) return []
  return input.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n))
}

// ============ 可拖拽的操作步骤定义 ============
const availableActions: Array<{
  action: WorkflowStepAction
  label: string
  icon: string
  color: string
  defaultParams: Record<string, any>
}> = [
  { action: 'replay', label: '重放流量', icon: 'RefreshRight', color: 'var(--on-cat-capture)', defaultParams: {} },
  { action: 'batch_replay', label: '批量重放', icon: 'Refresh', color: 'var(--on-cat-capture)', defaultParams: { qps: 5, override: {} } },
  { action: 'modify', label: '修改响应', icon: 'Edit', color: 'var(--on-cat-auto)', defaultParams: { rules: [] } },
  { action: 'delay', label: '添加延迟', icon: 'Clock', color: 'var(--on-amber)', defaultParams: { delayMs: 1000 } },
  { action: 'wait', label: '等待', icon: 'Timer', color: 'var(--on-cyan)', defaultParams: { ms: 1000 } },
  { action: 'export', label: '导出', icon: 'Download', color: 'var(--on-cat-codec)', defaultParams: { format: 'har' } },
  { action: 'tag', label: '添加标记', icon: 'PriceTag', color: 'var(--on-purple)', defaultParams: { tag: '', color: '#0d9488' } },
  { action: 'delete', label: '删除流量', icon: 'Delete', color: 'var(--on-error)', defaultParams: {} },
  { action: 'copy', label: '复制流量', icon: 'DocumentCopy', color: 'var(--on-blue)', defaultParams: {} },
]

// ============ 计算属性 ============
const currentWorkflow = computed(() => workflowStore.currentWorkflow)
const executionStatus = computed(() => workflowStore.executionStatus)
const executionProgress = computed(() => workflowStore.executionProgress)
const executionLogs = computed(() => workflowStore.executionLogs)
const executionStats = computed(() => workflowStore.executionStats)

// ============ 方法 ============

// 创建工作流
async function onCreateWorkflow() {
  if (!newWorkflowName.value.trim()) {
    ElMessage.warning(t('workflow.nameRequired') || '请输入工作流名称')
    return
  }
  creating.value = true
  try {
    const wf = workflowStore.createWorkflow(
      newWorkflowName.value.trim(),
      newWorkflowDesc.value.trim(),
      newWorkflowFromTemplate.value
    )
    workflowStore.currentWorkflowId = wf.id
    viewMode.value = 'designer'
    createDialogVisible.value = false
    newWorkflowName.value = ''
    newWorkflowDesc.value = ''
    newWorkflowFromTemplate.value = ''
    ElMessage.success(t('workflow.created') || '工作流已创建')
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    creating.value = false
  }
}

// 删除工作流
async function onDeleteWorkflow(id: string) {
  try {
    await ElMessageBox.confirm(
      t('workflow.deleteConfirm') || '确定删除此工作流？',
      t('common.confirm') || '确认',
      { type: 'warning' }
    )
    workflowStore.deleteWorkflow(id)
    ElMessage.success(t('workflow.deleted') || '已删除')
  } catch {
    // 取消
  }
}

// 复制工作流
async function onDuplicateWorkflow(id: string) {
  const wf = workflowStore.duplicateWorkflow(id)
  if (wf) {
    ElMessage.success(t('workflow.duplicated') || '已复制')
  }
}

// 打开工作流设计器
function openDesigner(workflow: Workflow) {
  workflowStore.currentWorkflowId = workflow.id
  viewMode.value = 'designer'
}

// 返回列表
function backToList() {
  viewMode.value = 'list'
  workflowStore.currentWorkflowId = null
}

// 添加步骤到工作流
function addStepToWorkflow(action: WorkflowStepAction) {
  if (!currentWorkflow.value) return

  const actionDef = availableActions.find(a => a.action === action)
  if (!actionDef) return

  workflowStore.addStep(currentWorkflow.value.id, {
    action,
    name: actionDef.label,
    params: { ...actionDef.defaultParams },
    enabled: true,
  })
}

// 删除步骤
function onDeleteStep(stepId: string) {
  if (!currentWorkflow.value) return
  workflowStore.removeStep(currentWorkflow.value.id, stepId)
}

// 打开编辑步骤对话框
function openEditStepDialog(step: WorkflowStep) {
  editingStep.value = { ...step, params: { ...step.params } }
  editingStepParams.value = { ...step.params }
  editStepDialogVisible.value = true
}

// 保存步骤修改
function saveStepEdit() {
  if (!currentWorkflow.value || !editingStep.value) return

  workflowStore.updateStep(
    currentWorkflow.value.id,
    editingStep.value.id,
    { params: editingStepParams.value }
  )
  editStepDialogVisible.value = false
}

// 执行工作流
async function onExecuteWorkflow() {
  if (!currentWorkflow.value) return

  // 获取选中的流量
  const selectedIds = flowsStore.flows
    .filter(f => flowsStore.selectedId === f.id)
    .map(f => f.id)

  if (selectedIds.length === 0 && executeFlowIdsInput.value.trim() === '') {
    ElMessage.warning(t('workflow.noFlowsSelected') || '请先在抓包页面选择流量，或输入流量 ID')
    return
  }

  const flowIds = executeFlowIdsInput.value.trim() ? parseFlowIds(executeFlowIdsInput.value) : selectedIds
  await workflowStore.executeWorkflow(currentWorkflow.value.id, flowIds)
  executeDialogVisible.value = false
}

// 停止执行
function onStopExecution() {
  workflowStore.stopExecution()
}

// 清除日志
function onClearLogs() {
  workflowStore.clearExecutionLogs()
}

// 导出工作流
function onExportWorkflow(id: string) {
  const json = workflowStore.exportWorkflow(id)
  if (!json) return

  const blob = new Blob([json], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `workflow_${Date.now()}.json`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
  ElMessage.success(t('workflow.exported') || '已导出')
}

// 导入工作流
function onImportWorkflow() {
  if (!importFile.value) return
  importing.value = true

  importFile.value.text().then(text => {
    const wf = workflowStore.importWorkflow(text)
    if (wf) {
      ElMessage.success(t('workflow.imported') || '已导入')
      importDialogVisible.value = false
    }
  }).catch((e: any) => {
    ElMessage.error(e?.message || String(e))
  }).finally(() => {
    importing.value = false
  })
}

// 格式化执行时长
function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${(ms / 60000).toFixed(1)}m`
}

// 获取执行状态类型
function getStatusType(status: string): string {
  switch (status) {
    case 'running': return 'primary'
    case 'success': return 'success'
    case 'failed': return 'danger'
    case 'skipped': return 'warning'
    default: return 'info'
  }
}

// 获取动作颜色
function getActionColor(action: WorkflowStepAction): string {
  const def = availableActions.find(a => a.action === action)
  return def?.color || 'var(--on-text-dim)'
}

// 拖拽相关
const draggingAction = ref<WorkflowStepAction | null>(null)
const dragOverStepIndex = ref<number | null>(null)

function onDragStart(e: DragEvent, action: WorkflowStepAction) {
  draggingAction.value = action
  if (e.dataTransfer) {
    e.dataTransfer.setData('text/plain', action)
    e.dataTransfer.effectAllowed = 'copy'
  }
}

function onDragEnd() {
  draggingAction.value = null
  dragOverStepIndex.value = null
}

function onDragOver(e: DragEvent, index: number) {
  e.preventDefault()
  dragOverStepIndex.value = index
}

function onDrop(e: DragEvent, index: number) {
  e.preventDefault()
  if (!currentWorkflow.value || !draggingAction.value) return

  const actionDef = availableActions.find(a => a.action === draggingAction.value)
  if (!actionDef) return

  // 添加步骤
  workflowStore.addStep(currentWorkflow.value.id, {
    action: draggingAction.value,
    name: actionDef.label,
    params: { ...actionDef.defaultParams },
    enabled: true,
  })

  draggingAction.value = null
  dragOverStepIndex.value = null
}

function onCanvasDrop(e: DragEvent) {
  e.preventDefault()
  if (!currentWorkflow.value || !draggingAction.value) return

  const actionDef = availableActions.find(a => a.action === draggingAction.value)
  if (!actionDef) return

  workflowStore.addStep(currentWorkflow.value.id, {
    action: draggingAction.value,
    name: actionDef.label,
    params: { ...actionDef.defaultParams },
    enabled: true,
  })

  draggingAction.value = null
}

// 初始化
onMounted(() => {
  workflowStore.initTemplates()
})

// 保存工作流
function saveCurrentWorkflow() {
  ElMessage.success(t('workflow.saved') || '已保存')
}
</script>

<template>
  <div class="workflow-view full flex flex-col">
    <!-- 列表视图 -->
    <template v-if="viewMode === 'list'">
      <div class="page-header">
        <div class="page-title no-select">
          <el-icon><Setting /></el-icon>&nbsp;{{ t('workflow.title') || '工作流' }}
        </div>
        <div class="header-actions">
          <el-button size="small" @click="importDialogVisible = true">
            <el-icon><UploadFilled /></el-icon>&nbsp;{{ t('workflow.import') || '导入' }}
          </el-button>
          <el-button size="small" type="primary" @click="createDialogVisible = true">
            <el-icon><Plus /></el-icon>&nbsp;{{ t('workflow.create') || '新建工作流' }}
          </el-button>
        </div>
      </div>

      <div class="workflow-content">
        <!-- 模板区域 -->
        <div v-if="workflowStore.templateWorkflows.length" class="workflow-section">
          <div class="section-title">{{ t('workflow.templates') || '预设模板' }}</div>
          <div class="workflow-grid">
            <div
              v-for="wf in workflowStore.templateWorkflows"
              :key="wf.id"
              class="workflow-card"
              :class="{ 'is-template': wf.isTemplate }"
            >
              <div class="card-header">
                <span class="card-name">{{ wf.name }}</span>
                <el-tag size="small" type="info">{{ t('workflow.template') || '模板' }}</el-tag>
              </div>
              <div class="card-desc text-dim">{{ wf.description }}</div>
              <div class="card-footer">
                <span class="text-dim" style="font-size: 11px">
                  {{ wf.steps.length }} {{ t('workflow.steps') || '步骤' }}
                </span>
                <el-button size="small" text @click="openDesigner(wf)">
                  {{ t('workflow.useTemplate') || '使用模板' }}&nbsp;<el-icon><ArrowRight /></el-icon>
                </el-button>
              </div>
            </div>
          </div>
        </div>

        <!-- 用户工作流区域 -->
        <div class="workflow-section">
          <div class="section-title">{{ t('workflow.myWorkflows') || '我的工作流' }}</div>
          <div v-if="!workflowStore.userWorkflows.length" class="empty-state">
            <el-icon class="empty-icon"><Folder /></el-icon>
            <div class="empty-text">{{ t('workflow.noWorkflows') || '暂无工作流，点击右上角新建' }}</div>
          </div>
          <div v-else class="workflow-grid">
            <div
              v-for="wf in workflowStore.userWorkflows"
              :key="wf.id"
              class="workflow-card"
            >
              <div class="card-header">
                <span class="card-name">{{ wf.name }}</span>
              </div>
              <div class="card-desc text-dim">{{ wf.description || '-' }}</div>
              <div class="card-meta text-dim">
                <span>{{ wf.steps.length }} {{ t('workflow.steps') || '步骤' }}</span>
                <span v-if="wf.lastRunAt">
                  {{ t('workflow.lastRun') || '上次运行' }}: {{ new Date(wf.lastRunAt).toLocaleString() }}
                </span>
                <span v-if="wf.runCount">{{ wf.runCount }} {{ t('workflow.runs') || '次运行' }}</span>
              </div>
              <div class="card-actions">
                <el-button size="small" type="primary" @click="openDesigner(wf)">
                  <el-icon><Edit /></el-icon>&nbsp;{{ t('common.edit') || '编辑' }}
                </el-button>
                <el-button size="small" @click="onDuplicateWorkflow(wf.id)">
                  <el-icon><Copy /></el-icon>&nbsp;{{ t('common.copy') || '复制' }}
                </el-button>
                <el-button size="small" @click="onExportWorkflow(wf.id)">
                  <el-icon><Download /></el-icon>&nbsp;{{ t('common.export') || '导出' }}
                </el-button>
                <el-button size="small" type="danger" @click="onDeleteWorkflow(wf.id)">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 设计器视图 -->
    <template v-else>
      <div class="designer-header">
        <div class="header-left">
          <el-button size="small" @click="backToList">
            <el-icon><ArrowRight style="transform: rotate(180deg)" /></el-icon>&nbsp;{{ t('common.back') || '返回' }}
          </el-button>
          <span class="workflow-name">{{ currentWorkflow?.name }}</span>
          <span v-if="currentWorkflow?.description" class="workflow-desc text-dim">
            {{ currentWorkflow.description }}
          </span>
        </div>
        <div class="header-actions">
          <el-button size="small" @click="executeDialogVisible = true" :disabled="executionStatus === 'running'">
            <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('workflow.execute') || '执行' }}
          </el-button>
          <el-button size="small" @click="onExportWorkflow(currentWorkflow!.id)">
            <el-icon><Download /></el-icon>&nbsp;{{ t('common.export') || '导出' }}
          </el-button>
        </div>
      </div>

      <div class="designer-body">
        <!-- 左侧：工具栏 -->
        <div class="designer-toolbar">
          <div class="toolbar-title">{{ t('workflow.actions') || '操作步骤' }}</div>
          <div class="action-list">
            <div
              v-for="action in availableActions"
              :key="action.action"
              class="action-item"
              draggable="true"
              @dragstart="onDragStart($event, action.action)"
              @dragend="onDragEnd"
            >
              <span class="action-icon" :style="{ color: action.color }">
                <component :is="action.icon" />
              </span>
              <span class="action-label">{{ action.label }}</span>
            </div>
          </div>
          <div class="toolbar-hint text-dim">
            {{ t('workflow.dragHint') || '拖拽到画布添加步骤' }}
          </div>
        </div>

        <!-- 中间：画布 -->
        <div
          class="designer-canvas"
          @dragover.prevent
          @drop="onCanvasDrop"
        >
          <div v-if="!currentWorkflow?.steps.length" class="canvas-empty">
            <el-icon class="empty-icon"><Files /></el-icon>
            <div class="empty-text">{{ t('workflow.dragToAdd') || '拖拽操作步骤到此处' }}</div>
          </div>
          <div v-else class="step-list">
            <div
              v-for="(step, index) in currentWorkflow.steps"
              :key="step.id"
              class="step-card"
              :class="{ 'step-disabled': !step.enabled }"
              @dragover="onDragOver($event, index)"
            >
              <div class="step-index">{{ index + 1 }}</div>
              <div class="step-info">
                <div class="step-name">
                  <span class="step-icon" :style="{ color: getActionColor(step.action) }">
                    <RefreshRight v-if="step.action === 'replay'" />
                    <Refresh v-else-if="step.action === 'batch_replay'" />
                    <Edit v-else-if="step.action === 'modify'" />
                    <Clock v-else-if="step.action === 'delay'" />
                    <Timer v-else-if="step.action === 'wait'" />
                    <Download v-else-if="step.action === 'export'" />
                    <PriceTag v-else-if="step.action === 'tag'" />
                    <Delete v-else-if="step.action === 'delete'" />
                    <DocumentCopy v-else-if="step.action === 'copy'" />
                    <span v-else class="step-icon" :style="{ color: getActionColor(step.action) }">
                      <Files v-if="!step.action" />
                      <span v-else>?</span>
                    </span>
                  </span>
                  <span>{{ step.name }}</span>
                </div>
                <div class="step-params text-dim">
                  <template v-if="step.params.qps">QPS: {{ step.params.qps }}</template>
                  <template v-else-if="step.params.delayMs">延迟: {{ step.params.delayMs }}ms</template>
                  <template v-else-if="step.params.ms">等待: {{ step.params.ms }}ms</template>
                  <template v-else-if="step.params.format">格式: {{ step.params.format.toUpperCase() }}</template>
                  <template v-else-if="step.params.tag">标记: {{ step.params.tag }}</template>
                  <template v-else-if="step.action === 'delete'">删除选中的流量</template>
                  <template v-else-if="step.action === 'replay'">重放单个流量</template>
                </div>
              </div>
              <div class="step-actions">
                <el-switch
                  :model-value="step.enabled"
                  size="small"
                  @change="(v: any) => workflowStore.updateStep(currentWorkflow!.id, step.id, { enabled: !!v })"
                />
                <el-button size="small" text @click="openEditStepDialog(step)">
                  <el-icon><Edit /></el-icon>
                </el-button>
                <el-button size="small" text type="danger" @click="onDeleteStep(step.id)">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
            </div>
          </div>
        </div>

        <!-- 右侧：执行面板 -->
        <div class="designer-execute-panel">
          <div class="panel-title">{{ t('workflow.execution') || '执行控制' }}</div>

          <!-- 执行状态 -->
          <div class="execution-status">
            <el-tag v-if="executionStatus === 'idle'" type="info">
              <el-icon><Files /></el-icon>&nbsp;{{ t('workflow.statusIdle') || '待机' }}
            </el-tag>
            <el-tag v-else-if="executionStatus === 'running'" type="primary">
              <el-icon class="is-loading"><Refresh /></el-icon>&nbsp;{{ t('workflow.statusRunning') || '运行中' }}
            </el-tag>
            <el-tag v-else-if="executionStatus === 'completed'" type="success">
              <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('workflow.statusCompleted') || '已完成' }}
            </el-tag>
            <el-tag v-else-if="executionStatus === 'stopped'" type="warning">
              <el-icon><VideoPause /></el-icon>&nbsp;{{ t('workflow.statusStopped') || '已停止' }}
            </el-tag>
            <el-tag v-else-if="executionStatus === 'error'" type="danger">
              <el-icon><Delete /></el-icon>&nbsp;{{ t('workflow.statusError') || '错误' }}
            </el-tag>
          </div>

          <!-- 进度条 -->
          <div v-if="executionStatus === 'running'" class="execution-progress">
            <el-progress :percentage="executionProgress" :stroke-width="8" />
            <span class="progress-text text-dim">{{ executionProgress }}%</span>
          </div>

          <!-- 统计 -->
          <div v-if="executionStats.total > 0" class="execution-stats">
            <div class="stat-row">
              <span class="stat-label">{{ t('workflow.total') || '总数' }}:</span>
              <span class="stat-value">{{ executionStats.total }}</span>
            </div>
            <div class="stat-row">
              <span class="stat-label text-success">{{ t('workflow.success') || '成功' }}:</span>
              <span class="stat-value text-success">{{ executionStats.success }}</span>
            </div>
            <div class="stat-row">
              <span class="stat-label text-danger">{{ t('workflow.failed') || '失败' }}:</span>
              <span class="stat-value text-danger">{{ executionStats.failed }}</span>
            </div>
            <div class="stat-row" v-if="executionStats.duration">
              <span class="stat-label text-dim">{{ t('workflow.duration') || '耗时' }}:</span>
              <span class="stat-value text-dim">{{ formatDuration(executionStats.duration) }}</span>
            </div>
          </div>

          <!-- 控制按钮 -->
          <div class="execution-controls">
            <el-button
              v-if="executionStatus === 'idle' || executionStatus === 'completed'"
              size="small"
              type="primary"
              @click="executeDialogVisible = true"
            >
              <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('workflow.execute') || '执行' }}
            </el-button>
            <el-button
              v-if="executionStatus === 'running'"
              size="small"
              type="danger"
              @click="onStopExecution"
            >
              <el-icon><VideoPause /></el-icon>&nbsp;{{ t('workflow.stop') || '停止' }}
            </el-button>
            <el-button
              v-if="executionLogs.length > 0"
              size="small"
              @click="onClearLogs"
            >
              {{ t('workflow.clearLogs') || '清除日志' }}
            </el-button>
          </div>

          <!-- 执行日志 -->
          <div class="execution-logs" v-if="executionLogs.length > 0">
            <div class="logs-title">{{ t('workflow.logs') || '执行日志' }}</div>
            <div class="logs-list">
              <div
                v-for="(log, idx) in executionLogs"
                :key="idx"
                class="log-entry"
                :class="`log-${log.status}`"
              >
                <span class="log-time">{{ new Date(log.timestamp).toLocaleTimeString() }}</span>
                <span class="log-step">{{ log.stepName }}</span>
                <span class="log-status" :type="getStatusType(log.status)">
                  {{ log.status === 'success' ? '成功' : log.status === 'failed' ? '失败' : log.status === 'skipped' ? '跳过' : '进行中' }}
                </span>
                <span v-if="log.message" class="log-message text-dim">{{ log.message }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 新建工作流对话框 -->
    <el-dialog v-model="createDialogVisible" :title="t('workflow.create') || '新建工作流'" width="480px" destroy-on-close>
      <div class="create-form">
        <div class="form-item">
          <label>{{ t('workflow.name') || '名称' }}</label>
          <el-input v-model="newWorkflowName" :placeholder="t('workflow.namePlaceholder') || '输入工作流名称'" />
        </div>
        <div class="form-item">
          <label>{{ t('workflow.description') || '描述' }}</label>
          <el-input v-model="newWorkflowDesc" type="textarea" :rows="2" :placeholder="t('workflow.descPlaceholder') || '可选描述'" />
        </div>
        <div class="form-item">
          <label>{{ t('workflow.fromTemplate') || '从模板创建' }}</label>
          <el-select v-model="newWorkflowFromTemplate" :placeholder="t('workflow.selectTemplate') || '选择模板（可选）'" clearable>
            <el-option
              v-for="tmpl in workflowStore.templateWorkflows"
              :key="tmpl.id"
              :label="tmpl.name"
              :value="tmpl.name"
            />
          </el-select>
        </div>
      </div>
      <template #footer>
        <el-button @click="createDialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="creating" @click="onCreateWorkflow">
          {{ t('workflow.create') || '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑步骤对话框 -->
    <el-dialog v-model="editStepDialogVisible" :title="t('workflow.editStep') || '编辑步骤'" width="400px" destroy-on-close>
      <div v-if="editingStep" class="step-edit-form">
        <div class="form-item">
          <label>{{ t('workflow.stepName') || '步骤名称' }}</label>
          <el-input v-model="editingStep.name" />
        </div>

        <!-- 批量重放参数 -->
        <template v-if="editingStep.action === 'batch_replay'">
          <div class="form-item">
            <label>{{ t('workflow.qps') || 'QPS' }}</label>
            <el-input-number v-model="editingStepParams.qps" :min="0" :max="100" />
          </div>
        </template>

        <!-- 延迟参数 -->
        <template v-if="editingStep.action === 'delay'">
          <div class="form-item">
            <label>{{ t('workflow.delayMs') || '延迟 (ms)' }}</label>
            <el-input-number v-model="editingStepParams.delayMs" :min="0" :max="60000" />
          </div>
        </template>

        <!-- 等待参数 -->
        <template v-if="editingStep.action === 'wait'">
          <div class="form-item">
            <label>{{ t('workflow.waitMs') || '等待 (ms)' }}</label>
            <el-input-number v-model="editingStepParams.ms" :min="0" :max="60000" />
          </div>
        </template>

        <!-- 导出参数 -->
        <template v-if="editingStep.action === 'export'">
          <div class="form-item">
            <label>{{ t('workflow.format') || '格式' }}</label>
            <el-select v-model="editingStepParams.format">
              <el-option label="HAR" value="har" />
              <el-option label="JSON" value="json" />
              <el-option label="CSV" value="csv" />
              <el-option label="cURL" value="curl" />
              <el-option label="Python" value="python-requests" />
              <el-option label="Postman" value="postman" />
            </el-select>
          </div>
        </template>

        <!-- 标记参数 -->
        <template v-if="editingStep.action === 'tag'">
          <div class="form-item">
            <label>{{ t('workflow.tagName') || '标记名称' }}</label>
            <el-input v-model="editingStepParams.tag" :placeholder="t('workflow.tagPlaceholder') || '输入标记名称'" />
          </div>
          <div class="form-item">
            <label>{{ t('workflow.tagColor') || '标记颜色' }}</label>
            <el-color-picker v-model="editingStepParams.color" />
          </div>
        </template>
      </div>
      <template #footer>
        <el-button @click="editStepDialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" @click="saveStepEdit">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 执行对话框 -->
    <el-dialog v-model="executeDialogVisible" :title="t('workflow.executeWorkflow') || '执行工作流'" width="420px" destroy-on-close>
      <div class="execute-form">
        <el-alert type="info" :closable="false">
          {{ t('workflow.executeHint') || '将使用当前选中的流量执行工作流' }}
        </el-alert>
        <div class="form-item" style="margin-top: 16px">
          <label>{{ t('workflow.selectedFlows') || '选中的流量' }}</label>
          <div class="selected-flows-info">
            <el-icon><Files /></el-icon>
            <span>{{ flowsStore.selectedId ? '1' : 0 }} {{ t('workflow.flowsSelected') || '条流量已选中' }}</span>
          </div>
        </div>
        <div class="form-item">
          <label>{{ t('workflow.orEnterIds') || '或输入流量 ID（逗号分隔）' }}</label>
          <el-input
            v-model="executeFlowIdsInput"
            :placeholder="t('workflow.flowIdsPlaceholder') || '如: 1,2,3,4,5'"
          />
        </div>
      </div>
      <template #footer>
        <el-button @click="executeDialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" @click="onExecuteWorkflow">
          <el-icon><VideoPlay /></el-icon>&nbsp;{{ t('workflow.startExecute') || '开始执行' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 导入对话框 -->
    <el-dialog v-model="importDialogVisible" :title="t('workflow.importWorkflow') || '导入工作流'" width="400px" destroy-on-close>
      <div class="import-form">
        <el-upload
          :auto-upload="false"
          :limit="1"
          accept=".json"
          drag
          @change="(f: any) => importFile = f.raw"
        >
          <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
          <div class="el-upload__text">
            {{ t('workflow.dropJsonFile') || '拖拽 JSON 文件或点击上传' }}
          </div>
        </el-upload>
      </div>
      <template #footer>
        <el-button @click="importDialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="importing" :disabled="!importFile" @click="onImportWorkflow">
          {{ t('common.import') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.workflow-view {
  background: var(--on-bg);
}

.workflow-content {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
}

.workflow-section {
  margin-bottom: 24px;
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--on-text);
  margin-bottom: 12px;
  padding-left: 8px;
  border-left: 3px solid var(--on-accent);
}

.workflow-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}

.workflow-card {
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-lg);
  padding: 16px;
  transition: all 0.2s;
}

.workflow-card:hover {
  border-color: var(--on-accent);
  box-shadow: var(--on-shadow-md);
}

.workflow-card.is-template {
  border-style: dashed;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.card-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--on-text);
}

.card-desc {
  font-size: 12px;
  margin-bottom: 8px;
}

.card-meta {
  display: flex;
  gap: 12px;
  font-size: 11px;
  margin-bottom: 12px;
}

.card-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 40px;
  color: var(--on-text-dim);
}

.empty-icon {
  font-size: 48px;
  margin-bottom: 12px;
  opacity: 0.5;
}

/* 设计器样式 */
.designer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  background: var(--on-bg-elevated);
  border-bottom: 1px solid var(--on-border-light);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.workflow-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--on-text);
}

.workflow-desc {
  font-size: 12px;
}

.designer-body {
  flex: 1;
  display: flex;
  overflow: hidden;
}

/* 左侧工具栏 */
.designer-toolbar {
  width: 200px;
  background: var(--on-bg-elevated);
  border-right: 1px solid var(--on-border-light);
  padding: 16px;
  overflow-y: auto;
  flex-shrink: 0;
}

.toolbar-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--on-text);
  margin-bottom: 12px;
}

.action-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.action-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--on-bg);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-md);
  cursor: grab;
  transition: all 0.15s;
  font-size: 13px;
}

.action-item:hover {
  border-color: var(--on-accent);
  background: var(--on-accent-glow);
  transform: translateX(2px);
}

.action-item:active {
  cursor: grabbing;
}

.action-icon {
  display: flex;
  align-items: center;
  font-size: 16px;
}

.action-label {
  color: var(--on-text);
}

.toolbar-hint {
  font-size: 11px;
  margin-top: 12px;
  text-align: center;
}

/* 画布 */
.designer-canvas {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
  background: var(--on-bg);
}

.canvas-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 300px;
  border: 2px dashed var(--on-border);
  border-radius: var(--on-radius-lg);
  color: var(--on-text-dim);
}

.step-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.step-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-md);
  transition: all 0.15s;
}

.step-card:hover {
  border-color: var(--on-accent);
}

.step-card.step-disabled {
  opacity: 0.5;
}

.step-index {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--on-accent);
  color: white;
  border-radius: 50%;
  font-size: 12px;
  font-weight: 600;
  flex-shrink: 0;
}

.step-info {
  flex: 1;
  min-width: 0;
}

.step-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 500;
}

.step-icon {
  display: flex;
  font-size: 16px;
}

.step-params {
  font-size: 11px;
  margin-top: 2px;
}

.step-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 执行面板 */
.designer-execute-panel {
  width: 280px;
  background: var(--on-bg-elevated);
  border-left: 1px solid var(--on-border-light);
  padding: 16px;
  overflow-y: auto;
  flex-shrink: 0;
}

.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--on-text);
  margin-bottom: 16px;
}

.execution-status {
  margin-bottom: 16px;
}

.execution-progress {
  margin-bottom: 16px;
}

.progress-text {
  font-size: 11px;
  margin-top: 4px;
  display: block;
  text-align: center;
}

.execution-stats {
  background: var(--on-bg);
  border-radius: var(--on-radius-md);
  padding: 12px;
  margin-bottom: 16px;
}

.stat-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  padding: 3px 0;
}

.stat-label {
  color: var(--on-text-dim);
}

.stat-value {
  font-weight: 600;
  font-family: var(--on-font-mono);
}

.execution-controls {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.execution-logs {
  flex: 1;
}

.logs-title {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--on-text);
}

.logs-list {
  max-height: 300px;
  overflow-y: auto;
  font-size: 11px;
}

.log-entry {
  padding: 6px 8px;
  border-radius: 4px;
  margin-bottom: 4px;
  background: var(--on-bg);
}

.log-entry.log-success {
  border-left: 2px solid var(--on-ok);
}

.log-entry.log-failed {
  border-left: 2px solid var(--on-error);
}

.log-entry.log-skipped {
  border-left: 2px solid var(--on-warn);
}

.log-time {
  color: var(--on-text-dim);
  margin-right: 8px;
  font-family: var(--on-font-mono);
}

.log-step {
  font-weight: 500;
}

.log-status {
  margin-left: 8px;
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 10px;
}

.log-message {
  display: block;
  margin-top: 2px;
  word-break: break-all;
}

/* 表单样式 */
.create-form,
.step-edit-form,
.execute-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-item label {
  font-size: 12px;
  font-weight: 500;
  color: var(--on-text-muted);
}

.selected-flows-info {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--on-bg);
  border-radius: var(--on-radius-md);
  font-size: 12px;
}

.text-success { color: var(--on-ok); }
.text-danger { color: var(--on-error); }
</style>
