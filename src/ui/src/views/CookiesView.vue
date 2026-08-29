<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Delete, Search, Edit, DocumentCopy, Download, Warning, Check, Clock } from '@element-plus/icons-vue'
import { api } from '../api/client'

const { t } = useI18n()

// ============ 类型定义 ============

interface CookieItem {
  name: string
  value: string
  domain: string
  path: string
  expires: string
  secure: boolean
  httponly: boolean
  samesite: string
  host: string
  source: 'set-cookie' | 'request-cookie'
  flow_id: number
  last_seen: string
}

interface CookieHostGroup {
  host: string
  count: number
  cookies: CookieItem[]
}

interface CookiesResult {
  hosts: CookieHostGroup[]
  total_hosts: number
  total_cookies: number
}

// ============ 响应式状态 ============

const loading = ref(false)
const cookiesData = ref<CookiesResult | null>(null)
const hostFilter = ref('')
const keywordFilter = ref('')
const expandedHosts = ref<Set<string>>(new Set())
const editingCookie = ref<{ host: string; cookie: CookieItem } | null>(null)
const editForm = ref({
  name: '',
  value: '',
  domain: '',
  path: '',
  expires: '',
  samesite: '',
  secure: false,
  httponly: false,
})

// ============ 计算属性 ============

const filteredHosts = computed(() => {
  if (!cookiesData.value?.hosts) return []

  let hosts = cookiesData.value.hosts

  // Host过滤
  if (hostFilter.value) {
    const filter = hostFilter.value.toLowerCase()
    hosts = hosts.filter(h => h.host.toLowerCase().includes(filter))
  }

  // 关键词过滤
  if (keywordFilter.value) {
    const kw = keywordFilter.value.toLowerCase()
    hosts = hosts.map(h => ({
      ...h,
      cookies: h.cookies.filter(c =>
        c.name.toLowerCase().includes(kw) ||
        c.value.toLowerCase().includes(kw)
      )
    })).filter(h => h.cookies.length > 0)
  }

  return hosts
})

const totalCookiesCount = computed(() => {
  return filteredHosts.value.reduce((sum, h) => sum + h.cookies.length, 0)
})

// ============ 工具函数 ============

function formatDate(timestamp: string | number): string {
  if (!timestamp) return '—'
  const date = new Date(Number(timestamp) * 1000)
  if (isNaN(date.getTime())) return '—'
  return date.toLocaleString()
}

function getExpiresClass(expires: string): string {
  if (!expires) return ''
  if (expires.startsWith('max-age=')) {
    const seconds = parseInt(expires.replace('max-age=', ''))
    if (seconds <= 0) return 'expired'
    if (seconds < 86400) return 'expires-soon'
    return ''
  }
  // RFC 2822格式日期
  const date = new Date(expires)
  if (isNaN(date.getTime())) return ''
  if (date < new Date()) return 'expired'
  const diffDays = (date.getTime() - Date.now()) / (1000 * 60 * 60 * 24)
  if (diffDays < 1) return 'expires-soon'
  return ''
}

function parseExpiresDisplay(expires: string): string {
  if (!expires) return t('cookies.session')
  if (expires.startsWith('max-age=')) {
    const seconds = parseInt(expires.replace('max-age=', ''))
    if (seconds <= 0) return t('cookies.expiredDays', { days: Math.abs(seconds) })
    if (seconds < 60) return `${seconds}s`
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
    return `${Math.floor(seconds / 86400)}d`
  }
  return expires
}

function buildCookieString(cookie: CookieItem): string {
  let str = `${cookie.name}=${cookie.value}`
  if (cookie.domain) str += `; Domain=${cookie.domain}`
  if (cookie.path) str += `; Path=${cookie.path}`
  if (cookie.expires && !cookie.expires.startsWith('max-age=')) {
    str += `; Expires=${cookie.expires}`
  }
  if (cookie.secure) str += '; Secure'
  if (cookie.httponly) str += '; HttpOnly'
  if (cookie.samesite) str += `; SameSite=${cookie.samesite}`
  return str
}

// ============ API调用 ============

async function loadCookies() {
  loading.value = true
  try {
    const result = await api.getCookies(hostFilter.value || undefined)
    cookiesData.value = result
  } catch (e: any) {
    ElMessage.error(t('cookies.loadFailed', { msg: e.message }))
  } finally {
    loading.value = false
  }
}

async function deleteHostCookies(host: string) {
  try {
    await ElMessageBox.confirm(
      t('cookies.deleteHostConfirmMsg', { host }),
      t('cookies.deleteHostTitle'),
      { confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel'), type: 'warning' }
    )
    const result = await api.deleteHostCookies(host)
    ElMessage.success(t('cookies.deletedN', { n: result.flows_updated }))
    await loadCookies()
  } catch (e: any) {
    if (e !== 'cancel') {
      ElMessage.error(t('cookies.deleteFailed', { msg: e.message }))
    }
  }
}

async function clearAllCookies() {
  try {
    await ElMessageBox.confirm(
      t('cookies.clearAllConfirmMsg'),
      t('cookies.clearAllTitle'),
      { confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel'), type: 'warning' }
    )
    const result = await api.deleteAllCookies()
    ElMessage.success(t('cookies.deletedN', { n: result.flows_updated }))
    await loadCookies()
  } catch (e: any) {
    if (e !== 'cancel') {
      ElMessage.error(t('cookies.deleteFailed', { msg: e.message }))
    }
  }
}

// ============ UI操作 ============

function toggleHost(host: string) {
  if (expandedHosts.value.has(host)) {
    expandedHosts.value.delete(host)
  } else {
    expandedHosts.value.add(host)
  }
  expandedHosts.value = new Set(expandedHosts.value)
}

function startEdit(cookie: CookieItem) {
  editingCookie.value = { host: cookie.host, cookie }
  editForm.value = {
    name: cookie.name,
    value: cookie.value,
    domain: cookie.domain,
    path: cookie.path,
    expires: cookie.expires,
    samesite: cookie.samesite,
    secure: cookie.secure,
    httponly: cookie.httponly,
  }
}

function cancelEdit() {
  editingCookie.value = null
}

function copyCookie(cookie: CookieItem) {
  const str = buildCookieString(cookie)
  navigator.clipboard.writeText(str).then(() => {
    ElMessage.success(t('cookies.copyFull'))
  }).catch(() => {
    ElMessage.error(t('common.copyFailed'))
  })
}

function copyCookieName(name: string) {
  navigator.clipboard.writeText(name).then(() => {
    ElMessage.success(t('common.copied'))
  }).catch(() => {
    ElMessage.error(t('common.copyFailed'))
  })
}

function copyCookieValue(value: string) {
  navigator.clipboard.writeText(value).then(() => {
    ElMessage.success(t('common.copied'))
  }).catch(() => {
    ElMessage.error(t('common.copyFailed'))
  })
}

function sendToComposer(cookie: CookieItem) {
  // 构建请求字符串
  const requestStr = `GET / HTTP/1.1\r\nHost: ${cookie.host}\r\nCookie: ${cookie.name}=${cookie.value}\r\n\r\n`
  navigator.clipboard.writeText(requestStr).then(() => {
    ElMessage.success(t('cookies.replayToComposer'))
  }).catch(() => {
    ElMessage.error(t('common.copyFailed'))
  })
}

// 导出所有Cookie为curl命令
function exportAllCookies() {
  const lines: string[] = []
  for (const hostGroup of filteredHosts.value) {
    for (const cookie of hostGroup.cookies) {
      const str = buildCookieString(cookie)
      lines.push(`# ${hostGroup.host} - ${cookie.name}`)
      lines.push(`curl -H "Cookie: ${cookie.name}=${cookie.value}" https://${hostGroup.host}/`)
    }
    lines.push('')
  }
  navigator.clipboard.writeText(lines.join('\n')).then(() => {
    ElMessage.success(t('common.exported'))
  }).catch(() => {
    ElMessage.error(t('common.copyFailed'))
  })
}

// ============ 生命周期 ============

onMounted(() => {
  loadCookies()
})
</script>

<template>
  <div class="cookies-view">
    <!-- 头部工具栏 -->
    <div class="cookies-toolbar">
      <div class="toolbar-left">
        <el-button type="primary" :loading="loading" @click="loadCookies">
          <el-icon v-if="!loading"><Refresh /></el-icon>
          {{ t('cookies.refresh') }}
        </el-button>
        <el-button @click="exportAllCookies" :disabled="totalCookiesCount === 0">
          <el-icon><Download /></el-icon>
          {{ t('common.export') }}
        </el-button>
      </div>

      <div class="toolbar-right">
        <el-input
          v-model="hostFilter"
          :placeholder="t('cookies.hostFilterPlaceholder')"
          clearable
          style="width: 220px"
          @input="loadCookies"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>

        <el-input
          v-model="keywordFilter"
          :placeholder="t('cookies.keywordPlaceholder')"
          clearable
          style="width: 180px"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>

        <el-button type="danger" plain @click="clearAllCookies" :disabled="totalCookiesCount === 0">
          <el-icon><Delete /></el-icon>
          {{ t('cookies.clearAll') }}
        </el-button>
      </div>
    </div>

    <!-- 统计信息 -->
    <div class="cookies-stats">
      <div class="stat-item">
        <span class="stat-label">{{ t('cookies.hostsCount') }}</span>
        <span class="stat-value">{{ cookiesData?.total_hosts || 0 }}</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">{{ t('cookies.cookiesCount') }}</span>
        <span class="stat-value">{{ cookiesData?.total_cookies || 0 }}</span>
      </div>
      <div class="stat-item" v-if="keywordFilter">
        <span class="stat-value highlight">{{ filteredHosts.length }}</span>
        <span class="stat-label">{{ t('cookies.hostsCount') }}</span>
        <span class="stat-hint">{{ t('cookies.filteredHint') }}</span>
      </div>
    </div>

    <!-- Cookie列表 -->
    <div class="cookies-content" v-loading="loading">
      <!-- 空状态 -->
      <div class="empty-state" v-if="!loading && filteredHosts.length === 0">
        <el-icon class="empty-icon"><Warning /></el-icon>
        <p>{{ t('cookies.emptyHint') }}</p>
      </div>

      <!-- Host分组 -->
      <div class="host-list" v-else>
        <div
          v-for="hostGroup in filteredHosts"
          :key="hostGroup.host"
          class="host-group"
        >
          <!-- Host头部 -->
          <div
            class="host-header"
            :class="{ expanded: expandedHosts.has(hostGroup.host) }"
            @click="toggleHost(hostGroup.host)"
          >
            <el-icon class="expand-icon">
              <span :class="expandedHosts.has(hostGroup.host) ? 'expanded' : ''">▶</span>
            </el-icon>
            <span class="host-name">{{ hostGroup.host }}</span>
            <span class="cookie-count">{{ hostGroup.cookies.length }}</span>

            <div class="host-actions" @click.stop>
              <el-button
                size="small"
                type="danger"
                plain
                circle
                @click="deleteHostCookies(hostGroup.host)"
                :title="t('cookies.deleteHost')"
              >
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
          </div>

          <!-- Cookie表格 -->
          <div class="cookie-table-wrapper" v-show="expandedHosts.has(hostGroup.host)">
            <table class="cookie-table">
              <thead>
                <tr>
                  <th class="col-name">{{ t('cookies.colName') }}</th>
                  <th class="col-value">{{ t('cookies.colValue') }}</th>
                  <th class="col-domain">{{ t('cookies.colDomain') }}</th>
                  <th class="col-path">{{ t('cookies.colPath') }}</th>
                  <th class="col-expires">{{ t('cookies.colExpires') }}</th>
                  <th class="col-flags">{{ t('cookies.colFlags') }}</th>
                  <th class="col-source">{{ t('cookies.colSource') }}</th>
                  <th class="col-seen">{{ t('cookies.colSeen') }}</th>
                  <th class="col-actions">{{ t('cookies.colActions') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="cookie in hostGroup.cookies" :key="`${cookie.host}-${cookie.name}`">
                  <td class="col-name">
                    <span class="cookie-name" @click="copyCookieName(cookie.name)" title="点击复制">
                      {{ cookie.name }}
                    </span>
                  </td>
                  <td class="col-value">
                    <span class="cookie-value" @click="copyCookieValue(cookie.value)" title="点击复制">
                      {{ cookie.value || t('cookies.emptyValue') }}
                    </span>
                  </td>
                  <td class="col-domain">{{ cookie.domain || hostGroup.host }}</td>
                  <td class="col-path">{{ cookie.path || '/' }}</td>
                  <td class="col-expires">
                    <span :class="['expires-tag', getExpiresClass(cookie.expires)]">
                      <el-icon v-if="getExpiresClass(cookie.expires) === 'expired'" class="expired-icon"><Warning /></el-icon>
                      <el-icon v-else-if="getExpiresClass(cookie.expires) === 'expires-soon'" class="soon-icon"><Clock /></el-icon>
                      {{ parseExpiresDisplay(cookie.expires) }}
                    </span>
                  </td>
                  <td class="col-flags">
                    <span v-if="cookie.secure" class="flag-tag secure">Secure</span>
                    <span v-if="cookie.httponly" class="flag-tag httponly">HttpOnly</span>
                    <span v-if="cookie.samesite" class="flag-tag samesite">{{ cookie.samesite }}</span>
                  </td>
                  <td class="col-source">
                    <el-tag size="small" :type="cookie.source === 'set-cookie' ? 'success' : 'info'">
                      {{ cookie.source === 'set-cookie' ? t('cookies.sourceSetCookie') : t('cookies.sourceRequest') }}
                    </el-tag>
                  </td>
                  <td class="col-seen">{{ formatDate(cookie.last_seen) }}</td>
                  <td class="col-actions">
                    <div class="action-buttons">
                      <el-button size="small" circle @click="startEdit(cookie)" :title="t('cookies.edit')">
                        <el-icon><Edit /></el-icon>
                      </el-button>
                      <el-button size="small" circle @click="copyCookie(cookie)" :title="t('cookies.copyFull')">
                        <el-icon><DocumentCopy /></el-icon>
                      </el-button>
                      <el-button size="small" circle @click="sendToComposer(cookie)" :title="t('cookies.toComposer')">
                        <el-icon><Download /></el-icon>
                      </el-button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- 编辑对话框 -->
    <el-dialog
      :model-value="editingCookie !== null"
      :title="t('cookies.editCookie')"
      width="600px"
      @close="cancelEdit"
    >
      <el-form label-width="100px" v-if="editingCookie">
        <el-form-item :label="t('cookies.fieldName')">
          <el-input v-model="editForm.name" />
        </el-form-item>
        <el-form-item :label="t('cookies.fieldValue')">
          <el-input v-model="editForm.value" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item :label="t('cookies.fieldDomain')">
          <el-input v-model="editForm.domain" />
        </el-form-item>
        <el-form-item :label="t('cookies.fieldPath')">
          <el-input v-model="editForm.path" />
        </el-form-item>
        <el-form-item :label="t('cookies.fieldExpires')">
          <el-input v-model="editForm.expires" :placeholder="t('cookies.selectExpires')" />
        </el-form-item>
        <el-form-item :label="t('cookies.fieldSamesite')">
          <el-select v-model="editForm.samesite" clearable style="width: 100%">
            <el-option label="Strict" value="Strict" />
            <el-option label="Lax" value="Lax" />
            <el-option label="None" value="None" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="editForm.secure">{{ t('cookies.flagSecure') }}</el-checkbox>
          <el-checkbox v-model="editForm.httponly">{{ t('cookies.flagHttpOnly') }}</el-checkbox>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="cancelEdit">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" @click="cancelEdit">{{ t('common.confirm') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.cookies-view {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: var(--on-bg);
  height: 100%;
  overflow: hidden;
}

.cookies-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.cookies-stats {
  display: flex;
  gap: 24px;
  padding: 12px 16px;
  background: var(--on-bg-card);
  border-radius: var(--on-radius);
  border: 1px solid var(--on-border-light);
}

.stat-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.stat-label {
  color: var(--el-text-color-secondary, #888);
  font-size: 13px;
}

.stat-value {
  color: var(--el-text-color-primary, #eee);
  font-size: 14px;
  font-weight: 600;
}

.stat-value.highlight {
  color: var(--on-accent);
}

.stat-hint {
  color: var(--el-text-color-secondary, #888);
  font-size: 12px;
  font-style: italic;
}

.cookies-content {
  flex: 1;
  overflow: auto;
  background: var(--on-bg-card);
  border-radius: var(--on-radius);
  border: 1px solid var(--on-border-light);
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px 20px;
  color: var(--el-text-color-secondary, #888);
}

.empty-icon {
  font-size: 48px;
  margin-bottom: 16px;
  opacity: 0.5;
}

.host-list {
  display: flex;
  flex-direction: column;
}

.host-group {
  border-bottom: 1px solid var(--on-border-light);
}

.host-group:last-child {
  border-bottom: none;
}

.host-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  cursor: pointer;
  transition: background 0.15s;
}

.host-header:hover {
  background: var(--on-bg-hover);
}

.host-header.expanded {
  background: var(--on-bg-hover);
}

.expand-icon {
  color: var(--el-text-color-secondary, #888);
  transition: transform 0.2s;
}

.expand-icon span.expanded {
  transform: rotate(90deg);
  display: inline-block;
}

.host-name {
  flex: 1;
  font-weight: 600;
  color: var(--el-text-color-primary, #eee);
  font-size: 14px;
}

.cookie-count {
  background: var(--on-accent-dim);
  color: var(--on-accent);
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 500;
}

.host-actions {
  display: flex;
  gap: 8px;
  opacity: 0;
  transition: opacity 0.15s;
}

.host-header:hover .host-actions {
  opacity: 1;
}

.cookie-table-wrapper {
  overflow-x: auto;
}

.cookie-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.cookie-table thead {
  background: var(--on-bg-hover);
  position: sticky;
  top: 0;
  z-index: 1;
}

.cookie-table th {
  padding: 10px 12px;
  text-align: left;
  font-weight: 600;
  color: var(--el-text-color-secondary, #888);
  text-transform: uppercase;
  font-size: 11px;
  white-space: nowrap;
  border-bottom: 1px solid var(--on-border-light);
}

.cookie-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--on-border-light);
  vertical-align: middle;
}

.cookie-table tbody tr:hover {
  background: var(--on-bg-hover);
}

.cookie-table tbody tr:last-child td {
  border-bottom: none;
}

.col-name { min-width: 120px; max-width: 200px; }
.col-value { min-width: 150px; max-width: 300px; }
.col-domain { min-width: 100px; }
.col-path { min-width: 60px; }
.col-expires { min-width: 100px; }
.col-flags { min-width: 120px; }
.col-source { min-width: 90px; }
.col-seen { min-width: 140px; color: var(--el-text-color-secondary, #888); }
.col-actions { min-width: 100px; }

.cookie-name {
  cursor: pointer;
  color: var(--on-accent);
  font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
}

.cookie-name:hover {
  text-decoration: underline;
}

.cookie-value {
  cursor: pointer;
  font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: block;
}

.cookie-value:hover {
  text-decoration: underline;
}

.expires-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 11px;
  background: var(--on-bg-hover);
  color: var(--el-text-color-secondary, #888);
}

.expires-tag.expired {
  background: rgba(245, 108, 108, 0.15);
  color: #f56c6c;
}

.expires-tag.expires-soon {
  background: rgba(230, 162, 60, 0.15);
  color: #e6a23c;
}

.expired-icon,
.soon-icon {
  font-size: 12px;
}

.flag-tag {
  display: inline-block;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 10px;
  margin-right: 4px;
  background: var(--on-bg-hover);
  color: var(--el-text-color-secondary, #888);
}

.flag-tag.secure {
  background: rgba(64, 158, 255, 0.15);
  color: #409eff;
}

.flag-tag.httponly {
  background: rgba(103, 194, 58, 0.15);
  color: #67c23a;
}

.flag-tag.samesite {
  background: rgba(144, 147, 153, 0.15);
  color: #909399;
}

.action-buttons {
  display: flex;
  gap: 4px;
}

/* 滚动条样式 */
.cookies-content::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

.cookies-content::-webkit-scrollbar-track {
  background: var(--on-bg);
}

.cookies-content::-webkit-scrollbar-thumb {
  background: var(--on-border);
  border-radius: 4px;
}

.cookies-content::-webkit-scrollbar-thumb:hover {
  background: var(--on-border-light);
}
</style>
