<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile, UploadFiles } from 'element-plus'
import { Delete, Plus, Refresh, Download, Upload, UploadFilled, QuestionFilled, Clock, DataLine } from '@element-plus/icons-vue'
import { api } from '../api/client'

// 代理工具页：No Caching / Force CORS / Block List / Allow List / Map Local / Map Remote / Mirror
const { t } = useI18n()

interface MapLocalRule {
  pattern: string; mode: string; file_path: string;
  status?: number; content_type?: string;
  headers?: Record<string, string>
}
interface MapRemoteRule {
  pattern: string; mode: string; target_url: string;
  headers?: Record<string, string>
}
interface MirrorRule {
  pattern: string; mode: string; save_dir: string
}
interface HitStats {
  hit_count: number;
  last_hits: Array<{ ts: number; url: string; resolved_path: string }>;
}
interface ToolConfig {
  no_caching: boolean
  force_cors: boolean
  block_list_enabled: boolean
  block_list: Array<{ pattern: string; mode: string } | string>
  allow_list_enabled: boolean
  allow_list: Array<{ pattern: string; mode: string } | string>
  map_local_enabled: boolean
  map_local_rules: MapLocalRule[]
  map_remote_enabled: boolean
  map_remote_rules: MapRemoteRule[]
  mirror_enabled: boolean
  mirror_rules: MirrorRule[]
}

const config = ref<ToolConfig>({
  no_caching: false,
  force_cors: false,
  block_list_enabled: false,
  block_list: [],
  allow_list_enabled: false,
  allow_list: [],
  map_local_enabled: false,
  map_local_rules: [],
  map_remote_enabled: false,
  map_remote_rules: [],
  mirror_enabled: false,
  mirror_rules: [],
})
const loading = ref(false)
const hitStats = ref<Record<string, Record<string, HitStats>>>({
  map_local_rules: {},
  map_remote_rules: {},
})

// 展开/收起命中文档
const expandedStats = ref<Record<string, boolean>>({})

// 规则添加表单
const newBlockPattern = ref('')
const newBlockMode = ref('wildcard')
const newAllowPattern = ref('')
const newAllowMode = ref('wildcard')
const newMapLocalPattern = ref('')
const newMapLocalMode = ref('wildcard')
const newMapLocalFilePath = ref('')
const newMapLocalStatus = ref<number | undefined>(undefined)
const newMapLocalHeaderKey = ref('')
const newMapLocalHeaderVal = ref('')
const newMapRemotePattern = ref('')
const newMapRemoteMode = ref('wildcard')
const newMapRemoteTargetUrl = ref('')
const newMapRemoteHeaderKey = ref('')
const newMapRemoteHeaderVal = ref('')
const newMirrorPattern = ref('')
const newMirrorMode = ref('wildcard')
const newMirrorSaveDir = ref('')

// 变量替换帮助
const showVarHelp = ref(false)
const varHelpText = computed(() => ({
  patterns: [
    { var: '{path}', desc: t('settings.varPathDesc') || '完整路径，如 /api/users/123/profile' },
    { var: '{query.page}', desc: t('settings.varQueryDesc') || '查询参数，如 ?page=1&size=20 中的 page 值' },
    { var: '{segments[0]}', desc: t('settings.varSegmentsDesc') || '路径段（0-based），如 /api/users/123 中 segments[0]=api' },
    { var: '$1, $2', desc: t('settings.varRegexDesc') || '正则捕获组，如 /api/users/(\\d+) → $1 为用户ID' },
    { var: '{id}', desc: t('settings.varNamedDesc') || '命名路径变量，自动从路径段匹配' },
  ]
}))

// 导入相关
const importDialogVisible = ref(false)
const importFile = ref<File | null>(null)
const importMode = ref<'merge' | 'replace' | 'skip_conflict'>('merge')
const importPreview = ref<{ valid: boolean; conflicts: any[]; warnings: any[] } | null>(null)
const importing = ref(false)

async function load() {
  loading.value = true
  try {
    config.value = await api.proxyToolsStatus()
    // 加载命中统计
    hitStats.value = await api.proxyToolsStats()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    loading.value = false
  }
}

async function toggleNoCaching(val: boolean) {
  try {
    config.value = await api.proxyToolsUpdate({ no_caching: val })
  } catch (e: any) {
    config.value.no_caching = !val
    ElMessage.error(e?.message || String(e))
  }
}

async function toggleForceCors(val: boolean) {
  try {
    config.value = await api.proxyToolsUpdate({ force_cors: val })
  } catch (e: any) {
    config.value.force_cors = !val
    ElMessage.error(e?.message || String(e))
  }
}

async function toggleBlockList(val: boolean) {
  try {
    config.value = await api.proxyToolsUpdate({ block_list_enabled: val })
  } catch (e: any) {
    config.value.block_list_enabled = !val
    ElMessage.error(e?.message || String(e))
  }
}

async function toggleAllowList(val: boolean) {
  try {
    config.value = await api.proxyToolsUpdate({ allow_list_enabled: val })
  } catch (e: any) {
    config.value.allow_list_enabled = !val
    ElMessage.error(e?.message || String(e))
  }
}

async function toggleMapLocal(val: boolean) {
  try {
    config.value = await api.proxyToolsUpdate({ map_local_enabled: val })
  } catch (e: any) {
    config.value.map_local_enabled = !val
    ElMessage.error(e?.message || String(e))
  }
}

async function toggleMapRemote(val: boolean) {
  try {
    config.value = await api.proxyToolsUpdate({ map_remote_enabled: val })
  } catch (e: any) {
    config.value.map_remote_enabled = !val
    ElMessage.error(e?.message || String(e))
  }
}

async function toggleMirror(val: boolean) {
  try {
    config.value = await api.proxyToolsUpdate({ mirror_enabled: val })
  } catch (e: any) {
    config.value.mirror_enabled = !val
    ElMessage.error(e?.message || String(e))
  }
}

async function addBlockRule() {
  const p = newBlockPattern.value.trim()
  if (!p) return
  try {
    config.value = await api.blockListAdd(p, newBlockMode.value)
    newBlockPattern.value = ''
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function delBlockRule(idx: number) {
  try {
    config.value = await api.blockListDelete(idx)
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function addAllowRule() {
  const p = newAllowPattern.value.trim()
  if (!p) return
  try {
    config.value = await api.allowListAdd(p, newAllowMode.value)
    newAllowPattern.value = ''
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function delAllowRule(idx: number) {
  try {
    config.value = await api.allowListDelete(idx)
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function addMapLocalRule() {
  const p = newMapLocalPattern.value.trim()
  const fp = newMapLocalFilePath.value.trim()
  if (!p || !fp) return
  const hdrs: Record<string, string> | undefined = newMapLocalHeaderKey.value.trim() && newMapLocalHeaderVal.value.trim()
    ? { [newMapLocalHeaderKey.value.trim()]: newMapLocalHeaderVal.value.trim() }
    : undefined
  try {
    config.value = await api.mapLocalAdd(p, newMapLocalMode.value, fp, newMapLocalStatus.value, undefined, hdrs)
    newMapLocalPattern.value = ''
    newMapLocalFilePath.value = ''
    newMapLocalStatus.value = undefined
    newMapLocalHeaderKey.value = ''
    newMapLocalHeaderVal.value = ''
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function delMapLocalRule(idx: number) {
  try {
    config.value = await api.mapLocalDelete(idx)
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function addMapRemoteRule() {
  const p = newMapRemotePattern.value.trim()
  const tu = newMapRemoteTargetUrl.value.trim()
  if (!p || !tu) return
  const hdrs: Record<string, string> | undefined = newMapRemoteHeaderKey.value.trim() && newMapRemoteHeaderVal.value.trim()
    ? { [newMapRemoteHeaderKey.value.trim()]: newMapRemoteHeaderVal.value.trim() }
    : undefined
  try {
    config.value = await api.mapRemoteAdd(p, newMapRemoteMode.value, tu, hdrs)
    newMapRemotePattern.value = ''
    newMapRemoteTargetUrl.value = ''
    newMapRemoteHeaderKey.value = ''
    newMapRemoteHeaderVal.value = ''
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function delMapRemoteRule(idx: number) {
  try {
    config.value = await api.mapRemoteDelete(idx)
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function addMirrorRule() {
  const p = newMirrorPattern.value.trim()
  const sd = newMirrorSaveDir.value.trim()
  if (!p || !sd) return
  try {
    config.value = await api.mirrorAdd(p, newMirrorMode.value, sd)
    newMirrorPattern.value = ''
    newMirrorSaveDir.value = ''
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

async function delMirrorRule(idx: number) {
  try {
    config.value = await api.mirrorDelete(idx)
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

// 命中统计
function getRuleStats(ruleType: string, idx: number): HitStats | null {
  return hitStats.value[ruleType]?.[String(idx)] || null
}

function getRuleHitCount(ruleType: string, idx: number): number {
  const stats = getRuleStats(ruleType, idx)
  return stats?.hit_count || 0
}

function toggleStatsExpanded(key: string) {
  expandedStats.value[key] = !expandedStats.value[key]
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString()
}

async function clearStats(ruleType?: string, ruleIndex?: number) {
  try {
    await api.proxyToolsStatsClear(ruleType && ruleIndex !== undefined ? { rule_type: ruleType, rule_index: ruleIndex } : undefined)
    hitStats.value = await api.proxyToolsStats()
    ElMessage.success(t('settings.statsCleared') || '统计已清除')
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

// 导入/导出
async function exportRules() {
  try {
    const blob = await api.proxyToolsExport()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'telnix_proxy_rules.json'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    ElMessage.success(t('settings.exportSuccess') || '导出成功')
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

function openImportDialog() {
  importFile.value = null
  importPreview.value = null
  importDialogVisible.value = true
}

async function handleImportFileChange(uploadFile: UploadFile, _uploadFiles: UploadFiles) {
  const rawFile = uploadFile.raw
  if (!rawFile) {
    importFile.value = null
    importPreview.value = null
    return
  }
  importFile.value = rawFile

  // 读取并预览
  try {
    const text = await rawFile.text()
    const data = JSON.parse(text)
    if (data.rules) {
      // 验证导入
      importPreview.value = await api.proxyToolsValidateImport({ rules: data.rules })
    }
  } catch (e: any) {
    ElMessage.error(t('settings.invalidJsonFile') || '无效的 JSON 文件')
    importFile.value = null
    importPreview.value = null
  }
}

async function confirmImport() {
  if (!importFile.value) return
  importing.value = true
  try {
    const text = await importFile.value.text()
    const data = JSON.parse(text)
    const result = await api.proxyToolsImport({
      rules: data.rules || {},
      mode: importMode.value,
    })
    if (result.conflicts?.length) {
      ElMessage.warning(`${t('settings.imported') || '已导入'} ${result.imported}，${t('settings.skipped') || '跳过'} ${result.skipped}，${t('settings.conflicts') || '冲突'} ${result.conflicts.length}`)
    } else {
      ElMessage.success(`${t('settings.importSuccess') || '导入成功'}: ${result.imported}`)
    }
    importDialogVisible.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    importing.value = false
  }
}

function rulePattern(r: { pattern: string; mode: string } | string): string {
  return typeof r === 'string' ? r : r.pattern
}
function ruleMode(r: { pattern: string; mode: string } | string): string {
  return typeof r === 'string' ? 'wildcard' : r.mode
}

const modeOptions = [
  { labelKey: 'settings.modeWildcard', value: 'wildcard' },
  { labelKey: 'settings.modeExact', value: 'exact' },
  { labelKey: 'settings.modeRegex', value: 'regex' },
]

// 检查文件路径是否包含变量语法
function hasVarSyntax(path: string): boolean {
  return /\{path\}|\{query\.|\\$1|\\$2|\{segments\[|\{\w+\}/.test(path)
}

onMounted(load)
</script>

<template>
  <div class="tools-view full flex flex-col">
    <div class="page-header">
      <div class="page-title no-select">
        <el-icon><ToolsIcon /></el-icon>&nbsp;{{ t('nav.tools') }}
      </div>
      <div class="header-actions">
        <el-button size="small" @click="exportRules">
          <el-icon><Download /></el-icon>&nbsp;{{ t('settings.exportRules') || '导出规则' }}
        </el-button>
        <el-button size="small" @click="openImportDialog">
          <el-icon><Upload /></el-icon>&nbsp;{{ t('settings.importRules') || '导入规则' }}
        </el-button>
        <el-button size="small" @click="clearStats()" :disabled="!hitStats.map_local_rules && !hitStats.map_remote_rules">
          <el-icon><DataLine /></el-icon>&nbsp;{{ t('settings.clearAllStats') || '清除统计' }}
        </el-button>
        <el-button size="small" @click="load" :loading="loading">
          <el-icon><Refresh /></el-icon>&nbsp;{{ t('common.refresh') }}
        </el-button>
      </div>
    </div>

    <div class="t-content">
      <!-- No Caching -->
      <div class="t-card t-card-accent hover-lift">
        <div class="t-card-title no-select">
          <span class="t-badge t-badge-primary">CACHE</span>
          {{ t('settings.noCaching') }}
          <div class="flex-1"></div>
          <el-switch :model-value="config.no_caching" @change="(v: string | number | boolean) => toggleNoCaching(!!v)" />
        </div>
        <div class="t-card-desc no-select">{{ t('settings.noCachingHint') }}</div>
      </div>

      <!-- Force CORS -->
      <div class="t-card t-card-accent hover-lift">
        <div class="t-card-title no-select">
          <span class="t-badge t-badge-info">CORS</span>
          {{ t('settings.forceCors') }}
          <div class="flex-1"></div>
          <el-switch :model-value="config.force_cors" @change="(v: string | number | boolean) => toggleForceCors(!!v)" />
        </div>
        <div class="t-card-desc no-select">{{ t('settings.forceCorsHint') }}</div>
      </div>

      <!-- Block List -->
      <div class="t-card hover-lift">
        <div class="t-card-title no-select">
          <span class="t-badge t-badge-danger">BLOCK</span>
          {{ t('settings.blockList') }}
          <div class="flex-1"></div>
          <el-switch
            :model-value="config.block_list_enabled"
            @change="(v: string | number | boolean) => toggleBlockList(!!v)"
            size="small"
          />
        </div>
        <div class="t-card-desc no-select">{{ t('settings.blockListHint') }}</div>
        <div class="rule-add-row">
          <el-input
            v-model="newBlockPattern"
            :placeholder="t('settings.patternPlaceholder')"
            size="small"
            class="mono"
            @keyup.enter="addBlockRule"
          />
          <el-select v-model="newBlockMode" size="small" style="width: 110px">
            <el-option v-for="m in modeOptions" :key="m.value" :label="t(m.labelKey)" :value="m.value" />
          </el-select>
          <el-button size="small" type="primary" @click="addBlockRule">
            <el-icon><Plus /></el-icon>&nbsp;{{ t('settings.addRule') }}
          </el-button>
        </div>
        <div class="rule-list" v-if="config.block_list.length">
          <div v-for="(r, i) in config.block_list" :key="i" class="rule-item">
            <span class="rule-mode-tag no-select">{{ ruleMode(r) }}</span>
            <span class="rule-pattern mono no-select">{{ rulePattern(r) }}</span>
            <el-button size="small" text type="danger" @click="delBlockRule(i)">
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
        <div v-else class="rule-empty no-select">{{ t('settings.noRules') }}</div>
      </div>

      <!-- Allow List -->
      <div class="t-card hover-lift">
        <div class="t-card-title no-select">
          <span class="t-badge t-badge-success">ALLOW</span>
          {{ t('settings.allowList') }}
          <div class="flex-1"></div>
          <el-switch
            :model-value="config.allow_list_enabled"
            @change="(v: string | number | boolean) => toggleAllowList(!!v)"
            size="small"
          />
        </div>
        <div class="t-card-desc no-select">{{ t('settings.allowListHint') }}</div>
        <div class="rule-add-row">
          <el-input
            v-model="newAllowPattern"
            :placeholder="t('settings.patternPlaceholder')"
            size="small"
            class="mono"
            @keyup.enter="addAllowRule"
          />
          <el-select v-model="newAllowMode" size="small" style="width: 110px">
            <el-option v-for="m in modeOptions" :key="m.value" :label="t(m.labelKey)" :value="m.value" />
          </el-select>
          <el-button size="small" type="primary" @click="addAllowRule">
            <el-icon><Plus /></el-icon>&nbsp;{{ t('settings.addRule') }}
          </el-button>
        </div>
        <div class="rule-list" v-if="config.allow_list.length">
          <div v-for="(r, i) in config.allow_list" :key="i" class="rule-item">
            <span class="rule-mode-tag no-select">{{ ruleMode(r) }}</span>
            <span class="rule-pattern mono no-select">{{ rulePattern(r) }}</span>
            <el-button size="small" text type="danger" @click="delAllowRule(i)">
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
        <div v-else class="rule-empty no-select">{{ t('settings.noRules') }}</div>
      </div>

      <!-- Map Local -->
      <div class="t-card hover-lift">
        <div class="t-card-title no-select">
          <span class="t-badge t-badge-warning">MAP LOCAL</span>
          {{ t('settings.mapLocal') }}
          <div class="flex-1"></div>
          <el-button size="small" text @click="showVarHelp = !showVarHelp" :title="t('settings.varHelpTitle') || '变量替换语法帮助'">
            <el-icon><QuestionFilled /></el-icon>
          </el-button>
          <el-switch
            :model-value="config.map_local_enabled"
            @change="(v: string | number | boolean) => toggleMapLocal(!!v)"
            size="small"
          />
        </div>
        <!-- 变量替换帮助 -->
        <div v-if="showVarHelp" class="var-help-box">
          <div class="var-help-title">{{ t('settings.varSyntax') || '变量替换语法' }}</div>
          <div v-for="p in varHelpText.patterns" :key="p.var" class="var-help-item">
            <code class="var-code">{{ p.var }}</code>
            <span class="var-desc">{{ p.desc }}</span>
          </div>
          <div class="var-help-example">
            <div class="var-help-title">{{ t('settings.varExamples') || '示例' }}</div>
            <div class="var-example-item"><code>/api/users/{id}/profile</code> → <code>D:\mock\users\{id}\profile.json</code></div>
            <div class="var-example-item"><code>/api/v2/users/(\d+)/orders</code> → <code>mock/users_$1_orders.json</code></div>
            <div class="var-example-item"><code>/api/search?q={query.q}&page={query.page}</code> → <code>mock/search_page_{query.page}.json</code></div>
          </div>
        </div>
        <div class="t-card-desc no-select">{{ t('settings.mapLocalHint') }}</div>
        <div class="rule-add-row map-local-add">
          <el-input
            v-model="newMapLocalPattern"
            :placeholder="t('settings.patternPlaceholder')"
            size="small"
            class="mono"
            @keyup.enter="addMapLocalRule"
          />
          <el-select v-model="newMapLocalMode" size="small" style="width: 110px">
            <el-option v-for="m in modeOptions" :key="m.value" :label="t(m.labelKey)" :value="m.value" />
          </el-select>
        </div>
        <div class="rule-add-row">
          <el-input
            v-model="newMapLocalFilePath"
            :placeholder="t('settings.filePathPlaceholder')"
            size="small"
            style="flex: 1.5"
          />
          <el-input-number
            v-model="newMapLocalStatus"
            :placeholder="t('settings.statusCode')"
            size="small"
            :min="100"
            :max="599"
            style="width: 110px"
          />
          <el-button size="small" type="primary" @click="addMapLocalRule">
            <el-icon><Plus /></el-icon>&nbsp;{{ t('settings.addRule') }}
          </el-button>
        </div>
        <div class="rule-add-row rule-header-row">
          <span class="text-dim" style="font-size: 11px">Header条件：</span>
          <el-input v-model="newMapLocalHeaderKey" placeholder="X-Header-Name" size="small" style="width: 130px" />
          <span class="text-dim" style="font-size: 11px">=</span>
          <el-input v-model="newMapLocalHeaderVal" placeholder="*value* (含*前后缀)" size="small" style="width: 150px" />
          <span class="text-dim" style="font-size: 10px">无*前缀=精确，*前后缀=包含匹配</span>
        </div>
        <div class="rule-list" v-if="config.map_local_rules.length">
          <div v-for="(r, i) in config.map_local_rules" :key="i" class="rule-item rule-item-2line">
            <div class="rule-line">
              <span class="rule-mode-tag no-select">{{ r.mode }}</span>
              <span class="rule-pattern mono no-select">{{ r.pattern }}</span>
              <!-- 命中统计 -->
              <el-tooltip :content="`命中 ${getRuleHitCount('map_local_rules', i)} 次`" placement="top">
                <span class="hit-count-badge" :class="{ 'has-hits': getRuleHitCount('map_local_rules', i) > 0 }">
                  <el-icon><DataLine /></el-icon>&nbsp;{{ getRuleHitCount('map_local_rules', i) }}
                </span>
              </el-tooltip>
              <el-tag v-if="r.status" size="small" type="info" style="margin-left: auto" class="no-select">{{ r.status }}</el-tag>
              <el-tag v-if="hasVarSyntax(r.file_path)" size="small" type="success" class="no-select">VAR</el-tag>
              <el-button size="small" text @click="toggleStatsExpanded('map_local_' + i)">
                <el-icon><Clock /></el-icon>
              </el-button>
              <el-button size="small" text type="danger" @click="delMapLocalRule(i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <!-- 命中文档时间线 -->
            <div v-if="expandedStats['map_local_' + i] && getRuleStats('map_local_rules', i)" class="rule-stats-timeline">
              <div v-for="(hit, hi) in getRuleStats('map_local_rules', i)?.last_hits" :key="hi" class="stats-hit-item">
                <span class="stats-time no-select">{{ formatTime(hit.ts) }}</span>
                <span class="stats-url mono no-select">{{ hit.url }}</span>
                <span class="stats-path text-dim no-select" v-if="hit.resolved_path">{{ hit.resolved_path }}</span>
              </div>
              <el-button size="small" text type="warning" @click="clearStats('map_local_rules', i)" class="clear-stats-btn">
                {{ t('settings.clearStats') || '清除统计' }}
              </el-button>
            </div>
            <div class="rule-line rule-file-path no-select" :class="{ 'has-var': hasVarSyntax(r.file_path) }">
              <span class="file-path-text">{{ r.file_path }}</span>
            </div>
            <div v-if="r.headers" class="rule-line rule-header-tags">
              <el-tag v-for="(v, k) in r.headers" :key="k" size="small" type="warning" class="no-select">{{ k }}={{ v }}</el-tag>
            </div>
          </div>
        </div>
        <div v-else class="rule-empty no-select">{{ t('settings.noRules') }}</div>
      </div>

      <!-- Map Remote -->
      <div class="t-card hover-lift">
        <div class="t-card-title no-select">
          <span class="t-badge t-badge-purple">MAP REMOTE</span>
          {{ t('settings.mapRemote') }}
          <div class="flex-1"></div>
          <el-switch
            :model-value="config.map_remote_enabled"
            @change="(v: string | number | boolean) => toggleMapRemote(!!v)"
            size="small"
          />
        </div>
        <div class="t-card-desc no-select">{{ t('settings.mapRemoteHint') }}</div>
        <div class="rule-add-row">
          <el-input
            v-model="newMapRemotePattern"
            :placeholder="t('settings.patternPlaceholder')"
            size="small"
            class="mono"
            @keyup.enter="addMapRemoteRule"
          />
          <el-select v-model="newMapRemoteMode" size="small" style="width: 110px">
            <el-option v-for="m in modeOptions" :key="m.value" :label="t(m.labelKey)" :value="m.value" />
          </el-select>
        </div>
        <div class="rule-add-row">
          <el-input
            v-model="newMapRemoteTargetUrl"
            :placeholder="t('settings.targetUrlPlaceholder')"
            size="small"
            style="flex: 1"
          />
          <el-button size="small" type="primary" @click="addMapRemoteRule">
            <el-icon><Plus /></el-icon>&nbsp;{{ t('settings.addRule') }}
          </el-button>
        </div>
        <div class="rule-add-row rule-header-row">
          <span class="text-dim" style="font-size: 11px">Header条件：</span>
          <el-input v-model="newMapRemoteHeaderKey" placeholder="X-Header-Name" size="small" style="width: 130px" />
          <span class="text-dim" style="font-size: 11px">=</span>
          <el-input v-model="newMapRemoteHeaderVal" placeholder="*value* (含*前后缀)" size="small" style="width: 150px" />
          <span class="text-dim" style="font-size: 10px">无*前缀=精确，*前后缀=包含匹配</span>
        </div>
        <div class="rule-list" v-if="config.map_remote_rules.length">
          <div v-for="(r, i) in config.map_remote_rules" :key="i" class="rule-item rule-item-2line">
            <div class="rule-line">
              <span class="rule-mode-tag no-select">{{ r.mode }}</span>
              <span class="rule-pattern mono no-select">{{ r.pattern }}</span>
              <!-- 命中统计 -->
              <el-tooltip :content="`命中 ${getRuleHitCount('map_remote_rules', i)} 次`" placement="top">
                <span class="hit-count-badge" :class="{ 'has-hits': getRuleHitCount('map_remote_rules', i) > 0 }">
                  <el-icon><DataLine /></el-icon>&nbsp;{{ getRuleHitCount('map_remote_rules', i) }}
                </span>
              </el-tooltip>
              <el-button size="small" text @click="toggleStatsExpanded('map_remote_' + i)">
                <el-icon><Clock /></el-icon>
              </el-button>
              <el-button size="small" text type="danger" @click="delMapRemoteRule(i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <!-- 命中文档时间线 -->
            <div v-if="expandedStats['map_remote_' + i] && getRuleStats('map_remote_rules', i)" class="rule-stats-timeline">
              <div v-for="(hit, hi) in getRuleStats('map_remote_rules', i)?.last_hits" :key="hi" class="stats-hit-item">
                <span class="stats-time no-select">{{ formatTime(hit.ts) }}</span>
                <span class="stats-url mono no-select">{{ hit.url }}</span>
                <span class="stats-path text-dim no-select" v-if="hit.resolved_path">→ {{ hit.resolved_path }}</span>
              </div>
              <el-button size="small" text type="warning" @click="clearStats('map_remote_rules', i)" class="clear-stats-btn">
                {{ t('settings.clearStats') || '清除统计' }}
              </el-button>
            </div>
            <div class="rule-line rule-target-url mono no-select">{{ r.target_url }}</div>
            <div v-if="r.headers" class="rule-line rule-header-tags">
              <el-tag v-for="(v, k) in r.headers" :key="k" size="small" type="warning" class="no-select">{{ k }}={{ v }}</el-tag>
            </div>
          </div>
        </div>
        <div v-else class="rule-empty no-select">{{ t('settings.noRules') }}</div>
      </div>

      <!-- Mirror -->
      <div class="t-card hover-lift">
        <div class="t-card-title no-select">
          <span class="t-badge t-badge-cyan">MIRROR</span>
          {{ t('settings.mirror') }}
          <div class="flex-1"></div>
          <el-switch
            :model-value="config.mirror_enabled"
            @change="(v: string | number | boolean) => toggleMirror(!!v)"
            size="small"
          />
        </div>
        <div class="t-card-desc no-select">{{ t('settings.mirrorHint') }}</div>
        <div class="rule-add-row">
          <el-input
            v-model="newMirrorPattern"
            :placeholder="t('settings.patternPlaceholder')"
            size="small"
            class="mono"
            @keyup.enter="addMirrorRule"
          />
          <el-select v-model="newMirrorMode" size="small" style="width: 110px">
            <el-option v-for="m in modeOptions" :key="m.value" :label="t(m.labelKey)" :value="m.value" />
          </el-select>
        </div>
        <div class="rule-add-row">
          <el-input
            v-model="newMirrorSaveDir"
            :placeholder="t('settings.saveDirPlaceholder')"
            size="small"
            style="flex: 1"
          />
          <el-button size="small" type="primary" @click="addMirrorRule">
            <el-icon><Plus /></el-icon>&nbsp;{{ t('settings.addRule') }}
          </el-button>
        </div>
        <div class="rule-list" v-if="config.mirror_rules.length">
          <div v-for="(r, i) in config.mirror_rules" :key="i" class="rule-item rule-item-2line">
            <div class="rule-line">
              <span class="rule-mode-tag no-select">{{ r.mode }}</span>
              <span class="rule-pattern mono no-select">{{ r.pattern }}</span>
              <el-button size="small" text type="danger" @click="delMirrorRule(i)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <div class="rule-line rule-file-path no-select">{{ r.save_dir }}</div>
          </div>
        </div>
        <div v-else class="rule-empty no-select">{{ t('settings.noRules') }}</div>
      </div>
    </div>

    <!-- 导入规则对话框 -->
    <el-dialog v-model="importDialogVisible" :title="t('settings.importRules') || '导入规则'" width="500px" destroy-on-close>
      <div class="import-dialog-content">
        <el-alert v-if="importPreview && !importPreview.valid" type="error" :title="`${t('settings.conflicts') || '冲突'}: ${importPreview.conflicts.length}`" style="margin-bottom: 16px" />
        <el-alert v-if="importPreview && importPreview.warnings.length" type="warning" :title="`${t('settings.warnings') || '警告'}: ${importPreview.warnings.length}`" style="margin-bottom: 16px" />
        <div class="import-file-select">
          <el-upload
            :auto-upload="false"
            :limit="1"
            accept=".json"
            :on-change="handleImportFileChange"
            drag
          >
            <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
            <div class="el-upload__text">
              {{ t('settings.dropJsonFile') || '拖拽 JSON 文件或点击上传' }}
            </div>
            <template #tip>
              <div class="el-upload__tip">{{ t('settings.jsonRuleFile') || '支持 telnix_proxy_rules.json 格式' }}</div>
            </template>
          </el-upload>
        </div>
        <div class="import-mode-select" v-if="importFile">
          <span class="text-dim">{{ t('settings.importMode') || '导入模式' }}：</span>
          <el-radio-group v-model="importMode" size="small">
            <el-radio-button value="merge">{{ t('settings.importMerge') || '合并' }}</el-radio-button>
            <el-radio-button value="replace">{{ t('settings.importReplace') || '替换' }}</el-radio-button>
            <el-radio-button value="skip_conflict">{{ t('settings.importSkipConflict') || '跳过冲突' }}</el-radio-button>
          </el-radio-group>
        </div>
        <div class="import-preview" v-if="importPreview">
          <div v-if="importPreview.conflicts.length" class="conflict-list">
            <div class="conflict-title">{{ t('settings.conflictRules') || '冲突规则' }}</div>
            <div v-for="(c, ci) in importPreview.conflicts.slice(0, 5)" :key="ci" class="conflict-item">
              <el-tag size="small" type="danger">{{ c.rule_type }}</el-tag>
              <code class="mono">{{ c.pattern }}</code>
              <span class="text-dim">vs</span>
              <code class="mono text-dim">{{ c.existing_target }}</code>
            </div>
            <div v-if="importPreview.conflicts.length > 5" class="text-dim">
              ...{{ t('settings.andMoreConflicts') || `还有 ${importPreview.conflicts.length - 5} 个冲突` }}
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="importDialogVisible = false">{{ t('common.cancel') || '取消' }}</el-button>
        <el-button type="primary" @click="confirmImport" :loading="importing" :disabled="!importFile">
          {{ t('settings.confirmImport') || '确认导入' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tools-view {
  background: var(--on-bg);
}
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.page-title { font-size: 15px; font-weight: 600; display: flex; align-items: center; }
.page-title-icon { flex-shrink: 0; }
.header-actions { display: flex; align-items: center; gap: 8px; }
.tools-content, .t-content {
  padding: 16px 20px;
}
.t-content {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 14px;
  align-content: start;
}
.tool-card, .t-card {
  background: var(--on-bg-card);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-lg);
  padding: 16px 18px;
  transition: border-color 0.2s;
}
.tool-card:hover, .t-card:hover {
  border-color: var(--on-border);
}
.tool-card-header, .t-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.tool-card-title, .t-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--on-text);
}
.tool-card-desc, .t-card-desc {
  font-size: 12px;
  color: var(--on-text-dim);
  line-height: 1.5;
  margin-bottom: 12px;
}
.tool-badge {
  display: none;
}
.t-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: var(--on-radius-sm);
  background: var(--on-accent-glow); color: var(--on-accent);
  letter-spacing: .5px;
}
.t-badge-primary { background: var(--on-accent-glow); color: var(--on-accent); }
.t-badge-info { background: rgba(99, 102, 241, 0.15); color: var(--on-cat-analyze); }
.t-badge-danger { background: var(--on-rose-glow); color: var(--on-error); }
.t-badge-success { background: rgba(16, 185, 129, 0.15); color: var(--on-cat-codec); }
.t-badge-warning { background: var(--on-amber-glow); color: var(--on-amber); }
.t-badge-purple { background: var(--on-purple-glow); color: var(--on-purple); }
.t-badge-cyan { background: var(--on-cyan-glow); color: var(--on-cyan); }

.rule-add-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
.rule-add-row .el-input {
  flex: 1;
}
.map-local-add .el-input {
  flex: 1;
}
.rule-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.rule-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--on-bg-hover);
  transition: background 0.15s;
}
.rule-item:hover {
  background: var(--on-bg-hover-strong);
}
.rule-item-2line {
  flex-direction: column;
  align: stretch;
  gap: 2px;
}
.rule-line {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rule-mode-tag {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--on-bg-card);
  border: 1px solid var(--on-border-light);
  color: var(--on-text-dim);
  text-transform: uppercase;
  min-width: 56px;
  text-align: center;
}
.rule-pattern {
  flex: 1;
  font-size: 13px;
  color: var(--on-text);
  word-break: break-all;
}
.rule-file-path,
.rule-target-url {
  font-size: 12px;
  color: var(--on-text-dim);
  word-break: break-all;
  padding-left: 64px;
}
.rule-empty {
  font-size: 13px;
  color: var(--on-text-dim);
  padding: 8px 0;
}
.rule-header-row {
  margin-top: 4px;
  padding: 4px 8px;
  background: var(--on-bg);
  border-radius: 4px;
  align-items: center;
  flex-wrap: nowrap;
}
.rule-header-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 2px;
}

/* 命中统计 */
.hit-count-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--on-bg-card);
  border: 1px solid var(--on-border-light);
  color: var(--on-text-dim);
  display: inline-flex;
  align-items: center;
  gap: 2px;
  cursor: pointer;
  transition: all 0.15s;
}
.hit-count-badge:hover {
  border-color: var(--on-accent);
  color: var(--on-accent);
}
.hit-count-badge.has-hits {
  background: rgba(16, 185, 129, 0.15);
  border-color: var(--on-cat-codec);
  color: var(--on-cat-codec);
}

.rule-stats-timeline {
  padding: 8px;
  background: var(--on-bg);
  border-radius: 6px;
  margin: 4px 0;
  max-height: 200px;
  overflow-y: auto;
}
.stats-hit-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  font-size: 11px;
  border-bottom: 1px solid var(--on-border-light);
}
.stats-hit-item:last-of-type {
  border-bottom: none;
}
.stats-time {
  color: var(--on-text-dim);
  white-space: nowrap;
  min-width: 70px;
}
.stats-url {
  flex: 1;
  color: var(--on-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.stats-path {
  font-size: 10px;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.clear-stats-btn {
  margin-top: 8px;
}

/* 变量替换帮助 */
.var-help-box {
  background: var(--on-bg);
  border: 1px solid var(--on-accent-glow);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
  font-size: 12px;
}
.var-help-title {
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--on-accent);
}
.var-help-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 3px 0;
}
.var-code {
  background: var(--on-bg-card);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: monospace;
  color: var(--on-amber);
  min-width: 100px;
}
.var-desc {
  color: var(--on-text-dim);
}
.var-help-example {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--on-border-light);
}
.var-example-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  font-size: 11px;
}
.var-example-item code {
  background: var(--on-bg-card);
  padding: 1px 4px;
  border-radius: 3px;
  font-family: monospace;
}

/* 变量路径样式 */
.rule-file-path.has-var {
  color: var(--on-amber);
}
.file-path-text {
  word-break: break-all;
}

/* 导入对话框 */
.import-dialog-content {
  min-height: 200px;
}
.import-file-select {
  margin-bottom: 16px;
}
.import-mode-select {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}
.import-preview {
  max-height: 200px;
  overflow-y: auto;
}
.conflict-list {
  background: var(--on-bg);
  border-radius: 6px;
  padding: 8px;
}
.conflict-title {
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--on-error);
}
.conflict-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  font-size: 12px;
  flex-wrap: wrap;
}
</style>