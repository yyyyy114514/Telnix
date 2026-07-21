<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type AutoReplyRule } from '../api/client'
import RuleEditor from '../components/RuleEditor.vue'

// 自动修改规则管理
const rules = ref<AutoReplyRule[]>([])
const editorVisible = ref(false)
const editingRule = ref<AutoReplyRule | null>(null)
const loading = ref(false)

// 多选
const multiSelectMode = ref(false)
const selectedRuleIds = ref<Set<string>>(new Set())

function toggleMultiSelect() {
  multiSelectMode.value = !multiSelectMode.value
  if (!multiSelectMode.value) {
    selectedRuleIds.value.clear()
  }
}

function toggleRuleCheck(id: string) {
  if (selectedRuleIds.value.has(id)) {
    selectedRuleIds.value.delete(id)
  } else {
    selectedRuleIds.value.add(id)
  }
  selectedRuleIds.value = new Set(selectedRuleIds.value)
}

function selectAllRules() {
  selectedRuleIds.value = new Set(rules.value.map(r => r.id!).filter(Boolean) as string[])
}

function clearSelection() {
  selectedRuleIds.value = new Set()
}

const selectedCount = computed(() => selectedRuleIds.value.size)

// ---------- 多选悬浮工具栏（参照 FlowList.vue）----------
// 滚动时隐藏，N 秒不操作再显示（默认 1 秒）
const scrollContainerRef = ref<HTMLElement | null>(null)
const floatBarVisible = ref(true)
let floatBarTimer: number | null = null
const FLOAT_BAR_DELAY_SEC = 1

function hideFloatBar() {
  if (!multiSelectMode.value) return
  floatBarVisible.value = false
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
  floatBarTimer = window.setTimeout(() => {
    if (multiSelectMode.value) floatBarVisible.value = true
  }, FLOAT_BAR_DELAY_SEC * 1000)
}

function showFloatBarNow() {
  if (!multiSelectMode.value) return
  floatBarVisible.value = true
  if (floatBarTimer !== null) {
    clearTimeout(floatBarTimer)
    floatBarTimer = null
  }
}

watch(multiSelectMode, (v) => {
  if (v) showFloatBarNow()
})

onUnmounted(() => {
  if (floatBarTimer !== null) clearTimeout(floatBarTimer)
})

async function load() {
  loading.value = true
  try {
    rules.value = await api.getRules()
  } catch (e: any) {
    ElMessage.error('加载规则失败：' + (e?.message || e))
  } finally {
    loading.value = false
  }
}

function newRule() {
  editingRule.value = null
  editorVisible.value = true
}

function editRule(r: AutoReplyRule) {
  editingRule.value = r
  editorVisible.value = true
}

async function saveRule(r: AutoReplyRule) {
  try {
    if (r.id) {
      await api.updateRule(r.id, r)
    } else {
      await api.createRule(r)
    }
    ElMessage.success('保存成功')
    await load()
  } catch (e: any) {
    ElMessage.error('保存失败：' + (e?.message || e))
  }
}

async function deleteRule(r: AutoReplyRule) {
  try {
    await ElMessageBox.confirm(`确定删除该规则？`, '删除', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.deleteRule(r.id!)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    ElMessage.error('删除失败：' + (e?.message || e))
  }
}

async function toggleRule(r: AutoReplyRule) {
  r.enabled = !r.enabled
  try {
    await api.updateRule(r.id!, r)
  } catch (e: any) {
    r.enabled = !r.enabled
    ElMessage.error('更新失败：' + (e?.message || e))
  }
}

// 批量删除
async function batchDelete() {
  const ids = [...selectedRuleIds.value]
  if (!ids.length) return
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${ids.length} 条规则？`, '批量删除', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.batchDeleteRules(ids)
    ElMessage.success(`已删除 ${ids.length} 条`)
    selectedRuleIds.value.clear()
    multiSelectMode.value = false
    await load()
  } catch (e: any) {
    ElMessage.error('批量删除失败：' + (e?.message || e))
  }
}

// 批量启用/禁用
async function batchSetEnabled(enabled: boolean) {
  const ids = [...selectedRuleIds.value]
  if (!ids.length) return
  try {
    await api.batchUpdateRules(ids, enabled)
    ElMessage.success(`已${enabled ? '启用' : '禁用'} ${ids.length} 条`)
    selectedRuleIds.value.clear()
    multiSelectMode.value = false
    await load()
  } catch (e: any) {
    ElMessage.error('批量更新失败：' + (e?.message || e))
  }
}

const actionLabel: Record<string, string> = {
  mock: 'Mock 返回',
  modify_request: '修改请求',
  modify_response: '修改响应',
}
const matchLabel: Record<string, string> = {
  wildcard: '通配符',
  exact: '精确',
  regex: '正则',
}

onMounted(load)
</script>

<template>
  <div class="auto-reply-view full flex flex-col">
    <div class="page-header">
      <div class="page-title">
        <el-icon><SetUp /></el-icon>&nbsp;自动修改规则
      </div>
      <div style="display: flex; gap: 8px; align-items: center">
        <el-tooltip :content="multiSelectMode ? '退出多选' : '多选模式'" placement="top">
          <el-button
            size="small"
            :type="multiSelectMode ? 'warning' : 'default'"
            circle
            @click="toggleMultiSelect"
          >
            <el-icon><CircleCheck /></el-icon>
          </el-button>
        </el-tooltip>
        <el-button type="primary" size="small" @click="newRule" :disabled="multiSelectMode">
          <el-icon><Plus /></el-icon>&nbsp;新建规则
        </el-button>
      </div>
    </div>
    <div
      ref="scrollContainerRef"
      class="flex-1 overflow-auto rules-scroll-container"
      style="padding: 0 16px 16px"
      @scroll="hideFloatBar"
    >
      <!-- 多选模式悬浮工具栏（贴右上角，滚动隐藏，N 秒后显示） -->
      <transition name="float-bar">
        <div
          v-if="multiSelectMode && floatBarVisible"
          class="multi-float-bar"
          @mouseenter="showFloatBarNow"
        >
          <span class="mfb-count">已选 {{ selectedCount }}</span>
          <el-button size="small" @click="selectAllRules">全选</el-button>
          <el-button size="small" @click="clearSelection" :disabled="!selectedCount">清空</el-button>
          <el-button
            size="small"
            type="success"
            :disabled="!selectedCount"
            @click="batchSetEnabled(true)"
          >
            <el-icon><Open /></el-icon>&nbsp;启用
          </el-button>
          <el-button
            size="small"
            type="warning"
            :disabled="!selectedCount"
            @click="batchSetEnabled(false)"
          >
            <el-icon><TurnOff /></el-icon>&nbsp;禁用
          </el-button>
          <el-button
            size="small"
            type="danger"
            :disabled="!selectedCount"
            @click="batchDelete"
          >
            <el-icon><Delete /></el-icon>&nbsp;删除
          </el-button>
        </div>
      </transition>
      <el-table :data="rules" v-loading="loading" size="small" border stripe>
        <el-table-column v-if="multiSelectMode" label="" width="40">
          <template #default="{ row }">
            <el-checkbox
              :model-value="selectedRuleIds.has(row.id)"
              size="small"
              @change="toggleRuleCheck(row.id)"
            />
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" @change="toggleRule(row as any)" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="140">
          <template #default="{ row }">
            <span v-if="row.note" :title="row.note">{{ row.note }}</span>
            <span v-else class="text-dim">-</span>
          </template>
        </el-table-column>
        <el-table-column label="匹配模式" width="100">
          <template #default="{ row }">{{ matchLabel[row.match_mode] || row.match_mode }}</template>
        </el-table-column>
        <el-table-column label="URL 模式" prop="pattern" class-name="mono" />
        <el-table-column label="动作" width="120">
          <template #default="{ row }">{{ actionLabel[row.action] || row.action }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="editRule(row as any)">编辑</el-button>
            <el-button link type="danger" size="small" @click="deleteRule(row as any)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text text-dim">暂无规则，点击右上角"新建规则"</div>
        </template>
      </el-table>
    </div>

    <RuleEditor v-model="editorVisible" :rule="editingRule" @save="saveRule" />
  </div>
</template>

<style scoped>
.auto-reply-view { background: var(--on-bg); }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.page-title { font-size: 15px; font-weight: 600; display: flex; align-items: center; }
.empty-text { padding: 30px; }

/* 多选悬浮工具栏（贴滚动容器右上角，滚动时隐藏，N 秒后显示） */
.rules-scroll-container { position: relative; }
.multi-float-bar {
  position: absolute;
  top: 8px;
  right: 24px;
  z-index: 20;
  display: flex; align-items: center; gap: 6px;
  padding: 6px 10px;
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-border, #333344);
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.45);
  font-size: 12px;
}
.mfb-count {
  color: var(--on-accent, #2dd4bf);
  font-weight: 600;
  padding-right: 4px;
  white-space: nowrap;
}

/* 过渡动画 */
.float-bar-enter-active,
.float-bar-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}
.float-bar-enter-from,
.float-bar-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
</style>
