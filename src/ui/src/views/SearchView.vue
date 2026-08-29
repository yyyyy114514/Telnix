<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { api, type Flow } from '../api/client'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import { useSearchHistoryStore } from '../stores/searchHistory'
import { useRegexTemplate } from '../stores/regexTemplate'
import PreviewView from '../components/PreviewView.vue'
import TextSearch from '../components/TextSearch.vue'
import ImportButton from '../components/ImportButton.vue'

// 跨流量搜索页：
// - 搜索结果单击右侧显示响应 Preview 或 请求 Raw（带高亮和搜索），双击跳转抓包页面
// - 右侧详情右上角有关闭按钮
const { t } = useI18n()
const router = useRouter()
const capture = useCaptureStore()
const flowsStore = useFlowsStore()
const searchHistory = useSearchHistoryStore()
const { validateRegex, templates: regexTemplates } = useRegexTemplate()

// ============ 搜索条件 ============
const bodyRegex = ref('')
const binaryHex = ref('')
const searchAllSessions = ref(true) // 默认跨会话
const searchResults = ref<Flow[]>([])
const searching = ref(false)

// ============ 正则表达式验证 ============
// 主正则输入验证
const bodyRegexError = ref('')
const headerRegexError = ref('')

// 验证主正则表达式
function validateBodyRegex() {
  if (!bodyRegex.value.trim()) {
    bodyRegexError.value = ''
    return true
  }
  const result = validateRegex(bodyRegex.value)
  bodyRegexError.value = result.valid ? '' : (result.error || '无效的正则表达式')
  return result.valid
}

// 验证头部正则表达式
function validateHeaderRegex() {
  if (!filterHeaderRegex.value.trim()) {
    headerRegexError.value = ''
    return true
  }
  const result = validateRegex(filterHeaderRegex.value)
  headerRegexError.value = result.valid ? '' : (result.error || '无效的正则表达式')
  return result.valid
}

// ============ 正则模板下拉 ============
const showRegexTemplates = ref(false)

function applyRegexTemplate(pattern: string) {
  bodyRegex.value = pattern
  bodyRegexError.value = ''
  showRegexTemplates.value = false
}

// ============ 历史记录下拉 ============
const showHistory = ref(false)
const historyDropdownRef = ref<HTMLElement | null>(null)

// 格式化历史时间
function formatHistoryTime(timestamp: number): string {
  const now = Date.now()
  const diff = now - timestamp
  const minute = 60 * 1000
  const hour = 60 * minute
  const day = 24 * hour

  if (diff < minute) return '刚刚'
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`
  if (diff < 7 * day) return `${Math.floor(diff / day)} 天前`
  return new Date(timestamp).toLocaleDateString('zh-CN')
}

// 点击历史记录，回填搜索条件
function applyHistory(historyId: string) {
  const item = searchHistory.getHistory(historyId)
  if (!item) return

  bodyRegex.value = item.bodyRegex
  binaryHex.value = item.binaryHex
  filterMethod.value = item.filterMethod
  filterStatusCode.value = item.filterStatusCode
  filterStatusMin.value = item.filterStatusMin
  filterStatusMax.value = item.filterStatusMax
  filterHost.value = item.filterHost
  filterPid.value = item.filterPid
  filterProcess.value = item.filterProcess
  filterHeaderRegex.value = item.filterHeaderRegex

  // 验证回填的正则
  validateBodyRegex()
  validateHeaderRegex()

  showHistory.value = false
  ElMessage.success(t('search.historyApplied'))
}

// 删除单条历史
function deleteHistory(e: Event, historyId: string) {
  e.stopPropagation()
  searchHistory.removeHistory(historyId)
}

// 清空全部历史
function clearAllHistory() {
  searchHistory.clearHistory()
  showHistory.value = false
  ElMessage.success(t('search.historyCleared'))
}

// 关闭历史下拉（点击外部）
function handleClickOutside(e: MouseEvent) {
  if (historyDropdownRef.value && !historyDropdownRef.value.contains(e.target as Node)) {
    showHistory.value = false
  }
}

// ============ 高级筛选条件 ============
const showAdvanced = ref(false)
const filterMethod = ref('')        // GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS
const filterStatusCode = ref<number | null>(null)  // 精确状态码
const filterStatusMin = ref<number | null>(null)   // 状态码区间起
const filterStatusMax = ref<number | null>(null)   // 状态码区间止
const filterHost = ref('')          // host 子串
const filterPid = ref<number | null>(null)
const filterProcess = ref('')
const filterHeaderRegex = ref('')

// ============ 详情相关 ============
type DetailMode = 'response' | 'request'
const detailMode = ref<DetailMode>('response')
const selectedFlowId = ref<number | null>(null)
const selectedFlow = computed(() => searchResults.value.find(f => f.id === selectedFlowId.value) || null)

// ============ 搜索结果上下文高亮 ============
// 匹配信息气泡
const highlightTooltip = ref<{ visible: boolean; x: number; y: number; text: string; type: string }>({
  visible: false, x: 0, y: 0, text: '', type: '',
})

// 搜索请求令牌：丢弃过期结果，避免快速多次搜索时旧请求覆盖新结果
let searchToken = 0

async function onSearch() {
  // 校验：至少一个条件
  const hasAny = bodyRegex.value.trim() || binaryHex.value.trim()
    || filterHeaderRegex.value.trim() || filterMethod.value
    || filterStatusCode.value !== null || filterPid.value !== null
    || filterProcess.value.trim() || filterHost.value.trim()
    || filterStatusMin.value !== null || filterStatusMax.value !== null
  if (!hasAny) {
    ElMessage.warning(t('search.inputRequired'))
    return
  }

  // 验证正则表达式
  if (!validateBodyRegex()) {
    ElMessage.warning(t('search.invalidRegex', { field: t('search.bodyRegex') }))
    return
  }
  if (!validateHeaderRegex()) {
    ElMessage.warning(t('search.invalidRegex', { field: t('search.headerRegex') }))
    return
  }

  // 跨会话模式用 0，否则用当前会话 id
  const sid = searchAllSessions.value ? 0 : (capture.status.session_id || 0)
  if (!searchAllSessions.value && !sid) {
    ElMessage.warning(t('search.noActiveSession'))
    return
  }

  const myToken = ++searchToken
  searching.value = true
  selectedFlowId.value = null
  try {
    const r = await api.searchFlows({
      session_id: sid,
      body_regex: bodyRegex.value.trim() || undefined,
      binary_hex: binaryHex.value.trim() || undefined,
      header_regex: filterHeaderRegex.value.trim() || undefined,
      method: filterMethod.value || undefined,
      status_code: filterStatusCode.value ?? undefined,
      pid: filterPid.value ?? undefined,
      process_name: filterProcess.value.trim() || undefined,
      host: filterHost.value.trim() || undefined,
      status_min: filterStatusMin.value ?? undefined,
      status_max: filterStatusMax.value ?? undefined,
      limit: 500,
    })

    // 已发起新搜索，丢弃过期结果
    if (myToken !== searchToken) return
    searchResults.value = r.matches || []

    // 保存到搜索历史
    if (searchResults.value.length > 0) {
      searchHistory.addHistory({
        bodyRegex: bodyRegex.value,
        binaryHex: binaryHex.value,
        filterMethod: filterMethod.value,
        filterStatusCode: filterStatusCode.value,
        filterStatusMin: filterStatusMin.value,
        filterStatusMax: filterStatusMax.value,
        filterHost: filterHost.value,
        filterPid: filterPid.value,
        filterProcess: filterProcess.value,
        filterHeaderRegex: filterHeaderRegex.value,
      })
    }

    ElMessage.success(t('search.matchesFound', { count: r.count }))
  } catch (e: any) {
    if (myToken !== searchToken) return
    ElMessage.error(t('search.searchFailed') + (e?.message || e))
  } finally {
    if (myToken === searchToken) {
      searching.value = false
    }
  }
}

// 重置所有筛选条件
function onReset() {
  bodyRegex.value = ''
  binaryHex.value = ''
  filterMethod.value = ''
  filterStatusCode.value = null
  filterStatusMin.value = null
  filterStatusMax.value = null
  filterHost.value = ''
  filterPid.value = null
  filterProcess.value = ''
  filterHeaderRegex.value = ''
  bodyRegexError.value = ''
  headerRegexError.value = ''
}

function formatTime(ts: string): string {
  if (!ts) return ''
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
}

// 单击：选中显示详情
function onResultClick(f: Flow) {
  selectedFlowId.value = f.id
}

// 双击：跳转抓包页面并选中（用 selectFlow 注入 flow 对象，不依赖 store 列表查找）
function onResultDblClick(f: Flow) {
  flowsStore.selectFlow(f)
  flowsStore.aiFlowIds = []
  router.push('/capture')
}

// 关闭详情面板
function closeDetail() {
  selectedFlowId.value = null
}

// 从 response_headers 提取 Content-Type
function extractContentType(f: Flow | null): string {
  if (!f) return ''
  try {
    const obj = JSON.parse(f.response_headers || '{}')
    for (const k of Object.keys(obj)) {
      if (k.toLowerCase() === 'content-type') return String(obj[k])
    }
  } catch { /* ignore */ }
  return ''
}

// Content-Type 简短显示（如 application/json → json，text/html → html）
function contentTypeShort(f: Flow): string {
  const ct = extractContentType(f)
  if (!ct) return '-'
  // 取分号前的主类型，再取 / 后的子类型
  const main = ct.split(';')[0].trim()
  const slashIdx = main.indexOf('/')
  return slashIdx >= 0 ? main.substring(slashIdx + 1) : main
}

// 请求 Raw 文本
const requestRaw = computed(() => {
  const f = selectedFlow.value
  if (!f) return ''
  const headers = f.request_headers || '{}'
  let headerStr = ''
  try {
    const obj = JSON.parse(headers)
    headerStr = Object.entries(obj).map(([k, v]) => `${k}: ${v}`).join('\n')
  } catch {
    headerStr = headers
  }
  const line = `${f.method} ${f.url} HTTP/1.1`
  const body = f.request_body || ''
  return body ? `${line}\n${headerStr}\n\n${body}` : `${line}\n${headerStr}`
})

const responseContentType = computed(() => extractContentType(selectedFlow.value))

// ============ 高亮匹配字段 ============
// 获取高亮样式类（根据当前搜索条件判断哪些字段匹配）
function getHighlightClass(f: Flow, field: string): string {
  const classes: string[] = []

  // host 匹配
  if (field === 'host' && filterHost.value && f.host) {
    if (f.host.toLowerCase().includes(filterHost.value.toLowerCase())) {
      classes.push('match-host')
    }
  }

  // method 匹配
  if (field === 'method' && filterMethod.value && f.method) {
    if (f.method === filterMethod.value) {
      classes.push('match-method')
    }
  }

  // status 匹配
  if (field === 'status' && (filterStatusCode.value !== null || filterStatusMin.value !== null)) {
    if (filterStatusCode.value !== null && f.status_code === filterStatusCode.value) {
      classes.push('match-status')
    }
    if (filterStatusMin.value !== null && filterStatusMax.value !== null) {
      if (f.status_code !== null && f.status_code >= filterStatusMin.value && f.status_code <= filterStatusMax.value) {
        classes.push('match-status')
      }
    }
  }

  return classes.join(' ')
}

// hover 高亮气泡
function showHighlightTip(e: MouseEvent, type: string) {
  highlightTooltip.value = {
    visible: true,
    x: e.clientX + 10,
    y: e.clientY + 10,
    text: getMatchDescription(type),
    type,
  }
}

function hideHighlightTip() {
  highlightTooltip.value.visible = false
}

function getMatchDescription(type: string): string {
  switch (type) {
    case 'host': return `${t('search.matched')}: Host "${filterHost.value}"`
    case 'method': return `${t('search.matched')}: Method "${filterMethod.value}"`
    case 'status': return `${t('search.matched')}: Status ${filterStatusCode.value || `${filterStatusMin.value}-${filterStatusMax.value}`}`
    case 'body': return `${t('search.matched')}: Body Regex "${bodyRegex.value}"`
    case 'header': return `${t('search.matched')}: Header Regex "${filterHeaderRegex.value}"`
    default: return t('search.matched')
  }
}

// 点击搜索框时显示历史
function onSearchInputFocus() {
  if (searchHistory.history.length > 0) {
    showHistory.value = true
  }
}

// 键盘事件：上下箭头选择历史
function onSearchInputKeydown(e: KeyboardEvent) {
  if (!showHistory.value || searchHistory.history.length === 0) return

  if (e.key === 'ArrowDown') {
    e.preventDefault()
    // 选择下一条历史
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    // 选择上一条历史
  } else if (e.key === 'Escape') {
    showHistory.value = false
  }
}

onMounted(() => {
  // 进入页面即可输入搜索
  document.addEventListener('click', handleClickOutside)
})
</script>

<template>
  <div class="search-view-page full flex flex-col">
    <!-- 搜索表单区域 -->
    <div class="search-form">
      <!-- 主搜索框 + 历史记录下拉 -->
      <div class="search-input-group" ref="historyDropdownRef">
        <el-input
          v-model="bodyRegex"
          :placeholder="t('search.regexPlaceholder')"
          size="small"
          clearable
          style="flex: 1; min-width: 260px"
          :class="{ 'regex-error': bodyRegexError }"
          v-code-assist
          @keyup.enter="onSearch"
          @focus="onSearchInputFocus"
          @blur="validateBodyRegex"
        >
          <template #suffix>
            <el-dropdown v-if="regexTemplates.length > 0" trigger="click" @command="applyRegexTemplate" @visible-change="(v: boolean) => showRegexTemplates = v">
              <el-tooltip :content="t('search.regexTemplates')" placement="bottom">
                <el-icon class="cursor-pointer"><Document /></el-icon>
              </el-tooltip>
              <template #dropdown>
                <el-dropdown-menu class="regex-template-menu">
                  <div class="regex-template-header">{{ t('search.regexTemplates') }}</div>
                  <el-dropdown-item
                    v-for="tmpl in regexTemplates"
                    :key="tmpl.name"
                    :command="tmpl.pattern"
                    :title="tmpl.description"
                  >
                    <div class="regex-template-item">
                      <span class="regex-template-name">{{ tmpl.name }}</span>
                      <span class="regex-template-pattern">{{ tmpl.pattern }}</span>
                    </div>
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-input>

        <!-- 正则验证错误提示 -->
        <div v-if="bodyRegexError" class="regex-error-tip">
          <el-icon><WarningFilled /></el-icon>
          {{ bodyRegexError }}
        </div>

        <!-- 历史记录下拉 -->
        <transition name="el-fade-in">
          <div v-if="showHistory && searchHistory.history.length > 0" class="history-dropdown">
            <div class="history-header">
              <span class="history-title">{{ t('search.searchHistory') }}</span>
              <el-button size="small" text type="danger" @click="clearAllHistory">
                {{ t('search.clearHistory') }}
              </el-button>
            </div>
            <div class="history-list">
              <div
                v-for="item in searchHistory.history"
                :key="item.id"
                class="history-item"
                @click="applyHistory(item.id)"
              >
                <div class="history-content">
                  <span class="history-text">{{ item.displayText }}</span>
                  <span class="history-time">{{ formatHistoryTime(item.timestamp) }}</span>
                </div>
                <el-button
                  size="small"
                  text
                  circle
                  class="history-delete"
                  @click="(e: Event) => deleteHistory(e, item.id)"
                >
                  <el-icon><Close /></el-icon>
                </el-button>
              </div>
            </div>
          </div>
        </transition>
      </div>

      <el-input
        v-model="binaryHex"
        :placeholder="t('search.hexPlaceholder')"
        size="small"
        style="width: 220px"
        clearable
        @keyup.enter="onSearch"
      />
      <el-button size="small" :type="showAdvanced ? 'primary' : 'default'" @click="showAdvanced = !showAdvanced">
        <el-icon><Filter /></el-icon>&nbsp;{{ t('search.advanced') }}
      </el-button>
      <el-checkbox v-model="searchAllSessions" size="small">{{ t('search.allSessions') }}</el-checkbox>

      <!-- 历史按钮 -->
      <el-tooltip :content="t('search.searchHistory')" placement="bottom">
        <el-button
          size="small"
          circle
          @click="showHistory = !showHistory"
          :type="showHistory ? 'primary' : 'default'"
        >
          <el-icon><Clock /></el-icon>
        </el-button>
      </el-tooltip>

      <el-button type="primary" size="small" :loading="searching" @click="onSearch">
        <el-icon><Search /></el-icon>&nbsp;{{ t('search.searchButton') }}
      </el-button>
      <el-button size="small" @click="onReset" :title="t('common.reset')">
        <el-icon><RefreshLeft /></el-icon>&nbsp;{{ t('common.reset') }}
      </el-button>
      <div class="flex-1"></div>
      <ImportButton />
    </div>

    <!-- 高级筛选条件（折叠面板） -->
    <el-collapse-transition>
      <div v-show="showAdvanced" class="advanced-filters">
        <div class="filter-row">
          <span class="filter-label">{{ t('common.method') }}</span>
          <el-select v-model="filterMethod" size="small" clearable style="width: 120px" :placeholder="t('search.any')">
            <el-option label="GET" value="GET" />
            <el-option label="POST" value="POST" />
            <el-option label="PUT" value="PUT" />
            <el-option label="DELETE" value="DELETE" />
            <el-option label="PATCH" value="PATCH" />
            <el-option label="HEAD" value="HEAD" />
            <el-option label="OPTIONS" value="OPTIONS" />
          </el-select>
          <span class="filter-label">{{ t('search.statusCode') }}</span>
          <el-input-number v-model="filterStatusCode" size="small" :min="100" :max="599" :placeholder="t('search.any')" style="width: 110px" />
          <span class="filter-label">{{ t('search.statusRange') }}</span>
          <el-input-number v-model="filterStatusMin" size="small" :min="100" :max="599" :placeholder="t('search.min')" style="width: 90px" />
          <span class="filter-sep">-</span>
          <el-input-number v-model="filterStatusMax" size="small" :min="100" :max="599" :placeholder="t('search.max')" style="width: 90px" />
          <span class="filter-label">{{ t('common.host') }}</span>
          <el-input v-model="filterHost" size="small" :placeholder="t('search.hostPlaceholder')" clearable style="width: 180px" />
        </div>
        <div class="filter-row">
          <span class="filter-label">PID</span>
          <el-input-number v-model="filterPid" size="small" :min="0" :placeholder="t('search.any')" style="width: 120px" />
          <span class="filter-label">{{ t('search.process') }}</span>
          <el-input v-model="filterProcess" size="small" :placeholder="t('search.processPlaceholder')" clearable style="width: 180px" />
          <span class="filter-label">{{ t('search.headerRegex') }}</span>
          <el-input
            v-model="filterHeaderRegex"
            size="small"
            :placeholder="t('search.headerRegexPlaceholder')"
            clearable
            style="flex: 1; min-width: 240px"
            :class="{ 'regex-error': headerRegexError }"
            v-code-assist
            @blur="validateHeaderRegex"
          />
          <!-- 头部正则验证错误 -->
          <div v-if="headerRegexError" class="regex-error-tip small">
            <el-icon><WarningFilled /></el-icon>
            {{ headerRegexError }}
          </div>
        </div>
      </div>
    </el-collapse-transition>

    <!-- 主体：左侧结果列表 + 右侧详情 -->
    <div class="search-body flex overflow-hidden">
      <div class="result-list">
        <div v-if="!searchResults.length" class="empty-text text-dim">
          {{ t('search.emptyHint') }}<br />
          <span style="font-size: 11px">{{ t('search.emptyHintSub') }}</span>
        </div>
        <div
          v-for="f in searchResults"
          :key="f.id"
          class="sr-row mono"
          :class="{ selected: f.id === selectedFlowId }"
          @click="onResultClick(f)"
          @dblclick="onResultDblClick(f)"
        >
          <span class="sr-id text-dim">#{{ f.id }}</span>
          <span
            class="sr-method"
            :class="['m-' + (f.method || '').toLowerCase(), getHighlightClass(f, 'method')]"
            @mouseenter="(e) => filterMethod && showHighlightTip(e, 'method')"
            @mouseleave="hideHighlightTip"
          >{{ f.method }}</span>
          <span
            class="sr-host text-truncate"
            :class="getHighlightClass(f, 'host')"
            @mouseenter="(e) => filterHost && showHighlightTip(e, 'host')"
            @mouseleave="hideHighlightTip"
          >{{ f.host }}{{ f.path }}</span>
          <span class="sr-ctype text-dim text-truncate">{{ contentTypeShort(f) }}</span>
          <span
            class="sr-code"
            :class="[f.status_code && f.status_code < 400 ? 'ok' : 'err', getHighlightClass(f, 'status')]"
            @mouseenter="(e) => (filterStatusCode !== null || filterStatusMin !== null) && showHighlightTip(e, 'status')"
            @mouseleave="hideHighlightTip"
          >{{ f.status_code }}</span>
          <span class="sr-time text-muted">{{ formatTime(f.timestamp) }}</span>
        </div>
      </div>

      <div class="detail-pane">
        <div v-if="!selectedFlow" class="empty-text text-dim">
          {{ t('search.detailEmptyHint') }}
        </div>
        <template v-else>
          <!-- 顶部条：模式切换 + 标题 + 关闭按钮 -->
          <div class="detail-header">
            <el-radio-group v-model="detailMode" size="small">
              <el-radio-button value="response">{{ t('search.responsePreview') }}</el-radio-button>
              <el-radio-button value="request">{{ t('search.requestRaw') }}</el-radio-button>
            </el-radio-group>
            <span class="detail-title text-dim">
              #{{ selectedFlow.id }} {{ selectedFlow.method }} {{ selectedFlow.host }}{{ selectedFlow.path }}
            </span>
            <div class="flex-1"></div>
            <el-button size="small" circle plain @click="closeDetail" :title="t('search.close')">
              <el-icon><Close /></el-icon>
            </el-button>
          </div>
          <!-- 请求 Raw -->
          <div v-if="detailMode === 'request'" class="detail-content">
            <TextSearch :text="requestRaw" searchable language="http" class="detail-code-wrap" />
          </div>
          <!-- 响应 Preview -->
          <div v-else class="detail-content">
            <PreviewView :body="selectedFlow.response_body || ''" :content-type="responseContentType" />
          </div>
        </template>
      </div>
    </div>

    <!-- 匹配信息气泡 -->
    <teleport to="body">
      <transition name="el-fade-in">
        <div
          v-if="highlightTooltip.visible"
          class="highlight-tooltip"
          :style="{ left: highlightTooltip.x + 'px', top: highlightTooltip.y + 'px' }"
        >
          {{ highlightTooltip.text }}
        </div>
      </transition>
    </teleport>
  </div>
</template>

<style scoped>
.search-view-page { background: var(--on-bg); padding: 12px; }

.search-form {
  display: flex; align-items: flex-start; gap: 8px; flex-wrap: wrap;
  padding-bottom: 10px; border-bottom: 1px solid var(--on-border-light);
  position: relative;
}

/* 搜索输入组 */
.search-input-group {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 260px;
  max-width: 400px;
}

/* 正则表达式验证错误样式 */
.regex-error :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--on-error, #f56c6c) inset !important;
}

.regex-error-tip {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--on-error, #f56c6c);
  margin-top: 2px;
  padding-left: 4px;
}

.regex-error-tip.small {
  font-size: 10px;
  white-space: nowrap;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 正则模板下拉 */
.regex-template-menu {
  max-height: 320px;
  overflow-y: auto;
}

.regex-template-header {
  padding: 8px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--on-text-dim);
  border-bottom: 1px solid var(--on-border-light);
}

.regex-template-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px 0;
}

.regex-template-name {
  font-size: 13px;
  color: var(--on-text);
}

.regex-template-pattern {
  font-size: 11px;
  color: var(--on-text-dim);
  font-family: var(--on-font-mono, monospace);
  word-break: break-all;
}

/* 历史记录下拉 */
.history-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  z-index: 100;
  min-width: 320px;
  max-width: 500px;
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-border, #333344);
  border-radius: var(--on-radius-md, 6px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
  margin-top: 4px;
}

.history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--on-border-light);
}

.history-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--on-text-dim);
}

.history-list {
  max-height: 300px;
  overflow-y: auto;
}

.history-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  cursor: pointer;
  transition: background 0.15s ease;
  border-bottom: 1px solid var(--on-border-light);
}

.history-item:last-child {
  border-bottom: none;
}

.history-item:hover {
  background: var(--on-bg-hover, #2a2a3e);
}

.history-content {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.history-text {
  font-size: 12px;
  color: var(--on-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.history-time {
  font-size: 10px;
  color: var(--on-text-dim);
}

.history-delete {
  opacity: 0;
  transition: opacity 0.15s ease;
  flex-shrink: 0;
  margin-left: 8px;
}

.history-item:hover .history-delete {
  opacity: 1;
}

/* 匹配高亮样式 */
:deep(.match-host) {
  background: rgba(45, 212, 191, 0.2);
  border-radius: 2px;
}

:deep(.match-method) {
  background: rgba(45, 212, 191, 0.2);
  border-radius: 2px;
}

:deep(.match-status) {
  background: rgba(45, 212, 191, 0.2);
  border-radius: 2px;
}

/* 高亮气泡 */
.highlight-tooltip {
  position: fixed;
  z-index: 9999;
  padding: 6px 10px;
  background: var(--on-bg-elevated, #1e1e2e);
  border: 1px solid var(--on-accent, #2dd4bf);
  border-radius: 4px;
  font-size: 12px;
  color: var(--on-accent, #2dd4bf);
  pointer-events: none;
  white-space: nowrap;
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}

/* 高级筛选条件 */
.advanced-filters {
  padding: 10px 12px;
  background: var(--on-bg-elevated);
  border-bottom: 1px solid var(--on-border-light);
  display: flex; flex-direction: column; gap: 8px;
}

.filter-row {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}

.filter-label {
  font-size: 12px; color: var(--on-text-dim);
  white-space: nowrap; min-width: 50px;
}

.filter-sep { color: var(--on-text-dim); }

/* 搜索结果区域 */
.search-body { flex: 1; min-height: 0; padding-top: 8px; }
.result-list {
  width: 50%; overflow-y: auto;
  border-right: 1px solid var(--on-border-light);
}

.sr-row {
  display: grid;
  grid-template-columns: 50px 54px 1fr 70px 44px 64px;
  gap: 6px; padding: 5px 8px; font-size: 12px;
  border-bottom: 1px solid var(--on-border-light);
  cursor: pointer;
  transition: background .1s ease;
}

.sr-row:hover { background: var(--on-bg-hover); }
.sr-row.selected { background: rgba(64, 158, 255, 0.18); }
.sr-method { font-weight: 700; }
.m-get { color: var(--on-ok); }
.m-post { color: var(--on-redirect); }
.m-put { color: var(--on-warn); }
.m-delete { color: var(--on-error); }
.sr-ctype { font-size: 11px; }
.sr-code.ok { color: var(--on-ok); }
.sr-code.err { color: var(--on-error); }
.text-truncate { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* 详情面板 */
.detail-pane {
  flex: 1; min-width: 0; overflow: hidden;
  display: flex; flex-direction: column;
}
.detail-header {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.detail-title {
  font-size: 12px; font-family: var(--on-font-mono);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.detail-content { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.detail-code-wrap {
  flex: 1; height: 0;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
}

/* 通用样式 */
.empty-text { text-align: center; padding: 40px; line-height: 1.8; }
.cursor-pointer { cursor: pointer; }
</style>
