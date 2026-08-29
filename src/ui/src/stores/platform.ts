import { reactive } from 'vue'
import { api } from '../api/client'

export interface PlatformCapabilities {
  platform: string
  is_windows: boolean
  is_linux: boolean
  is_macos: boolean
  is_unix: boolean
  is_admin: boolean
  capabilities: {
    raw_capture: { supported: boolean; backend: string; needs_admin: boolean; admin_hint: string }
    transparent_proxy: { supported: boolean; backend: string; needs_admin: boolean; admin_hint: string }
    dns_hijack: { supported: boolean; backend: string; needs_admin: boolean; admin_hint: string }
    system_proxy: { supported: boolean; backend: string; hint: string }
    windivert_warning: { supported: boolean; needed: boolean; ack: boolean }
    admin_elevation: { supported: boolean; backend: string; hint: string }
  }
}

/**
 * 全局预加载的平台能力数据，供设置页透明代理等需要 is_admin 的场景直接使用，
 * 避免每次进入设置页都重新请求 /system/platform-capabilities 导致状态跳动。
 */
export const platformCapabilities = reactive<PlatformCapabilities>({
  platform: '',
  is_windows: false,
  is_linux: false,
  is_macos: false,
  is_unix: false,
  is_admin: false,
  capabilities: {
    raw_capture: { supported: false, backend: 'none', needs_admin: true, admin_hint: '' },
    transparent_proxy: { supported: false, backend: 'none', needs_admin: true, admin_hint: '' },
    dns_hijack: { supported: false, backend: 'none', needs_admin: true, admin_hint: '' },
    system_proxy: { supported: false, backend: 'none', hint: '' },
    windivert_warning: { supported: false, needed: false, ack: false },
    admin_elevation: { supported: false, backend: 'none', hint: '' },
  },
})

let loading = false
let loadPromise: Promise<PlatformCapabilities> | null = null

export async function loadPlatformCapabilities(): Promise<PlatformCapabilities> {
  if (loadPromise) return loadPromise
  if (loading) {
    // 等待其他调用完成（通过创建一个新的等待 Promise）
    loadPromise = new Promise<PlatformCapabilities>((resolve) => {
      const check = setInterval(() => {
        if (!loading) {
          clearInterval(check)
          resolve(platformCapabilities)
        }
      }, 50)
    })
    return loadPromise
  }
  loading = true
  loadPromise = (async () => {
    try {
      const data = await api.platformCapabilities()
      if (data && typeof data === 'object') {
        Object.assign(platformCapabilities, data)
      }
    } catch {
      /* ignore: 后端不可达时保持默认值 */
    } finally {
      loading = false
    }
    return platformCapabilities
  })()
  return loadPromise
}
