<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type LogEntry } from '../api/client'

// 日志查看页：筛选、查看、清空、导出
const entries = ref<LogEntry[]>([])
const total = ref(0)
const loading = ref(false)
const levelFilter = ref('')
const categoryFilter = ref('')
const keyword = ref('')
const page = ref(1)
const pageSize = 100

const levelOptions = [
  { label: '全部', value: '' },
  { label: 'DEBUG', value: 'DEBUG' },
  { label: 'INFO', value: 'INFO' },
  { label: 'WARNING', value: 'WARNING' },
  { label: 'ERROR', value: 'ERROR' },
]

// 收集已有分类（前端缓存）
const categories = ref<string[]>([])

async function loadLogs() {
  loading.value = true
  try {
    const res = await api.logList({
      level: levelFilter.value || undefined,
      category: categoryFilter.value || undefined,
      keyword: keyword.value || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    })
    entries.value = res.entries || []
    total.value = res.total || 0
    // 收集分类
    const cats = new Set<string>()
    for (const e of entries.value) {
      if (e.category) cats.add(e.category)
    }
    categories.value = Array.from(cats).sort()
  } catch (e: any) {
    ElMessage.error('加载日志失败：' + (e?.message || e))
  } finally {
    loading.value = false
  }
}

async function clearLogs() {
  try {
    await ElMessageBox.confirm('确定清空所有日志？', '清空日志', {
      confirmButtonText: '清空', cancelButtonText: '取消', type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.logClear()
    entries.value = []
    total.value = 0
    ElMessage.success('已清空')
  } catch (e: any) {
    ElMessage.error('清空失败：' + (e?.message || e))
  }
}

function exportLogs() {
  const url = api.logExportUrl({
    level: levelFilter.value || undefined,
    category: categoryFilter.value || undefined,
    keyword: keyword.value || undefined,
  })
  // 生成文件名 yyyymmdd-hhmmss.jsonl
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const fname = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.jsonl`
  // 用 fetch 下载（带认证头），转 blob
  fetch(url)
    .then((r) => r.blob())
    .then((b) => {
      const a = document.createElement('a')
      a.href = URL.createObjectURL(b)
      a.download = fname
      a.click()
      URL.revokeObjectURL(a.href)
    })
    .catch(() => ElMessage.error('导出失败'))
}

function onFilterChange() {
  page.value = 1
  loadLogs()
}

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

function levelTag(level: string): TagType {
  switch (level) {
    case 'ERROR': return 'danger'
    case 'WARNING': return 'warning'
    case 'INFO': return 'info'
    case 'DEBUG': return 'success'
    default: return 'info'
  }
}

function formatTs(s: string): string {
  if (!s) return ''
  return s.replace('T', ' ').replace(/\.\d+$/, '')
}

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

onMounted(loadLogs)
</script>

<template>
  <div class="log-view full flex flex-col">
    <!-- 工具栏 -->
    <div class="log-toolbar">
      <el-select v-model="levelFilter" placeholder="级别" size="small" style="width: 110px" @change="onFilterChange">
        <el-option v-for="o in levelOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-select v-model="categoryFilter" placeholder="分类" size="small" clearable style="width: 120px" @change="onFilterChange">
        <el-option v-for="c in categories" :key="c" :label="c" :value="c" />
      </el-select>
      <el-input v-model="keyword" placeholder="关键词搜索" size="small" clearable style="width: 200px" @change="onFilterChange" @clear="onFilterChange" />
      <el-button size="small" @click="loadLogs" :loading="loading">
        <el-icon><Refresh /></el-icon>&nbsp;刷新
      </el-button>
      <el-button size="small" @click="exportLogs">
        <el-icon><Download /></el-icon>&nbsp;导出
      </el-button>
      <el-button size="small" type="danger" @click="clearLogs">
        <el-icon><Delete /></el-icon>&nbsp;清空
      </el-button>
      <div class="flex-1"></div>
      <span class="text-dim" style="font-size: 12px">共 {{ total }} 条</span>
    </div>

    <!-- 日志列表 -->
    <div class="log-list flex-1 overflow-auto">
      <div v-if="!entries.length && !loading" class="empty-text text-dim">
        暂无日志记录
      </div>
      <div v-for="e in entries" :key="e.id" class="log-row" :class="'lv-' + e.level.toLowerCase()">
        <span class="log-ts text-dim mono">{{ formatTs(e.timestamp) }}</span>
        <el-tag :type="levelTag(e.level)" size="small" class="log-level">{{ e.level }}</el-tag>
        <span class="log-cat mono">{{ e.category }}</span>
        <span class="log-msg">{{ e.message }}</span>
        <div v-if="e.detail" class="log-detail mono text-dim">{{ e.detail }}</div>
      </div>
    </div>

    <!-- 分页 -->
    <div class="log-pager">
      <el-pagination
        v-model="page"
        :total="total"
        :page-size="pageSize"
        layout="prev, pager, next"
        small
        @current-change="loadLogs"
      />
    </div>
  </div>
</template>

<style scoped>
.log-view { background: var(--on-bg); }
.log-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.log-list { padding: 4px 0; }
.log-row {
  padding: 4px 12px; border-bottom: 1px solid var(--on-border-light);
  display: grid; grid-template-columns: 140px 70px 80px 1fr;
  gap: 8px; align-items: start; font-size: 12px; line-height: 1.6;
}
.log-row .log-detail {
  grid-column: 4 / 5; margin-top: 2px;
  font-size: 11px; white-space: pre-wrap; word-break: break-all;
  max-height: 120px; overflow-y: auto;
}
.log-ts { font-size: 11px; }
.log-cat { color: var(--on-text-muted); font-size: 11px; }
.log-msg { color: var(--on-text); }
.log-row.lv-error .log-msg { color: var(--on-warn, #f85149); }
.log-row.lv-warning .log-msg { color: var(--on-pending, #d29922); }
.empty-text { text-align: center; padding: 40px; font-size: 13px; }
.log-pager {
  display: flex; justify-content: center;
  padding: 6px; border-top: 1px solid var(--on-border-light);
}
.mono { font-family: 'Consolas', 'Monaco', monospace; }
</style>
