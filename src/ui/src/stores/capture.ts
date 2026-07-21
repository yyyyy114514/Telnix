import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, type Status, type BpStatus } from '../api/client'
import { clearFlowCache, useFlowsStore } from './flows'

/** 抓包状态 store：抓包/证书/断点状态，2 秒轮询 */
export const useCaptureStore = defineStore('capture', () => {
  const status = ref<Status>({ capturing: false, proxy_port: 8888, session_id: 0, cert_installed: false })
  const bpStatus = ref<BpStatus>({ break_on_request: false, break_on_response: false })
  const polling = ref(false)
  let timer: number | null = null
  // 抓包切换保护期：capture start/stop 后短时间内跳过轮询的 fetchStatus，
  // 避免后端尚未完成状态切换时把乐观更新的状态覆盖回去（"闪一下变回去"现象）
  let _captureGuardUntil = 0

  async function fetchStatus() {
    try {
      const s = await api.getStatus()
      // 检测后端重启：started_at 变化时清空前端流量缓存，
      // 避免旧缓存的 id 与后端重置后的 id 冲突导致重复/乱序
      if (s.started_at) {
        const prev = localStorage.getItem('opennet_backend_started_at')
        if (prev && prev !== String(s.started_at)) {
          clearFlowCache()
        }
        localStorage.setItem('opennet_backend_started_at', String(s.started_at))
      }
      // 抓包切换保护期：保护期内不覆盖 capturing（避免后端尚未完成状态切换时
      // 把乐观更新的 capturing=true 覆盖回 false，导致"闪一下变回去"）
      if (Date.now() < _captureGuardUntil) {
        s.capturing = status.value.capturing
      }
      status.value = s
    } catch (e) {
      /* 静默 */
    }
  }

  async function fetchBpStatus() {
    try {
      bpStatus.value = await api.getBpStatus()
    } catch (e) {
      /* 静默 */
    }
  }

  function startPolling() {
    if (polling.value) return
    polling.value = true
    fetchStatus()
    fetchBpStatus()
    timer = window.setInterval(() => {
      fetchStatus()
      fetchBpStatus()
    }, 2000)
  }

  function stopPolling() {
    polling.value = false
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  async function toggleCapture() {
    // 乐观更新由 store 内部完成，调用方不应预先修改 status.value.capturing，
    // 否则会导致这里的 if 判断反转（调用方设 true 后，这里误以为是"正在抓包"而执行 stop）
    const wasCapturing = status.value.capturing
    try {
      if (wasCapturing) {
        status.value.capturing = false
        _captureGuardUntil = Date.now() + 1500
        await api.captureStop()
        fetchStatus()  // 异步同步完整状态，不阻塞返回
      } else {
        status.value.capturing = true
        _captureGuardUntil = Date.now() + 1500
        await api.captureStart()
        fetchStatus()
      }
    } catch (e: any) {
      // 失败回滚到原状态
      status.value.capturing = wasCapturing
      throw e
    }
  }

  async function clearSessions(): Promise<any> {
    return await api.captureClear()
  }

  async function toggleBpRequest() {
    const newVal = !bpStatus.value.break_on_request
    // 乐观更新：先改 UI，再发请求
    bpStatus.value.break_on_request = newVal
    try {
      await api.setBpRequest({ enabled: newVal })
    } catch (e: any) {
      // 失败时回滚
      bpStatus.value.break_on_request = !newVal
      throw e
    }
  }

  async function toggleBpResponse() {
    const newVal = !bpStatus.value.break_on_response
    bpStatus.value.break_on_response = newVal
    try {
      await api.setBpResponse({ enabled: newVal })
    } catch (e: any) {
      bpStatus.value.break_on_response = !newVal
      throw e
    }
  }

  // 全部放行（走批量 API，避免 N+1 HTTP 调用）
  async function releaseAll() {
    const pending = bpStatus.value.pending_flows || []
    const ids = pending.map((f: any) => f.id || f.flow_id).filter(Boolean)
    if (ids.length) {
      try {
        await api.batchReleaseFlows(ids, 'release')
        // 同步更新 flows store 的断点状态（避免流量列表仍显示断点闪烁/菜单）
        useFlowsStore().patchFlows(ids, (f) => { f.breakpoint_status = null })
      } catch {
        /* skip */
      }
    }
    // 立即清空 pending 列表让 BreakpointBar 按钮消失，再异步刷新真实状态
    bpStatus.value = { ...bpStatus.value, pending_flows: [] }
    fetchBpStatus()
  }

  // 全部丢弃（走批量 API，避免 N+1 HTTP 调用）
  async function dropAll() {
    const pending = bpStatus.value.pending_flows || []
    const ids = pending.map((f: any) => f.id || f.flow_id).filter(Boolean)
    if (ids.length) {
      try {
        await api.batchReleaseFlows(ids, 'drop')
        useFlowsStore().patchFlows(ids, (f) => { f.breakpoint_status = null })
      } catch {
        /* skip */
      }
    }
    bpStatus.value = { ...bpStatus.value, pending_flows: [] }
    fetchBpStatus()
  }

  async function installCert() {
    await api.installCert()
    await fetchStatus()
  }

  return {
    status,
    bpStatus,
    polling,
    startPolling,
    stopPolling,
    fetchStatus,
    fetchBpStatus,
    toggleCapture,
    clearSessions,
    toggleBpRequest,
    toggleBpResponse,
    releaseAll,
    dropAll,
    installCert,
  }
})
