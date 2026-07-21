import { defineStore } from 'pinia'
import { ref, shallowRef, computed, triggerRef } from 'vue'
import { api, type Flow } from '../api/client'

const CACHE_KEY = 'opennet_flows_cache'
const CACHE_THRESHOLD_KEY = 'opennet_cache_threshold'
const CACHE_AUTOCLEAN_KEY = 'opennet_cache_autoclean'

/** 计算 localStorage 中流量缓存的大小（字节） */
export function getCacheSize(): number {
  try {
    const data = localStorage.getItem(CACHE_KEY)
    if (!data) return 0
    return new Blob([data]).size
  } catch {
    return 0
  }
}

/** 清空流量缓存（localStorage），并广播事件让 flows store 清内存 */
export function clearFlowCache() {
  localStorage.removeItem(CACHE_KEY)
  // 通知 flows store 清空内存中的列表（避免循环依赖，用事件解耦）
  window.dispatchEvent(new CustomEvent('opennet:flows-cache-cleared'))
}

/** 自动清理：超过阈值时删除最早的流量 */
function autoCleanCache(thresholdMB: number) {
  try {
    const data = localStorage.getItem(CACHE_KEY)
    if (!data) return
    const size = new Blob([data]).size
    const thresholdBytes = thresholdMB * 1024 * 1024
    if (size <= thresholdBytes) return
    // 超过阈值，保留最新的 70% 流量
    const flows: Flow[] = JSON.parse(data)
    const keep = flows.slice(Math.floor(flows.length * 0.3))
    localStorage.setItem(CACHE_KEY, JSON.stringify(keep))
  } catch {
    /* ignore */
  }
}

/** 流量列表 store：列表 + 选中 + 筛选 + localStorage 缓存
 *
 * 性能优化：
 * 1. shallowRef 替代 ref：避免 Vue 递归把每条 flow 的大 body 字段转成响应式 proxy
 * 2. Map<id, Flow> 索引：O(1) 查找 selectedFlow，替代 O(n) find
 * 3. debounce saveToCache：避免每次增量轮询都全量 stringify 阻塞主线程
 * 4. pollNewFlows 用 unshift + triggerRef 替代新建数组
 */
export const useFlowsStore = defineStore('flows', () => {
  // shallowRef：只追踪 .value 引用变化，不递归追踪 flow 对象内部字段
  const flows = shallowRef<Flow[]>([])
  const total = ref(0)
  // 当前已加载的最大 flow id，用于增量轮询
  const maxFlowId = ref(0)
  // 轮询锁：防止并发 pollNewFlows/loadAllFlows 用相同 maxFlowId 拉到重复数据
  let polling = false
  const selectedId = ref<number | null>(null)
  // 跨页跳转时直接注入的 flow 对象（不依赖列表查找，解决搜索/分析页 flow 不在 store 列表中的问题）
  const selectedFlowOverride = ref<Flow | null>(null)
  // 跳过下一次自动滚动（跨页跳转注入 flow 时设置，避免 length watch 把页面滚回顶部）
  const skipNextAutoScroll = ref(false)

  // Map 索引：id → Flow，O(1) 查找。与 flows 同步维护
  const flowIndex = new Map<number, Flow>()

  function rebuildIndex() {
    flowIndex.clear()
    for (const f of flows.value) flowIndex.set(f.id, f)
  }

  const selectedFlow = computed<Flow | null>(() => {
    if (selectedId.value == null) return selectedFlowOverride.value || null
    return flowIndex.get(selectedId.value) || selectedFlowOverride.value || null
  })

  // 筛选条件
  const filterHost = ref('')
  // 筛选进程：支持多选 PID（前端过滤），不再走后端 process 参数
  const filterProcessPids = ref<number[]>([])
  const filterProcessName = ref<string>('')
  // 筛选状态码：多选（前端过滤）
  const filterStatusCodes = ref<string[]>([])
  // 筛选方法：多选（前端过滤）
  const filterMethods = ref<string[]>([])
  // 筛选 Content-Type 大类：多选（前端过滤，如 image/text/application/video 等）
  const filterContentTypes = ref<string[]>([])
  // 筛选协议：多选（前端过滤，http/https/tcp/udp）
  const filterProtocols = ref<string[]>([])

  // 自动滚动（从 localStorage 恢复，默认开启，延迟默认 10 秒）
  const autoScroll = ref(localStorage.getItem('opennet_auto_scroll') !== 'false')
  const autoScrollDelay = ref(Number(localStorage.getItem('opennet_auto_scroll_delay')) || 10)
  const autoScrollPaused = ref(false) // 用户滚动时暂停

  // 自动滚动设置持久化
  function persistAutoScroll() {
    localStorage.setItem('opennet_auto_scroll', autoScroll.value ? 'true' : 'false')
    localStorage.setItem('opennet_auto_scroll_delay', String(autoScrollDelay.value))
  }

  // 跨页携带：供 AI 分析使用的选中流量
  const aiFlowIds = ref<number[]>([])

  // 性能优化：debounce saveToCache，避免每次增量轮询都全量 stringify 阻塞主线程
  let saveTimer: number | null = null
  function saveToCache() {
    if (saveTimer != null) {
      window.clearTimeout(saveTimer)
    }
    saveTimer = window.setTimeout(() => {
      saveTimer = null
      try {
        localStorage.setItem(CACHE_KEY, JSON.stringify(flows.value))
        // 检查是否需要自动清理
        const threshold = parseFloat(localStorage.getItem(CACHE_THRESHOLD_KEY) || '10')
        const autoClean = localStorage.getItem(CACHE_AUTOCLEAN_KEY) !== 'false'
        if (autoClean && threshold > 0) {
          autoCleanCache(threshold)
        }
      } catch {
        /* localStorage 满了，忽略 */
      }
    }, 1000)
  }

  // 立即保存（用于 clear 等需要立即持久化的场景）
  function saveToCacheNow() {
    if (saveTimer != null) {
      window.clearTimeout(saveTimer)
      saveTimer = null
    }
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(flows.value))
    } catch {
      /* ignore */
    }
  }

  // 从 localStorage 恢复
  function restoreFromCache() {
    try {
      const data = localStorage.getItem(CACHE_KEY)
      if (data) {
        const cached = JSON.parse(data)
        if (Array.isArray(cached) && cached.length > 0) {
          // 去重：按 id 保留最新一条，修复历史缓存可能存在的重复
          const seen = new Set<number>()
          const deduped: Flow[] = []
          for (const f of cached) {
            if (f && typeof f.id === 'number' && !seen.has(f.id)) {
              seen.add(f.id)
              deduped.push(f)
            }
          }
          // 强制降序排序，保证最新在顶部
          deduped.sort((a: any, b: any) => b.id - a.id)
          flows.value = deduped
          rebuildIndex()
          total.value = deduped.length
          // 列表已降序，第一条就是最大 id
          maxFlowId.value = deduped[0]?.id || 0
        }
      }
    } catch {
      /* ignore */
    }
  }

  async function loadFlows(sessionId: number) {
    if (!sessionId) return
    if (polling) return
    polling = true
    try {
      // 进程/状态码/方法改为前端过滤，不再传后端参数
      const params: any = { limit: 300, offset: 0 }
      const res = await api.getFlows(sessionId, params)
      flows.value = res.flows || []
      rebuildIndex()
      total.value = res.total || 0
      updateMaxFlowId()
      saveToCache()
    } catch (e) {
      /* 静默 */
    } finally {
      polling = false
    }
  }

  // 跨会话加载最近 N 条流量（用于抓包页面显示历史流量，不依赖活动会话）
  async function loadAllFlows(limit: number = 300) {
    if (polling) return
    polling = true
    try {
      const res: any = await api.getAllFlows({ limit, offset: 0 })
      let list: Flow[] = res.flows || []
      // 强制按 id 降序排序，保证最新流量在顶部（防止后端排序异常或缓存乱序）
      list = list.sort((a: any, b: any) => b.id - a.id)
      flows.value = list
      rebuildIndex()
      total.value = res.total || 0
      // 列表已降序，第一条就是最大 id
      maxFlowId.value = list[0]?.id || 0
      saveToCache()
    } catch (e) {
      /* 静默 */
    } finally {
      polling = false
    }
  }

  // 增量轮询：用 since_id 拉取 maxFlowId 之后的新流量，插入列表顶部
  // 返回新增条数（用于触发自动滚动）
  async function pollNewFlows(): Promise<number> {
    if (!maxFlowId.value) return 0
    if (polling) return 0
    polling = true
    try {
      const res: any = await api.getAllFlows({ since_id: maxFlowId.value, limit: 200 })
      const list: Flow[] = res.flows || []
      if (!list.length) return 0
      // 后端返回按 id DESC（最大 id 在前），直接插入顶部即可保持降序
      // 插入前去重：过滤掉列表中已存在的 id（防止并发/缓存导致的重复）
      const deduped = list.filter(f => !flowIndex.has(f.id))
      if (!deduped.length) {
        // 后端返回的 list[0].id 可能比当前 maxFlowId 大（其他客户端插入）
        const newMax = list[0]?.id || 0
        if (newMax > maxFlowId.value) maxFlowId.value = newMax
        return 0
      }
      // 性能优化：unshift 原地修改 + triggerRef，避免新建大数组
      flows.value.unshift(...deduped)
      for (const f of deduped) flowIndex.set(f.id, f)
      triggerRef(flows)
      // deduped 已按 id DESC，第一条就是最大 id
      const newMax = deduped[0].id
      if (newMax > maxFlowId.value) maxFlowId.value = newMax
      saveToCache()
      return deduped.length
    } catch (e) {
      return 0
    } finally {
      polling = false
    }
  }

  // 更新 maxFlowId（取当前列表最大 id）
  function updateMaxFlowId() {
    let m = 0
    for (const f of flows.value) {
      if (f.id > m) m = f.id
    }
    maxFlowId.value = m
  }

  function select(id: number | null) {
    selectedId.value = id
    // 清除 override，让 selectedFlow 走列表查找
    selectedFlowOverride.value = null
  }

  /** 跨页跳转用：直接注入 flow 对象，不依赖列表查找。
   *  如果 flow 不在当前列表中（如历史会话的 flow），注入到列表头部，
   *  保证 FlowList 能高亮选中行并滚动定位。设置 skipNextAutoScroll 避免注入触发自动滚动到顶部。
   */
  function selectFlow(flow: Flow) {
    selectedId.value = flow.id
    selectedFlowOverride.value = flow
    if (!flowIndex.has(flow.id)) {
      skipNextAutoScroll.value = true
      flows.value = [flow, ...flows.value]
      flowIndex.set(flow.id, flow)
      total.value = flows.value.length
    }
  }

  function clear() {
    flows.value = []
    flowIndex.clear()
    total.value = 0
    maxFlowId.value = 0
    selectedId.value = null
    clearFlowCache()
  }

  // 从前端列表移除指定 id 的流量（批量删除/放行后用，避免全量重载）
  function removeFlows(ids: number[]) {
    if (!ids.length) return
    const idSet = new Set(ids)
    flows.value = flows.value.filter(f => !idSet.has(f.id))
    for (const id of ids) flowIndex.delete(id)
    total.value = flows.value.length
    // 如果选中的被移除了，清除选中
    if (selectedId.value && idSet.has(selectedId.value)) {
      selectedId.value = null
    }
    saveToCache()
  }

  /** 手动触发 flows 引用更新通知（shallowRef 模式下，原地 mutate 对象属性后调用） */
  function touchFlows() {
    triggerRef(flows)
  }

  /** 批量更新指定 id 的 flow 属性（原地 mutate + triggerRef，避免重建大数组） */
  function patchFlows(ids: number[], patch: (f: Flow) => void) {
    if (!ids.length) return
    const idSet = new Set(ids)
    for (const f of flows.value) {
      if (idSet.has(f.id)) patch(f)
    }
    triggerRef(flows)
  }

  // 初始化时恢复缓存（同步更新 maxFlowId，避免首次轮询用 0 拉全量导致重复）
  restoreFromCache()

  // 监听缓存清空事件（后端重启时 capture store 触发），清空内存列表
  function onCacheCleared() {
    flows.value = []
    flowIndex.clear()
    total.value = 0
    maxFlowId.value = 0
    selectedId.value = null
    selectedFlowOverride.value = null
  }
  window.addEventListener('opennet:flows-cache-cleared', onCacheCleared)

  return {
    flows,
    total,
    maxFlowId,
    selectedId,
    selectedFlow,
    filterHost,
    filterProcessPids,
    filterProcessName,
    filterStatusCodes,
    filterMethods,
    filterContentTypes,
    filterProtocols,
    autoScroll,
    autoScrollDelay,
    autoScrollPaused,
    skipNextAutoScroll,
    persistAutoScroll,
    aiFlowIds,
    loadFlows,
    loadAllFlows,
    pollNewFlows,
    select,
    selectFlow,
    clear,
    removeFlows,
    saveToCache,
    saveToCacheNow,
    restoreFromCache,
    touchFlows,
    patchFlows,
  }
})
