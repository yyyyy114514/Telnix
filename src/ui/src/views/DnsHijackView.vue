<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Delete, Histogram, Key, Plus, Refresh, WarningFilled,
  Top, Bottom, Sort, Folder, FolderOpened, FolderAdd
} from '@element-plus/icons-vue'
import Sortable from 'sortablejs'
import { api, type DnsHijackStatus } from '../api/client'
import DohWarningDialog from '../components/DohWarningDialog.vue'

const { t } = useI18n()

// DoH 对话框 ref
const dohDialogRef = ref<InstanceType<typeof DohWarningDialog> | null>(null)

// ============ 状态定义 ============
const status = shallowRef<DnsHijackStatus | null>(null)
const loading = ref(false)
const toggling = ref(false)
const saving = ref(false)
const restartingAsAdmin = ref(false)

// DoH 检测相关
const checkingDoh = ref(false)

// ============ DNS 分组管理状态 ============
interface DnsGroup {
  id: number
  name: string
  priority: number
  enabled: boolean
  rule_count: number
}

interface DnsRule {
  id: number
  group_id: number
  pattern: string
  mode: string
  action: string
  redirect_to: string | null
}

const groups = ref<DnsGroup[]>([])
const selectedGroupId = ref<number | null>(null)
const rules = ref<DnsRule[]>([])
const groupDirty = ref(false)
const editingGroup = ref<{ id: number | null; name: string; priority: number; enabled: boolean } | null>(null)
const showGroupDialog = ref(false)

const logs = shallowRef<DnsHijackStatus['log']>([])

let pollTimer: number | null = null
let loadingTimer: number | null = null
let pollThrottleTimer: number | null = null
let pendingPoll = false
const LOADING_TIMEOUT_MS = 5000
const POLL_INTERVAL_MS = 3000
const POLL_THROTTLE_MS = 500

// ============ 辅助函数 ============
function clearLoadingTimer() {
  if (loadingTimer !== null) {
    clearTimeout(loadingTimer)
    loadingTimer = null
  }
}

function schedulePoll() {
  if (pollThrottleTimer !== null) {
    pendingPoll = true
    return
  }
  pollThrottleTimer = window.setTimeout(() => {
    pollThrottleTimer = null
    if (pendingPoll) {
      pendingPoll = false
      schedulePoll()
    }
  }, POLL_THROTTLE_MS)
}

// ============ 状态 API ============
async function refresh(opts: { silent?: boolean } = {}) {
  const { silent = false } = opts
  clearLoadingTimer()
  if (!silent) loading.value = true
  loadingTimer = window.setTimeout(() => {
    loading.value = false
  }, LOADING_TIMEOUT_MS)
  try {
    const s = await api.dnsHijackStatus()
    status.value = s
    if (s.log) {
      logs.value = [...s.log]
    }
  } catch (e: any) {
    ElMessage.error(t('dns.getStatusFailed', { msg: e?.message || String(e) }))
  } finally {
    clearLoadingTimer()
    loading.value = false
  }
}

// ============ DNS 劫持开关 ============
async function onToggle(val: boolean | string | number) {
  if (val === true) {
    await startHijack()
  } else {
    await stopHijack()
  }
}

async function startHijack() {
  toggling.value = true
  try {
    // 从分组规则生成劫持规则
    await api.applyDnsGroupRules()
    ElMessage.success(t('dns.started'))
    await refresh({ syncRules: true })
  } catch (e: any) {
    ElMessage.error(t('dns.startFailed', { msg: e?.message || String(e) }))
  } finally {
    toggling.value = false
  }
}

async function stopHijack() {
  toggling.value = true
  try {
    await api.dnsHijackStop()
    ElMessage.info(t('dns.stopped'))
  } catch (e: any) {
    ElMessage.error(t('dns.stopFailed', { msg: e?.message || String(e) }))
  } finally {
    toggling.value = false
  }
}

// ============ 分组管理 ============
async function loadGroups() {
  try {
    groups.value = await api.getDnsGroups()
    if (groups.value.length > 0 && selectedGroupId.value === null) {
      selectedGroupId.value = groups.value[0].id
    }
    if (selectedGroupId.value !== null) {
      await loadRules(selectedGroupId.value)
    }
  } catch (e: any) {
    ElMessage.error('加载分组失败: ' + (e?.message || String(e)))
  }
}

async function loadRules(groupId: number) {
  try {
    rules.value = await api.getDnsGroupRules(groupId)
  } catch (e: any) {
    ElMessage.error('加载规则失败: ' + (e?.message || String(e)))
  }
}

function selectGroup(groupId: number) {
  selectedGroupId.value = groupId
  loadRules(groupId)
}

function openCreateGroupDialog() {
  editingGroup.value = {
    id: null,
    name: '',
    priority: groups.value.length,
    enabled: true,
  }
  showGroupDialog.value = true
}

function openEditGroupDialog(group: DnsGroup) {
  editingGroup.value = {
    id: group.id,
    name: group.name,
    priority: group.priority,
    enabled: group.enabled,
  }
  showGroupDialog.value = true
}

async function saveGroup() {
  if (!editingGroup.value) return
  if (!editingGroup.value.name.trim()) {
    ElMessage.warning('分组名称不能为空')
    return
  }
  try {
    if (editingGroup.value.id === null) {
      // 创建新分组
      const result = await api.createDnsGroup({
        name: editingGroup.value.name.trim(),
        priority: editingGroup.value.priority,
        enabled: editingGroup.value.enabled,
      })
      ElMessage.success('分组创建成功')
      showGroupDialog.value = false
      await loadGroups()
      if (result.id) {
        selectGroup(result.id)
      }
    } else {
      // 更新分组
      await api.updateDnsGroup(editingGroup.value.id, {
        name: editingGroup.value.name.trim(),
        priority: editingGroup.value.priority,
        enabled: editingGroup.value.enabled,
      })
      ElMessage.success('分组更新成功')
      showGroupDialog.value = false
      await loadGroups()
    }
  } catch (e: any) {
    ElMessage.error(e?.message || '保存失败')
  }
}

async function deleteGroup(groupId: number) {
  try {
    await ElMessageBox.confirm('确定要删除该分组及其所有规则吗？', '删除分组', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.deleteDnsGroup(groupId)
    ElMessage.success('分组已删除')
    if (selectedGroupId.value === groupId) {
      selectedGroupId.value = null
      rules.value = []
    }
    await loadGroups()
  } catch (e: any) {
    ElMessage.error(e?.message || '删除失败')
  }
}

async function toggleGroupEnabled(group: DnsGroup) {
  try {
    await api.updateDnsGroup(group.id, { enabled: !group.enabled })
    await loadGroups()
  } catch (e: any) {
    ElMessage.error(e?.message || '更新失败')
    // 回滚 UI
    group.enabled = !group.enabled
  }
}

async function applyGroupRules() {
  saving.value = true
  try {
    const result = await api.applyDnsGroupRules()
    ElMessage.success(`已应用 ${result.rule_count} 条规则`)
  } catch (e: any) {
    ElMessage.error(e?.message || '应用规则失败')
  } finally {
    saving.value = false
  }
}

// 初始化拖拽排序
let sortableInstance: any = null
function initSortable(el: HTMLElement) {
  if (sortableInstance) {
    sortableInstance.destroy()
  }
  sortableInstance = Sortable.create(el, {
    animation: 150,
    handle: '.drag-handle',
    onEnd: async (evt: any) => {
      const { oldIndex, newIndex } = evt
      if (oldIndex === newIndex) return
      // 更新排序
      const movedItem = groups.value.splice(oldIndex, 1)[0]
      groups.value.splice(newIndex, 0, movedItem)
      // 同步更新优先级
      const groupIds = groups.value.map(g => g.id)
      try {
        await api.reorderDnsGroups(groupIds)
      } catch (e: any) {
        ElMessage.error(e?.message || '排序保存失败')
        await loadGroups() // 回滚
      }
    },
  })
}

// ============ 规则管理 ============
function openCreateRuleDialog() {
  if (selectedGroupId.value === null) {
    ElMessage.warning('请先选择一个分组')
    return
  }
  editingRule.value = {
    id: null,
    pattern: '',
    mode: 'wildcard',
    action: 'block',
    redirect_to: '',
  }
  showRuleDialog.value = true
}

const editingRule = ref<{
  id: number | null
  pattern: string
  mode: string
  action: string
  redirect_to: string
} | null>(null)
const showRuleDialog = ref(false)

async function saveRule() {
  if (!editingRule.value) return
  if (!editingRule.value.pattern.trim()) {
    ElMessage.warning('域名模式不能为空')
    return
  }
  if (editingRule.value.action === 'redirect' && !editingRule.value.redirect_to.trim()) {
    ElMessage.warning('重定向动作需要指定目标 IP')
    return
  }
  try {
    if (editingRule.value.id === null) {
      // 创建规则
      await api.createDnsRule(selectedGroupId.value!, {
        pattern: editingRule.value.pattern.trim(),
        mode: editingRule.value.mode,
        action: editingRule.value.action,
        redirect_to: editingRule.value.action === 'redirect' ? editingRule.value.redirect_to.trim() : undefined,
      })
      ElMessage.success('规则创建成功')
    } else {
      // 更新规则
      await api.updateDnsRule(editingRule.value.id, {
        pattern: editingRule.value.pattern.trim(),
        mode: editingRule.value.mode,
        action: editingRule.value.action,
        redirect_to: editingRule.value.action === 'redirect' ? editingRule.value.redirect_to.trim() : undefined,
      })
      ElMessage.success('规则更新成功')
    }
    showRuleDialog.value = false
    await loadRules(selectedGroupId.value!)
    await loadGroups() // 更新 rule_count
  } catch (e: any) {
    ElMessage.error(e?.message || '保存失败')
  }
}

function openEditRuleDialog(rule: DnsRule) {
  editingRule.value = {
    id: rule.id,
    pattern: rule.pattern,
    mode: rule.mode,
    action: rule.action,
    redirect_to: rule.redirect_to || '',
  }
  showRuleDialog.value = true
}

async function deleteRule(ruleId: number) {
  try {
    await ElMessageBox.confirm('确定要删除该规则吗？', '删除规则', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.deleteDnsRule(ruleId)
    ElMessage.success('规则已删除')
    await loadRules(selectedGroupId.value!)
    await loadGroups() // 更新 rule_count
  } catch (e: any) {
    ElMessage.error(e?.message || '删除失败')
  }
}

// ============ 其他操作 ============
async function restartAsAdmin() {
  restartingAsAdmin.value = true
  try {
    await api.restartAsAdmin()
    ElMessage.success(t('dns.restartingAsAdmin'))
  } catch (e: any) {
    ElMessage.error(t('dns.restartFailed', { msg: e?.message || e }))
    restartingAsAdmin.value = false
  }
}

async function clearLog() {
  try {
    await ElMessageBox.confirm(t('dns.clearLogConfirmMsg'), t('dns.clearLog'), {
      confirmButtonText: t('dns.clear'),
      cancelButtonText: t('dns.cancel'),
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.dnsHijackClearLog()
    ElMessage.success(t('dns.cleared'))
    await refresh({ silent: true })
  } catch (e: any) {
    ElMessage.error(t('dns.clearFailed', { msg: e?.message || String(e) }))
  }
}

function formatTime(ts: string) {
  if (!ts) return ''
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
}

// ============ DoH 检测 ============
async function checkDohFlows() {
  checkingDoh.value = true
  try {
    // 获取最近100条日志流量的 host 用于检测
    const flows = logs.value.map(l => ({
      host: l.domain,
      sni: l.domain,
    }))
    if (flows.length === 0) {
      ElMessage.info('暂无流量数据，请先抓包后再检测')
      return
    }
    await dohDialogRef.value?.checkForDoh(flows)
  } finally {
    checkingDoh.value = false
  }
}

// ============ 计算属性 ============
const isAdmin = computed(() => status.value?.is_admin ?? false)
const isWindows = computed(() => status.value?.is_windows ?? true)
const running = computed(() => status.value?.running ?? false)
const stats = computed(() => status.value?.stats || { total_packets: 0, hijacked_packets: 0, skipped_no_match: 0, errors: 0 })
const backend = computed(() => status.value?.backend || (isWindows.value ? 'windivert' : 'none'))
const supported = computed(() => status.value?.supported ?? isWindows.value)

const backendDisplayName = computed(() => {
  const b = backend.value
  if (b === 'windivert') return 'WinDivert'
  if (b === 'iptables+local_dns') return t('dns.backendIptablesLocal')
  if (b === 'pf+local_dns') return t('dns.backendPfLocal')
  return t('dns.hijack')
})

const adminTerm = computed(() => isWindows.value ? t('dns.adminTerm') : 'root')
const canStart = computed(() => supported.value && isAdmin.value)
const selectedGroup = computed(() => groups.value.find(g => g.id === selectedGroupId.value))

// ============ 生命周期 ============
onMounted(() => {
  refresh()
  loadGroups()
  pollTimer = window.setInterval(() => {
    if (running.value) {
      schedulePoll()
    }
  }, POLL_INTERVAL_MS)
})

onUnmounted(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
  if (pollThrottleTimer !== null) clearTimeout(pollThrottleTimer)
  if (sortableInstance) sortableInstance.destroy()
})
</script>

<template>
  <div class="dns-view full flex flex-col">
    <!-- 工具栏 -->
    <div class="dns-toolbar">
      <span class="dns-title">
        <el-icon><Histogram /></el-icon>&nbsp;{{ t('dns.title') }}
      </span>
      <div class="flex-1"></div>
      <el-button v-if="supported && !isAdmin" size="small" type="warning" :loading="restartingAsAdmin" @click="restartAsAdmin">
        <el-icon><Key /></el-icon>&nbsp;{{ isWindows ? t('dns.adminRestart') : t('dns.elevateRestart') }}
      </el-button>
      <el-button size="small" @click="refresh({ silent: false })" :loading="loading">
        <el-icon><Refresh /></el-icon>&nbsp;{{ t('dns.refresh') }}
      </el-button>
      <el-button size="small" type="danger" @click="clearLog">
        <el-icon><Delete /></el-icon>&nbsp;{{ t('dns.clearLog') }}
      </el-button>
      <el-button size="small" type="primary" @click="applyGroupRules" :loading="saving">
        {{ t('dns.applyRules') }}
      </el-button>
      <el-button size="small" type="info" @click="checkDohFlows" :loading="checkingDoh">
        <el-icon><WarningFilled /></el-icon>&nbsp;{{ t('doh.detectBtn') || 'DoH检测' }}
      </el-button>
    </div>

    <div class="dns-body flex-1 overflow-auto">
      <!-- 状态卡 -->
      <div class="dns-card">
        <div class="dns-card-header">
          <div class="dns-card-title">
            <div class="title-text">{{ t('dns.toggleSwitch') }}</div>
            <div class="title-sub">{{ backendDisplayName }} · {{ t('dns.modifyAR') }}</div>
          </div>
          <el-switch
            :model-value="running"
            :loading="toggling"
            :disabled="!canStart"
            @change="onToggle"
            class="dns-switch"
          />
        </div>
        <div class="dns-card-body">
          <div class="dns-status-tags">
            <el-tag size="small" :type="isAdmin ? 'success' : 'danger'">
              {{ isAdmin ? adminTerm : t('dns.nonAdmin', { term: adminTerm }) }}
            </el-tag>
            <el-tag v-if="backend !== 'none'" size="small" type="info">
              {{ backendDisplayName }}
            </el-tag>
            <el-tag v-if="running" size="small" type="success">{{ t('dns.hijacking') }}</el-tag>
          </div>
          <div v-if="!supported" class="dns-hint warn" style="margin-top: 10px">
            <el-icon><WarningFilled /></el-icon>&nbsp;{{ t('dns.platformNotSupported') }}
          </div>
          <div v-else-if="!isAdmin" class="dns-hint warn" style="margin-top: 10px">
            <el-icon><WarningFilled /></el-icon>&nbsp;{{ t('dns.needAdminRestart', { term: adminTerm }) }}
          </div>
          <div v-else-if="status?.last_error" class="dns-hint err" style="margin-top: 10px">
            <el-icon><WarningFilled /></el-icon>&nbsp;{{ status.last_error }}
          </div>
          <div v-else-if="running" class="dns-hint ok" style="margin-top: 10px">
            <el-icon><Histogram /></el-icon>&nbsp;{{ t('dns.runningHint') }}
          </div>
          <div v-else class="dns-hint" style="margin-top: 10px">
            <el-icon><Histogram /></el-icon>&nbsp;{{ t('dns.readyHint') }}
          </div>

          <!-- 统计 -->
          <div class="dns-stats" v-if="running || stats.total_packets > 0">
            <div class="stat-item">
              <div class="stat-label">{{ t('dns.capturedPackets') }}</div>
              <div class="stat-val mono">{{ stats.total_packets }}</div>
            </div>
            <div class="stat-item hl">
              <div class="stat-label">{{ t('dns.hijacked') }}</div>
              <div class="stat-val mono">{{ stats.hijacked_packets }}</div>
            </div>
            <div class="stat-item">
              <div class="stat-label">{{ t('dns.notMatched') }}</div>
              <div class="stat-val mono">{{ stats.skipped_no_match }}</div>
            </div>
            <div class="stat-item">
              <div class="stat-label">{{ t('dns.errors') }}</div>
              <div class="stat-val mono">{{ stats.errors }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 分组管理区 -->
      <div class="dns-card dns-groups-card">
        <div class="dns-card-header">
          <div class="dns-card-title">
            <div class="title-text">{{ t('dns.groupManagement') }}</div>
            <div class="title-sub">{{ t('dns.groupSubtitle') }}</div>
          </div>
          <div class="dns-card-actions">
            <el-button size="small" type="primary" plain @click="openCreateGroupDialog">
              <el-icon><Plus /></el-icon>&nbsp;{{ t('dns.addGroup') }}
            </el-button>
          </div>
        </div>
        <div class="dns-card-body dns-groups-body">
          <!-- 分组列表 -->
          <div class="groups-list" ref="groupsListRef">
            <div
              v-for="group in groups"
              :key="group.id"
              class="group-item"
              :class="{ active: selectedGroupId === group.id, disabled: !group.enabled }"
              @click="selectGroup(group.id)"
            >
              <div class="drag-handle">
                <el-icon><Sort /></el-icon>
              </div>
              <div class="group-icon">
                <el-icon v-if="selectedGroupId === group.id"><FolderOpened /></el-icon>
                <el-icon v-else><Folder /></el-icon>
              </div>
              <div class="group-info">
                <div class="group-name">{{ group.name }}</div>
                <div class="group-meta">{{ group.rule_count }} {{ t('dns.rules') }}</div>
              </div>
              <div class="group-actions" @click.stop>
                <el-switch
                  :model-value="group.enabled"
                  size="small"
                  @change="toggleGroupEnabled(group)"
                />
                <el-button size="small" circle @click="openEditGroupDialog(group)" text>
                  <el-icon><Refresh /></el-icon>
                </el-button>
                <el-button size="small" circle @click="deleteGroup(group.id)" text type="danger">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
            </div>
            <div v-if="!groups.length" class="empty-text text-dim">
              {{ t('dns.noGroups') }}
            </div>
          </div>

          <!-- 规则列表 -->
          <div class="rules-panel">
            <div class="rules-panel-header">
              <span class="rules-panel-title">
                {{ selectedGroup ? selectedGroup.name : t('dns.selectGroup') }}
                <span v-if="selectedGroup" class="rules-count">({{ rules.length }})</span>
              </span>
              <el-button
                v-if="selectedGroup"
                size="small"
                type="primary"
                plain
                @click="openCreateRuleDialog"
              >
                <el-icon><Plus /></el-icon>&nbsp;{{ t('dns.addRule') }}
              </el-button>
            </div>
            <div class="rules-table">
              <div class="rule-row rule-header">
                <div class="rule-cell c-pattern">{{ t('dns.domainPattern') }}</div>
                <div class="rule-cell c-mode">{{ t('dns.matchMode') }}</div>
                <div class="rule-cell c-action">{{ t('dns.action') }}</div>
                <div class="rule-cell c-redirect">{{ t('dns.redirectTo') }}</div>
                <div class="rule-cell c-op">{{ t('dns.operation') }}</div>
              </div>
              <div v-if="!rules.length" class="empty-text text-dim rule-empty">
                {{ selectedGroup ? t('dns.noRules') : t('dns.selectGroupToViewRules') }}
              </div>
              <div v-for="rule in rules" :key="rule.id" class="rule-row">
                <div class="rule-cell c-pattern">
                  <code class="pattern-text">{{ rule.pattern }}</code>
                </div>
                <div class="rule-cell c-mode">
                  <el-tag size="small" type="info">{{ rule.mode }}</el-tag>
                </div>
                <div class="rule-cell c-action">
                  <el-tag
                    size="small"
                    :type="rule.action === 'allow' ? 'success' : rule.action === 'block' ? 'danger' : 'warning'"
                  >
                    {{ rule.action }}
                  </el-tag>
                </div>
                <div class="rule-cell c-redirect mono text-dim">
                  {{ rule.redirect_to || '-' }}
                </div>
                <div class="rule-cell c-op">
                  <el-button size="small" circle @click="openEditRuleDialog(rule)" text>
                    <el-icon><Refresh /></el-icon>
                  </el-button>
                  <el-button size="small" circle @click="deleteRule(rule.id)" text type="danger">
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 劫持日志 -->
      <div class="dns-card">
        <div class="dns-card-header">
          <div class="dns-card-title">
            <div class="title-text">{{ t('dns.hijackLog') }}</div>
            <div class="title-sub">{{ t('dns.logSubtitle') }}</div>
          </div>
        </div>
        <div class="dns-card-body">
          <div v-if="!logs.length" class="empty-text text-dim">{{ t('dns.noHijackLogs') }}</div>
          <div v-else class="log-list">
            <div v-for="(l, i) in logs" :key="i" class="log-row">
              <span class="log-ts mono text-dim">{{ formatTime(l.ts) }}</span>
              <span class="log-domain mono">{{ l.domain }}</span>
              <span class="log-arrow">→</span>
              <span class="log-new mono hl">{{ l.new_ip }}</span>
              <span class="log-old text-dim mono">{{ t('dns.original', { ips: l.original_ips.join(', ') }) }}</span>
              <span class="log-src text-dim mono">{{ l.dns_server }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 分组编辑对话框 -->
    <el-dialog
      v-model="showGroupDialog"
      :title="editingGroup?.id === null ? t('dns.createGroup') : t('dns.editGroup')"
      width="400px"
    >
      <el-form v-if="editingGroup" label-position="top">
        <el-form-item :label="t('dns.groupName')">
          <el-input v-model="editingGroup.name" :placeholder="t('dns.groupNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('dns.priority')">
          <el-input-number v-model="editingGroup.priority" :min="0" :max="999" />
          <span class="text-dim" style="margin-left: 8px; font-size: 12px;">{{ t('dns.priorityHint') }}</span>
        </el-form-item>
        <el-form-item :label="t('dns.enabled')">
          <el-switch v-model="editingGroup.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showGroupDialog = false">{{ t('dns.cancel') }}</el-button>
        <el-button type="primary" @click="saveGroup">{{ t('dns.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 规则编辑对话框 -->
    <el-dialog
      v-model="showRuleDialog"
      :title="editingRule?.id === null ? t('dns.createRule') : t('dns.editRule')"
      width="500px"
    >
      <el-form v-if="editingRule" label-position="top">
        <el-form-item :label="t('dns.domainPattern')" required>
          <el-input v-model="editingRule.pattern" :placeholder="t('dns.patternPlaceholder')" />
          <span class="text-dim" style="font-size: 12px; margin-top: 4px; display: block;">
            {{ t('dns.patternHint') }}
          </span>
        </el-form-item>
        <el-form-item :label="t('dns.matchMode')">
          <el-select v-model="editingRule.mode" style="width: 100%">
            <el-option value="wildcard" label="Wildcard (通配符 * ?)" />
            <el-option value="exact" label="Exact (精确匹配)" />
            <el-option value="regex" label="Regex (正则表达式)" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('dns.action')">
          <el-select v-model="editingRule.action" style="width: 100%">
            <el-option value="block" label="Block (拦截 → 0.0.0.0)" />
            <el-option value="allow" label="Allow (放行)" />
            <el-option value="redirect" label="Redirect (重定向)" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="editingRule.action === 'redirect'" :label="t('dns.redirectTo')" required>
          <el-input v-model="editingRule.redirect_to" :placeholder="t('dns.redirectToPlaceholder')" class="mono" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showRuleDialog = false">{{ t('dns.cancel') }}</el-button>
        <el-button type="primary" @click="saveRule">{{ t('dns.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- DoH 检测提示对话框 -->
    <DohWarningDialog ref="dohDialogRef" />
  </div>
</template>

<style scoped>
.dns-view { background: var(--on-bg); display: flex; flex-direction: column; }
.dns-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.dns-title { font-size: 14px; font-weight: 600; color: var(--on-text); display: flex; align-items: center; }
.dns-body { padding: 12px 16px; display: flex; flex-direction: column; gap: 14px; }

.dns-card {
  background: var(--on-bg-card);
  border: 1px solid var(--on-border-light);
  border-radius: 8px;
  overflow: hidden;
}
.dns-card-header {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated, var(--on-bg-card));
}
.dns-card-title { flex: 1; min-width: 0; }
.title-text { font-size: 14px; font-weight: 600; color: var(--on-text); }
.title-sub { font-size: 12px; color: var(--on-text-dim); margin-top: 2px; }
.dns-card-actions { display: flex; gap: 8px; flex-shrink: 0; }
.dns-card-body { padding: 14px 16px; }

.dns-status-tags {
  display: flex; align-items: center; gap: 8px; margin-bottom: 4px;
}

.dns-hint {
  display: flex; align-items: center;
  padding: 8px 12px; border-radius: 4px;
  background: rgba(107, 114, 128, 0.08);
  color: var(--on-text-muted);
  font-size: 13px;
}
.dns-hint.ok { background: rgba(46, 160, 67, 0.1); color: var(--on-success); }
.dns-hint.warn { background: rgba(210, 153, 34, 0.1); color: var(--on-warn); }
.dns-hint.err { background: rgba(248, 81, 73, 0.1); color: var(--on-error); }

.dns-stats {
  display: flex; gap: 12px;
  margin-top: 14px; padding: 10px 12px;
  background: rgba(0, 0, 0, 0.18); border-radius: 6px;
}
.stat-item { flex: 1; text-align: center; }
.stat-item.hl .stat-val { color: var(--on-accent); }
.stat-label { font-size: 11px; color: var(--on-text-dim); margin-bottom: 2px; }
.stat-val { font-size: 18px; font-weight: 700; color: var(--on-text); }

/* 分组管理 */
.dns-groups-card .dns-card-body { padding: 0; }
.dns-groups-body {
  display: flex;
  min-height: 300px;
}
.groups-list {
  width: 280px;
  border-right: 1px solid var(--on-border-light);
  overflow-y: auto;
  padding: 8px;
}
.group-item {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
}
.group-item:hover { background: var(--on-bg-hover); }
.group-item.active { background: var(--on-accent-bg, rgba(59, 130, 246, 0.1)); }
.group-item.disabled { opacity: 0.6; }
.drag-handle { cursor: grab; color: var(--on-text-dim); padding: 4px; }
.drag-handle:active { cursor: grabbing; }
.group-icon { font-size: 18px; color: var(--on-accent); }
.group-info { flex: 1; min-width: 0; }
.group-name { font-size: 13px; font-weight: 500; color: var(--on-text); }
.group-meta { font-size: 11px; color: var(--on-text-dim); }
.group-actions { display: flex; align-items: center; gap: 4px; }

.rules-panel {
  flex: 1;
  display: flex; flex-direction: column;
  overflow: hidden;
}
.rules-panel-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated, var(--on-bg-card));
}
.rules-panel-title { font-size: 14px; font-weight: 600; color: var(--on-text); }
.rules-count { font-weight: normal; color: var(--on-text-dim); }

/* 规则表 */
.rules-table {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex; flex-direction: column; gap: 6px;
}
.rule-row {
  display: grid;
  grid-template-columns: 1fr 100px 80px 120px 80px;
  gap: 8px; align-items: center;
}
.rule-header {
  font-size: 11px; color: var(--on-text-dim);
  padding: 0 4px 6px;
  border-bottom: 1px solid var(--on-border-light);
}
.rule-empty { padding: 20px 0; text-align: center; }
.pattern-text { font-size: 12px; }

/* 日志 */
.log-list {
  max-height: 320px; overflow-y: auto;
  display: flex; flex-direction: column; gap: 2px;
}
.log-row {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 8px; font-size: 12px; border-radius: 3px;
}
.log-row:hover { background: var(--on-bg-hover); }
.log-ts { width: 70px; font-size: 11px; flex-shrink: 0; }
.log-domain { flex: 1; min-width: 0; color: var(--on-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.log-arrow { color: var(--on-text-dim); flex-shrink: 0; }
.log-new { color: var(--on-accent); font-weight: 600; flex-shrink: 0; }
.log-old { flex-shrink: 0; font-size: 11px; }
.log-src { flex-shrink: 0; font-size: 11px; margin-left: auto; }

.empty-text { text-align: center; padding: 30px; }
</style>
