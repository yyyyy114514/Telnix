<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import TextSearch from './TextSearch.vue'

// Headers 标签页：key-value 表格，支持编辑（断点时）
const props = defineProps<{
  modelValue: string | null
  editable?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [string] }>()

interface Row {
  key: string
  value: string
}

function parse(str: string | null): Row[] {
  if (!str) return []
  // 优先按 JSON 解析
  try {
    const obj = JSON.parse(str)
    if (obj && typeof obj === 'object') {
      return Object.entries(obj).map(([k, v]) => ({ key: k, value: String(v) }))
    }
  } catch {
    /* 非 JSON */
  }
  // 回退：原始 HTTP 头解析
  const rows: Row[] = []
  for (const line of str.split(/\r?\n/)) {
    const idx = line.indexOf(':')
    if (idx > 0) {
      rows.push({ key: line.slice(0, idx).trim(), value: line.slice(idx + 1).trim() })
    }
  }
  return rows
}

const rows = ref<Row[]>(parse(props.modelValue))

// 非编辑模式下，外部变化同步
watch(
  () => props.modelValue,
  (v) => {
    if (!props.editable) rows.value = parse(v)
  }
)

// 搜索用的纯文本（非编辑模式）
const searchText = computed(() => {
  return rows.value.map(r => `${r.key}: ${r.value}`).join('\n')
})

function serialize(): string {
  const obj: Record<string, string> = {}
  for (const r of rows.value) {
    if (r.key) obj[r.key] = r.value
  }
  return JSON.stringify(obj)
}

function onEdit() {
  emit('update:modelValue', serialize())
}

function addRow() {
  rows.value.push({ key: '', value: '' })
  onEdit()
}

function removeRow(i: number) {
  rows.value.splice(i, 1)
  onEdit()
}
</script>

<template>
  <div class="header-view full">
    <!-- 编辑模式：表格 -->
    <div v-if="editable" class="overflow-auto full">
      <table class="kv-table">
        <thead>
          <tr>
            <th style="width: 38%">名称</th>
            <th>值</th>
            <th style="width: 40px"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in rows" :key="i">
            <td class="mono">
              <el-input v-model="row.key" size="small" @input="onEdit" />
            </td>
            <td class="mono">
              <el-input v-model="row.value" size="small" @input="onEdit" />
            </td>
            <td>
              <el-button link type="danger" size="small" @click="removeRow(i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td colspan="3" class="empty-text text-dim">（无 Headers）</td>
          </tr>
        </tbody>
      </table>
      <div class="add-row-bar">
        <el-button size="small" @click="addRow">
          <el-icon><Plus /></el-icon>&nbsp;新增 Header
        </el-button>
      </div>
    </div>
    <!-- 非编辑模式：可搜索文本，HTTP 头语法高亮 -->
    <TextSearch v-else :text="searchText" searchable language="http" />
  </div>
</template>

<style scoped>
.header-view { padding: 6px 8px; }
.kv-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.kv-table th {
  text-align: left; padding: 6px 8px; color: var(--on-text-muted);
  font-weight: 600; border-bottom: 1px solid var(--on-border-light);
  position: sticky; top: 0; background: var(--on-bg-elevated); z-index: 1;
}
.kv-table td { padding: 5px 8px; border-bottom: 1px solid var(--on-border-light); vertical-align: middle; word-break: break-all; }
.kv-table tr:hover td { background: var(--on-bg-hover); }
.kv-value { color: var(--on-text); }
.empty-text { text-align: center; padding: 18px; }
.add-row-bar { padding: 8px; }
</style>
