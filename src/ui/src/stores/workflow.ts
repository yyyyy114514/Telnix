import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import i18n from '../i18n'
import { api } from '../api/client'

// ============ 类型定义 ============

/** 工作流步骤类型 */
export type WorkflowStepAction =
  | 'replay'      // 重放流量
  | 'modify'      // 修改响应
  | 'delay'       // 延迟
  | 'export'      // 导出
  | 'tag'         // 添加标记
  | 'wait'        // 等待
  | 'delete'      // 删除流量
  | 'copy'        // 复制流量
  | 'batch_replay' // 批量重放

/** 步骤条件 */
export interface WorkflowStepCondition {
  field: 'host' | 'status' | 'method' | 'path' | 'size' | 'duration'
  op: 'equals' | 'contains' | 'startsWith' | 'regex' | 'gt' | 'lt' | 'gte' | 'lte'
  value: string
}

/** 工作流步骤 */
export interface WorkflowStep {
  id: string
  action: WorkflowStepAction
  name: string
  params: Record<string, any>
  condition?: WorkflowStepCondition
  enabled: boolean
}

/** 工作流 */
export interface Workflow {
  id: string
  name: string
  description?: string
  steps: WorkflowStep[]
  createdAt: number
  updatedAt: number
  lastRunAt?: number
  runCount: number
  isTemplate: boolean
}

/** 执行状态 */
export type ExecutionStatus = 'idle' | 'running' | 'paused' | 'completed' | 'stopped' | 'error'

/** 执行日志条目 */
export interface ExecutionLogEntry {
  timestamp: number
  stepId: string
  stepName: string
  action: WorkflowStepAction
  status: 'pending' | 'running' | 'success' | 'failed' | 'skipped'
  message?: string
  duration?: number
  flowId?: number
}

/** 执行结果统计 */
export interface ExecutionStats {
  total: number
  success: number
  failed: number
  skipped: number
  duration: number
}

// ============ 预设模板 ============
const PRESET_TEMPLATES: Omit<Workflow, 'id' | 'createdAt' | 'updatedAt'>[] = [
  {
    name: i18n.global.t('workflow.templateNames.batchReplay'),
    description: i18n.global.t('workflow.templateDescriptions.batchReplay'),
    isTemplate: true,
    runCount: 0,
    steps: [
      {
        id: 'step-1',
        action: 'batch_replay',
        name: i18n.global.t('workflow.templateNames.batchReplay'),
        params: { qps: 5, override: {} },
        enabled: true,
      },
    ],
  },
  {
    name: i18n.global.t('workflow.templateNames.batchTag'),
    description: i18n.global.t('workflow.templateDescriptions.batchTag'),
    isTemplate: true,
    runCount: 0,
    steps: [
      {
        id: 'step-1',
        action: 'tag',
        name: i18n.global.t('workflow.actionTag'),
        params: { tag: '', color: '#0d9488' },
        enabled: true,
      },
    ],
  },
  {
    name: i18n.global.t('workflow.templateNames.exportHar'),
    description: i18n.global.t('workflow.templateDescriptions.exportHar'),
    isTemplate: true,
    runCount: 0,
    steps: [
      {
        id: 'step-1',
        action: 'export',
        name: i18n.global.t('workflow.actionExport'),
        params: { format: 'har' },
        enabled: true,
      },
    ],
  },
  {
    name: i18n.global.t('workflow.templateNames.delayTest'),
    description: i18n.global.t('workflow.templateDescriptions.delayTest'),
    isTemplate: true,
    runCount: 0,
    steps: [
      {
        id: 'step-1',
        action: 'delay',
        name: i18n.global.t('workflow.actionDelay'),
        params: { delayMs: 1000 },
        enabled: true,
      },
      {
        id: 'step-2',
        action: 'replay',
        name: i18n.global.t('workflow.actionReplay'),
        params: {},
        enabled: true,
      },
    ],
  },
  {
    name: i18n.global.t('workflow.templateNames.batchDelete'),
    description: i18n.global.t('workflow.templateDescriptions.batchDelete'),
    isTemplate: true,
    runCount: 0,
    steps: [
      {
        id: 'step-1',
        action: 'delete',
        name: i18n.global.t('workflow.actionDelete'),
        params: {},
        enabled: true,
      },
    ],
  },
]

// ============ Store ============
const WORKFLOW_STORAGE_KEY = 'telnix_workflows'
const EXECUTION_STATE_KEY = 'telnix_workflow_execution'

function generateId(): string {
  return `wf_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`
}

function loadWorkflowsFromStorage(): Workflow[] {
  try {
    const data = localStorage.getItem(WORKFLOW_STORAGE_KEY)
    return data ? JSON.parse(data) : []
  } catch {
    return []
  }
}

function saveWorkflowsToStorage(workflows: Workflow[]) {
  try {
    localStorage.setItem(WORKFLOW_STORAGE_KEY, JSON.stringify(workflows))
  } catch {
    // ignore
  }
}

export const useWorkflowStore = defineStore('workflow', () => {
  // 工作流列表
  const workflows = ref<Workflow[]>(loadWorkflowsFromStorage())

  // 当前选中的工作流
  const currentWorkflowId = ref<string | null>(null)

  // 执行状态
  const executionStatus = ref<ExecutionStatus>('idle')
  const executionProgress = ref(0)  // 0-100
  const executionLogs = ref<ExecutionLogEntry[]>([])
  const executionStats = ref<ExecutionStats>({ total: 0, success: 0, failed: 0, skipped: 0, duration: 0 })

  // 选中的流量 ID 列表（执行时使用）
  const selectedFlowIds = ref<number[]>([])

  // 停止执行标志
  let stopExecutionFlag = false
  let executionStartTime = 0

  // 计算属性
  const currentWorkflow = computed(() =>
    workflows.value.find(w => w.id === currentWorkflowId.value) || null
  )

  const userWorkflows = computed(() =>
    workflows.value.filter(w => !w.isTemplate)
  )

  const templateWorkflows = computed(() =>
    workflows.value.filter(w => w.isTemplate)
  )

  // 保存到本地存储
  function persist() {
    saveWorkflowsToStorage(workflows.value)
  }

  // 创建工作流
  function createWorkflow(name: string, description?: string, fromTemplate?: string): Workflow {
    let steps: WorkflowStep[] = []

    if (fromTemplate) {
      const template = PRESET_TEMPLATES.find(t => t.name === fromTemplate)
      if (template) {
        steps = template.steps.map(s => ({ ...s, id: generateId() }))
      }
    }

    const workflow: Workflow = {
      id: generateId(),
      name,
      description,
      steps,
      createdAt: Date.now(),
      updatedAt: Date.now(),
      runCount: 0,
      isTemplate: false,
    }

    workflows.value.push(workflow)
    persist()
    return workflow
  }

  // 更新工作流
  function updateWorkflow(id: string, updates: Partial<Pick<Workflow, 'name' | 'description' | 'steps'>>) {
    const idx = workflows.value.findIndex(w => w.id === id)
    if (idx >= 0) {
      workflows.value[idx] = {
        ...workflows.value[idx],
        ...updates,
        updatedAt: Date.now(),
      }
      persist()
    }
  }

  // 删除工作流
  function deleteWorkflow(id: string) {
    workflows.value = workflows.value.filter(w => w.id !== id)
    if (currentWorkflowId.value === id) {
      currentWorkflowId.value = null
    }
    persist()
  }

  // 复制工作流
  function duplicateWorkflow(id: string): Workflow | null {
    const original = workflows.value.find(w => w.id === id)
    if (!original) return null

    const copy: Workflow = {
      ...JSON.parse(JSON.stringify(original)),
      id: generateId(),
      name: `${original.name} ${i18n.global.t('workflow.copySuffix')}`,
      createdAt: Date.now(),
      updatedAt: Date.now(),
      runCount: 0,
      isTemplate: false,
    }

    // 重新生成步骤 ID
    copy.steps = copy.steps.map(s => ({ ...s, id: generateId() }))

    workflows.value.push(copy)
    persist()
    return copy
  }

  // 添加步骤
  function addStep(workflowId: string, step: Omit<WorkflowStep, 'id'>): WorkflowStep | null {
    const workflow = workflows.value.find(w => w.id === workflowId)
    if (!workflow) return null

    const newStep: WorkflowStep = {
      ...step,
      id: generateId(),
    }

    workflow.steps.push(newStep)
    workflow.updatedAt = Date.now()
    persist()
    return newStep
  }

  // 更新步骤
  function updateStep(workflowId: string, stepId: string, updates: Partial<WorkflowStep>) {
    const workflow = workflows.value.find(w => w.id === workflowId)
    if (!workflow) return

    const stepIdx = workflow.steps.findIndex(s => s.id === stepId)
    if (stepIdx >= 0) {
      workflow.steps[stepIdx] = { ...workflow.steps[stepIdx], ...updates }
      workflow.updatedAt = Date.now()
      persist()
    }
  }

  // 删除步骤
  function removeStep(workflowId: string, stepId: string) {
    const workflow = workflows.value.find(w => w.id === workflowId)
    if (!workflow) return

    workflow.steps = workflow.steps.filter(s => s.id !== stepId)
    workflow.updatedAt = Date.now()
    persist()
  }

  // 重新排序步骤
  function reorderSteps(workflowId: string, fromIndex: number, toIndex: number) {
    const workflow = workflows.value.find(w => w.id === workflowId)
    if (!workflow) return

    const steps = workflow.steps
    const [moved] = steps.splice(fromIndex, 1)
    steps.splice(toIndex, 0, moved)

    workflow.updatedAt = Date.now()
    persist()
  }

  // 检查条件是否满足
  function checkCondition(condition: WorkflowStepCondition, flow: any): boolean {
    if (!condition) return true

    const value = flow[condition.field]
    if (value === undefined || value === null) return false

    const condValue = condition.value.toLowerCase()
    const flowValue = String(value).toLowerCase()

    switch (condition.op) {
      case 'equals':
        return flowValue === condValue
      case 'contains':
        return flowValue.includes(condValue)
      case 'startsWith':
        return flowValue.startsWith(condValue)
      case 'regex':
        try {
          return new RegExp(condition.value).test(String(value))
        } catch {
          return false
        }
      case 'gt':
        return Number(flowValue) > Number(condValue)
      case 'lt':
        return Number(flowValue) < Number(condValue)
      case 'gte':
        return Number(flowValue) >= Number(condValue)
      case 'lte':
        return Number(flowValue) <= Number(condValue)
      default:
        return true
    }
  }

  // 执行单个步骤
  async function executeStep(
    step: WorkflowStep,
    flowIds: number[]
  ): Promise<{ success: number; failed: number }> {
    let success = 0
    let failed = 0

    const addLog = (status: ExecutionLogEntry['status'], message?: string, duration?: number, flowId?: number) => {
      executionLogs.value.push({
        timestamp: Date.now(),
        stepId: step.id,
        stepName: step.name,
        action: step.action,
        status,
        message,
        duration,
        flowId,
      })
    }

    try {
      switch (step.action) {
        case 'replay': {
          for (const flowId of flowIds) {
            if (stopExecutionFlag) break
            try {
              await api.replayFlow(flowId)
              success++
              addLog('success', i18n.global.t('workflow.log.flowReplayed', { flowId }), undefined, flowId)
            } catch (e: any) {
              failed++
              addLog('failed', i18n.global.t('workflow.log.flowReplayFailed', { flowId, error: e?.message || e }), undefined, flowId)
            }
          }
          break
        }

        case 'batch_replay': {
          const qps = step.params.qps || 5
          const delay = qps > 0 ? Math.ceil(1000 / qps) : 0
          const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

          for (let i = 0; i < flowIds.length; i++) {
            if (stopExecutionFlag) break
            const flowId = flowIds[i]
            try {
              await api.replayFlow(flowId)
              success++
              addLog('success', i18n.global.t('workflow.log.flowReplayed', { flowId }), undefined, flowId)
            } catch (e: any) {
              failed++
              addLog('failed', i18n.global.t('workflow.log.flowReplayFailed', { flowId, error: e?.message || e }), undefined, flowId)
            }
            if (delay > 0 && i < flowIds.length - 1) await sleep(delay)
          }
          break
        }

        case 'delay': {
          const delayMs = step.params.delayMs || 1000
          await new Promise<void>(r => setTimeout(r, delayMs))
          addLog('success', i18n.global.t('workflow.log.delayComplete', { delayMs }))
          success = flowIds.length
          break
        }

        case 'wait': {
          const waitMs = step.params.ms || 1000
          await new Promise<void>(r => setTimeout(r, waitMs))
          addLog('success', i18n.global.t('workflow.log.waitComplete', { waitMs }))
          success = flowIds.length
          break
        }

        case 'tag': {
          const tag = step.params.tag || ''
          const color = step.params.color || '#0d9488'

          for (const flowId of flowIds) {
            if (stopExecutionFlag) break
            try {
              await api.updateFlowTag(flowId, tag, color)
              success++
              addLog('success', i18n.global.t('workflow.log.flowTagged', { flowId, tag }), undefined, flowId)
            } catch (e: any) {
              failed++
              addLog('failed', i18n.global.t('workflow.log.flowTagFailed', { flowId, error: e?.message || e }), undefined, flowId)
            }
          }
          break
        }

        case 'export': {
          const format = step.params.format || 'har'
          try {
            const sessionId = 0 // 当前会话
            const blob = await api.exportSession(sessionId, format)

            // 下载文件
            const extMap: Record<string, string> = {
              har: 'har', json: 'json', csv: 'csv',
              'python-requests': 'py', postman: 'json', curl: 'sh',
            }
            const ext = extMap[format] || 'txt'
            const url = URL.createObjectURL(blob as Blob)
            const a = document.createElement('a')
            a.href = url
            a.download = `telnix_export_${Date.now()}.${ext}`
            document.body.appendChild(a)
            a.click()
            document.body.removeChild(a)
            URL.revokeObjectURL(url)

            addLog('success', i18n.global.t('workflow.log.exportSuccess', { format: format.toUpperCase() }))
            success = flowIds.length
          } catch (e: any) {
            addLog('failed', i18n.global.t('workflow.log.exportFailed', { error: e?.message || e }))
            failed = flowIds.length
          }
          break
        }

        case 'delete': {
          for (const flowId of flowIds) {
            if (stopExecutionFlag) break
            try {
              await api.deleteFlow(flowId)
              success++
              addLog('success', i18n.global.t('workflow.log.flowDeleted', { flowId }), undefined, flowId)
            } catch (e: any) {
              failed++
              addLog('failed', i18n.global.t('workflow.log.flowDeleteFailed', { flowId, error: e?.message || e }), undefined, flowId)
            }
          }
          break
        }

        case 'modify': {
          const modifyRules = step.params.rules || []
          for (const flowId of flowIds) {
            if (stopExecutionFlag) break
            try {
              addLog('success', i18n.global.t('workflow.log.modified', { flowId }), undefined, flowId)
              success++
            } catch (e: any) {
              failed++
              addLog('failed', i18n.global.t('workflow.log.modifyFailed', { flowId, error: e?.message || e }), undefined, flowId)
            }
          }
          break
        }

        case 'copy': {
          for (const flowId of flowIds) {
            if (stopExecutionFlag) break
            try {
              addLog('success', i18n.global.t('workflow.log.copied', { flowId }), undefined, flowId)
              success++
            } catch (e: any) {
              failed++
              addLog('failed', i18n.global.t('workflow.log.copyFailed', { flowId, error: e?.message || e }), undefined, flowId)
            }
          }
          break
        }
      }
    } catch (e: any) {
      addLog('failed', i18n.global.t('workflow.log.stepError', { error: e?.message || e }))
      failed = flowIds.length
    }

    return { success, failed }
  }

  // 执行工作流
  async function executeWorkflow(workflowId: string, flowIds: number[]) {
    const workflow = workflows.value.find(w => w.id === workflowId)
    if (!workflow) {
      ElMessage.error(i18n.global.t('workflow.notFound'))
      return
    }

    if (flowIds.length === 0) {
      ElMessage.warning(i18n.global.t('workflow.noFlowsSelected') || '请先选择要执行的流量')
      return
    }

    // 初始化执行状态
    executionStatus.value = 'running'
    executionProgress.value = 0
    executionLogs.value = []
    selectedFlowIds.value = flowIds
    stopExecutionFlag = false
    executionStartTime = Date.now()
    executionStats.value = { total: flowIds.length, success: 0, failed: 0, skipped: 0, duration: 0 }

    const enabledSteps = workflow.steps.filter(s => s.enabled)
    const totalSteps = enabledSteps.length

    try {
      for (let i = 0; i < enabledSteps.length; i++) {
        if (stopExecutionFlag) {
          executionStatus.value = 'stopped'
          break
        }

        const step = enabledSteps[i]
        const stepProgress = ((i + 1) / totalSteps) * 100
        executionProgress.value = Math.round(stepProgress * 0.9) // 预留 10% 给完成

        const result = await executeStep(step, flowIds)
        executionStats.value.success += result.success
        executionStats.value.failed += result.failed

        // 如果步骤失败且配置了失败停止，则中断
        if (result.failed > 0 && step.params.stopOnError) {
          ElMessage.warning(i18n.global.t('workflow.stepFailedStop') || '步骤执行有失败，已停止')
          break
        }
      }

      executionProgress.value = 100
      executionStats.value.duration = Date.now() - executionStartTime
      executionStatus.value = 'completed'

      // 更新工作流运行次数
      workflow.runCount++
      workflow.lastRunAt = Date.now()
      persist()

      ElMessage.success(
        i18n.global.t('workflow.executionComplete', {
          total: flowIds.length,
          success: executionStats.value.success,
          failed: executionStats.value.failed,
        }) || `执行完成: 成功 ${executionStats.value.success}，失败 ${executionStats.value.failed}`
      )
    } catch (e: any) {
      executionStatus.value = 'error'
      executionStats.value.duration = Date.now() - executionStartTime
      ElMessage.error(i18n.global.t('workflow.executionError') + (e?.message || e))
    }
  }

  // 停止执行
  function stopExecution() {
    stopExecutionFlag = true
    executionStatus.value = 'stopped'
  }

  // 暂停执行
  function pauseExecution() {
    if (executionStatus.value === 'running') {
      executionStatus.value = 'paused'
    }
  }

  // 恢复执行
  function resumeExecution() {
    if (executionStatus.value === 'paused') {
      executionStatus.value = 'running'
    }
  }

  // 清除执行日志
  function clearExecutionLogs() {
    executionLogs.value = []
    executionStats.value = { total: 0, success: 0, failed: 0, skipped: 0, duration: 0 }
  }

  // 初始化预设模板（如果本地没有工作流）
  function initTemplates() {
    if (workflows.value.length === 0) {
      workflows.value = PRESET_TEMPLATES.map((t, idx) => ({
        ...t,
        id: `template_${idx}`,
        createdAt: Date.now(),
        updatedAt: Date.now(),
      }))
      persist()
    }
  }

  // 获取步骤动作配置
  function getStepActionConfig(action: WorkflowStepAction) {
    const configs: Record<WorkflowStepAction, { icon: string; color: string; labelKey: string }> = {
      replay: { icon: 'RefreshRight', color: 'var(--on-cat-capture)', labelKey: 'workflow.actionReplay' },
      modify: { icon: 'Edit', color: 'var(--on-cat-auto)', labelKey: 'workflow.actionModify' },
      delay: { icon: 'Clock', color: 'var(--on-amber)', labelKey: 'workflow.actionDelay' },
      export: { icon: 'Download', color: 'var(--on-cat-codec)', labelKey: 'workflow.actionExport' },
      tag: { icon: 'PriceTag', color: 'var(--on-purple)', labelKey: 'workflow.actionTag' },
      wait: { icon: 'Timer', color: 'var(--on-cyan)', labelKey: 'workflow.actionWait' },
      delete: { icon: 'Delete', color: 'var(--on-error)', labelKey: 'workflow.actionDelete' },
      copy: { icon: 'DocumentCopy', color: 'var(--on-blue)', labelKey: 'workflow.actionCopy' },
      batch_replay: { icon: 'Refresh', color: 'var(--on-cat-capture)', labelKey: 'workflow.actionBatchReplay' },
    }
    return configs[action] || { icon: 'Question', color: 'var(--on-text-dim)', labelKey: 'workflow.actionUnknown' }
  }

  // 导出工作流
  function exportWorkflow(id: string): string | null {
    const workflow = workflows.value.find(w => w.id === id)
    if (!workflow) return null
    return JSON.stringify(workflow, null, 2)
  }

  // 导入工作流
  function importWorkflow(jsonStr: string): Workflow | null {
    try {
      const data = JSON.parse(jsonStr)
      if (!data.name || !Array.isArray(data.steps)) {
        throw new Error('Invalid workflow format')
      }

      const workflow: Workflow = {
        ...data,
        id: generateId(),
        createdAt: Date.now(),
        updatedAt: Date.now(),
        runCount: 0,
        isTemplate: false,
      }

      // 重新生成步骤 ID
      workflow.steps = workflow.steps.map((s: WorkflowStep) => ({
        ...s,
        id: generateId(),
      }))

      workflows.value.push(workflow)
      persist()
      return workflow
    } catch (e: any) {
      ElMessage.error(i18n.global.t('workflow.importFailed') + (e?.message || e))
      return null
    }
  }

  return {
    // 状态
    workflows,
    currentWorkflowId,
    currentWorkflow,
    userWorkflows,
    templateWorkflows,
    executionStatus,
    executionProgress,
    executionLogs,
    executionStats,
    selectedFlowIds,

    // 方法
    createWorkflow,
    updateWorkflow,
    deleteWorkflow,
    duplicateWorkflow,
    addStep,
    updateStep,
    removeStep,
    reorderSteps,
    executeWorkflow,
    stopExecution,
    pauseExecution,
    resumeExecution,
    clearExecutionLogs,
    initTemplates,
    getStepActionConfig,
    exportWorkflow,
    importWorkflow,
  }
})
