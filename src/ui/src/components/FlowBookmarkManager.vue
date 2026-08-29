<script setup lang="ts">
import { ref, computed, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useFlowBookmarkStore } from '../stores/flowBookmark'
import { useFlowsStore } from '../stores/flows'

const props = defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const bookmarkStore = useFlowBookmarkStore()
const flowsStore = useFlowsStore()
const { t } = useI18n()

// 新建组
const newGroupName = ref('')
const newGroupColor = ref('#3b82f6')

// 编辑状态
const editingGroupId = ref<string | null>(null)
const editingGroupName = ref('')
const editingGroupColor = ref('')

// 书签编辑
const editingBookmarkId = ref<string | null>(null)
const editingBookmark = ref({ name: '', group_id: '', note: '' })

// 选中要移动的书签
const movingBookmarkId = ref<string | null>(null)

// 弹窗控制
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

function close() {
  visible.value = false
}

// 组管理
function addGroup() {
  if (!newGroupName.value.trim()) {
    ElMessage.warning(t('flowBookmark.nameRequired'))
    return
  }
  bookmarkStore.addGroup(newGroupName.value.trim(), newGroupColor.value)
  newGroupName.value = ''
  ElMessage.success(t('flowBookmark.groupCreated'))
}

function startEditGroup(id: string) {
  const group = bookmarkStore.groups.find(g => g.id === id)
  if (group) {
    editingGroupId.value = id
    editingGroupName.value = group.name
    editingGroupColor.value = group.color
  }
}

function saveEditGroup() {
  if (!editingGroupName.value.trim()) {
    ElMessage.warning(t('flowBookmark.nameRequired'))
    return
  }
  bookmarkStore.updateGroup(editingGroupId.value!, {
    name: editingGroupName.value.trim(),
    color: editingGroupColor.value,
  })
  editingGroupId.value = null
  ElMessage.success(t('flowBookmark.groupUpdated'))
}

function cancelEditGroup() {
  editingGroupId.value = null
}

async function deleteGroup(id: string) {
  if (id === 'default') {
    ElMessage.warning(t('flowBookmark.cannotDeleteDefault'))
    return
  }
  try {
    await ElMessageBox.confirm(t('flowBookmark.deleteGroupConfirm'), t('common.delete'), {
      confirmButtonText: t('common.delete'),
      cancelButtonText: t('common.cancel'),
      type: 'warning',
    })
    bookmarkStore.deleteGroup(id)
    ElMessage.success(t('flowBookmark.groupDeleted'))
  } catch {
    // 用户取消
  }
}

// 书签编辑
function startEditBookmark(id: string) {
  const bookmark = bookmarkStore.bookmarks.find(b => b.id === id)
  if (bookmark) {
    editingBookmarkId.value = id
    editingBookmark.value = { ...bookmark }
  }
}

function saveEditBookmark() {
  if (!editingBookmark.value.name.trim()) {
    ElMessage.warning(t('flowBookmark.nameRequired'))
    return
  }
  bookmarkStore.updateBookmark(editingBookmarkId.value!, editingBookmark.value)
  editingBookmarkId.value = null
  ElMessage.success(t('flowBookmark.bookmarkUpdated'))
}

function cancelEditBookmark() {
  editingBookmarkId.value = null
}

async function deleteBookmark(id: string) {
  try {
    await ElMessageBox.confirm(t('flowBookmark.deleteConfirm'), t('common.delete'), {
      confirmButtonText: t('common.delete'),
      cancelButtonText: t('common.cancel'),
      type: 'warning',
    })
    bookmarkStore.deleteBookmark(id)
    ElMessage.success(t('flowBookmark.bookmarkDeleted'))
  } catch {
    // 用户取消
  }
}

// 移动书签到组
function startMoveBookmark(id: string) {
  movingBookmarkId.value = id
}

function confirmMoveBookmark(groupId: string) {
  if (movingBookmarkId.value) {
    bookmarkStore.moveBookmarkToGroup(movingBookmarkId.value, groupId)
    movingBookmarkId.value = null
    ElMessage.success(t('flowBookmark.movedToGroup'))
  }
}

function cancelMoveBookmark() {
  movingBookmarkId.value = null
}

// 跳转到书签对应的流量
function jumpToBookmark(flowId: number) {
  flowsStore.select(flowId)
  close()
}

// 获取书签对应的 flow（用于显示更多信息）
function getFlowInfo(flowId: number) {
  return flowsStore.getFlow(flowId)
}

// 获取组颜色
function getGroupColor(groupId: string): string {
  const group = bookmarkStore.groups.find(g => g.id === groupId)
  return group?.color || '#3b82f6'
}

// 获取组名称
function getGroupName(groupId: string): string {
  const group = bookmarkStore.groups.find(g => g.id === groupId)
  return group?.name || groupId
}
</script>

<template>
  <el-drawer
    v-model="visible"
    :title="t('flowBookmark.managerTitle')"
    size="520px"
    direction="rtl"
  >
    <div class="bookmark-manager">
      <!-- 书签组管理 -->
      <div class="section">
        <h3 class="section-title">{{ t('flowBookmark.groups') }}</h3>

        <!-- 添加新组 -->
        <div class="add-form">
          <el-input
            v-model="newGroupName"
            :placeholder="t('flowBookmark.groupNamePlaceholder')"
            size="small"
            clearable
            @keyup.enter="addGroup"
          />
          <el-color-picker
            v-model="newGroupColor"
            size="small"
          />
          <el-button type="primary" size="small" @click="addGroup">
            {{ t('common.add') }}
          </el-button>
        </div>

        <!-- 组列表 -->
        <div class="group-list">
          <div
            v-for="group in bookmarkStore.groups"
            :key="group.id"
            class="group-item"
          >
            <template v-if="editingGroupId === group.id">
              <el-input
                v-model="editingGroupName"
                size="small"
                style="width: 120px"
              />
              <el-color-picker
                v-model="editingGroupColor"
                size="small"
              />
              <el-button size="small" type="primary" @click="saveEditGroup">
                {{ t('common.save') }}
              </el-button>
              <el-button size="small" @click="cancelEditGroup">
                {{ t('common.cancel') }}
              </el-button>
            </template>
            <template v-else>
              <span class="group-color" :style="{ background: group.color }"></span>
              <span class="group-name">{{ group.name }}</span>
              <span class="group-count">
                {{ bookmarkStore.getGroupBookmarks(group.id).length }}
              </span>
              <div class="group-actions">
                <el-button
                  v-if="group.id !== 'default'"
                  size="small"
                  text
                  @click="startEditGroup(group.id)"
                >
                  {{ t('common.edit') }}
                </el-button>
                <el-button
                  v-if="group.id !== 'default'"
                  size="small"
                  text
                  type="danger"
                  @click="deleteGroup(group.id)"
                >
                  {{ t('common.delete') }}
                </el-button>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- 书签列表 -->
      <div class="section">
        <h3 class="section-title">
          {{ t('flowBookmark.bookmarks') }}
          <span class="bookmark-count">{{ bookmarkStore.bookmarkCount }}</span>
        </h3>

        <!-- 书签列表 -->
        <div class="bookmark-list">
          <div
            v-for="bookmark in bookmarkStore.bookmarks"
            :key="bookmark.id"
            class="bookmark-item"
          >
            <template v-if="editingBookmarkId === bookmark.id">
              <div class="bookmark-edit-form">
                <el-input
                  v-model="editingBookmark.name"
                  :placeholder="t('flowBookmark.namePlaceholder')"
                  size="small"
                  style="width: 160px"
                />
                <el-select
                  v-model="editingBookmark.group_id"
                  size="small"
                  style="width: 100px"
                >
                  <el-option
                    v-for="group in bookmarkStore.groups"
                    :key="group.id"
                    :label="group.name"
                    :value="group.id"
                  />
                </el-select>
                <el-input
                  v-model="editingBookmark.note"
                  :placeholder="t('flowBookmark.notePlaceholder')"
                  size="small"
                  style="flex: 1"
                />
                <el-button size="small" type="primary" @click="saveEditBookmark">
                  {{ t('common.save') }}
                </el-button>
                <el-button size="small" @click="cancelEditBookmark">
                  {{ t('common.cancel') }}
                </el-button>
              </div>
            </template>
            <template v-else-if="movingBookmarkId === bookmark.id">
              <div class="bookmark-move-form">
                <span class="bookmark-name">{{ bookmark.name }}</span>
                <span class="move-label">{{ t('flowBookmark.moveToGroup') }}</span>
                <el-select
                  :model-value="bookmark.group_id"
                  size="small"
                  style="width: 120px"
                  @change="confirmMoveBookmark"
                >
                  <el-option
                    v-for="group in bookmarkStore.groups"
                    :key="group.id"
                    :label="group.name"
                    :value="group.id"
                  />
                </el-select>
                <el-button size="small" @click="cancelMoveBookmark">
                  {{ t('common.cancel') }}
                </el-button>
              </div>
            </template>
            <template v-else>
              <span class="bookmark-color" :style="{ background: getGroupColor(bookmark.group_id) }"></span>
              <span class="bookmark-name">{{ bookmark.name }}</span>
              <span class="bookmark-flow-id">#{{ bookmark.flow_id }}</span>
              <span v-if="bookmark.note" class="bookmark-note" :title="bookmark.note">
                {{ bookmark.note }}
              </span>
              <div class="bookmark-actions">
                <el-button size="small" text @click="jumpToBookmark(bookmark.flow_id)">
                  {{ t('flowBookmark.goto') }}
                </el-button>
                <el-button size="small" text @click="startMoveBookmark(bookmark.id)">
                  {{ t('flowBookmark.move') }}
                </el-button>
                <el-button size="small" text @click="startEditBookmark(bookmark.id)">
                  {{ t('common.edit') }}
                </el-button>
                <el-button size="small" text type="danger" @click="deleteBookmark(bookmark.id)">
                  {{ t('common.delete') }}
                </el-button>
              </div>
            </template>
          </div>

          <div v-if="bookmarkStore.bookmarks.length === 0" class="empty-state">
            {{ t('flowBookmark.noBookmarks') }}
          </div>
        </div>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.bookmark-manager {
  padding: 0 8px;
}

.section {
  margin-bottom: 24px;
}

.section-title {
  margin: 0 0 12px 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--on-text);
  display: flex;
  align-items: center;
  gap: 8px;
}

.bookmark-count {
  font-size: 12px;
  font-weight: normal;
  color: var(--on-text-muted);
}

.add-form {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.group-list,
.bookmark-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.group-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--on-bg);
  border-radius: 6px;
  border: 1px solid var(--on-border-light);
}

.group-color {
  width: 12px;
  height: 12px;
  border-radius: 3px;
  flex-shrink: 0;
}

.group-name {
  flex: 1;
  font-size: 13px;
}

.group-count {
  font-size: 11px;
  color: var(--on-text-muted);
  background: var(--on-bg-hover);
  padding: 2px 6px;
  border-radius: 10px;
}

.group-actions {
  display: flex;
  gap: 4px;
}

.bookmark-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--on-bg);
  border-radius: 6px;
  border: 1px solid var(--on-border-light);
  flex-wrap: wrap;
}

.bookmark-color {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  flex-shrink: 0;
}

.bookmark-name {
  font-size: 13px;
  font-weight: 500;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bookmark-flow-id {
  font-size: 11px;
  color: var(--on-text-muted);
  font-family: var(--on-font-mono, monospace);
}

.bookmark-note {
  font-size: 11px;
  color: var(--on-text-dim);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bookmark-actions,
.group-actions {
  display: flex;
  gap: 2px;
  margin-left: auto;
}

.bookmark-edit-form,
.bookmark-move-form {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.move-label {
  font-size: 12px;
  color: var(--on-text-muted);
}

.empty-state {
  text-align: center;
  padding: 20px;
  color: var(--on-text-muted);
  font-size: 13px;
}
</style>
