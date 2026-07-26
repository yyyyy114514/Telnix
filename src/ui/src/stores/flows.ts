import { defineStore } from 'pinia'
import { ref, shallowRef, computed, triggerRef } from 'vue'
import { api, type Flow } from '../api/client'

// SSE 推送：替代 500ms 轮询，新 flow 入库后立即推送，UI 延迟 <50ms
// EventSource 自动重连，无需手动管理。后端 /flows/stream 推送 lite 字段。
let sseSource: EventSource | null = null
// SSE 连接状态：连接正常时前端无需再跑流量兜底轮询（由 SSE 推送即可）
const sseActive = ref(false)

const CACHE_KEY = 'telnix_flows_cache'
const CACHE_THRESHOLD_KEY = 'telnix_cache_threshold'
const CACHE_AUTOCLEAN_KEY = 'telnix_cache_autoclean'

// 性能优化：缓存时只保留 lite 字段，去掉大 body（request_body/response_body/
// request_headers/response_headers/raw_data），减少 localStorage 序列化体积和耗时。
// 选中详情时会单独 GET /flows/{id} 补齐，不影响功能。
const CACHE_LITE_FIELDS: (keyof Flow)[] = [
  'id', 'session_id', 'timestamp', 'pid', 'process_name', 'method', 'url',
  'scheme', 'host', 'path', 'status_code', 'duration_ms', 'size',
  'breakpoint_status', 'protocol', 'src_port', 'dst_port', 'remote_ip',
  'ip_region', 'tags', 'tag_note', 'http_version',
]

function toLiteFlow(f: Flow): Partial<Flow> {
  const lite: any = {}
  for (const k of CACHE_LITE_FIELDS) {
    lite[k] = (f as any)[k]
  }
  return lite
}

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
  window.dispatchEvent(new CustomEvent('telnix:flows-cache-cleared'))
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
  const autoScroll = ref(localStorage.getItem('telnix_auto_scroll') !== 'false')
  const autoScrollDelay = ref(Number(localStorage.getItem('telnix_auto_scroll_delay')) || 10)
  const autoScrollPaused = ref(false) // 用户滚动时暂停

  // 自动滚动设置持久化
  function persistAutoScroll() {
    localStorage.setItem('telnix_auto_scroll', autoScroll.value ? 'true' : 'false')
    localStorage.setItem('telnix_auto_scroll_delay', String(autoScrollDelay.value))
  }

  // 跨页携带：供 AI 分析使用的选中流量
  const aiFlowIds = ref<number[]>([])

  // 性能优化：debounce saveToCache 到 2 秒，避免每次增量轮询都全量 stringify 阻塞主线程
  // 实际 stringify 操作用 requestIdleCallback 延迟到空闲期执行，避免阻塞 SSE onmessage
  let saveTimer: number | null = null
  function saveToCache() {
    if (saveTimer != null) {
      window.clearTimeout(saveTimer)
    }
    saveTimer = window.setTimeout(() => {
      saveTimer = null
      // 用 requestIdleCallback 把 stringify 移到空闲期，避免阻塞 SSE 消息处理
      const doSave = () => {
        try {
          const liteFlows = flows.value.map(toLiteFlow)
          localStorage.setItem(CACHE_KEY, JSON.stringify(liteFlows))
          const threshold = parseFloat(localStorage.getItem(CACHE_THRESHOLD_KEY) || '10')
          const autoClean = localStorage.getItem(CACHE_AUTOCLEAN_KEY) !== 'false'
          if (autoClean && threshold > 0) {
            autoCleanCache(threshold)
          }
        } catch {
          /* localStorage 满了，忽略 */
        }
      }
      if ('requestIdleCallback' in window) {
        (window as any).requestIdleCallback(doSave, { timeout: 5000 })
      } else {
        doSave()
      }
    }, 2000)
  }

  // 立即保存（用于 clear 等需要立即持久化的场景）
  function saveToCacheNow() {
    if (saveTimer != null) {
      window.clearTimeout(saveTimer)
      saveTimer = null
    }
    try {
      const liteFlows = flows.value.map(toLiteFlow)
      localStorage.setItem(CACHE_KEY, JSON.stringify(liteFlows))
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
      // 性能优化：用 lite=true 只拉轻量字段（不含 body/headers），减少 80%+ 传输量和主线程反序列化开销
      // 选中详情时会单独 GET /flows/{id} 补齐
      const res: any = await api.getAllFlows({ since_id: maxFlowId.value, limit: 200, lite: true })
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
    // SSE 推送的 flow 是 lite 字段（缺少 request_headers/response_headers 等），
    // 选中时自动从后端拉取完整数据，确保 Inspector 能显示标签内容
    if (id != null) {
      const f = flowIndex.get(id)
      if (f && f.request_headers === undefined) {
        // lite flow，异步拉取完整数据并原地更新
        api.getFlow(id).then((full: Flow) => {
          // 原地 mutate flow 对象（shallowRef 模式下需 triggerRef）
          Object.assign(f, full)
          triggerRef(flows)
        }).catch(() => { /* 静默 */ })
      }
    }
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
    // 清空 SSE 待处理批次，丢弃 clear 之前已发出的旧 flow 事件
    sseBatch = []
    sseBatchScheduled = false
    // 重启 SSE 连接：丢弃浏览器缓冲中的旧事件，重新同步基线
    stopSSE()
    startSSE()
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

  // ============ SSE 推送（替代 500ms 轮询）============
  // 后端 /flows/stream 在新 flow 入库后立即推送 lite 字段，
  // 前端收到后直接插入列表顶部，无需轮询。
  // EventSource 自动重连（默认 3 秒）。SSE 连接时后端先推送 init 事件同步基线。
  // 性能优化：批量攒批处理，避免逐条 unshift + triggerRef 导致的高频重渲染
  let sseBatch: Flow[] = []
  let sseBatchScheduled = false
  function flushSSEBatch() {
    sseBatchScheduled = false
    if (!sseBatch.length) return
    const batch = sseBatch
    sseBatch = []
    // 去重 + 按 id DESC 排序（保持列表降序）
    const deduped = batch.filter(f => !flowIndex.has(f.id))
    if (!deduped.length) return
    deduped.sort((a, b) => b.id - a.id)
    flows.value.unshift(...deduped)
    for (const f of deduped) flowIndex.set(f.id, f)
    triggerRef(flows)
    const newMax = deduped[0].id
    if (newMax > maxFlowId.value) maxFlowId.value = newMax
    saveToCache()
  }
  function scheduleSSEBatch() {
    if (sseBatchScheduled) return
    sseBatchScheduled = true
    // 用 microtask 攒批：同一 tick 内的多个 SSE 消息合并为一次渲染
    Promise.resolve().then(flushSSEBatch)
  }
  function startSSE() {
    if (sseSource) return
    try {
      sseSource = new EventSource('/api/flows/stream')
      sseSource.onopen = () => { sseActive.value = true }
      sseSource.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg.type === 'init') {
            // 后端推送当前 max_id，用于检测 SSE 连接期间漏掉的 flow
            const serverMax = msg.max_id || 0
            if (serverMax > maxFlowId.value) {
              // SSE 重连后可能有漏掉的 flow，用增量查询补齐
              pollNewFlows()
            }
            // 只升不降：避免后端重启后 maxFlowId 被清零导致兜底轮询失效
            if (serverMax > maxFlowId.value) maxFlowId.value = serverMax
          } else if (msg.type === 'flow') {
            const flow: Flow = msg.flow
            if (!flow || typeof flow.id !== 'number') return
            // 去重：flow 已在列表中则跳过（防止重复推送）
            if (flowIndex.has(flow.id)) return
            // 攒批处理：加入待处理队列，microtask 中统一 flush
            sseBatch.push(flow)
            scheduleSSEBatch()
          }
        } catch {
          /* ignore parse error */
        }
      }
      sseSource.onerror = () => {
        // 连接异常：标记 SSE 失活，让前端回退到流量兜底轮询
        sseActive.value = false
        // EventSource 会自动重连，无需手动处理
        // 重连后 onmessage 收到 init 事件会自动补齐漏掉的 flow
      }
    } catch {
      sseSource = null
    }
  }

  function stopSSE() {
    if (sseSource) {
      sseSource.close()
      sseSource = null
    }
    sseActive.value = false
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
  window.addEventListener('telnix:flows-cache-cleared', onCacheCleared)

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
    startSSE,
    stopSSE,
    sseActive,
  }
})
