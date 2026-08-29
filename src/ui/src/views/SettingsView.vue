<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownIt from 'markdown-it'
import QRCode from 'qrcode'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import { api, type Settings, type IgnoredProcess, type TransparentProxyStatus } from '../api/client'
import { getCacheSize, clearFlowCache } from '../stores/flows'
import { syncPrefs } from '../stores/prefs'
import { getLang, setLang } from '../i18n'
import { platformCapabilities, loadPlatformCapabilities } from '../stores/platform'

const capture = useCaptureStore()
const flows = useFlowsStore()
const router = useRouter()
const { t } = useI18n()
const form = ref<Settings>({})
const certInstalled = ref(false)
const saving = ref(false)
const cacheSize = ref(0)
const cacheThreshold = ref(10)
// mitmproxy 引擎是否可用（后端 /settings 接口注入，前端据此禁用选项）
const mitmproxyAvailable = ref(false)

// 透明代理模式状态
const transparentProxy = ref<TransparentProxyStatus>({
  supported: true, running: false,
})
const transparentLoading = ref(false)
const transparentIsAdmin = ref(false)  // 是否已是管理员
const transparentAdminLoading = ref(true)  // 管理员状态加载中（避免初始值 false 导致标签颜色跳动）
const transparentRestarting = ref(false)  // 管理员重启中
let transparentPollTimer: number | null = null

// 透明代理后端友好显示名
const transparentBackendName = computed(() => {
  const b = transparentProxy.value.backend
  if (b === 'windivert') return 'WinDivert'
  if (b === 'iptables') return 'iptables'
  if (b === 'pf') return 'pf'
  return t('settings.transparentProxy')
})
// 平台类型：Windows 用"管理员"，Unix 用"root"
const transparentAdminTerm = computed(() => {
  const b = transparentProxy.value.backend
  if (b === 'windivert') return t('settings.admin')
  return 'root'
})

// ============ AI 服务管理 ============
const aiUsage = ref({
  today: { requests: 0, input_tokens: 0, output_tokens: 0, total_cost: 0 },
  month: { requests: 0, input_tokens: 0, output_tokens: 0, total_cost: 0 },
  all_time: { requests: 0, input_tokens: 0, output_tokens: 0, total_cost: 0 },
})

async function loadAiUsage() {
  try {
    const stats = await api.aiGetUsage()
    aiUsage.value = stats
  } catch (e: any) {
    // 忽略错误
  }
}

async function onAiServiceChange(val: string) {
  form.value.ai_service = val
  await doSave()
  ElMessage.success(t('settings.aiServiceChanged'))
}

// ============ 性能配置 ============
const perfConfig = ref({
  max_body_size: 10 * 1024 * 1024,
  decompress_threshold: 1024,
  ssl_context_cache_size: 256,
  max_connections: 200,
})
const perfSaving = ref(false)
const currentPreset = ref('standard')

async function loadPerfConfig() {
  try {
    const cfg = await api.getPerformanceConfig()
    perfConfig.value = cfg
    // 根据配置推断当前预设
    if (cfg.max_body_size <= 5 * 1024 * 1024) currentPreset.value = 'light'
    else if (cfg.max_body_size >= 50 * 1024 * 1024) currentPreset.value = 'high_performance'
    else currentPreset.value = 'standard'
  } catch { /* ignore */ }
}

async function savePerfConfig() {
  perfSaving.value = true
  try {
    await api.updatePerformanceConfig({
      max_body_size: perfConfig.value.max_body_size,
      decompress_threshold: perfConfig.value.decompress_threshold,
      ssl_context_cache_size: perfConfig.value.ssl_context_cache_size,
      max_connections: perfConfig.value.max_connections,
    })
    ElMessage.success(t('settings.saved'))
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e.message }))
  } finally {
    perfSaving.value = false
  }
}

async function applyPerfPreset(preset: any) {
  try {
    await ElMessageBox.confirm(t('settings.configChangeMessage'), t('settings.configChangeConfirm'), {
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
      type: 'warning',
    })
  } catch { return }
  try {
    const cfg = await api.applyPerformancePreset(preset)
    perfConfig.value = { ...cfg }
    currentPreset.value = preset
    ElMessage.success(t('settings.saved'))
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e.message }))
  }
}

// ============ SSL/TLS 配置 ============
const sslConfig = ref({
  min_tls_version: 'TLS 1.2',
  cipher_suites: 'DEFAULT',
  sni_spoofing: false,
})
const sslSaving = ref(false)

async function loadSslConfig() {
  try {
    sslConfig.value = await api.getSslConfig()
  } catch { /* ignore */ }
}

async function saveSslConfig() {
  sslSaving.value = true
  try {
    await api.updateSslConfig({
      min_tls_version: sslConfig.value.min_tls_version,
      cipher_suites: sslConfig.value.cipher_suites,
      sni_spoofing: sslConfig.value.sni_spoofing,
    })
    ElMessage.success(t('settings.saved'))
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e.message }))
  } finally {
    sslSaving.value = false
  }
}

// ============ 证书详情 ============
const certDetails = ref<{
  root_cert_path: string
  installed: boolean
  thumbprint: string
  issued_date: string | null
  expiry_date: string | null
  expiry_countdown: number | null
  serial_number: string | null
  leaf_cert_count: number
  cert_expiry_alert: boolean
} | null>(null)
const certDetailsLoading = ref(false)
const certRegenerating = ref(false)

async function loadCertDetails() {
  certDetailsLoading.value = true
  try {
    certDetails.value = await api.getCertDetails()
  } catch { /* ignore */ } finally {
    certDetailsLoading.value = false
  }
}

async function toggleCertExpiryAlert(val: any) {
  try {
    await api.setCertExpiryAlert(val)
    if (certDetails.value) certDetails.value.cert_expiry_alert = val
    ElMessage.success(t('settings.saved'))
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e.message }))
  }
}

async function regenerateCert() {
  try {
    await ElMessageBox.confirm(
      t('settings.updateCertConfirm'),
      t('settings.dangerousOperationConfirm'),
      { confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel'), type: 'warning' }
    )
  } catch { return }
  certRegenerating.value = true
  try {
    await api.regenerateCert()
    ElMessage.success(t('settings.updateCertSuccess'))
    await loadCertDetails()
    certInstalled.value = false
  } catch (e: any) {
    ElMessage.error(t('settings.updateCertFailed', { msg: e.message }))
  } finally {
    certRegenerating.value = false
  }
}

const isWindowsBackend = computed(() => transparentProxy.value.backend === 'windivert')

async function loadTransparentStatus() {
  // 优先使用全局预加载的 platformCapabilities.is_admin（带 30 秒后端缓存），
  // 避免每次进入设置页都请求 raw/status 导致状态 2 秒跳动。
  // 如果预加载尚未完成，等待其完成（最多等待 500ms 避免长时间阻塞）。
  if (!platformCapabilities.platform) {
    await Promise.race([loadPlatformCapabilities(), new Promise(r => setTimeout(r, 500))])
  }

  transparentAdminLoading.value = true
  try {
    const s = await api.transparentProxyStatus()
    transparentProxy.value = s

    // 使用预加载的 platformCapabilities.is_admin
    if (platformCapabilities.is_admin) {
      transparentIsAdmin.value = true
    } else if (typeof s.is_admin === 'boolean') {
      transparentIsAdmin.value = s.is_admin
    } else {
      // 只有在预加载的 platformCapabilities 未获取到 is_admin 时才请求 rawStatus
      try {
        const raw: any = await api.rawStatus()
        transparentIsAdmin.value = !!raw.is_admin
      } catch {
        /* rawStatus 失败时保持默认值 */
      }
    }
  } catch (e: any) { /* ignore */ } finally {
    transparentAdminLoading.value = false
  }
}

async function onToggleTransparent(val: any) {
  transparentLoading.value = true
  try {
    if (val) {
      const r: any = await api.transparentProxyStart()
      ElMessage.success(r.msg || t('settings.transparentStarted'))
    } else {
      const r: any = await api.transparentProxyStop()
      ElMessage.success(r.msg || t('settings.transparentStopped'))
    }
    await loadTransparentStatus()
  } catch (e: any) {
    ElMessage.error(t('settings.operationFailed', { msg: e?.message || e }))
  } finally {
    transparentLoading.value = false
  }
}

async function restartTransparentAsAdmin() {
  transparentRestarting.value = true
  try {
    await api.restartAsAdmin()
    ElMessage.success(t('settings.restartingAsAdmin'))
  } catch (e: any) {
    ElMessage.error(t('settings.restartFailed', { msg: e?.message || e }))
    transparentRestarting.value = false
  }
}

// 主题模式（dark/light），从 localStorage 读取，默认 dark
const themeMode = ref<'dark' | 'light'>(
  (localStorage.getItem('telnix_theme') as 'dark' | 'light') || 'dark'
)
function onThemeChange(val: any) {
  const mode = val as string
  localStorage.setItem('telnix_theme', mode)
  if (mode === 'dark') {
    document.documentElement.classList.add('dark')
  } else {
    document.documentElement.classList.remove('dark')
  }
  // 同步到 settings.json
  syncPrefs()
}

// 界面语言（zh/en），从 i18n 读取，切换即时生效（vue-i18n 响应式）
const langMode = ref<'zh' | 'en'>(getLang())
function onLangChange(val: any) {
  const lang = val as 'zh' | 'en'
  setLang(lang)
  syncPrefs()
}
const cacheAutoClean = ref(true)
const ignoredProcesses = ref<IgnoredProcess[]>([])
// 忽略进程：支持单独按 PID 或单独按进程名添加
const newIgnorePid = ref<number | null>(null)
const newIgnoreName = ref('')
// 忽略 host 通配符（支持 * ? 通配符，如 *.example.com）
const newIgnoreHost = ref('')
const ignoredHosts = ref<{ id: number; host_pattern: string; created_at: string }[]>([])

const optionalTabs = computed(() => [
  { key: 'cookies', label: 'Cookies' },
  { key: 'cache', label: 'Cache' },
  { key: 'auth', label: 'Auth' },
  { key: 'xml', label: 'XML' },
  { key: 'cert', label: t('settings.cert') },
  { key: 'timeline', label: t('inspector.tcpTimeline') },
])

// 流量列表可选列（默认全不显示，核心列 # / Host / URL / 进程 永远显示）
const optionalColumns = computed(() => [
  { key: 'status', label: t('settings.statusCode') },
  { key: 'method', label: t('settings.method') },
  { key: 'protocol', label: t('settings.protocol') },
  { key: 'content_type', label: t('settings.contentType') },
  { key: 'pid', label: 'PID' },
  { key: 'size', label: t('settings.size') },
  { key: 'duration', label: t('settings.duration') },
  { key: 'remote_ip', label: t('settings.remoteIp') },
  { key: 'ip_region', label: t('settings.ipRegion') },
])

// 右键复制可选项（# 不参与复制）
const copyFieldOptions = computed(() => [
  { key: 'url', label: 'URL' },
  { key: 'curl', label: 'cURL' },
  { key: 'host', label: 'Host' },
  { key: 'method', label: t('settings.method') },
  { key: 'path', label: 'Path' },
  { key: 'status', label: t('settings.statusCode') },
  { key: 'protocol', label: t('settings.protocol') },
  { key: 'content_type', label: t('settings.contentType') },
  { key: 'pid', label: 'PID' },
  { key: 'process', label: t('settings.process') },
  { key: 'size', label: t('settings.size') },
  { key: 'duration', label: t('settings.duration') },
  { key: 'remote_ip', label: t('settings.remoteIp') },
  { key: 'ip_region', label: t('settings.ipRegion') },
])
const COPY_PREF_KEY = 'telnix_copy_fields'
const enabledCopyFields = ref<string[]>(['url', 'curl'])

function loadCopyPrefs() {
  try {
    const saved = localStorage.getItem(COPY_PREF_KEY)
    if (saved) {
      const arr = JSON.parse(saved)
      if (Array.isArray(arr)) enabledCopyFields.value = arr
    }
  } catch { /* ignore */ }
}
function toggleCopyField(key: string) {
  const idx = enabledCopyFields.value.indexOf(key)
  if (idx >= 0) enabledCopyFields.value.splice(idx, 1)
  else enabledCopyFields.value.push(key)
  localStorage.setItem(COPY_PREF_KEY, JSON.stringify(enabledCopyFields.value))
}
function copyFieldOn(key: string): boolean {
  return enabledCopyFields.value.includes(key)
}

// 目录选择器对话框
const dirPickerVisible = ref(false)
const dirPickerTarget = ref<'data_path' | 'ai_path'>('data_path')
const dirPickerCurrent = ref('')
const dirPickerDirs = ref<string[]>([])
const dirPickerParent = ref('')
const dirPickerLoading = ref(false)

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '-'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(2) + ' MB'
}

function refreshCacheSize() {
  cacheSize.value = getCacheSize()
}

async function loadIgnored() {
  try {
    ignoredProcesses.value = await api.getIgnoredProcesses()
  } catch {
    /* ignore */
  }
  try {
    ignoredHosts.value = await api.getIgnoredHosts()
  } catch {
    /* ignore */
  }
}

// 添加忽略进程：PID 和进程名至少填一个
async function addIgnored() {
  const pid = newIgnorePid.value
  const name = newIgnoreName.value.trim()
  if (pid === null && !name) {
    ElMessage.warning(t('settings.requirePidOrName'))
    return
  }
  try {
    // pid 为空时传 null，后端用 NULL 存储表示「仅按进程名忽略」
    const actualName = name || (pid !== null ? `PID ${pid}` : t('settings.ignoreByName'))
    await api.ignoreProcess({ pid: pid, name: actualName })
    ElMessage.success(t('settings.addedIgnoredProcess'))
    newIgnorePid.value = null
    newIgnoreName.value = ''
    await loadIgnored()
  } catch (e: any) {
    ElMessage.error(t('settings.addFailed', { msg: e?.message || e }))
  }
}

async function removeIgnored(rowId: number) {
  try {
    await api.unignoreProcess(rowId)
    ElMessage.success(t('settings.unignored'))
    await loadIgnored()
  } catch (e: any) {
    ElMessage.error(t('settings.operationFailed', { msg: e?.message || e }))
  }
}

// 添加忽略 host 通配符（支持 * ? 通配符）
async function addIgnoredHost() {
  const h = newIgnoreHost.value.trim()
  if (!h) {
    ElMessage.warning(t('settings.requireHostPattern'))
    return
  }
  try {
    await api.ignoreHost(h)
    ElMessage.success(t('settings.ignoredHostAdded', { host: h }))
    newIgnoreHost.value = ''
    await loadIgnored()
  } catch (e: any) {
    ElMessage.error(t('settings.addFailed', { msg: e?.message || e }))
  }
}

async function removeIgnoredHost(id: number) {
  try {
    await api.unignoreHost(id)
    ElMessage.success(t('settings.unignored'))
    await loadIgnored()
  } catch (e: any) {
    ElMessage.error(t('settings.operationFailed', { msg: e?.message || e }))
  }
}

async function openCurrentPath(path: string) {
  // 路径为空时后端使用默认数据目录并自动创建
  try {
    await api.openPath(path || '')
  } catch (e: any) {
    ElMessage.error(t('settings.openFailed', { msg: e?.message || e }))
  }
}

async function openDirPicker(target: 'data_path' | 'ai_path') {
  dirPickerTarget.value = target
  dirPickerVisible.value = true
  const current = form.value[target] || ''
  await loadDirs(current)
}

async function loadDirs(path: string) {
  dirPickerLoading.value = true
  try {
    const res = await api.listDirs(path || undefined)
    dirPickerCurrent.value = res.current
    dirPickerDirs.value = res.dirs
    dirPickerParent.value = res.parent
  } catch (e: any) {
    ElMessage.error(t('settings.readDirFailed', { msg: e?.message || e }))
  } finally {
    dirPickerLoading.value = false
  }
}

function selectDir(dir: string) {
  const full = dirPickerCurrent.value ? `${dirPickerCurrent.value}\\${dir}` : dir
  loadDirs(full)
}

function goParent() {
  if (dirPickerParent.value) loadDirs(dirPickerParent.value)
}

function confirmDir() {
  form.value[dirPickerTarget.value] = dirPickerCurrent.value
  dirPickerVisible.value = false
  // 路径通过选择器变更，标记 dirty 等待手动保存
  if (loaded.value) textDirty.value = true
}

// loaded 标志：load 完成后才启用自动保存，避免初始化触发
const loaded = ref(false)
// 文本/数字输入框 dirty 标志：修改后显示单独保存按钮，不参与自动保存
const textDirty = ref(false)
let autoSaveTimer: number | null = null
let savedMsgTimer: number | null = null

// ---------- Clash 集成配置 ----------
const clashEnabled = ref(false)
const clashTesting = ref(false)
const clashTestResult = ref<{ ok: boolean; msg: string } | null>(null)
// loadClashStatus 期间禁止 onClashToggle 触发（避免程序化设置 el-switch 时误调 doSave 覆盖后端）
let clashLoading = false

async function loadClashStatus() {
  clashLoading = true
  try {
    // 用轻量接口 /clash/enabled（只读 settings.json，不探测 Mihomo），
    // 避免后端刚重启或 Mihomo 不可达时等 1 秒 is_reachable 超时
    const r = await api.clashEnabledQuick()
    clashEnabled.value = !!r?.enabled
    // 同步到 form，避免 doSave 用旧值覆盖后端
    form.value.clash_enabled = !!r?.enabled
  } catch { /* ignore */ } finally {
    await nextTick()
    clashLoading = false
  }
}

async function onClashToggle(val: any) {
  // loadClashStatus 程序化设置触发的 change 忽略
  if (clashLoading) return
  // 设置页"启用Clash页"开关主要控制侧边栏入口可见性（clash_enabled）。
  // 关闭时联动关闭 clash_integrated（流量直连），避免用户关了 Clash 页但流量仍走代理。
  // 启用时不会自动开集成（需用户在 Clash 页手动开，因为要先探测 Mihomo 可达性）。
  form.value.clash_enabled = !!val
  try {
    await doSave()
    if (!val) {
      // 联动关闭集成：流量立即直连（后端 invalidate 缓存）
      try { await api.clashDisable() } catch { /* 集成本来就关着的话忽略 */ }
    }
    clashEnabled.value = !!val
    // 通知 App.vue 立即刷新侧边栏（无需手动刷新页面）
    window.dispatchEvent(new Event('telnix-clash-toggle'))
    if (val) {
      ElMessage.success(t('settings.clashPageEnabled'))
    } else {
      ElMessage.info(t('settings.clashPageDisabled'))
    }
  } catch (e: any) {
    ElMessage.error(t('settings.operationFailed', { msg: e.message }))
    clashEnabled.value = !val
    await loadClashStatus()
  }
}

async function testClashConnection() {
  clashTesting.value = true
  clashTestResult.value = null
  try {
    // 先保存配置再测试
    await doSave()
    // 用 /clash/test 强制探测可达性（不受 integrated 状态影响）
    const r = await api.clashTest()
    if (r.reachable && !r.error) {
      clashTestResult.value = { ok: true, msg: t('settings.clashConnSuccess', { ver: r.version || '?', port: r.mixed_port || '?' }) }
    } else if (r.reachable && r.error) {
      // 端口可达但 API 调用失败（如 secret 错误 401）
      clashTestResult.value = { ok: false, msg: t('settings.clashApiFailed', { err: r.error }) }
    } else {
      clashTestResult.value = { ok: false, msg: r.error || t('settings.mihomoOffline') }
    }
  } catch (e: any) {
    clashTestResult.value = { ok: false, msg: t('settings.clashConnFailed', { msg: e.message }) }
  } finally {
    clashTesting.value = false
  }
}

// ---------- Clash 教程对话框 ----------
const tutorialVisible = ref(false)
const tutorialHtml = ref('')
const tutorialLoading = ref(false)

// ---------- 代理引擎选择说明对话框 ----------
const engineHelpVisible = ref(false)
// markdown-it 实例：把 .\docs\xxx 反斜杠路径转为 /docs/xxx，让浏览器能加载
// 安全：html:false 禁用原始 HTML 渲染，防止教程文件被篡改后 XSS
const tutorialMd = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
})
// 安全：仅允许安全协议链接，阻断 javascript:/data:/vbscript: 等
tutorialMd.validateLink = (url: string): boolean => {
  const s = url.trim().toLowerCase()
  if (/^(#|\/|\.\/|\.\.\/|\?)/.test(s) || !/^[a-z][a-z0-9+.-]*:/.test(s)) return true
  return /^(https?|mailto|tel):/.test(s)
}

async function showTutorial() {
  tutorialVisible.value = true
  tutorialLoading.value = true
  tutorialHtml.value = ''
  try {
    const r = await api.clashTutorial()
    // 把 markdown 中的 .\docs\clash\1.png 转换为 /docs/clash/1.png
    // 同时把反斜杠转为正斜杠，确保浏览器能正确加载
    let content: string = r.content || ''
    content = content.replace(/\.\s*\\?\s*docs\s*\\/g, '/docs/')
                     .replace(/\\+/g, '/')
    tutorialHtml.value = tutorialMd.render(content)
  } catch (e: any) {
    // 错误消息用 escapeHtml 转义后拼接，避免注入
    const errMsg = String(e?.message || e).replace(/[<>&"']/g, (c) => ({
      '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;'
    }[c] || c))
    tutorialHtml.value = `<p style="color: var(--on-error)">${t('settings.tutorialLoadFailed', { msg: errMsg })}</p>`
  } finally {
    tutorialLoading.value = false
  }
}

// ---------- 手机抓包向导对话框 ----------
const mobileVisible = ref(false)
const mobileLoading = ref(false)
const mobileSetup = ref<{ lan_ip: string; proxy_host: string; proxy_port: number; api_port: number; cert_download_url: string; android_cert_url?: string } | null>(null)
const mobileQrDataUrl = ref('')

// ---------- 弱网模拟（Throttle） ----------
const throttle = ref({ enabled: false, latency_ms: 0, bps_kbps: 0, drop_pct: 0 })
let throttleSaveTimer: any = null

async function loadThrottle() {
  try {
    throttle.value = await api.getThrottle()
  } catch { /* ignore */ }
}

function goDelayRules() {
  flows.rememberPage('/settings')
  router.push('/delay')
}

async function saveThrottle() {
  // 防抖：连续修改时只保留最后一次
  if (throttleSaveTimer) clearTimeout(throttleSaveTimer)
  throttleSaveTimer = setTimeout(async () => {
    try {
      await api.setThrottle({ ...throttle.value })
    } catch (e: any) {
      ElMessage.error(t('settings.throttleSaveFailed', { msg: e.message }))
    }
  }, 300)
}

async function showMobileWizard() {
  mobileVisible.value = true
  mobileLoading.value = true
  mobileSetup.value = null
  mobileQrDataUrl.value = ''
  try {
    const r = await api.mobileSetup()
    mobileSetup.value = r
    // 生成证书下载 URL 的二维码（手机扫码直接下载 .pem）
    mobileQrDataUrl.value = await QRCode.toDataURL(r.cert_download_url, {
      width: 240,
      margin: 1,
      color: { dark: '#000000', light: '#ffffff' },
    })
  } catch (e: any) {
    ElMessage.error(t('settings.mobileSetupFailed', { msg: e.message }))
  } finally {
    mobileLoading.value = false
  }
}

// 下载根证书到本机（用相对路径避免跨域：cert_download_url 是局域网 IP，
// 前端从 127.0.0.1 访问会跨域导致 fetch 失败）
async function downloadCert() {
  if (!mobileSetup.value?.cert_download_url) return
  try {
    // 提取路径部分（/api/cert/root.pem），用相对路径访问当前 origin
    const u = new URL(mobileSetup.value.cert_download_url)
    const resp = await fetch(u.pathname)
    if (!resp.ok) throw new Error(t('settings.downloadFailedHttpStatus', { status: resp.status }))
    const blob = await resp.blob()
    const objUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objUrl
    a.download = 'telnix_root.pem'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(objUrl)
    ElMessage.success(t('settings.certDownloaded'))
  } catch (e: any) {
    ElMessage.error(t('settings.certDownloadFailed', { msg: e.message }))
  }
}

// 下载安卓 7+ 系统证书格式（文件名 <hash>.0，已由后端计算好哈希）
async function downloadAndroidCert() {
  if (!mobileSetup.value?.android_cert_url) return
  try {
    const u = new URL(mobileSetup.value.android_cert_url)
    const resp = await fetch(u.pathname)
    if (!resp.ok) throw new Error(t('settings.downloadFailedHttpStatus', { status: resp.status }))
    // 从 Content-Disposition 提取文件名（后端已计算为 <hash>.0）
    const cd = resp.headers.get('content-disposition') || ''
    let fname = 'telnix_android.0'
    const m = /filename="?([^";]+)"?/.exec(cd)
    if (m) fname = m[1]
    const blob = await resp.blob()
    const objUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objUrl
    a.download = fname
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(objUrl)
    ElMessage.success(t('settings.androidCertDownloaded', { fname }))
  } catch (e: any) {
    ElMessage.error(t('settings.certDownloadFailed', { msg: e.message }))
  }
}

// 打开安卓 7+ 系统证书安装教程
function openAndroidTutorial(type: 'mumu' | 'leidian') {
  const urls = {
    mumu: 'https://www.cnblogs.com/wutou/p/17873632.html',
    leidian: 'https://zhuanlan.zhihu.com/p/259255855',
  }
  window.open(urls[type], '_blank', 'noopener')
}

// "允许局域网设备连接"开关：写 proxy_listen_host，需要重启后端生效
async function onLanToggle(val: any) {
  form.value.proxy_listen_host = val ? '0.0.0.0' : '127.0.0.1'
  try {
    await api.saveSettings({ proxy_listen_host: form.value.proxy_listen_host } as Settings)
    if (val) {
      ElMessageBox.alert(
        t('settings.lanEnabledRestartNeeded'),
        t('settings.notice'),
        { confirmButtonText: t('settings.gotIt') }
      )
    } else {
      ElMessage.success(t('settings.switchedToLocalListen'))
    }
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e.message }))
  }
}

// 代理引擎切换：保存设置并提示重启后端生效
async function onProxyEngineChange(val: any) {
  try {
    await doSave()
    ElMessageBox.alert(
      t('settings.engineSwitchedRestartNeeded'),
      t('settings.notice'),
      { confirmButtonText: t('settings.gotIt') }
    )
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e.message }))
  }
}

// ---------- 端口配置 ----------
// 端口范围校验：1024-65535
function isValidPort(v: any): boolean {
  const n = Number(v)
  return Number.isInteger(n) && n >= 1024 && n <= 65535
}

// 端口配置保存：单独保存端口相关字段，并提示重启生效
// 随机端口开关切换时也走这个流程（开关立即保存，数字框需手动点保存）
async function onPortSettingSave() {
  // 校验端口数字（随机端口模式下端口值不会用到，仍校验避免脏数据）
  if (!isValidPort(form.value.api_port)) {
    ElMessage.error(t('settings.portInvalid'))
    return
  }
  if (!isValidPort(form.value.proxy_port)) {
    ElMessage.error(t('settings.portInvalid'))
    return
  }
  try {
    await api.saveSettings({
      api_port: Number(form.value.api_port),
      proxy_port: Number(form.value.proxy_port),
      random_port: form.value.random_port ? '1' : '0',
    } as Settings)
    textDirty.value = false
    ElMessageBox.alert(
      t('settings.portSavedRestartNeeded'),
      t('settings.notice'),
      { confirmButtonText: t('settings.gotIt') }
    )
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e.message }))
  }
}

// 随机端口开关切换：立即保存（不触发 textDirty，因为已即时保存）
async function onRandomPortToggle(val: any) {
  form.value.random_port = !!val
  // 随机端口开启时不需要校验端口号（不会用到），但仍保存原值方便用户切回时使用
  try {
    await api.saveSettings({ random_port: val ? '1' : '0' } as Settings)
    ElMessage.success(t('settings.saved'))
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e.message }))
  }
}

// ---------- mitmproxy ----------
// mitmproxy 是必选依赖（pyproject.toml），正常情况下始终可用。
// mitmproxyAvailable 由后端 /settings 接口注入（MITMPROXY_AVAILABLE），
// 仅在 import 异常时为 false（如依赖损坏），提示用户重新安装。

async function load() {
  // 先从 localStorage 立即恢复（避免页面空白等待后端）
  try {
    const cached = localStorage.getItem('telnix_settings_cache')
    if (cached) {
      const s = JSON.parse(cached)
      form.value = { ...s }
      form.value.inspector_tabs = Array.isArray(s.inspector_tabs) ? s.inspector_tabs : []
      form.value.flow_columns = Array.isArray(s.flow_columns) ? s.flow_columns : []
      // proxy_engine 默认 builtin
      if (!form.value.proxy_engine) form.value.proxy_engine = 'builtin'
      // mitmproxy 可用性也从缓存恢复（避免异步加载前短暂显示"加载失败"）
      mitmproxyAvailable.value = !!s.mitmproxy_available
      // autoScroll/autoScrollDelay 由 store 自己从 localStorage 持久化，不在这里覆盖
    }
  } catch { /* ignore */ }
  // 异步从后端加载最新值
  try {
    const s = await api.getSettings()
    form.value = { ...s }
    form.value.inspector_tabs = Array.isArray(s.inspector_tabs) ? s.inspector_tabs : []
    form.value.flow_columns = Array.isArray(s.flow_columns) ? s.flow_columns : []
    // deepseek_model 默认 flash
    if (!form.value.deepseek_model) form.value.deepseek_model = 'deepseek-v4-flash'
    // anthropic_model 默认 claude-3-5-sonnet-20241022
    if (!form.value.anthropic_model) form.value.anthropic_model = 'claude-3-5-sonnet-20241022'
    // clash 默认值
    if (!form.value.clash_api_url) form.value.clash_api_url = 'http://127.0.0.1:9090'
    // clash_secret: 非回环访问时后端不返回密钥（只返回 has_clash_secret 布尔值），
    // 不要设为空字符串，否则保存时会覆盖真实密钥导致 Clash 401
    if (form.value.clash_secret === undefined) form.value.clash_secret = ''
    if (form.value.clash_mixed_port == null) form.value.clash_mixed_port = 0
    // proxy_listen_host 默认 127.0.0.1（仅本机）
    if (!form.value.proxy_listen_host) form.value.proxy_listen_host = '127.0.0.1'
    // proxy_engine 默认 builtin（内置线程代理）
    if (!form.value.proxy_engine) form.value.proxy_engine = 'builtin'
    // 端口配置默认值（未设置时用后端默认 18901/8888）
    if (!form.value.api_port) form.value.api_port = 18901
    if (!form.value.proxy_port) form.value.proxy_port = 8888
    // random_port 后端存的是字符串 "0"/"1"，前端转为布尔
    form.value.random_port = form.value.random_port === '1' || form.value.random_port === true
    // mitmproxy 可用性（后端注入，控制下拉选项是否可选）
    mitmproxyAvailable.value = !!s.mitmproxy_available
    // 语言：后端 settings.json 中的 lang 优先（跨设备同步）
    if (s.lang === 'zh' || s.lang === 'en') {
      const cur = getLang()
      if (cur !== s.lang) {
        setLang(s.lang)
        langMode.value = s.lang
      }
    }
    // autoScroll/autoScrollDelay 由 store 自己持久化，不从后端覆盖
    // 同步到 localStorage（剔除敏感凭据，避免明文密钥落盘到浏览器存储）
    const _cacheS = { ...s }
    for (const _k of ['deepseek_api_key', 'clash_secret', 'api_token']) {
      delete _cacheS[_k]
    }
    localStorage.setItem('telnix_settings_cache', JSON.stringify(_cacheS))
  } catch {
    /* ignore */
  }
  try {
    const r = await api.getCertStatus()
    certInstalled.value = r.installed
  } catch {
    /* ignore */
  }
  // 加载缓存设置
  cacheThreshold.value = parseFloat(localStorage.getItem('telnix_cache_threshold') || '10')
  cacheAutoClean.value = localStorage.getItem('telnix_cache_autoclean') !== 'false'
  refreshCacheSize()
  await loadIgnored()
  loaded.value = true
}

// 自动保存：仅开关/复选框使用（immediate=true 立即保存）
// 文本/数字输入框不参与自动保存，通过 saveText() 手动保存
function autoSave(immediate: boolean = false) {
  if (!loaded.value) return
  if (immediate) {
    if (autoSaveTimer !== null) { clearTimeout(autoSaveTimer); autoSaveTimer = null }
    doSave()
    return
  }
  // 兼容旧调用（不应再被触发）
  if (autoSaveTimer !== null) clearTimeout(autoSaveTimer)
  autoSaveTimer = window.setTimeout(doSave, 500)
}

async function doSave() {
  saving.value = true
  try {
    // 同步自动滚动设置到后端
    form.value.auto_scroll = flows.autoScroll
    form.value.auto_scroll_delay = flows.autoScrollDelay
    await api.saveSettings(form.value)
    // 同步到 localStorage：剥离敏感凭据，避免明文密钥落盘
    const _cacheS = { ...form.value }
    for (const _k of ['deepseek_api_key', 'clash_secret', 'api_token']) {
      delete _cacheS[_k]
    }
    localStorage.setItem('telnix_settings_cache', JSON.stringify(_cacheS))
    // 保存缓存设置到 localStorage
    localStorage.setItem('telnix_cache_threshold', String(cacheThreshold.value))
    localStorage.setItem('telnix_cache_autoclean', String(cacheAutoClean.value))
    // 同步 GUI 偏好（列顺序/导航顺序/主题等）到 settings.json
    syncPrefs(false)
    // 文本框修改已保存，清除 dirty 标志
    textDirty.value = false
    // 轻量提示"已保存"（节流，避免频繁弹窗）
    showSavedTip()
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { msg: e?.message || e }))
  } finally {
    saving.value = false
  }
}

// 文本/数字输入框手动保存
function saveText() {
  doSave()
}

function showSavedTip() {
  // 用 ElMessage 节流显示
  if (savedMsgTimer !== null) return
  ElMessage({ message: t('settings.saved'), type: 'success', duration: 1200 })
  savedMsgTimer = window.setTimeout(() => { savedMsgTimer = null }, 1500)
}

// 不再监听 form 整体变化自动保存（文本框改为手动保存）
// 监听缓存设置变化（纯 localStorage 持久化，不需要后端保存）
watch([cacheThreshold, cacheAutoClean], () => {
  if (!loaded.value) return
  localStorage.setItem('telnix_cache_threshold', String(cacheThreshold.value))
  localStorage.setItem('telnix_cache_autoclean', String(cacheAutoClean.value))
})
// 自动滚动开关/暂停秒数：立即持久化到 localStorage（store 单例，切换页面不丢失）
watch(() => flows.autoScroll, (v) => {
  flows.persistAutoScroll()
  // 同步到后端 settings
  if (loaded.value) autoSave(true)
})
watch(() => flows.autoScrollDelay, (v) => {
  flows.persistAutoScroll()
  // 同步到后端 settings
  if (loaded.value) autoSave(true)
})

async function installCert() {
  try {
    await ElMessageBox.confirm(t('settings.installCertConfirm'), t('settings.installCert'), {
      confirmButtonText: t('settings.install'), cancelButtonText: t('settings.cancel'), type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.installCert()
    await capture.fetchStatus()
    const r = await api.getCertStatus()
    certInstalled.value = r.installed
    ElMessage.success(certInstalled.value ? t('settings.certInstalled') : t('settings.installSubmitted'))
  } catch (e: any) {
    ElMessage.error(t('settings.installFailed', { msg: e?.message || e }))
  }
}

function toggleTab(key: string) {
  if (!form.value.inspector_tabs) form.value.inspector_tabs = []
  const idx = form.value.inspector_tabs.indexOf(key)
  if (idx >= 0) form.value.inspector_tabs.splice(idx, 1)
  else form.value.inspector_tabs.push(key)
  autoSave(true)
}

function tabOn(key: string): boolean {
  return !!form.value.inspector_tabs && form.value.inspector_tabs.includes(key)
}

function toggleColumn(key: string) {
  if (!form.value.flow_columns) form.value.flow_columns = []
  const idx = form.value.flow_columns.indexOf(key)
  if (idx >= 0) form.value.flow_columns.splice(idx, 1)
  else form.value.flow_columns.push(key)
  autoSave(true)
}

function colOn(key: string): boolean {
  return !!form.value.flow_columns && form.value.flow_columns.includes(key)
}

async function reset() {
  try {
    await ElMessageBox.confirm(t('settings.resetConfirm'), t('settings.reset'), {
      confirmButtonText: t('settings.reset'), cancelButtonText: t('settings.cancel'), type: 'warning',
    })
  } catch {
    return
  }
  form.value = { inspector_tabs: [], flow_columns: [] }
  flows.autoScroll = false
  flows.autoScrollDelay = 3
  cacheThreshold.value = 10
  cacheAutoClean.value = true
  // 清除本地缓存，避免重置后又被旧缓存覆盖
  localStorage.removeItem('telnix_settings_cache')
  localStorage.setItem('telnix_cache_threshold', String(cacheThreshold.value))
  localStorage.setItem('telnix_cache_autoclean', String(cacheAutoClean.value))
  textDirty.value = false
  // 手动保存到后端（不再有 watch 自动触发）
  autoSave(true)
  ElMessage.success(t('settings.resetDone'))
}

function clearCache() {
  clearFlowCache()
  refreshCacheSize()
  ElMessage.success(t('settings.cacheCleared'))
}

// ---------- 数据库维护 ----------
const dbStats = ref({ path: '', size_mb: 0, flows: 0, sessions: 0, rules: 0, ai_chats: 0, ai_messages: 0 })
const dbLoading = ref(false)
const clearAllDialogVisible = ref(false)
const clearAllCountdown = ref(3)
let clearAllTimer: number | null = null

async function loadDbStats() {
  try {
    dbStats.value = await api.dbStats()
  } catch { /* ignore */ }
}

async function cleanupDb() {
  try {
    await ElMessageBox.confirm(t('settings.dbCleanupConfirm'), t('settings.dbCleanup'), {
      confirmButtonText: t('settings.clearNow'),
      cancelButtonText: t('settings.cancel'),
      type: 'warning',
    })
  } catch {
    return
  }
  dbLoading.value = true
  try {
    const r = await api.cleanupDb()
    ElMessage.success(t('settings.dbCleanupDone', {
      before: r.before_mb,
      after: r.after_mb,
      reclaimed: r.reclaimed_mb,
    }))
    await loadDbStats()
  } catch (e: any) {
    ElMessage.error(t('settings.dbCleanupFailed', { msg: e?.message || e }))
  } finally {
    dbLoading.value = false
  }
}

async function openSettingsFile() {
  try {
    await api.openSettingsFile()
    ElMessage.success(t('settings.openedSettingsFile'))
  } catch (e: any) {
    ElMessage.error(t('settings.openFailed', { msg: e?.message || e }))
  }
}

const clearTypeLabelMap: Record<string, string> = {
  flows: 'settings.clearFlows',
  sessions: 'settings.clearSessions',
  rules: 'settings.clearRules',
  ai_chats: 'settings.clearAiChats',
}

async function clearData(type: string) {
  const typeLabel = t(clearTypeLabelMap[type] || type)
  try {
    await ElMessageBox.confirm(
      t('settings.clearDataConfirm', { type: typeLabel }),
      t('settings.clearAllTitle'),
      { confirmButtonText: t('settings.clearNow'), cancelButtonText: t('settings.cancel'), type: 'warning' }
    )
  } catch {
    return
  }
  dbLoading.value = true
  try {
    const r = await api.clearData(type)
    ElMessage.success(t('settings.clearDataDone', { type: typeLabel, n: r.deleted ?? 0 }))
    await loadDbStats()
  } catch (e: any) {
    ElMessage.error(t('settings.clearDataFailed', { msg: e?.message || e }))
  } finally {
    dbLoading.value = false
  }
}

function stopClearAllTimer() {
  if (clearAllTimer) {
    clearInterval(clearAllTimer)
    clearAllTimer = null
  }
}

function openClearAllDialog() {
  clearAllDialogVisible.value = true
  clearAllCountdown.value = 3
  stopClearAllTimer()
  clearAllTimer = window.setInterval(() => {
    if (clearAllCountdown.value > 0) {
      clearAllCountdown.value -= 1
    } else {
      stopClearAllTimer()
    }
  }, 1000)
}

function closeClearAllDialog() {
  clearAllDialogVisible.value = false
  stopClearAllTimer()
}

async function doClearAll() {
  if (clearAllCountdown.value > 0) return
  closeClearAllDialog()
  dbLoading.value = true
  try {
    const r = await api.clearData('all')
    ElMessage.success(t('settings.clearAllDone', {
      flows: r.flows ?? 0,
      sessions: r.sessions ?? 0,
      rules: r.rules ?? 0,
      ai_chats: r.ai_chats ?? 0,
    }))
    await loadDbStats()
  } catch (e: any) {
    ElMessage.error(t('settings.clearDataFailed', { msg: e?.message || e }))
  } finally {
    dbLoading.value = false
  }
}

onMounted(() => {
  load()
  loadClashStatus()
  loadCopyPrefs()
  loadThrottle()
  loadTransparentStatus()
  loadDbStats()
  loadAiUsage()
  loadPerfConfig()
  loadSslConfig()
  loadCertDetails()
  // 透明代理运行时每 5s 轮询统计
  transparentPollTimer = window.setInterval(() => {
    if (transparentProxy.value.running) loadTransparentStatus()
  }, 5000)
})

onUnmounted(() => {
  if (transparentPollTimer) {
    clearInterval(transparentPollTimer)
    transparentPollTimer = null
  }
  stopClearAllTimer()
})

// 左侧锚点导航分组（顺序与右侧 section 实际渲染顺序一致）
const groups = computed(() => [
  { id: 'sec-appearance', label: t('settings.appearance'), icon: 'Brush' },
  { id: 'sec-capture', label: t('settings.captureBehavior'), icon: 'Aim' },
  { id: 'sec-transparent', label: t('settings.transparentProxy'), icon: 'Connection' },
  { id: 'sec-display', label: t('settings.displayOptions'), icon: 'View' },
  { id: 'sec-processes', label: t('settings.ignoreRules'), icon: 'Cpu' },
  { id: 'sec-clash', label: t('settings.clashIntegration'), icon: 'ClashIcon' },
  { id: 'sec-cert', label: t('settings.httpsCert'), icon: 'Lock' },
  { id: 'sec-performance', label: t('settings.performanceConfig'), icon: 'Odometer' },
  { id: 'sec-ssl', label: t('settings.sslTlsConfig'), icon: 'Key' },
  { id: 'sec-mobile', label: t('settings.mobileCapture'), icon: 'Iphone' },
  { id: 'sec-throttle', label: t('settings.throttle'), icon: 'Connection' },
  { id: 'sec-ports', label: t('settings.portConfig'), icon: 'Connection' },
  { id: 'sec-paths', label: t('settings.pathConfig'), icon: 'FolderOpened' },
  { id: 'sec-ai', label: t('settings.aiConfig'), icon: 'MagicStick' },
  { id: 'sec-cache', label: t('settings.browserCache'), icon: 'Coin' },
  { id: 'sec-db', label: t('settings.dbMaintenance'), icon: 'DataLine' },
  { id: 'sec-settings-file', label: t('settings.settingsFile'), icon: 'Document' },
])
const activeGroup = ref('sec-appearance')
const contentRef = ref<HTMLElement | null>(null)

function scrollTo(id: string) {
  const el = document.getElementById(id)
  if (el && contentRef.value) {
    // 用 getBoundingClientRect 相对计算，避免 offsetParent 不是滚动容器导致偏移错误
    // （.settings-content 未设 position:relative 时，offsetTop 会相对更外层祖先）
    const containerRect = contentRef.value.getBoundingClientRect()
    const elRect = el.getBoundingClientRect()
    const offset = elRect.top - containerRect.top + contentRef.value.scrollTop - 8
    contentRef.value.scrollTo({ top: Math.max(0, offset), behavior: 'smooth' })
    activeGroup.value = id
  }
}

function onScroll() {
  if (!contentRef.value) return
  const containerTop = contentRef.value.getBoundingClientRect().top
  // 找到当前可见的第一个 section（相对滚动容器顶部计算）
  let current = groups.value[0].id
  for (const g of groups.value) {
    const el = document.getElementById(g.id)
    if (el) {
      const relTop = el.getBoundingClientRect().top - containerTop
      if (relTop - 40 <= 0) {
        current = g.id
      }
    }
  }
  activeGroup.value = current
}
</script>

<template>
  <div class="settings-view full flex flex-col">
    <div class="page-header">
      <div class="page-title no-select"><el-icon><Setting /></el-icon>&nbsp;{{ t('settings.settings') }}</div>
      <div class="header-actions">
        <el-button v-if="textDirty" type="primary" size="small" :loading="saving" @click="saveText">
          <el-icon><Check /></el-icon>&nbsp;{{ t('settings.save') }}
        </el-button>
        <el-button size="small" @click="reset">
          <el-icon><RefreshLeft /></el-icon>&nbsp;{{ t('settings.reset') }}
        </el-button>
      </div>
    </div>

    <div class="settings-body flex-1 overflow-hidden flex">
      <!-- 左侧锚点导航 -->
      <div class="settings-nav">
        <a
          v-for="g in groups"
          :key="g.id"
          class="settings-nav-item no-select"
          :class="{ active: activeGroup === g.id }"
          @click="scrollTo(g.id)"
        >
          <el-icon><component :is="g.icon" /></el-icon>
          <span>{{ g.label }}</span>
        </a>
      </div>

      <!-- 右侧设置内容（滚动区） -->
      <div class="settings-content flex-1 overflow-auto" ref="contentRef" @scroll="onScroll">
        <!-- 外观 -->
        <div class="section" id="sec-appearance">
          <div class="section-title no-select"><el-icon><Brush /></el-icon>&nbsp;{{ t('settings.appearance') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.theme')">
              <el-radio-group v-model="themeMode" @change="onThemeChange">
                <el-radio-button value="dark">{{ t('settings.dark') }}</el-radio-button>
                <el-radio-button value="light">{{ t('settings.light') }}</el-radio-button>
              </el-radio-group>
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.themeHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.language')">
              <el-radio-group v-model="langMode" @change="onLangChange">
                <el-radio-button value="zh">{{ t('settings.chinese') }}</el-radio-button>
                <el-radio-button value="en">{{ t('settings.english') }}</el-radio-button>
              </el-radio-group>
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.languageHint') }}</span>
            </el-form-item>
          </el-form>
        </div>

        <!-- 抓包行为 -->
        <div class="section" id="sec-capture">
          <div class="section-title"><el-icon><Aim /></el-icon>&nbsp;{{ t('settings.captureBehavior') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.proxyEngine')">
              <el-select v-model="form.proxy_engine" style="max-width: 260px" @change="onProxyEngineChange">
                <el-option value="builtin" :label="t('settings.engineBuiltin')" />
                <el-option value="v2" :label="t('settings.engineV2')" />
                <el-option value="async" :label="t('settings.engineAsync')" />
                <el-option value="mitmproxy" :label="t('settings.engineMitmproxy')" :disabled="!mitmproxyAvailable" />
              </el-select>
              <span class="hint text-dim" style="margin-left: 12px">
                <span v-if="!mitmproxyAvailable" style="color: var(--on-warn)">{{ t('settings.mitmproxyFailed') }}</span>
              </span>
            </el-form-item>
            <el-form-item :label="t('settings.autoScroll')">
              <el-switch v-model="flows.autoScroll" @change="autoSave(true)" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.autoScrollHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.scrollPauseSeconds')">
              <el-input-number v-model="flows.autoScrollDelay" :min="0" :max="60" :step="1" controls-position="right" @change="textDirty = true" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.scrollPauseHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.autoSwitchTab')">
              <el-switch v-model="form.auto_switch_preview" :active-value="'1'" :inactive-value="'0'" @change="autoSave(true)" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.autoSwitchTabHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.multiSelectBarDelay')">
              <el-input-number v-model="form.multi_select_bar_delay" :min="0" :max="10" :step="0.5" controls-position="right" @change="textDirty = true" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.multiSelectDelayHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.triggerCaptureEnabled')">
              <el-switch v-model="form.trigger_capture_enabled" @change="autoSave(true)" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.triggerCaptureEnabledHint') }}</span>
            </el-form-item>
          </el-form>
        </div>

        <!-- 透明代理 -->
        <div class="section" id="sec-transparent">
          <div class="section-title no-select"><el-icon><Connection /></el-icon>&nbsp;{{ t('settings.transparentProxy') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.transparentProxyMode')">
              <el-switch
                :model-value="transparentProxy.running"
                :loading="transparentLoading"
                :disabled="!transparentProxy.supported"
                @change="onToggleTransparent"
              />
              <span class="hint text-dim" style="margin-left: 12px">
                <span v-if="!transparentProxy.supported" style="color: var(--on-warn)">
                  {{ transparentProxy.hint || t('settings.platformNotSupported') }}
                </span>
                <span v-else-if="transparentProxy.running" style="color: var(--on-ok)">
                  {{ t('settings.transparentEnabled', { name: transparentBackendName }) }}
                </span>
                <span v-else>{{ t('settings.transparentOff', { name: transparentBackendName }) }}</span>
              </span>
              <el-tag v-if="transparentProxy.supported && transparentProxy.backend" size="small" type="info" style="margin-left: 12px">
                {{ transparentBackendName }}
              </el-tag>
              <el-tag v-if="transparentProxy.supported && !transparentAdminLoading" size="small" :type="transparentIsAdmin ? 'success' : 'danger'" style="margin-left: 4px">
                <span v-if="transparentIsAdmin">{{ transparentAdminTerm }}</span>
                <span v-else>{{ t('settings.nonAdmin', { term: transparentAdminTerm }) }}</span>
              </el-tag>
              <el-button
                v-if="transparentProxy.supported && !transparentAdminLoading && !transparentIsAdmin"
                size="small"
                type="warning"
                style="margin-left: 12px"
                :loading="transparentRestarting"
                @click="restartTransparentAsAdmin"
              >
                <el-icon><Key /></el-icon>&nbsp;{{ isWindowsBackend ? t('settings.adminRestart') : t('settings.elevateRestart') }}
              </el-button>
            </el-form-item>
            <el-form-item v-if="transparentProxy.running" :label="t('settings.redirectStats')">
              <span class="text-dim mono" style="font-size: 12px">
                {{ t('settings.redirectStatsDetail', { packets: transparentProxy.redirected_count || 0, nat: transparentProxy.nat_table_size || 0, port: transparentProxy.local_port || 8888 }) }}
              </span>
              <el-button text size="small" style="margin-left: 12px" @click="loadTransparentStatus">{{ t('settings.refresh') }}</el-button>
            </el-form-item>
            <el-form-item v-if="transparentProxy.last_error" :label="t('settings.lastError')">
              <span style="color: var(--on-error); font-size: 12px">{{ transparentProxy.last_error }}</span>
            </el-form-item>
          </el-form>
          <div class="hint text-dim" style="margin: 4px 12px 0; padding: 8px 12px; background: var(--on-bg); border-radius: 4px">
            {{ t('settings.transparentExperimentalHint') }}
          </div>
        </div>

        <!-- 显示选项（合并流量列表列 + 检查器标签） -->
        <div class="section" id="sec-display">
          <div class="section-title"><el-icon><View /></el-icon>&nbsp;{{ t('settings.displayOptions') }}</div>
          <div class="sub-label">{{ t('settings.flowListColumns') }}</div>
          <div class="tab-checkboxes" style="margin-bottom: 12px">
            <el-checkbox
              v-for="c in optionalColumns"
              :key="c.key"
              :model-value="colOn(c.key)"
              @change="() => toggleColumn(c.key)"
            >{{ c.label }}</el-checkbox>
          </div>
          <div class="hint text-dim" style="margin-bottom: 16px">{{ t('settings.flowListColumnsHint') }}</div>

          <div class="sub-label">{{ t('settings.copyFields') }}</div>
          <div class="tab-checkboxes" style="margin-bottom: 8px">
            <el-checkbox
              v-for="c in copyFieldOptions"
              :key="c.key"
              :model-value="copyFieldOn(c.key)"
              @change="() => toggleCopyField(c.key)"
            >{{ c.label }}</el-checkbox>
          </div>
          <div class="hint text-dim" style="margin-bottom: 12px">{{ t('settings.copyFieldsHint') }}</div>

          <div class="sub-label">{{ t('settings.inspectorTabs') }}</div>
          <div class="tab-checkboxes" style="margin-bottom: 8px">
            <el-checkbox
              v-for="tb in optionalTabs"
              :key="tb.key"
              :model-value="tabOn(tb.key)"
              @change="() => toggleTab(tb.key)"
            >{{ tb.label }}</el-checkbox>
          </div>
          <div class="hint text-dim">{{ t('settings.inspectorTabsHint') }}</div>
        </div>

        <!-- 进程过滤 + Host 忽略 -->
        <div class="section" id="sec-processes">
          <div class="section-title"><el-icon><Cpu /></el-icon>&nbsp;{{ t('settings.ignoreRules') }}</div>
          <div class="hint text-dim" style="margin-bottom: 12px">
            {{ t('settings.ignoreRulesHint') }}
          </div>
          <div class="ignore-add-row">
            <el-input-number v-model="newIgnorePid" :controls="false" :placeholder="t('settings.pidOptional')" style="width: 130px" :min="1" />
            <span class="text-dim" style="font-size: 12px">{{ t('settings.or') }}</span>
            <el-input v-model="newIgnoreName" :placeholder="t('settings.processNameOptional')" style="width: 240px" class="mono" />
            <el-button type="primary" size="small" @click="addIgnored" :disabled="newIgnorePid === null && !newIgnoreName.trim()">
              <el-icon><Plus /></el-icon>&nbsp;{{ t('settings.add') }}
            </el-button>
          </div>
          <el-table v-if="ignoredProcesses.length" :data="ignoredProcesses" size="small" style="margin-top: 10px; max-width: 600px; max-height: 240px; overflow-y: auto" border>
            <el-table-column prop="pid" :label="t('settings.pid')" width="100">
              <template #default="{ row }">
                <span v-if="row.pid != null && row.pid > 0">{{ row.pid }}</span>
                <span v-else class="text-dim">{{ t('settings.byName') }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="process_name" :label="t('settings.processName')" />
            <el-table-column :label="t('settings.action')" width="100">
              <template #default="{ row }">
                <el-button link type="danger" size="small" @click="removeIgnored(row.id)">
                  <el-icon><Delete /></el-icon>&nbsp;{{ t('settings.remove') }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <div v-else class="hint text-dim" style="margin-top: 8px">{{ t('settings.noIgnoredProcesses') }}</div>

          <!-- 忽略 Host 通配符 -->
          <div class="ignore-add-row" style="margin-top: 16px">
            <el-input v-model="newIgnoreHost" :placeholder="t('settings.hostWildcardPlaceholder')" style="width: 320px" class="mono" @keyup.enter="addIgnoredHost" />
            <el-button type="primary" size="small" @click="addIgnoredHost" :disabled="!newIgnoreHost.trim()">
              <el-icon><Plus /></el-icon>&nbsp;{{ t('settings.addHost') }}
            </el-button>
          </div>
          <el-table v-if="ignoredHosts.length" :data="ignoredHosts" size="small" style="margin-top: 10px; max-width: 600px" border>
            <el-table-column prop="host_pattern" :label="t('settings.hostWildcard')" />
            <el-table-column :label="t('settings.action')" width="100">
              <template #default="{ row }">
                <el-button link type="danger" size="small" @click="removeIgnoredHost(row.id)">
                  <el-icon><Delete /></el-icon>&nbsp;{{ t('settings.remove') }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <div v-else class="hint text-dim" style="margin-top: 8px">{{ t('settings.noIgnoredHosts') }}</div>
        </div>

        <!-- Clash 集成 -->
        <div class="section" id="sec-clash">
          <div class="section-title"><el-icon><ClashIcon /></el-icon>&nbsp;{{ t('settings.clashIntegration') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.enableClashPage')">
              <el-switch v-model="clashEnabled" @change="onClashToggle" />
              <span class="hint text-dim" style="margin-left: 12px">
                {{ t('settings.enableClashPageHint') }}
              </span>
            </el-form-item>
            <el-form-item :label="t('settings.externalControllerAddr')">
              <el-input v-model="form.clash_api_url" placeholder="http://127.0.0.1:9090" class="mono" style="max-width: 420px" @input="textDirty = true" />
            </el-form-item>
            <el-form-item :label="t('settings.apiSecret')">
              <el-input v-model="form.clash_secret" :placeholder="t('settings.noSecretPlaceholder')" show-password class="mono" style="max-width: 420px" @input="textDirty = true" />
            </el-form-item>
            <el-form-item :label="t('settings.mixedProxyPort')">
              <el-input-number v-model="form.clash_mixed_port" :min="0" :max="65535" :step="1" style="width: 150px" @change="textDirty = true" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.zeroAutoFromMihomo') }}</span>
            </el-form-item>
            <el-form-item>
              <el-button @click="testClashConnection" :loading="clashTesting">{{ t('settings.testConnection') }}</el-button>
              <el-button @click="showTutorial">{{ t('settings.viewTutorial') }}</el-button>
              <span v-if="clashTestResult" class="hint" :style="{ color: clashTestResult.ok ? 'var(--on-ok)' : 'var(--on-error)', marginLeft: '12px' }">
                {{ clashTestResult.msg }}
              </span>
            </el-form-item>
            <div class="hint text-dim" style="margin-left: 160px">
              {{ t('settings.clashExternalModeDesc') }}
            </div>
          </el-form>
        </div>

        <!-- HTTPS 证书 -->
        <div class="section" id="sec-cert">
          <div class="section-title"><el-icon><Lock /></el-icon>&nbsp;{{ t('settings.httpsCert') }}</div>
          <div class="cert-row">
            <span class="cert-status">
              <el-icon :class="{ ok: certInstalled }">
                <component :is="certInstalled ? 'CircleCheck' : 'WarningFilled'" />
              </el-icon>
              {{ certInstalled ? t('settings.certInstalledEnabled') : t('settings.certNotInstalled') }}
            </span>
            <el-button v-if="!certInstalled" type="primary" size="small" @click="installCert">{{ t('settings.installCert') }}</el-button>
            <el-button v-else size="small" @click="installCert">{{ t('settings.reinstall') }}</el-button>
          </div>
        </div>

        <!-- 证书生命周期管理 -->
        <div class="section" id="sec-cert-lifecycle">
          <div class="section-title"><el-icon><Timer /></el-icon>&nbsp;{{ t('settings.certLifecycle') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.certValidityCountdown')">
              <template v-if="certDetails">
                <el-tag v-if="certDetails.expiry_countdown !== null" :type="certDetails.expiry_countdown <= 30 ? 'danger' : certDetails.expiry_countdown <= 90 ? 'warning' : 'success'">
                  {{ certDetails.expiry_countdown <= 0 ? t('settings.certExpired') : t('settings.certDaysLeft', { days: certDetails.expiry_countdown }) }}
                </el-tag>
                <span v-else class="text-dim">-</span>
              </template>
              <el-button size="small" style="margin-left: 12px" @click="loadCertDetails" :loading="certDetailsLoading">{{ t('settings.refresh') }}</el-button>
            </el-form-item>
            <el-form-item v-if="certDetails?.issued_date" :label="t('settings.certIssuedDate')">
              <span class="mono">{{ certDetails.issued_date }}</span>
            </el-form-item>
            <el-form-item v-if="certDetails?.expiry_date" :label="t('settings.certExpiryDate')">
              <span class="mono">{{ certDetails.expiry_date }}</span>
            </el-form-item>
            <el-form-item v-if="certDetails?.serial_number" :label="t('settings.certSerialNumber')">
              <span class="mono" style="font-size: 11px">{{ certDetails.serial_number }}</span>
            </el-form-item>
            <el-form-item v-if="certDetails?.thumbprint" :label="t('settings.certThumbprint')">
              <span class="mono" style="font-size: 11px">{{ certDetails.thumbprint }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.leafCertCache')">
              <span class="mono">{{ certDetails?.leaf_cert_count ?? '-' }}</span>
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.leafCertCacheHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.certExpiryAlert')">
              <el-switch
                :model-value="certDetails?.cert_expiry_alert ?? true"
                @change="toggleCertExpiryAlert"
              />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.certExpiryAlertHint') }}</span>
            </el-form-item>
            <el-form-item>
              <el-button type="danger" size="small" @click="regenerateCert" :loading="certRegenerating">
                <el-icon><Refresh /></el-icon>&nbsp;{{ t('settings.updateCert') }}
              </el-button>
            </el-form-item>
          </el-form>
        </div>

        <!-- 性能配置可视化面板 -->
        <div class="section" id="sec-performance">
          <div class="section-title"><el-icon><Odometer /></el-icon>&nbsp;{{ t('settings.performanceConfig') }}</div>
          <div class="hint text-dim" style="margin-bottom: 12px">{{ t('settings.performanceConfigDesc') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.performancePreset')">
              <el-radio-group v-model="currentPreset" @change="applyPerfPreset">
                <el-radio-button value="light">{{ t('settings.presetLight') }}</el-radio-button>
                <el-radio-button value="standard">{{ t('settings.presetStandard') }}</el-radio-button>
                <el-radio-button value="high_performance">{{ t('settings.presetHighPerformance') }}</el-radio-button>
              </el-radio-group>
              <div class="hint text-dim" style="margin-top: 4px">
                <span v-if="currentPreset === 'light'">{{ t('settings.presetLightDesc') }}</span>
                <span v-else-if="currentPreset === 'standard'">{{ t('settings.presetStandardDesc') }}</span>
                <span v-else>{{ t('settings.presetHighPerformanceDesc') }}</span>
              </div>
            </el-form-item>
            <el-divider />
            <el-form-item :label="t('settings.maxBodySize')">
              <el-input-number
                v-model="perfConfig.max_body_size"
                :min="1"
                :max="100"
                :step="1"
                controls-position="right"
                @change="() => { currentPreset = ''; textDirty = true }"
              />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.maxBodySizeHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.decompressThreshold')">
              <el-input-number
                v-model="perfConfig.decompress_threshold"
                :min="0"
                :max="10240"
                :step="64"
                controls-position="right"
                @change="() => { currentPreset = ''; textDirty = true }"
              />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.decompressThresholdHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.sslContextCache')">
              <el-input-number
                v-model="perfConfig.ssl_context_cache_size"
                :min="16"
                :max="2048"
                :step="16"
                controls-position="right"
                @change="() => { currentPreset = ''; textDirty = true }"
              />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.sslContextCacheHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.maxConnections')">
              <el-input-number
                v-model="perfConfig.max_connections"
                :min="10"
                :max="2000"
                :step="10"
                controls-position="right"
                @change="() => { currentPreset = ''; textDirty = true }"
              />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.maxConnectionsHint') }}</span>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" size="small" @click="savePerfConfig" :loading="perfSaving">
                <el-icon><Check /></el-icon>&nbsp;{{ t('settings.save') }}
              </el-button>
            </el-form-item>
          </el-form>
        </div>

        <!-- SSL/TLS 高级配置 -->
        <div class="section" id="sec-ssl">
          <div class="section-title"><el-icon><Key /></el-icon>&nbsp;{{ t('settings.sslTlsConfig') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.minTlsVersion')">
              <el-select v-model="sslConfig.min_tls_version" style="width: 180px" @change="sslSaving = true; saveSslConfig()">
                <el-option value="TLS 1.0" label="TLS 1.0" />
                <el-option value="TLS 1.1" label="TLS 1.1" />
                <el-option value="TLS 1.2" label="TLS 1.2 (推荐)" />
                <el-option value="TLS 1.3" label="TLS 1.3" />
              </el-select>
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.minTlsVersionHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.cipherSuites')">
              <el-select v-model="sslConfig.cipher_suites" style="width: 320px" @change="sslSaving = true; saveSslConfig()">
                <el-option value="DEFAULT" :label="t('settings.cipherSuiteDefault')" />
                <el-option value="MODERN" :label="t('settings.cipherSuiteModern')" />
                <el-option value="ALL" :label="t('settings.cipherSuiteAll')" />
              </el-select>
              <span class="hint text-dim" style="margin-left: 12px; display: block; margin-top: 4px">{{ t('settings.cipherSuitesHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.sniSpoofing')">
              <el-switch v-model="sslConfig.sni_spoofing" @change="sslSaving = true; saveSslConfig()" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.sniSpoofingHint') }}</span>
            </el-form-item>
          </el-form>
        </div>

        <!-- 手机抓包（安卓） -->
        <div class="section" id="sec-mobile">
          <div class="section-title"><el-icon><Iphone /></el-icon>&nbsp;{{ t('settings.mobileCapture') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.allowLanDevices')">
              <el-switch
                :model-value="form.proxy_listen_host === '0.0.0.0'"
                @change="onLanToggle"
              />
              <span class="hint text-dim" style="margin-left: 12px">
                {{ t('settings.allowLanDevicesHint') }}
              </span>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="showMobileWizard">
                <el-icon><Iphone /></el-icon>&nbsp;{{ t('settings.mobileWizard') }}
              </el-button>
              <span class="hint text-dim" style="margin-left: 12px">
                {{ t('settings.mobileWizardHint') }}
              </span>
            </el-form-item>
          </el-form>
        </div>

        <!-- 弱网模拟（Throttle） -->
        <div class="section" id="sec-throttle">
          <div class="section-title no-select"><el-icon><Connection /></el-icon>&nbsp;{{ t('settings.throttle') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.enableThrottle')">
              <el-switch v-model="throttle.enabled" @change="saveThrottle" />
              <span class="hint text-dim" style="margin-left: 12px">
                {{ t('settings.throttleHint') }}
              </span>
            </el-form-item>
            <el-form-item :label="t('settings.latencyMs')">
              <el-input-number v-model="throttle.latency_ms" :min="0" :max="10000" :step="100" :disabled="!throttle.enabled" @change="saveThrottle" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.latencyHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.bandwidthLimit')">
              <el-input-number v-model="throttle.bps_kbps" :min="0" :max="102400" :step="10" :disabled="!throttle.enabled" @change="saveThrottle" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.zeroUnlimited') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.dropRate')">
              <el-input-number v-model="throttle.drop_pct" :min="0" :max="100" :step="1" :disabled="!throttle.enabled" @change="saveThrottle" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.dropRateHint') }}</span>
            </el-form-item>
          </el-form>
        </div>

        <!-- 延迟规则 -->
        <div class="section" id="sec-delay">
          <div class="section-title"><el-icon><Timer /></el-icon>&nbsp;{{ t('delay.pageTitle') }}</div>
          <div class="hint text-dim" style="margin-bottom: 12px">{{ t('delay.pageTitle') }}：对匹配 URL 的请求/响应注入固定延迟，模拟网络环境</div>
          <el-button type="primary" size="small" @click="goDelayRules">
            <el-icon><Setting /></el-icon>&nbsp;{{ t('delay.pageTitle') }}
          </el-button>
        </div>

        <!-- 端口配置 -->
        <div class="section" id="sec-ports">
          <div class="section-title no-select"><el-icon><Connection /></el-icon>&nbsp;{{ t('settings.portConfig') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.randomPort')">
              <el-switch :model-value="!!form.random_port" @change="onRandomPortToggle" />
              <span class="hint text-dim" style="margin-left: 12px">
                {{ t('settings.randomPortHint') }}
              </span>
            </el-form-item>
            <el-form-item :label="t('settings.apiPort')">
              <el-input-number
                v-model="form.api_port"
                :min="1024" :max="65535" :step="1" controls-position="right"
                :disabled="!!form.random_port"
                @change="textDirty = true"
              />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.apiPortHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.proxyPortSetting')">
              <el-input-number
                v-model="form.proxy_port"
                :min="1024" :max="65535" :step="1" controls-position="right"
                :disabled="!!form.random_port"
                @change="textDirty = true"
              />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.proxyPortHint') }}</span>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" size="small" @click="onPortSettingSave" :disabled="!!form.random_port">
                <el-icon><Check /></el-icon>&nbsp;{{ t('settings.save') }}
              </el-button>
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.portRangeHint') }}</span>
            </el-form-item>
          </el-form>
          <div class="hint text-dim" style="margin: 4px 12px 0; padding: 8px 12px; background: var(--on-bg); border-radius: 4px">
            {{ t('settings.portConfigHint') }}
          </div>
        </div>

        <!-- 路径配置 -->
        <div class="section" id="sec-paths">
          <div class="section-title"><el-icon><FolderOpened /></el-icon>&nbsp;{{ t('settings.pathConfig') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.dataPath')">
              <div class="path-row">
                <span class="path-display mono" :title="form.data_path || t('settings.defaultPath')">{{ form.data_path || t('settings.defaultPath') }}</span>
                <el-button size="small" @click="openCurrentPath(form.data_path || '')" :title="t('settings.openInExplorer')">
                  <el-icon><FolderOpened /></el-icon>
                </el-button>
                <el-button size="small" @click="openDirPicker('data_path')" :title="t('settings.selectDir')">
                  <el-icon><Folder /></el-icon>&nbsp;{{ t('settings.selectDir') }}
                </el-button>
              </div>
              <div class="hint text-dim">{{ t('settings.dataPathHint') }}</div>
            </el-form-item>
          </el-form>
        </div>

        <!-- AI 配置 -->
        <div class="section" id="sec-ai">
          <div class="section-title"><el-icon><MagicStick /></el-icon>&nbsp;{{ t('settings.aiConfig') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.aiService')">
              <el-select v-model="form.ai_service" style="max-width: 200px" @change="onAiServiceChange">
                <el-option value="deepseek" label="DeepSeek" />
                <el-option value="anthropic" label="Claude (Anthropic)" />
                <el-option value="openai" label="OpenAI" />
                <el-option value="gemini" label="Gemini" />
                <el-option value="ollama" label="Ollama" />
              </el-select>
              <div class="hint text-dim" style="margin-left: 12px">{{ t('settings.aiServiceHint') }}</div>
            </el-form-item>

            <!-- Claude 配置 -->
            <template v-if="form.ai_service === 'claude' || !form.ai_service">
              <el-form-item label="Claude API Key">
                <el-input v-model="form.deepseek_api_key" placeholder="sk-..." show-password class="mono" style="max-width: 420px" @input="textDirty = true" />
              </el-form-item>
              <el-form-item :label="t('settings.model')">
                <el-select v-model="form.deepseek_model" style="max-width: 260px" @change="textDirty = true">
                  <el-option value="deepseek-v4-flash" :label="t('settings.modelFlashDefault')" />
                  <el-option value="deepseek-v4-pro" :label="t('settings.modelProPowerful')" />
                </el-select>
                <div class="hint text-dim">{{ t('settings.modelHint') }}</div>
              </el-form-item>
            </template>

            <!-- Anthropic/Claude 配置 -->
            <template v-if="form.ai_service === 'anthropic'">
              <el-form-item label="Anthropic API Key">
                <el-input v-model="form.anthropic_api_key" placeholder="sk-ant-..." show-password class="mono" style="max-width: 420px" @input="textDirty = true" />
              </el-form-item>
              <el-form-item :label="t('settings.model')">
                <el-select v-model="form.anthropic_model" style="max-width: 260px" @change="textDirty = true">
                  <el-option value="claude-3-5-sonnet-20241022" label="Claude 3.5 Sonnet" />
                  <el-option value="claude-3-5-haiku-20241022" label="Claude 3.5 Haiku" />
                  <el-option value="claude-3-opus-20240229" label="Claude 3 Opus" />
                </el-select>
                <div class="hint text-dim">{{ t('settings.modelHint') }}</div>
              </el-form-item>
            </template>

            <!-- OpenAI 配置 -->
            <template v-if="form.ai_service === 'openai'">
              <el-form-item label="OpenAI API Key">
                <el-input v-model="form.openai_api_key" placeholder="sk-..." show-password class="mono" style="max-width: 420px" @input="textDirty = true" />
              </el-form-item>
              <el-form-item :label="t('settings.model')">
                <el-select v-model="form.openai_model" style="max-width: 260px" @change="textDirty = true">
                  <el-option value="gpt-4o" label="GPT-4o" />
                  <el-option value="gpt-4o-mini" label="GPT-4o Mini" />
                  <el-option value="gpt-4-turbo" label="GPT-4 Turbo" />
                  <el-option value="gpt-3.5-turbo" label="GPT-3.5 Turbo" />
                </el-select>
              </el-form-item>
            </template>

            <!-- Gemini 配置 -->
            <template v-if="form.ai_service === 'gemini'">
              <el-form-item label="Gemini API Key">
                <el-input v-model="form.gemini_api_key" placeholder="AI..." show-password class="mono" style="max-width: 420px" @input="textDirty = true" />
              </el-form-item>
              <el-form-item :label="t('settings.model')">
                <el-select v-model="form.gemini_model" style="max-width: 260px" @change="textDirty = true">
                  <el-option value="gemini-1.5-pro" label="Gemini 1.5 Pro" />
                  <el-option value="gemini-1.5-flash" label="Gemini 1.5 Flash" />
                  <el-option value="gemini-1.5-pro-latest" label="Gemini 1.5 Pro (Latest)" />
                  <el-option value="gemini-1.5-flash-latest" label="Gemini 1.5 Flash (Latest)" />
                </el-select>
              </el-form-item>
            </template>

            <!-- Ollama 配置 -->
            <template v-if="form.ai_service === 'ollama'">
              <el-form-item label="Ollama Endpoint">
                <el-input v-model="form.ollama_endpoint" placeholder="http://localhost:11434" class="mono" style="max-width: 420px" @input="textDirty = true" />
                <div class="hint text-dim">{{ t('settings.ollamaEndpointHint') }}</div>
              </el-form-item>
              <el-form-item :label="t('settings.model')">
                <el-input v-model="form.ollama_model" placeholder="llama3.2" class="mono" style="max-width: 260px" @input="textDirty = true" />
                <div class="hint text-dim">{{ t('settings.ollamaModelHint') }}</div>
              </el-form-item>
            </template>

            <!-- 使用统计 -->
            <el-form-item :label="t('settings.aiUsage')">
              <div class="ai-usage-card">
                <div class="usage-row">
                  <span class="usage-label">{{ t('settings.todayUsage') }}:</span>
                  <span class="usage-value">{{ aiUsage.today.requests }} {{ t('settings.requests') }}, {{ aiUsage.today.input_tokens }} in / {{ aiUsage.today.output_tokens }} out</span>
                </div>
                <div class="usage-row">
                  <span class="usage-label">{{ t('settings.monthUsage') }}:</span>
                  <span class="usage-value">{{ aiUsage.month.requests }} {{ t('settings.requests') }}, ${{ aiUsage.month.total_cost.toFixed(4) }}</span>
                </div>
                <div class="usage-row">
                  <span class="usage-label">{{ t('settings.totalUsage') }}:</span>
                  <span class="usage-value">${{ aiUsage.all_time.total_cost.toFixed(4) }}</span>
                </div>
              </div>
              <el-button size="small" style="margin-left: 12px" @click="loadAiUsage">{{ t('settings.refresh') }}</el-button>
            </el-form-item>
          </el-form>
        </div>

        <div class="section" id="sec-cache">
          <div class="section-title"><el-icon><Coin /></el-icon>&nbsp;{{ t('settings.browserCache') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.currentCacheSize')">
              <span class="mono">{{ formatSize(cacheSize) }}</span>
              <el-button size="small" style="margin-left: 16px" @click="refreshCacheSize">{{ t('settings.refresh') }}</el-button>
            </el-form-item>
            <el-form-item :label="t('settings.autoClean')">
              <el-switch v-model="cacheAutoClean" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.autoCleanHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.thresholdMB')">
              <el-input-number v-model="cacheThreshold" :min="1" :max="100" :step="1" controls-position="right" />
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.default10MB') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.clearCache')">
              <el-button type="danger" size="small" @click="clearCache">
                <el-icon><Delete /></el-icon>&nbsp;{{ t('settings.clearNow') }}
              </el-button>
            </el-form-item>
          </el-form>
        </div>

        <!-- 数据库维护 -->
        <div class="section" id="sec-db">
          <div class="section-title"><el-icon><DataLine /></el-icon>&nbsp;{{ t('settings.dbMaintenance') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item :label="t('settings.currentDbSize')">
              <span class="mono">{{ formatSize(dbStats.size_mb * 1024 * 1024) }}</span>
              <el-button size="small" style="margin-left: 16px" @click="loadDbStats" :loading="dbLoading">{{ t('settings.refresh') }}</el-button>
            </el-form-item>
            <el-form-item :label="t('settings.dbFlows')">
              <span class="mono">{{ dbStats.flows }}</span>
              <el-button type="danger" link size="small" style="margin-left: 12px" :loading="dbLoading" @click="clearData('flows')">
                <el-icon><Delete /></el-icon>{{ t('common.clear') }}
              </el-button>
            </el-form-item>
            <el-form-item :label="t('settings.dbSessions')">
              <span class="mono">{{ dbStats.sessions }}</span>
              <el-button type="danger" link size="small" style="margin-left: 12px" :loading="dbLoading" @click="clearData('sessions')">
                <el-icon><Delete /></el-icon>{{ t('common.clear') }}
              </el-button>
            </el-form-item>
            <el-form-item :label="t('settings.dbRules')">
              <span class="mono">{{ dbStats.rules }}</span>
              <el-button type="danger" link size="small" style="margin-left: 12px" :loading="dbLoading" @click="clearData('rules')">
                <el-icon><Delete /></el-icon>{{ t('common.clear') }}
              </el-button>
            </el-form-item>
            <el-form-item :label="t('settings.dbAiChats')">
              <span class="mono">{{ dbStats.ai_chats }}</span>
              <el-button type="danger" link size="small" style="margin-left: 12px" :loading="dbLoading" @click="clearData('ai_chats')">
                <el-icon><Delete /></el-icon>{{ t('common.clear') }}
              </el-button>
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.dbAiChatsHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('settings.dbCleanup')">
              <el-button type="danger" size="small" @click="openClearAllDialog" :loading="dbLoading">
                <el-icon><Delete /></el-icon>&nbsp;{{ t('settings.clearAll') }}
              </el-button>
              <span class="hint text-dim" style="margin-left: 12px">{{ t('settings.dbCleanupHint') }}</span>
            </el-form-item>
          </el-form>
          <div class="hint text-dim" style="margin: 4px 12px 0; padding: 8px 12px; background: var(--on-bg); border-radius: 4px">
            {{ t('settings.dbCleanupDesc') }}
          </div>
        </div>

        <!-- 设置文件 -->
        <div class="section" id="sec-settings-file">
          <div class="section-title no-select"><el-icon><Document /></el-icon>&nbsp;{{ t('settings.settingsFile') }}</div>
          <el-form label-width="220px" size="default">
            <el-form-item label="settings.json">
              <span class="path-display mono" :title="form.settings_file_path || ''">{{ form.settings_file_path || t('settings.notGenerated') }}</span>
              <el-button size="small" style="margin-left: 12px" @click="openSettingsFile" :title="t('settings.openSettingsFileTitle')">
                <el-icon><Document /></el-icon>&nbsp;{{ t('settings.openSettingsFile') }}
              </el-button>
            </el-form-item>
            <div class="hint text-dim" style="margin-left: 160px">
              {{ t('settings.settingsFileHint') }}
            </div>
          </el-form>
        </div>
      </div>
    </div>

    <!-- 目录选择器对话框 -->
    <el-dialog v-model="dirPickerVisible" :title="t('settings.selectDir')" width="min(560px, 95vw)">
      <div class="dir-picker">
        <div class="dir-current mono">{{ dirPickerCurrent || t('settings.selectDrive') }}</div>
        <div class="dir-toolbar">
          <el-button size="small" @click="goParent" :disabled="!dirPickerParent" link>
            <el-icon><Back /></el-icon>&nbsp;{{ t('settings.parentDir') }}
          </el-button>
          <el-button size="small" @click="loadDirs('')" link>
            <el-icon><Monitor /></el-icon>&nbsp;{{ t('settings.rootDir') }}
          </el-button>
        </div>
        <div class="dir-list">
          <div
            v-for="d in dirPickerDirs"
            :key="d"
            class="dir-item"
            @click="selectDir(d)"
            @dblclick="selectDir(d)"
          >
            <el-icon><Folder /></el-icon>
            <span class="mono">{{ d }}</span>
          </div>
          <div v-if="!dirPickerDirs.length && !dirPickerLoading" class="text-dim" style="padding: 12px">
            {{ t('settings.noSubDirs') }}
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="dirPickerVisible = false">{{ t('settings.cancel') }}</el-button>
        <el-button type="primary" @click="confirmDir" :disabled="!dirPickerCurrent">{{ t('settings.selectThisDir') }}</el-button>
      </template>
    </el-dialog>

    <!-- 代理引擎选择说明对话框 -->
    <el-dialog
      v-model="engineHelpVisible"
      :title="t('settings.engineHelpTitle')"
      width="640px"
      destroy-on-close
      align-center
      :lock-scroll="true"
    >
      <div class="engine-help-body">
        <p class="engine-help-intro">{{ t('settings.engineHelpIntro') }}</p>

        <div class="engine-card">
          <div class="engine-card-title">{{ t('settings.engineBuiltinTitle') }}</div>
          <div class="engine-card-desc">{{ t('settings.engineBuiltinDesc') }}</div>
        </div>

        <div class="engine-card">
          <div class="engine-card-title">{{ t('settings.engineV2Title') }}</div>
          <div class="engine-card-desc">{{ t('settings.engineV2Desc') }}</div>
        </div>

        <div class="engine-card">
          <div class="engine-card-title">{{ t('settings.engineMitmproxyTitle') }}</div>
          <div class="engine-card-desc">{{ t('settings.engineMitmproxyDesc') }}</div>
        </div>

        <div class="engine-card">
          <div class="engine-card-title">{{ t('settings.engineAsyncTitle') }}</div>
          <div class="engine-card-desc">{{ t('settings.engineAsyncDesc') }}</div>
        </div>

        <div class="engine-help-tip">
          <strong>{{ t('settings.engineHelpTipLabel') }}</strong>{{ t('settings.engineHelpTipText') }}
        </div>
      </div>
      <template #footer>
        <el-button type="primary" @click="engineHelpVisible = false">{{ t('settings.gotIt') }}</el-button>
      </template>
    </el-dialog>

    <!-- Clash 教程对话框 -->
    <el-dialog
      v-model="tutorialVisible"
      :title="t('settings.clashTutorialTitle')"
      width="80%"
      top="5vh"
      class="tutorial-dialog"
      destroy-on-close
    >
      <div class="tutorial-body" v-html="tutorialHtml"></div>
    </el-dialog>

    <!-- 手机抓包向导 -->
    <el-dialog
      v-model="mobileVisible"
      :title="t('settings.mobileWizardTitle')"
      width="560px"
      destroy-on-close
      align-center
      :lock-scroll="true"
      class="mobile-dialog"
    >
      <div class="mobile-wizard">
        <template v-if="mobileSetup">
          <div class="mobile-step">
            <div class="step-num">1</div>
            <div class="step-content">
              <div class="step-title">{{ t('settings.mobileStep1Title') }}</div>
              <div class="step-desc">
                {{ t('settings.mobileStep1Desc') }}
                {{ t('settings.currentListenAddr') }}<code>{{ form.proxy_listen_host || '127.0.0.1' }}</code>
                <span v-if="form.proxy_listen_host !== '0.0.0.0'" style="color: var(--on-error)">{{ t('settings.notEnabledRestart') }}</span>
              </div>
            </div>
          </div>

          <div class="mobile-step">
            <div class="step-num">2</div>
            <div class="step-content">
              <div class="step-title">{{ t('settings.mobileStep2Title') }}</div>
              <div class="step-desc">
                {{ t('settings.mobileStep2Desc') }}
                <code style="word-break: break-all">{{ mobileSetup.cert_download_url }}</code>)
              </div>
              <div class="qr-wrap" v-if="mobileQrDataUrl">
                <img :src="mobileQrDataUrl" :alt="t('settings.certDownloadQr')" />
              </div>
              <div class="cert-download-row">
                <el-button size="small" type="primary" plain @click="downloadCert">
                  <el-icon><Download /></el-icon>&nbsp;{{ t('settings.downloadCertToLocal') }}
                </el-button>
                <span class="text-dim cert-hint">{{ t('settings.transferToPhoneHint') }}</span>
              </div>
            </div>
          </div>

          <div class="mobile-step">
            <div class="step-num">3</div>
            <div class="step-content">
              <div class="step-title">{{ t('settings.mobileStep3Title') }}</div>
              <div class="step-desc">
                {{ t('settings.mobileStep3Desc') }}<br/>
                <span style="color: var(--on-warn)">{{ t('settings.androidUserCertWarning') }}</span>
              </div>
              <div class="android-sys-cert" v-if="mobileSetup.android_cert_url">
                <div class="android-title">{{ t('settings.androidSysCertTitle') }}</div>
                <div class="android-desc">
                  {{ t('settings.androidSysCertDesc') }}
                </div>
                <div class="cert-download-row">
                  <el-button size="small" type="warning" plain @click="downloadAndroidCert">
                    <el-icon><Download /></el-icon>&nbsp;{{ t('settings.downloadAndroidCert') }}
                  </el-button>
                  <span class="text-dim cert-hint">{{ t('settings.androidCertFilenameFormat') }}</span>
                </div>
                <div class="android-tutorials">
                  <el-button size="small" type="primary" @click="openAndroidTutorial('mumu')">
                    {{ t('settings.mumuTutorial') }}
                  </el-button>
                  <el-button size="small" type="primary" @click="openAndroidTutorial('leidian')">
                    {{ t('settings.leidianTutorial') }}
                  </el-button>
                </div>
              </div>
            </div>
          </div>

          <div class="mobile-step">
            <div class="step-num">4</div>
            <div class="step-content">
              <div class="step-title">{{ t('settings.mobileStep4Title') }}</div>
              <div class="step-desc">
                {{ t('settings.mobileStep4Desc') }}<br/>
                {{ t('settings.proxyHost') }}<code>{{ mobileSetup.proxy_host }}</code><br/>
                {{ t('settings.proxyPort') }}<code>{{ mobileSetup.proxy_port }}</code>
              </div>
            </div>
          </div>

          <div class="mobile-step">
            <div class="step-num">5</div>
            <div class="step-content">
              <div class="step-title">{{ t('settings.mobileStep5Title') }}</div>
              <div class="step-desc">
                {{ t('settings.mobileStep5Desc') }}
              </div>
            </div>
          </div>
        </template>
      </div>
    </el-dialog>

    <!-- 全部清空 3 秒确认对话框 -->
    <el-dialog
      v-model="clearAllDialogVisible"
      :title="t('settings.clearAllTitle')"
      width="min(420px, 95vw)"
      align-center
      :close-on-click-modal="false"
      @closed="stopClearAllTimer"
    >
      <p>{{ t('settings.clearAllConfirm') }}</p>
      <p class="hint text-dim">{{ t('settings.clearAllCountdownHint') }}</p>
      <template #footer>
        <el-button @click="closeClearAllDialog">{{ t('settings.cancel') }}</el-button>
        <el-button type="danger" :disabled="clearAllCountdown > 0" @click="doClearAll">
          {{ clearAllCountdown > 0 ? t('settings.clearAllCountdown', { n: clearAllCountdown }) : t('settings.confirmClearAll') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.settings-view { background: var(--on-bg); }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.header-actions { display: flex; align-items: center; gap: 8px; }
.page-title { font-size: 15px; font-weight: 600; display: flex; align-items: center; }

/* 左右两栏布局 */
.settings-body { min-height: 0; }
.settings-nav {
  width: 160px; flex-shrink: 0;
  border-right: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
  padding: 12px 8px;
  display: flex; flex-direction: column; gap: 2px;
  overflow-y: auto;
}
.settings-nav-item {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; border-radius: 6px; cursor: pointer;
  color: var(--on-text-muted); font-size: 13px;
  transition: all .15s;
}
.settings-nav-item:hover { background: var(--on-bg-hover); color: var(--on-text); }
.settings-nav-item.active {
  background: var(--on-accent-glow); color: var(--on-accent);
  box-shadow: inset 2px 0 0 var(--on-accent);
}
.settings-content { padding: 16px 24px; min-width: 0; }

.section { margin-bottom: 28px; padding-bottom: 20px; border-bottom: 1px solid var(--on-border-light); }
.section:last-child { border-bottom: none; }
.section-title {
  font-size: 14px; color: var(--on-text); font-weight: 600; margin-bottom: 14px;
  display: flex; align-items: center;
  border-left: 3px solid var(--on-accent); padding-left: 8px;
}
.tab-checkboxes { display: flex; flex-wrap: wrap; gap: 12px 20px; padding: 4px 0; }
.sub-label { font-size: 13px; font-weight: 600; color: var(--on-text); margin-bottom: 6px; }
.hint { font-size: 12px; margin-top: 6px; }
.ai-usage-card {
  display: flex; flex-direction: column; gap: 6px;
  padding: 10px 12px; background: var(--on-bg-hover);
  border: 1px solid var(--on-border-light); border-radius: var(--on-radius-md);
  min-width: 280px;
}
.usage-row {
  display: flex; justify-content: space-between; gap: 16px;
  font-size: 12px;
}
.usage-label { color: var(--on-text-dim); }
.usage-value { color: var(--on-text); font-weight: 500; }
.cert-row { display: flex; align-items: center; gap: 16px; }
.cert-status { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.cert-status .ok { color: var(--on-ok); }
.action-row { display: flex; gap: 12px; padding: 12px 0 0 160px; }
.ignore-add-row { display: flex; align-items: center; gap: 10px; }
.path-row { display: flex; align-items: center; gap: 6px; }
.path-display {
  flex: 1; max-width: 360px;
  padding: 6px 10px;
  background: var(--on-bg-hover, #1e1e2e);
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
  font-size: 12.5px;
  color: var(--on-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 目录选择器 */
.dir-picker { display: flex; flex-direction: column; gap: 8px; }
.dir-current {
  padding: 6px 10px; background: var(--on-bg-hover, #1e1e2e);
  border-radius: 4px; font-size: 12px; word-break: break-all;
}
.dir-toolbar { display: flex; gap: 8px; }
.dir-list {
  max-height: 320px; overflow-y: auto;
  border: 1px solid var(--on-border-light); border-radius: 4px;
}
.dir-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; cursor: pointer; font-size: 13px;
}
.dir-item:hover { background: var(--on-bg-hover); }
.mono { font-family: 'Consolas', 'Monaco', monospace; }

/* Clash 教程对话框 */
.tutorial-body {
  max-height: 75vh;
  overflow-y: auto;
  padding: 0 8px 8px;
  line-height: 1.7;
  color: var(--el-text-color-primary, #eee);
}
.tutorial-body :deep(h4) {
  margin: 16px 0 12px;
  font-size: 16px;
  font-weight: 600;
  color: var(--el-text-color-primary, #eee);
}
.tutorial-body :deep(img) {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 8px 0;
  border-radius: 6px;
  border: 1px solid var(--el-border-color, #333);
}
.tutorial-body :deep(p) {
  margin: 8px 0;
}

/* 代理引擎选择说明 */
.engine-help-body {
  padding: 4px 8px 8px;
  line-height: 1.7;
  color: var(--el-text-color-primary, #eee);
}
.engine-help-intro {
  margin: 0 0 16px;
  color: var(--el-text-color-secondary, #aaa);
}
.engine-card {
  margin-bottom: 14px;
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--el-fill-color-light, rgba(255,255,255,0.04));
  border: 1px solid var(--el-border-color, rgba(255,255,255,0.08));
}
.engine-card-title {
  margin-bottom: 6px;
  font-size: 15px;
  font-weight: 600;
  color: var(--on-accent);
}
.engine-card-desc {
  font-size: 13px;
  color: var(--on-text);
}
.engine-help-tip {
  margin-top: 16px;
  padding: 10px 14px;
  border-radius: var(--on-radius-md);
  background: var(--on-accent-glow);
  border-left: 3px solid var(--on-accent);
  font-size: 13px;
  color: var(--on-text);
}

/* 手机抓包向导 */
.mobile-wizard {
  padding: 8px 4px;
  max-height: calc(100vh - 200px);
  overflow-y: auto;
}
.mobile-step {
  display: flex;
  gap: 12px;
  margin-bottom: 18px;
}
.mobile-step .step-num {
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--on-accent);
  color: var(--on-bg);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 14px;
}
.mobile-step .step-title {
  font-weight: 600;
  margin-bottom: 4px;
  color: var(--el-text-color-primary, #eee);
}
.mobile-step .step-desc {
  color: var(--on-text);
  line-height: 1.6;
  font-size: 13px;
}
.mobile-step .step-desc code {
  background: var(--on-bg-hover);
  padding: 1px 6px;
  border-radius: var(--on-radius-sm);
  font-family: 'Consolas', 'Monaco', monospace;
  color: var(--on-ok);
}
.mobile-step .qr-wrap {
  margin-top: 10px;
  text-align: center;
}
.mobile-step .qr-wrap img {
  width: 240px;
  height: 240px;
  border: 1px solid var(--on-border);
  border-radius: var(--on-radius-md);
  background: #fff;
}
.cert-download-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  flex-wrap: wrap;
}
.cert-hint { font-size: 12px; }
.android-sys-cert {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px dashed var(--el-border-color, #444);
  border-radius: 6px;
  background: rgba(230, 162, 60, 0.05);
}
.android-title { font-weight: 600; margin-bottom: 4px; }
.android-desc { font-size: 12px; margin-bottom: 6px; }
.android-tutorials {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

/* AI 使用统计 */
.ai-usage-card {
  background: var(--on-bg);
  border-radius: var(--on-radius-md);
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.usage-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.usage-label {
  font-size: 12px;
  color: var(--on-text-dim);
}
.usage-value {
  font-size: 12px;
  color: var(--on-text);
  font-family: var(--on-font-mono);
}
.usage-total-value {
  font-size: 14px;
  font-weight: 700;
  color: var(--on-accent);
}
</style>
