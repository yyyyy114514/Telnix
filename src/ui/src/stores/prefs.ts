// 前端用户偏好同步：把 localStorage 中的 GUI 偏好同步到后端 settings.json
// 这样所有用户设置（含列排序、导航顺序等）统一存在 settings.json，用户可直接编辑
import { api } from '../api/client'

// 需要同步到 settings.json 的 localStorage key 映射
// key = localStorage 键名，value = settings.json 中的字段名
const PREFS_MAP: Record<string, string> = {
  opennet_theme: 'theme',
  opennet_list_no_select: 'list_no_select',
  opennet_nav_order: 'nav_order',
  opennet_col_order: 'col_order',
  opennet_copy_fields: 'copy_fields',
  opennet_cache_threshold: 'cache_threshold',
  opennet_cache_autoclean: 'cache_autoclean',
  opennet_auto_scroll: 'auto_scroll',
  opennet_auto_scroll_delay: 'auto_scroll_delay',
}

let syncTimer: number | null = null
let lastSyncHash = ''

function computePrefsSnapshot(): Record<string, any> {
  const out: Record<string, any> = {}
  for (const [lsKey, settingKey] of Object.entries(PREFS_MAP)) {
    const v = localStorage.getItem(lsKey)
    if (v == null) continue
    // 尝试 JSON 解析（数组/对象/布尔/数字），失败保留字符串
    try {
      out[settingKey] = JSON.parse(v)
    } catch {
      // 字符串值：尝试转布尔
      if (v === 'true') out[settingKey] = true
      else if (v === 'false') out[settingKey] = false
      else out[settingKey] = v
    }
  }
  return out
}

function computeHash(obj: any): string {
  try {
    return JSON.stringify(obj)
  } catch {
    return ''
  }
}

// 同步偏好到后端 settings.json（节流，500ms 内多次调用合并为一次）
export function syncPrefs(throttle = true) {
  if (throttle) {
    if (syncTimer !== null) clearTimeout(syncTimer)
    syncTimer = window.setTimeout(doSync, 500)
  } else {
    doSync()
  }
}

async function doSync() {
  syncTimer = null
  const snapshot = computePrefsSnapshot()
  const hash = computeHash(snapshot)
  if (hash === lastSyncHash) return  // 无变化
  lastSyncHash = hash
  try {
    await api.saveSettings(snapshot)
  } catch {
    // 忽略：失败时下次再试
  }
}

// 启动时同步一次（让 settings.json 立即包含所有 localStorage 偏好）
export function initPrefsSync() {
  // 延迟 2s 启动，避免与启动时其他初始化竞争
  setTimeout(() => syncPrefs(false), 2000)
  // 监听跨 tab 的 storage 事件，其他 tab 修改偏好时也同步
  window.addEventListener('storage', (e) => {
    if (e.key && PREFS_MAP[e.key]) {
      syncPrefs()
    }
  })
}
