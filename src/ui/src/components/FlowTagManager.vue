<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useFlowTagStore, TAG_COLORS } from '../stores/flowTag'

const props = defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const tagStore = useFlowTagStore()
const { t } = useI18n()

// 新建标记
const newTagName = ref('')
const newTagColor = ref(TAG_COLORS[0])

// 编辑状态
const editingTagId = ref<string | null>(null)
const editingTagName = ref('')
const editingTagColor = ref('')

// 自动规则
const showAutoRules = ref(false)
const newRuleName = ref('')
const newRuleTagId = ref('')
const newRuleDsl = ref('')
const editingRuleId = ref<string | null>(null)
const editingRule = ref({ name: '', tag_id: '', dsl: '', enabled: true })

// 弹窗控制
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

function close() {
  visible.value = false
}

// 添加标记
function addTag() {
  if (!newTagName.value.trim()) {
    ElMessage.warning(t('flowTag.nameRequired'))
    return
  }
  tagStore.addTag(newTagName.value.trim(), newTagColor.value)
  newTagName.value = ''
  newTagColor.value = TAG_COLORS[0]
  ElMessage.success(t('flowTag.tagCreated'))
}

// 开始编辑
function startEditTag(id: string) {
  const tag = tagStore.tags.find(t => t.id === id)
  if (tag) {
    editingTagId.value = id
    editingTagName.value = tag.name
    editingTagColor.value = tag.color
  }
}

function saveEditTag() {
  if (!editingTagName.value.trim()) {
    ElMessage.warning(t('flowTag.nameRequired'))
    return
  }
  tagStore.updateTag(editingTagId.value!, {
    name: editingTagName.value.trim(),
    color: editingTagColor.value,
  })
  editingTagId.value = null
  ElMessage.success(t('flowTag.tagUpdated'))
}

function cancelEditTag() {
  editingTagId.value = null
}

// 删除标记
async function deleteTag(id: string) {
  try {
    await ElMessageBox.confirm(t('flowTag.deleteConfirm'), t('common.delete'), {
      confirmButtonText: t('common.delete'),
      cancelButtonText: t('common.cancel'),
      type: 'warning',
    })
    tagStore.deleteTag(id)
    ElMessage.success(t('flowTag.tagDeleted'))
  } catch {
    // 用户取消
  }
}

// 自动规则
function addAutoRule() {
  if (!newRuleName.value.trim() || !newRuleTagId.value || !newRuleDsl.value.trim()) {
    ElMessage.warning(t('flowTag.ruleFieldsRequired'))
    return
  }
  tagStore.addAutoRule(newRuleName.value.trim(), newRuleTagId.value, newRuleDsl.value.trim())
  newRuleName.value = ''
  newRuleTagId.value = ''
  newRuleDsl.value = ''
  ElMessage.success(t('flowTag.ruleCreated'))
}

function startEditRule(id: string) {
  const rule = tagStore.autoRules.find(r => r.id === id)
  if (rule) {
    editingRuleId.value = id
    editingRule.value = { ...rule }
  }
}

function saveEditRule() {
  if (!editingRule.value.name.trim() || !editingRule.value.tag_id || !editingRule.value.dsl.trim()) {
    ElMessage.warning(t('flowTag.ruleFieldsRequired'))
    return
  }
  tagStore.updateAutoRule(editingRuleId.value!, editingRule.value)
  editingRuleId.value = null
  ElMessage.success(t('flowTag.ruleUpdated'))
}

function cancelEditRule() {
  editingRuleId.value = null
}

async function deleteRule(id: string) {
  try {
    await ElMessageBox.confirm(t('flowTag.deleteRuleConfirm'), t('common.delete'), {
      confirmButtonText: t('common.delete'),
      cancelButtonText: t('common.cancel'),
      type: 'warning',
    })
    tagStore.deleteAutoRule(id)
    ElMessage.success(t('flowTag.ruleDeleted'))
  } catch {
    // 用户取消
  }
}

function toggleRuleEnabled(id: string) {
  const rule = tagStore.autoRules.find(r => r.id === id)
  if (rule) {
    tagStore.updateAutoRule(id, { enabled: !rule.enabled })
  }
}
</script>

<template>
  <el-drawer
    v-model="visible"
    :title="t('flowTag.managerTitle')"
    size="480px"
    direction="rtl"
  >
    <div class="tag-manager">
      <!-- 手动标记管理 -->
      <div class="section">
        <h3 class="section-title">{{ t('flowTag.tags') }}</h3>

        <!-- 添加新标记 -->
        <div class="add-form">
          <el-input
            v-model="newTagName"
            :placeholder="t('flowTag.tagNamePlaceholder')"
            size="small"
            clearable
            @keyup.enter="addTag"
          />
          <div class="color-picker">
            <span
              v-for="color in TAG_COLORS"
              :key="color"
              class="color-dot"
              :class="{ active: newTagColor === color }"
              :style="{ background: color }"
              @click="newTagColor = color"
            />
          </div>
          <el-button type="primary" size="small" @click="addTag">
            {{ t('common.add') }}
          </el-button>
        </div>

        <!-- 标记列表 -->
        <div class="tag-list">
          <div
            v-for="tag in tagStore.tags"
            :key="tag.id"
            class="tag-item"
          >
            <template v-if="editingTagId === tag.id">
              <el-input
                v-model="editingTagName"
                size="small"
                style="width: 120px"
              />
              <div class="color-picker">
                <span
                  v-for="color in TAG_COLORS"
                  :key="color"
                  class="color-dot"
                  :class="{ active: editingTagColor === color }"
                  :style="{ background: color }"
                  @click="editingTagColor = color"
                />
              </div>
              <el-button size="small" type="primary" @click="saveEditTag">
                {{ t('common.save') }}
              </el-button>
              <el-button size="small" @click="cancelEditTag">
                {{ t('common.cancel') }}
              </el-button>
            </template>
            <template v-else>
              <span class="tag-dot" :style="{ background: tag.color }"></span>
              <span class="tag-name">{{ tag.name }}</span>
              <div class="tag-actions">
                <el-button size="small" text @click="startEditTag(tag.id)">
                  {{ t('common.edit') }}
                </el-button>
                <el-button size="small" text type="danger" @click="deleteTag(tag.id)">
                  {{ t('common.delete') }}
                </el-button>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- 自动标记规则 -->
      <div class="section">
        <div class="section-header">
          <h3 class="section-title">{{ t('flowTag.autoRules') }}</h3>
          <el-tag size="small" type="info">
            {{ tagStore.activeRulesCount }} {{ t('flowTag.activeRules') }}
          </el-tag>
        </div>

        <!-- 添加规则 -->
        <div class="add-form">
          <el-input
            v-model="newRuleName"
            :placeholder="t('flowTag.ruleNamePlaceholder')"
            size="small"
            clearable
            style="width: 100px"
          />
          <el-select
            v-model="newRuleTagId"
            :placeholder="t('flowTag.selectTag')"
            size="small"
            style="width: 100px"
          >
            <el-option
              v-for="tag in tagStore.tags"
              :key="tag.id"
              :label="tag.name"
              :value="tag.id"
            />
          </el-select>
          <el-input
            v-model="newRuleDsl"
            :placeholder="t('flowTag.ruleDslPlaceholder')"
            size="small"
            style="flex: 1"
          />
          <el-button type="primary" size="small" @click="addAutoRule">
            {{ t('common.add') }}
          </el-button>
        </div>

        <div class="dsl-hint">{{ t('flowTag.ruleDslHint') }}</div>

        <!-- 规则列表 -->
        <div class="rule-list">
          <div
            v-for="rule in tagStore.autoRules"
            :key="rule.id"
            class="rule-item"
            :class="{ disabled: !rule.enabled }"
          >
            <template v-if="editingRuleId === rule.id">
              <el-input
                v-model="editingRule.name"
                size="small"
                style="width: 80px"
              />
              <el-select
                v-model="editingRule.tag_id"
                size="small"
                style="width: 80px"
              >
                <el-option
                  v-for="tag in tagStore.tags"
                  :key="tag.id"
                  :label="tag.name"
                  :value="tag.id"
                />
              </el-select>
              <el-input
                v-model="editingRule.dsl"
                size="small"
                style="flex: 1"
              />
              <el-button size="small" type="primary" @click="saveEditRule">
                {{ t('common.save') }}
              </el-button>
              <el-button size="small" @click="cancelEditRule">
                {{ t('common.cancel') }}
              </el-button>
            </template>
            <template v-else>
              <el-switch
                :model-value="rule.enabled"
                size="small"
                @change="toggleRuleEnabled(rule.id)"
              />
              <span class="rule-name">{{ rule.name }}</span>
              <span class="tag-dot-inline" :style="{ background: tagStore.getTagColor(rule.tag_id) }"></span>
              <code class="rule-dsl">{{ rule.dsl }}</code>
              <div class="rule-actions">
                <el-button size="small" text @click="startEditRule(rule.id)">
                  {{ t('common.edit') }}
                </el-button>
                <el-button size="small" text type="danger" @click="deleteRule(rule.id)">
                  {{ t('common.delete') }}
                </el-button>
              </div>
            </template>
          </div>
        </div>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.tag-manager {
  padding: 0 8px;
}

.section {
  margin-bottom: 24px;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.section-title {
  margin: 0 0 12px 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--on-text);
}

.section-header .section-title {
  margin-bottom: 0;
}

.add-form {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.color-picker {
  display: flex;
  gap: 4px;
}

.color-dot {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  cursor: pointer;
  border: 2px solid transparent;
  transition: transform 0.15s;
}

.color-dot:hover {
  transform: scale(1.15);
}

.color-dot.active {
  border-color: var(--on-text);
  transform: scale(1.15);
}

.tag-list,
.rule-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.tag-item,
.rule-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--on-bg);
  border-radius: 6px;
  border: 1px solid var(--on-border-light);
}

.rule-item.disabled {
  opacity: 0.5;
}

.tag-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  flex-shrink: 0;
}

.tag-dot-inline {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.tag-name {
  flex: 1;
  font-size: 13px;
}

.tag-actions,
.rule-actions {
  display: flex;
  gap: 4px;
  margin-left: auto;
}

.rule-name {
  font-size: 12px;
  font-weight: 500;
  min-width: 60px;
}

.rule-dsl {
  font-family: var(--on-font-mono, monospace);
  font-size: 11px;
  color: var(--on-accent);
  background: var(--on-bg-hover);
  padding: 2px 6px;
  border-radius: 3px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dsl-hint {
  font-size: 11px;
  color: var(--on-text-muted);
  margin-bottom: 8px;
}
</style>
