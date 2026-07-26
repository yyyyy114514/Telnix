<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownIt from 'markdown-it'
import QRCode from 'qrcode'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import { api, type Settings, type IgnoredProcess, type TransparentProxyStatus } from '../api/client'
import { getCacheSize, clearFlowCache } from '../stores/flows'
import { syncPrefs } from '../stores/prefs'

const capture = useCaptureStore()
const flows = useFlowsStore()
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
const transparentRestarting = ref(false)  // 管理员重启中
let transparentPollTimer: number | null = null

async function loadTransparentStatus() {
  try {
    transparentProxy.value = await api.transparentProxyStatus()
    // 复用 raw/status 的 is_admin 字段判断当前进程是否管理员
    const raw: any = await api.rawStatus()
    transparentIsAdmin.value = !!raw.is_admin
  } catch (e: any) { /* ignore */ }
}

async function onToggleTransparent(val: any) {
  transparentLoading.value = true
  try {
    if (val) {
      const r: any = await api.transparentProxyStart()
      ElMessage.success(r.msg || '透明代理已启动')
    } else {
      const r: any = await api.transparentProxyStop()
      ElMessage.success(r.msg || '透明代理已停止')
    }
    await loadTransparentStatus()
  } catch (e: any) {
    ElMessage.error('操作失败：' + (e?.message || e))
  } finally {
    transparentLoading.value = false
  }
}

async function restartTransparentAsAdmin() {
  transparentRestarting.value = true
  try {
    await api.restartAsAdmin()
    ElMessage.success('正在以管理员身份重启，请稍候...')
  } catch (e: any) {
    ElMessage.error('重启失败：' + (e?.message || e))
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
const cacheAutoClean = ref(true)
const ignoredProcesses = ref<IgnoredProcess[]>([])
// 忽略进程：支持单独按 PID 或单独按进程名添加
const newIgnorePid = ref<number | null>(null)
const newIgnoreName = ref('')
// 忽略 host 通配符（支持 * ? 通配符，如 *.example.com）
const newIgnoreHost = ref('')
const ignoredHosts = ref<{ id: number; host_pattern: string; created_at: string }[]>([])

const optionalTabs = [
  { key: 'cookies', label: 'Cookies' },
  { key: 'cache', label: 'Cache' },
  { key: 'auth', label: 'Auth' },
  { key: 'xml', label: 'XML' },
  { key: 'cert', label: '证书' },
]

// 流量列表可选列（默认全不显示，核心列 # / Host / URL / 进程 永远显示）
const optionalColumns = [
  { key: 'status', label: '状态码' },
  { key: 'method', label: '方法' },
  { key: 'protocol', label: '协议' },
  { key: 'content_type', label: 'Content-Type' },
  { key: 'pid', label: 'PID' },
  { key: 'size', label: '大小' },
  { key: 'duration', label: '耗时' },
  { key: 'remote_ip', label: '对端 IP' },
  { key: 'ip_region', label: 'IP 属地' },
]

// 右键复制可选项（# 不参与复制）
const copyFieldOptions = [
  { key: 'url', label: 'URL' },
  { key: 'curl', label: 'cURL' },
  { key: 'host', label: 'Host' },
  { key: 'method', label: '方法' },
  { key: 'path', label: 'Path' },
  { key: 'status', label: '状态码' },
  { key: 'protocol', label: '协议' },
  { key: 'content_type', label: 'Content-Type' },
  { key: 'pid', label: 'PID' },
  { key: 'process', label: '进程' },
  { key: 'size', label: '大小' },
  { key: 'duration', label: '耗时' },
  { key: 'remote_ip', label: '对端 IP' },
  { key: 'ip_region', label: 'IP 属地' },
]
const COPY_PREF_KEY = 'telnix_copy_fields'
const enabledCopyFields = ref<string[]>(['url', 'curl'])
// 流量列表行禁选文字（默认开启）
const listNoSelect = ref(true)

function loadCopyPrefs() {
  try {
    const saved = localStorage.getItem(COPY_PREF_KEY)
    if (saved) {
      const arr = JSON.parse(saved)
      if (Array.isArray(arr)) enabledCopyFields.value = arr
    }
  } catch { /* ignore */ }
  listNoSelect.value = localStorage.getItem('telnix_list_no_select') !== 'false'
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
function onListNoSelectChange(val: any) {
  listNoSelect.value = val as boolean
  localStorage.setItem('telnix_list_no_select', String(listNoSelect.value))
  // 立即应用到 DOM
  document.documentElement.classList.toggle('list-no-select', listNoSelect.value)
}

// 目录选择器对话框
const dirPickerVisible = ref(false)
const dirPickerTarget = ref<'data_path' | 'ai_path'>('data_path')
const dirPickerCurrent = ref('')
const dirPickerDirs = ref<string[]>([])
const dirPickerParent = ref('')
const dirPickerLoading = ref(false)

function formatSize(bytes: number): string {
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
    ElMessage.warning('请至少填写 PID 或进程名其中之一')
    return
  }
  try {
    // pid 为空时传 null，后端用 NULL 存储表示「仅按进程名忽略」
    const actualName = name || (pid !== null ? `PID ${pid}` : '按名称忽略')
    await api.ignoreProcess({ pid: pid, name: actualName })
    ElMessage.success('已添加忽略进程')
    newIgnorePid.value = null
    newIgnoreName.value = ''
    await loadIgnored()
  } catch (e: any) {
    ElMessage.error('添加失败：' + (e?.message || e))
  }
}

async function removeIgnored(rowId: number) {
  try {
    await api.unignoreProcess(rowId)
    ElMessage.success('已取消忽略')
    await loadIgnored()
  } catch (e: any) {
    ElMessage.error('操作失败：' + (e?.message || e))
  }
}

// 添加忽略 host 通配符（支持 * ? 通配符）
async function addIgnoredHost() {
  const h = newIgnoreHost.value.trim()
  if (!h) {
    ElMessage.warning('请输入 Host 通配符')
    return
  }
  try {
    await api.ignoreHost(h)
    ElMessage.success(`已添加忽略 Host：${h}`)
    newIgnoreHost.value = ''
    await loadIgnored()
  } catch (e: any) {
    ElMessage.error('添加失败：' + (e?.message || e))
  }
}

async function removeIgnoredHost(id: number) {
  try {
    await api.unignoreHost(id)
    ElMessage.success('已取消忽略')
    await loadIgnored()
  } catch (e: any) {
    ElMessage.error('操作失败：' + (e?.message || e))
  }
}

async function openCurrentPath(path: string) {
  // 路径为空时后端使用默认数据目录并自动创建
  try {
    await api.openPath(path || '')
  } catch (e: any) {
    ElMessage.error('打开失败：' + (e?.message || e))
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
    ElMessage.error('读取目录失败：' + (e?.message || e))
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
      ElMessage.success('Clash 页已启用')
    } else {
      ElMessage.info('Clash 页已禁用，流量已切回直连')
    }
  } catch (e: any) {
    ElMessage.error('操作失败: ' + e.message)
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
      clashTestResult.value = { ok: true, msg: `连接成功（${r.version || '?'}，端口 ${r.mixed_port || '?'}）` }
    } else if (r.reachable && r.error) {
      // 端口可达但 API 调用失败（如 secret 错误 401）
      clashTestResult.value = { ok: false, msg: `端口可达但 API 调用失败：${r.error}` }
    } else {
      clashTestResult.value = { ok: false, msg: r.error || 'Mihomo 未在线，请确认 Clash 客户端已启动' }
    }
  } catch (e: any) {
    clashTestResult.value = { ok: false, msg: '连接失败: ' + e.message }
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
const tutorialMd = new MarkdownIt({
  html: true,
  breaks: true,
  linkify: true,
})

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
    tutorialHtml.value = `<p style="color: var(--on-error)">加载教程失败: ${e.message}</p>`
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

async function saveThrottle() {
  // 防抖：连续修改时只保留最后一次
  if (throttleSaveTimer) clearTimeout(throttleSaveTimer)
  throttleSaveTimer = setTimeout(async () => {
    try {
      await api.setThrottle({ ...throttle.value })
    } catch (e: any) {
      ElMessage.error('弱网配置保存失败: ' + e.message)
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
    ElMessage.error('获取手机抓包配置失败: ' + e.message)
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
    if (!resp.ok) throw new Error('下载失败：HTTP ' + resp.status)
    const blob = await resp.blob()
    const objUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objUrl
    a.download = 'telnix_root.pem'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(objUrl)
    ElMessage.success('证书已下载')
  } catch (e: any) {
    ElMessage.error('证书下载失败: ' + e.message)
  }
}

// 下载安卓 7+ 系统证书格式（文件名 <hash>.0，已由后端计算好哈希）
async function downloadAndroidCert() {
  if (!mobileSetup.value?.android_cert_url) return
  try {
    const u = new URL(mobileSetup.value.android_cert_url)
    const resp = await fetch(u.pathname)
    if (!resp.ok) throw new Error('下载失败：HTTP ' + resp.status)
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
    ElMessage.success(`安卓系统证书已下载（${fname}）`)
  } catch (e: any) {
    ElMessage.error('证书下载失败: ' + e.message)
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
        '已开启局域网监听，但需要重启 Telnix 后端才能生效。\n\n请到侧边栏底部点「重启服务」，或调 CLI `system restart`。',
        '提示',
        { confirmButtonText: '知道了' }
      )
    } else {
      ElMessage.success('已切换为仅本机监听（重启后端生效）')
    }
  } catch (e: any) {
    ElMessage.error('保存失败: ' + e.message)
  }
}

// 代理引擎切换：保存设置并提示重启后端生效
async function onProxyEngineChange(val: any) {
  try {
    await doSave()
    ElMessageBox.alert(
      '代理引擎已切换，需要重启 Telnix 后端才能生效。\n\n请到侧边栏底部点「重启服务」，或调 CLI `system restart`。',
      '提示',
      { confirmButtonText: '知道了' }
    )
  } catch (e: any) {
    ElMessage.error('保存失败: ' + e.message)
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
    // clash 默认值
    if (!form.value.clash_api_url) form.value.clash_api_url = 'http://127.0.0.1:9090'
    if (!form.value.clash_secret) form.value.clash_secret = ''
    if (form.value.clash_mixed_port == null) form.value.clash_mixed_port = 0
    // proxy_listen_host 默认 127.0.0.1（仅本机）
    if (!form.value.proxy_listen_host) form.value.proxy_listen_host = '127.0.0.1'
    // proxy_engine 默认 builtin（内置线程代理）
    if (!form.value.proxy_engine) form.value.proxy_engine = 'builtin'
    // mitmproxy 可用性（后端注入，控制下拉选项是否可选）
    mitmproxyAvailable.value = !!s.mitmproxy_available
    // autoScroll/autoScrollDelay 由 store 自己持久化，不从后端覆盖
    // 同步到 localStorage
    localStorage.setItem('telnix_settings_cache', JSON.stringify(s))
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
    // 同步到 localStorage（本地持久化，切换页面立即可用）
    localStorage.setItem('telnix_settings_cache', JSON.stringify(form.value))
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
    ElMessage.error('保存失败：' + (e?.message || e))
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
  ElMessage({ message: '已保存', type: 'success', duration: 1200 })
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
    await ElMessageBox.confirm('将安装 Telnix 根证书以启用 HTTPS 解密。继续？', '安装证书', {
      confirmButtonText: '安装', cancelButtonText: '取消', type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.installCert()
    await capture.fetchStatus()
    const r = await api.getCertStatus()
    certInstalled.value = r.installed
    ElMessage.success(certInstalled.value ? '证书已安装' : '安装请求已提交')
  } catch (e: any) {
    ElMessage.error('安装失败：' + (e?.message || e))
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
    await ElMessageBox.confirm('确定重置所有设置为默认值？', '重置', {
      confirmButtonText: '重置', cancelButtonText: '取消', type: 'warning',
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
  ElMessage.success('已重置')
}

function clearCache() {
  clearFlowCache()
  refreshCacheSize()
  ElMessage.success('缓存已清空')
}

async function openSettingsFile() {
  try {
    await api.openSettingsFile()
    ElMessage.success('已在系统默认编辑器中打开 settings.json')
  } catch (e: any) {
    ElMessage.error('打开失败：' + (e?.message || e))
  }
}

onMounted(() => {
  load()
  loadClashStatus()
  loadCopyPrefs()
  loadThrottle()
  loadTransparentStatus()
  // 透明代理运行时每 5s 轮询统计
  transparentPollTimer = window.setInterval(() => {
    if (transparentProxy.value.running) loadTransparentStatus()
  }, 5000)
  // 应用初始禁选状态
  document.documentElement.classList.toggle('list-no-select', listNoSelect.value)
})

onUnmounted(() => {
  if (transparentPollTimer) {
    clearInterval(transparentPollTimer)
    transparentPollTimer = null
  }
})

// 左侧锚点导航分组（顺序与右侧 section 实际渲染顺序一致）
const groups = [
  { id: 'sec-appearance', label: '外观', icon: 'Brush' },
  { id: 'sec-capture', label: '抓包行为', icon: 'Aim' },
  { id: 'sec-transparent', label: '透明代理', icon: 'Connection' },
  { id: 'sec-display', label: '显示选项', icon: 'View' },
  { id: 'sec-processes', label: '忽略规则', icon: 'Cpu' },
  { id: 'sec-clash', label: 'Clash 集成', icon: 'ClashIcon' },
  { id: 'sec-cert', label: 'HTTPS 证书', icon: 'Lock' },
  { id: 'sec-mobile', label: '手机抓包', icon: 'Iphone' },
  { id: 'sec-throttle', label: '弱网模拟', icon: 'Connection' },
  { id: 'sec-paths', label: '路径配置', icon: 'FolderOpened' },
  { id: 'sec-ai', label: 'AI 配置', icon: 'MagicStick' },
  { id: 'sec-cache', label: '浏览器缓存', icon: 'Coin' },
  { id: 'sec-settings-file', label: '设置文件', icon: 'Document' },
]
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
  let current = groups[0].id
  for (const g of groups) {
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
      <div class="page-title"><el-icon><Setting /></el-icon>&nbsp;设置</div>
      <div class="header-actions">
        <el-button v-if="textDirty" type="primary" size="small" :loading="saving" @click="saveText">
          <el-icon><Check /></el-icon>&nbsp;保存
        </el-button>
        <el-button size="small" @click="reset">
          <el-icon><RefreshLeft /></el-icon>&nbsp;重置
        </el-button>
      </div>
    </div>

    <div class="settings-body flex-1 overflow-hidden flex">
      <!-- 左侧锚点导航 -->
      <div class="settings-nav">
        <a
          v-for="g in groups"
          :key="g.id"
          class="settings-nav-item"
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
          <div class="section-title"><el-icon><Brush /></el-icon>&nbsp;外观</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="主题">
              <el-radio-group v-model="themeMode" @change="onThemeChange">
                <el-radio-button value="dark">暗色</el-radio-button>
                <el-radio-button value="light">亮色</el-radio-button>
              </el-radio-group>
              <span class="hint text-dim" style="margin-left: 12px">切换后立即生效，下次启动保留选择</span>
            </el-form-item>
          </el-form>
        </div>

        <!-- 抓包行为 -->
        <div class="section" id="sec-capture">
          <div class="section-title"><el-icon><Aim /></el-icon>&nbsp;抓包行为</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="代理引擎">
              <el-select v-model="form.proxy_engine" style="max-width: 260px" @change="onProxyEngineChange">
                <el-option value="builtin" label="内置线程（默认）" />
                <el-option value="async" label="asyncio（实验性）" />
                <el-option value="mitmproxy" label="mitmproxy（高性能）" :disabled="!mitmproxyAvailable" />
              </el-select>
              <span class="hint text-dim" style="margin-left: 12px">
                <span v-if="!mitmproxyAvailable" style="color: var(--on-warn)">mitmproxy 加载失败，请重新运行 install.ps1</span>
              </span>
            </el-form-item>
            <el-form-item label="自动滚动">
              <el-switch v-model="flows.autoScroll" @change="autoSave(true)" />
              <span class="hint text-dim" style="margin-left: 12px">新流量到达时自动滚动到顶部</span>
            </el-form-item>
            <el-form-item label="滚动暂停秒数">
              <el-input-number v-model="flows.autoScrollDelay" :min="1" :max="60" :step="1" controls-position="right" @change="textDirty = true" />
              <span class="hint text-dim" style="margin-left: 12px">用户手动滚动后暂停自动滚动 N 秒，默认 10 秒</span>
            </el-form-item>
            <el-form-item label="选项卡自动切换">
              <el-switch v-model="form.auto_switch_preview" :active-value="'1'" :inactive-value="'0'" @change="autoSave(true)" />
              <span class="hint text-dim" style="margin-left: 12px">切换流量时自动回到默认选项卡，关闭则保持当前选项卡（无对应选项时清空）</span>
            </el-form-item>
            <el-form-item label="多选工具栏延时">
              <el-input-number v-model="form.multi_select_bar_delay" :min="0" :max="10" :step="0.5" controls-position="right" @change="textDirty = true" />
              <span class="hint text-dim" style="margin-left: 12px">多选模式下滚动列表隐藏悬浮工具栏，N 秒不操作再显示，默认 1 秒</span>
            </el-form-item>
          </el-form>
        </div>

        <!-- 透明代理 -->
        <div class="section" id="sec-transparent">
          <div class="section-title"><el-icon><Connection /></el-icon>&nbsp;透明代理</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="透明代理模式">
              <el-switch
                :model-value="transparentProxy.running"
                :loading="transparentLoading"
                :disabled="!transparentProxy.supported"
                @change="onToggleTransparent"
              />
              <span class="hint text-dim" style="margin-left: 12px">
                <span v-if="!transparentProxy.supported" style="color: var(--on-warn)">
                  {{ transparentProxy.hint || '当前平台不支持' }}
                </span>
                <span v-else-if="transparentProxy.running" style="color: var(--on-ok)">
                  已启用 · HTTP(80) + HTTPS(443) 透明重定向
                </span>
                <span v-else>HTTP(80) 解析 + HTTPS(443) 隧道转发（不解密）</span>
              </span>
              <el-button
                v-if="!transparentIsAdmin"
                size="small"
                type="warning"
                style="margin-left: 12px"
                :loading="transparentRestarting"
                @click="restartTransparentAsAdmin"
              >
                <el-icon><Key /></el-icon>&nbsp;管理员重启
              </el-button>
            </el-form-item>
            <el-form-item v-if="transparentProxy.running" label="重定向统计">
              <span class="text-dim mono" style="font-size: 12px">
                已重定向 {{ transparentProxy.redirected_count || 0 }} 包 ·
                NAT 表 {{ transparentProxy.nat_table_size || 0 }} 条 ·
                本地端口 {{ transparentProxy.local_port || 8888 }}
              </span>
              <el-button text size="small" style="margin-left: 12px" @click="loadTransparentStatus">刷新</el-button>
            </el-form-item>
            <el-form-item v-if="transparentProxy.last_error" label="最近错误">
              <span style="color: var(--on-error); font-size: 12px">{{ transparentProxy.last_error }}</span>
            </el-form-item>
          </el-form>
          <div class="hint text-dim" style="margin: 4px 12px 0; padding: 8px 12px; background: var(--on-bg); border-radius: 4px">
            <strong>说明：</strong>启用后无需设置系统代理，应用发出的 HTTP(80) + HTTPS(443) 流量会被 WinDivert
            透明重定向到 Telnix 代理端口。<strong>需管理员权限</strong>。
            HTTP 走代理正常解析（可修改/记录）；HTTPS 仅做 TCP 隧道转发（端到端 TLS，<strong>不解密</strong>，但可记录元数据）。
            隐蔽性更强：应用无代理感知，难以通过常规手段探测。
          </div>
        </div>

        <!-- 显示选项（合并流量列表列 + 检查器标签） -->
        <div class="section" id="sec-display">
          <div class="section-title"><el-icon><View /></el-icon>&nbsp;显示选项</div>
          <div class="sub-label">流量列表显示列</div>
          <div class="tab-checkboxes" style="margin-bottom: 12px">
            <el-checkbox
              v-for="c in optionalColumns"
              :key="c.key"
              :model-value="colOn(c.key)"
              @change="() => toggleColumn(c.key)"
            >{{ c.label }}</el-checkbox>
          </div>
          <div class="hint text-dim" style="margin-bottom: 16px">核心列 # / Host / URL / 进程 始终显示，其余列在此开关（默认全部关闭）。</div>

          <div class="sub-label">右键复制项</div>
          <div class="tab-checkboxes" style="margin-bottom: 8px">
            <el-checkbox
              v-for="c in copyFieldOptions"
              :key="c.key"
              :model-value="copyFieldOn(c.key)"
              @change="() => toggleCopyField(c.key)"
            >{{ c.label }}</el-checkbox>
          </div>
          <div class="hint text-dim" style="margin-bottom: 12px">右键流量→「复制」hover 展开的可复制字段。默认 URL + cURL，# 列不参与复制。</div>

          <div class="sub-label">文字选择</div>
          <el-form label-width="180px" size="default" style="margin-bottom: 8px">
            <el-form-item label="流量列表禁选文字">
              <el-switch :model-value="listNoSelect" @change="onListNoSelectChange" />
              <span class="hint text-dim" style="margin-left: 12px">开启后双击包不会选中文字，详细信息仍可选</span>
            </el-form-item>
          </el-form>

          <div class="sub-label">检查器标签页</div>
          <div class="tab-checkboxes" style="margin-bottom: 8px">
            <el-checkbox
              v-for="t in optionalTabs"
              :key="t.key"
              :model-value="tabOn(t.key)"
              @change="() => toggleTab(t.key)"
            >{{ t.label }}</el-checkbox>
          </div>
          <div class="hint text-dim">默认启用 Headers / JSON / Raw / Hex，此处可额外开启以下标签。</div>
        </div>

        <!-- 进程过滤 + Host 忽略 -->
        <div class="section" id="sec-processes">
          <div class="section-title"><el-icon><Cpu /></el-icon>&nbsp;忽略规则</div>
          <div class="hint text-dim" style="margin-bottom: 12px">
            被忽略的进程/Host 流量将直接转发不抓包。PID 和进程名至少填一项；Host 支持 * ? 通配符（如 *.example.com）。
          </div>
          <div class="ignore-add-row">
            <el-input-number v-model="newIgnorePid" :controls="false" placeholder="PID（可选）" style="width: 130px" :min="1" />
            <span class="text-dim" style="font-size: 12px">或</span>
            <el-input v-model="newIgnoreName" placeholder="进程名（可选，如 chrome.exe）" style="width: 240px" class="mono" />
            <el-button type="primary" size="small" @click="addIgnored" :disabled="newIgnorePid === null && !newIgnoreName.trim()">
              <el-icon><Plus /></el-icon>&nbsp;添加
            </el-button>
          </div>
          <el-table v-if="ignoredProcesses.length" :data="ignoredProcesses" size="small" style="margin-top: 10px; max-width: 600px" border>
            <el-table-column prop="pid" label="PID" width="100">
              <template #default="{ row }">
                <span v-if="row.pid != null && row.pid > 0">{{ row.pid }}</span>
                <span v-else class="text-dim">（按名称）</span>
              </template>
            </el-table-column>
            <el-table-column prop="process_name" label="进程名" />
            <el-table-column label="操作" width="100">
              <template #default="{ row }">
                <el-button link type="danger" size="small" @click="removeIgnored(row.id)">
                  <el-icon><Delete /></el-icon>&nbsp;移除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <div v-else class="hint text-dim" style="margin-top: 8px">暂无忽略的进程</div>

          <!-- 忽略 Host 通配符 -->
          <div class="ignore-add-row" style="margin-top: 16px">
            <el-input v-model="newIgnoreHost" placeholder="Host 通配符（如 *.example.com）" style="width: 320px" class="mono" @keyup.enter="addIgnoredHost" />
            <el-button type="primary" size="small" @click="addIgnoredHost" :disabled="!newIgnoreHost.trim()">
              <el-icon><Plus /></el-icon>&nbsp;添加 Host
            </el-button>
          </div>
          <el-table v-if="ignoredHosts.length" :data="ignoredHosts" size="small" style="margin-top: 10px; max-width: 600px" border>
            <el-table-column prop="host_pattern" label="Host 通配符" />
            <el-table-column label="操作" width="100">
              <template #default="{ row }">
                <el-button link type="danger" size="small" @click="removeIgnoredHost(row.id)">
                  <el-icon><Delete /></el-icon>&nbsp;移除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <div v-else class="hint text-dim" style="margin-top: 8px">暂无忽略的 Host</div>
        </div>

        <!-- Clash 集成 -->
        <div class="section" id="sec-clash">
          <div class="section-title"><el-icon><ClashIcon /></el-icon>&nbsp;Clash 集成</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="启用Clash页">
              <el-switch v-model="clashEnabled" @change="onClashToggle" />
              <span class="hint text-dim" style="margin-left: 12px">
                启用后侧边栏显示 Clash 入口；流量是否走代理由 Clash 页内开关控制
              </span>
            </el-form-item>
            <el-form-item label="外部控制器监听地址">
              <el-input v-model="form.clash_api_url" placeholder="http://127.0.0.1:9090" class="mono" style="max-width: 420px" @input="textDirty = true" />
            </el-form-item>
            <el-form-item label="API 密钥">
              <el-input v-model="form.clash_secret" placeholder="（无密钥留空）" show-password class="mono" style="max-width: 420px" @input="textDirty = true" />
            </el-form-item>
            <el-form-item label="混合代理端口">
              <el-input-number v-model="form.clash_mixed_port" :min="0" :max="65535" :step="1" style="width: 150px" @change="textDirty = true" />
              <span class="hint text-dim" style="margin-left: 12px">0 = 自动从 Mihomo 获取</span>
            </el-form-item>
            <el-form-item>
              <el-button @click="testClashConnection" :loading="clashTesting">测试连接</el-button>
              <el-button @click="showTutorial">查看教程</el-button>
              <span v-if="clashTestResult" class="hint" :style="{ color: clashTestResult.ok ? '#67c23a' : '#f56c6c', marginLeft: '12px' }">
                {{ clashTestResult.msg }}
              </span>
            </el-form-item>
            <div class="hint text-dim" style="margin-left: 160px">
              外接模式：Telnix 不启动 Mihomo，只连接已运行的 Clash Verge / Mihomo 客户端。
              订阅和节点配置在 Clash 客户端管理，Telnix 只读取和切换。
            </div>
          </el-form>
        </div>

        <!-- HTTPS 证书 -->
        <div class="section" id="sec-cert">
          <div class="section-title"><el-icon><Lock /></el-icon>&nbsp;HTTPS 证书</div>
          <div class="cert-row">
            <span class="cert-status">
              <el-icon :class="{ ok: certInstalled }">
                <component :is="certInstalled ? 'CircleCheck' : 'WarningFilled'" />
              </el-icon>
              {{ certInstalled ? '根证书已安装，HTTPS 解密已启用' : '根证书未安装，HTTPS 解密不可用' }}
            </span>
            <el-button v-if="!certInstalled" type="primary" size="small" @click="installCert">安装证书</el-button>
            <el-button v-else size="small" @click="installCert">重新安装</el-button>
          </div>
        </div>

        <!-- 手机抓包（安卓） -->
        <div class="section" id="sec-mobile">
          <div class="section-title"><el-icon><Iphone /></el-icon>&nbsp;手机抓包（安卓）</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="允许局域网设备连接">
              <el-switch
                :model-value="form.proxy_listen_host === '0.0.0.0'"
                @change="onLanToggle"
              />
              <span class="hint text-dim" style="margin-left: 12px">
                开启后代理监听 0.0.0.0，手机配 WiFi 代理可连（需重启后端生效）
              </span>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="showMobileWizard">
                <el-icon><Iphone /></el-icon>&nbsp;手机抓包向导
              </el-button>
              <span class="hint text-dim" style="margin-left: 12px">
                显示二维码和配置步骤，扫码下载根证书
              </span>
            </el-form-item>
          </el-form>
        </div>

        <!-- 弱网模拟（Throttle） -->
        <div class="section" id="sec-throttle">
          <div class="section-title"><el-icon><Connection /></el-icon>&nbsp;弱网模拟（Throttle）</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="启用弱网模拟">
              <el-switch v-model="throttle.enabled" @change="saveThrottle" />
              <span class="hint text-dim" style="margin-left: 12px">
                模拟高延迟、低带宽、丢包等网络环境（仅作用于代理流量，热生效）
              </span>
            </el-form-item>
            <el-form-item label="延迟（毫秒）">
              <el-input-number v-model="throttle.latency_ms" :min="0" :max="10000" :step="100" :disabled="!throttle.enabled" @change="saveThrottle" />
              <span class="hint text-dim" style="margin-left: 12px">每连接启动延迟，模拟 RTT</span>
            </el-form-item>
            <el-form-item label="限速（KB/s）">
              <el-input-number v-model="throttle.bps_kbps" :min="0" :max="102400" :step="10" :disabled="!throttle.enabled" @change="saveThrottle" />
              <span class="hint text-dim" style="margin-left: 12px">0 = 不限速</span>
            </el-form-item>
            <el-form-item label="丢包率（%）">
              <el-input-number v-model="throttle.drop_pct" :min="0" :max="100" :step="1" :disabled="!throttle.enabled" @change="saveThrottle" />
              <span class="hint text-dim" style="margin-left: 12px">仅对 HTTPS 隧道流量生效</span>
            </el-form-item>
          </el-form>
        </div>

        <!-- 路径配置 -->
        <div class="section" id="sec-paths">
          <div class="section-title"><el-icon><FolderOpened /></el-icon>&nbsp;路径配置</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="数据存储路径">
              <div class="path-row">
                <span class="path-display mono" :title="form.data_path || '（默认路径）'">{{ form.data_path || '（默认路径）' }}</span>
                <el-button size="small" @click="openCurrentPath(form.data_path || '')" title="在资源管理器中打开">
                  <el-icon><FolderOpened /></el-icon>
                </el-button>
                <el-button size="small" @click="openDirPicker('data_path')" title="选择目录">
                  <el-icon><Folder /></el-icon>&nbsp;选择目录
                </el-button>
              </div>
              <div class="hint text-dim">流量记录、数据库等数据存储路径，修改后重启生效</div>
            </el-form-item>
          </el-form>
        </div>

        <!-- AI 配置 -->
        <div class="section" id="sec-ai">
          <div class="section-title"><el-icon><MagicStick /></el-icon>&nbsp;AI 配置</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="DeepSeek API Key">
              <el-input v-model="form.deepseek_api_key" placeholder="sk-..." show-password class="mono" style="max-width: 420px" @input="textDirty = true" />
            </el-form-item>
            <el-form-item label="模型">
              <el-select v-model="form.deepseek_model" style="max-width: 260px" @change="textDirty = true">
                <el-option value="deepseek-v4-flash" label="deepseek-v4-flash（快速，默认）" />
                <el-option value="deepseek-v4-pro" label="deepseek-v4-pro（强大）" />
              </el-select>
              <div class="hint text-dim">仅支持 v4-flash / v4-pro，其他模型已弃用</div>
            </el-form-item>
          </el-form>
        </div>

        <!-- 浏览器缓存 -->
        <div class="section" id="sec-cache">
          <div class="section-title"><el-icon><Coin /></el-icon>&nbsp;浏览器缓存</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="当前缓存大小">
              <span class="mono">{{ formatSize(cacheSize) }}</span>
              <el-button size="small" style="margin-left: 16px" @click="refreshCacheSize">刷新</el-button>
            </el-form-item>
            <el-form-item label="自动清理">
              <el-switch v-model="cacheAutoClean" />
              <span class="hint text-dim" style="margin-left: 12px">超过阈值时自动删除最早的流量记录</span>
            </el-form-item>
            <el-form-item label="阈值 (MB)">
              <el-input-number v-model="cacheThreshold" :min="1" :max="100" :step="1" controls-position="right" />
              <span class="hint text-dim" style="margin-left: 12px">默认 10MB</span>
            </el-form-item>
            <el-form-item label="清空缓存">
              <el-button type="danger" size="small" @click="clearCache">
                <el-icon><Delete /></el-icon>&nbsp;立即清空
              </el-button>
            </el-form-item>
          </el-form>
        </div>

        <!-- 设置文件 -->
        <div class="section" id="sec-settings-file">
          <div class="section-title"><el-icon><Document /></el-icon>&nbsp;设置文件</div>
          <el-form label-width="160px" size="default">
            <el-form-item label="settings.json">
              <span class="path-display mono" :title="form.settings_file_path || ''">{{ form.settings_file_path || '（未生成）' }}</span>
              <el-button size="small" style="margin-left: 12px" @click="openSettingsFile" title="用记事本打开 settings.json">
                <el-icon><Document /></el-icon>&nbsp;打开设置文件
              </el-button>
            </el-form-item>
            <div class="hint text-dim" style="margin-left: 160px">
              所有用户设置（含列排序、导航顺序等 GUI 偏好）统一存到此 JSON 文件，可直接编辑
            </div>
          </el-form>
        </div>
      </div>
    </div>

    <!-- 目录选择器对话框 -->
    <el-dialog v-model="dirPickerVisible" title="选择目录" width="560px">
      <div class="dir-picker">
        <div class="dir-current mono">{{ dirPickerCurrent || '（选择盘符）' }}</div>
        <div class="dir-toolbar">
          <el-button size="small" @click="goParent" :disabled="!dirPickerParent" link>
            <el-icon><Back /></el-icon>&nbsp;上级
          </el-button>
          <el-button size="small" @click="loadDirs('')" link>
            <el-icon><Monitor /></el-icon>&nbsp;根目录
          </el-button>
        </div>
        <div class="dir-list" v-loading="dirPickerLoading">
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
            （无子目录）
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="dirPickerVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmDir" :disabled="!dirPickerCurrent">选择此目录</el-button>
      </template>
    </el-dialog>

    <!-- 代理引擎选择说明对话框 -->
    <el-dialog
      v-model="engineHelpVisible"
      title="代理引擎如何选择？"
      width="640px"
      destroy-on-close
      align-center
      :lock-scroll="true"
    >
      <div class="engine-help-body">
        <p class="engine-help-intro">三个引擎都能完成日常 HTTP/HTTPS 抓包，差异主要在适用场景和功能完整度。</p>

        <div class="engine-card">
          <div class="engine-card-title">内置线程（默认推荐）</div>
          <div class="engine-card-desc">日常抓包首选。功能最完整，支持本机进程识别、TCP/UDP 抓包、WebSocket 透传、断点、自动回复规则等全部能力，零额外依赖，稳定性最好。已针对高并发做了连接池与后台线程优化，绝大多数场景都不会感觉卡。</div>
        </div>

        <div class="engine-card">
          <div class="engine-card-title">mitmproxy（高性能）</div>
          <div class="engine-card-desc">适合遇到罕见协议或畸形请求无法解析时使用。协议兼容性最强，SSL 握手略快。但会丢失本机进程信息、TCP/UDP 抓包等能力，断点也较易出问题，且需额外安装约 50MB 依赖。未安装时选项不可选。</div>
        </div>

        <div class="engine-card">
          <div class="engine-card-title">asyncio（实验性）</div>
          <div class="engine-card-desc">目前仍是半成品，实际连接处理仍走线程模型，与内置引擎几乎无差异，仅多一层事件循环开销。不建议日常使用，保留作为未来扩展的基础。</div>
        </div>

        <div class="engine-help-tip">
          <strong>一句话建议：</strong>保持默认「内置线程」即可；只在遇到解析不出的特殊流量时再尝试 mitmproxy。
        </div>
      </div>
      <template #footer>
        <el-button type="primary" @click="engineHelpVisible = false">明白了</el-button>
      </template>
    </el-dialog>

    <!-- Clash 教程对话框 -->
    <el-dialog
      v-model="tutorialVisible"
      title="Clash 集成教程"
      width="80%"
      top="5vh"
      class="tutorial-dialog"
      destroy-on-close
    >
      <div v-loading="tutorialLoading" class="tutorial-body" v-html="tutorialHtml"></div>
    </el-dialog>

    <!-- 手机抓包向导 -->
    <el-dialog
      v-model="mobileVisible"
      title="手机抓包向导（安卓）"
      width="560px"
      destroy-on-close
      align-center
      :lock-scroll="true"
      class="mobile-dialog"
    >
      <div v-loading="mobileLoading" class="mobile-wizard">
        <template v-if="mobileSetup">
          <div class="mobile-step">
            <div class="step-num">1</div>
            <div class="step-content">
              <div class="step-title">开启局域网监听</div>
              <div class="step-desc">
                设置页「允许局域网设备连接」开关需为开启状态（代理监听 0.0.0.0）。
                当前监听地址：<code>{{ form.proxy_listen_host || '127.0.0.1' }}</code>
                <span v-if="form.proxy_listen_host !== '0.0.0.0'" style="color: var(--on-error)">（未开启，请先开启并重启后端）</span>
              </div>
            </div>
          </div>

          <div class="mobile-step">
            <div class="step-num">2</div>
            <div class="step-content">
              <div class="step-title">扫码下载根证书</div>
              <div class="step-desc">
                手机浏览器扫下方二维码（或手动访问
                <code style="word-break: break-all">{{ mobileSetup.cert_download_url }}</code>）
              </div>
              <div class="qr-wrap" v-if="mobileQrDataUrl">
                <img :src="mobileQrDataUrl" alt="证书下载二维码" />
              </div>
              <div class="cert-download-row">
                <el-button size="small" type="primary" plain @click="downloadCert">
                  <el-icon><Download /></el-icon>&nbsp;下载证书到本机
                </el-button>
                <span class="text-dim cert-hint">下载后通过 USB / 网盘传到手机</span>
              </div>
            </div>
          </div>

          <div class="mobile-step">
            <div class="step-num">3</div>
            <div class="step-content">
              <div class="step-title">安装证书</div>
              <div class="step-desc">
                下载的 <code>telnix_root.pem</code> 在手机「设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书」中选择安装。<br/>
                <span style="color: var(--on-warn)">⚠ 安卓 7+ 默认不信任用户证书，HTTPS 抓包可能需要 root 后导入系统证书，或对目标 App 改 networkSecurityConfig。</span>
              </div>
              <div class="android-sys-cert" v-if="mobileSetup.android_cert_url">
                <div class="android-title">📱 安卓 7+ 系统证书（root 用户）</div>
                <div class="android-desc">
                  已计算好哈希文件名（<code>&lt;hash&gt;.0</code>），下载后参考以下教程导入系统证书目录：
                </div>
                <div class="cert-download-row">
                  <el-button size="small" type="warning" plain @click="downloadAndroidCert">
                    <el-icon><Download /></el-icon>&nbsp;下载安卓系统证书
                  </el-button>
                  <span class="text-dim cert-hint">文件名已是 <code>&lt;hash&gt;.0</code> 格式</span>
                </div>
                <div class="android-tutorials">
                  <el-button size="small" type="primary" @click="openAndroidTutorial('mumu')">
                    MuMu 模拟器教程
                  </el-button>
                  <el-button size="small" type="primary" @click="openAndroidTutorial('leidian')">
                    雷电模拟器 / root 实体设备教程
                  </el-button>
                </div>
              </div>
            </div>
          </div>

          <div class="mobile-step">
            <div class="step-num">4</div>
            <div class="step-content">
              <div class="step-title">手机配 WiFi 代理</div>
              <div class="step-desc">
                手机连同一 WiFi → 长按网络 → 修改 → 高级 → 代理 → 手动<br/>
                主机：<code>{{ mobileSetup.proxy_host }}</code><br/>
                端口：<code>{{ mobileSetup.proxy_port }}</code>
              </div>
            </div>
          </div>

          <div class="mobile-step">
            <div class="step-num">5</div>
            <div class="step-content">
              <div class="step-title">开始抓包</div>
              <div class="step-desc">
                Telnix 抓包页点「开始」，手机操作 App，流量即可在抓包页看到。
              </div>
            </div>
          </div>
        </template>
      </div>
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
  color: var(--el-color-primary, #409eff);
}
.engine-card-desc {
  font-size: 13px;
  color: var(--el-text-color-regular, #ccc);
}
.engine-help-tip {
  margin-top: 16px;
  padding: 10px 14px;
  border-radius: 6px;
  background: var(--el-color-primary-light-9, rgba(64,158,255,0.1));
  border-left: 3px solid var(--el-color-primary, #409eff);
  font-size: 13px;
  color: var(--el-text-color-regular, #ccc);
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
  background: var(--el-color-primary, #409eff);
  color: #fff;
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
  color: var(--el-text-color-regular, #ccc);
  line-height: 1.6;
  font-size: 13px;
}
.mobile-step .step-desc code {
  background: var(--el-fill-color-light, #2a2a2a);
  padding: 1px 6px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  color: var(--el-color-success, #67c23a);
}
.mobile-step .qr-wrap {
  margin-top: 10px;
  text-align: center;
}
.mobile-step .qr-wrap img {
  width: 240px;
  height: 240px;
  border: 1px solid var(--el-border-color, #333);
  border-radius: 6px;
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
</style>
