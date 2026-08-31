import { defineStore } from 'pinia'
import { ref, shallowRef, computed, triggerRef } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import i18n from '../i18n'
import { api, type Flow } from '../api/client'

// SSE 推送：替代 500ms 轮询，新 flow 入库后立即推送，UI 延迟 <50ms
// EventSource 自动重连，无需手动管理。后端 /flows/stream 推送 lite 字段。
let sseSource: EventSource | null = null
// SSE 连接状态：连接正常时前端无需再跑流量兜底轮询（由 SSE 推送即可）
const sseActive = ref(false)
// SSE 最近一次「收到消息」的时间戳（init/flow/ping 都会刷新）。
// 用于检测 SSE「连着但不推送」的假死状态（后端生成器异常退出、代理缓冲、
// 连接静默断开但浏览器未及时触发 onerror 等）：一旦超过阈值就回退到增量轮询，
// 避免新流量只有刷新页面才显示（修复「抓包页来新包不实时更新」）。
const lastSseActivityAt = ref(Date.now())
// 假死判定阈值（ms）：心跳 ping 每 15s 一次（data 消息），阈值取 20s
// （略大于心跳间隔，留 5s 容差），避免空闲期误判触发冗余轮询。
// 阈值过小（如 8s）会在心跳间隔内误判为假死，导致 SSE + 轮询同时运行。
const SSE_STALE_MS = 20000
// 判断 SSE 是否「连着但疑似假死」：连接标记为真，但近期无任何消息。
function sseStale(thresholdMs: number = SSE_STALE_MS): boolean {
  return sseActive.value && Date.now() - lastSseActivityAt.value > thresholdMs
}

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
  const lite: any = { _is_lite: true }
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
  // 记录用户上次访问的页面，用于返回按钮（从哪来回哪去）
  const lastPage = ref('/capture')
  // 跳页前调用：记录"从哪来"，返回按钮跳回 lastPage
  function rememberPage(path: string) {
    lastPage.value = path
  }
  // 跳过下一次自动滚动（跨页跳转注入 flow 时设置，避免 length watch 把页面滚回顶部）
  const skipNextAutoScroll = ref(false)
  // 跨页跳转待应用的 DSL 过滤（站点地图页点击叶子节点时设置，FlowList onMounted 时消费）
  const pendingDslFilter = ref('')

  // Map 索引：id → Flow，O(1) 查找。与 flows 同步维护
  const flowIndex = new Map<number, Flow>()

  function rebuildIndex() {
    flowIndex.clear()
    for (const f of flows.value) flowIndex.set(f.id, f)
  }

  // P1-2 修复：前端 flows 数组硬上限，避免长跑会话下无界增长导致
  // displayFlows 计算(O(n))/updateMaxFlowId(O(n))/saveToCache 序列化成本线性上升。
  // 列表按 id 降序，尾部为最旧流量，裁剪尾部不影响 since_id 增量基线（maxFlowId 在顶部）。
  const MAX_FLOWS = 5000
  function enforceMaxFlows() {
    const arr = flows.value
    if (arr.length <= MAX_FLOWS) return
    const removed = arr.slice(MAX_FLOWS)
    flows.value = arr.slice(0, MAX_FLOWS)
    for (const f of removed) flowIndex.delete(f.id)
    // 不更新 total：total 表示数据库中的流量总数，由 loadAllFlows 从后端获取
  }

  // selectedFlowRevision 用于强制 selectedFlow computed 重算：
  // shallowRef + Object.assign 的组合下，flowIndex.get(id) 返回同一引用，
  // computed 用 Object.is 比较认为值未变，下游 Inspector 不会刷新。
  // 引入 revision 作为哨兵，每次原地 mutate 后递增，强制重算。
  const selectedFlowRevision = ref(0)
  const selectedFlow = computed<Flow | null>(() => {
    // 依赖 selectedFlowRevision 以便 mutate 后能重算
    void selectedFlowRevision.value
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

  // 自动滚动（从 localStorage 恢复，默认开启，延迟默认 3 秒）
  const autoScroll = ref(localStorage.getItem('telnix_auto_scroll') !== 'false')
  const autoScrollDelay = ref((() => {
    // 注意：不能用 `Number(...) || 3`，否则用户设置的 0（立即恢复）会退化为 3
    const raw = localStorage.getItem('telnix_auto_scroll_delay')
    if (raw == null || raw === '') return 3
    const n = Number(raw)
    return Number.isFinite(n) && n >= 0 ? n : 3
  })())
  const autoScrollPaused = ref(false) // 用户滚动时暂停

  // UX 复审修复：首屏流量加载状态。列表为空时用于区分"加载中"与"确实无流量"，
  // 配合 FlowList 列表容器的 v-loading 显示加载反馈。
  const initialLoading = ref(false)
  // 清空操作进行中标志：阻止轮询在此期间触发 loadAllFlows 把未删完的旧包拉回
  const clearing = ref(false)

  // 自动滚动设置持久化
  function persistAutoScroll() {
    localStorage.setItem('telnix_auto_scroll', autoScroll.value ? 'true' : 'false')
    localStorage.setItem('telnix_auto_scroll_delay', String(autoScrollDelay.value))
  }

  // 跨页携带：供 AI 分析使用的选中流量
  const aiFlowIds = ref<number[]>([])

  // 临时发包数据：用于从 Cookie 管理器等页面跳转到发包工具时携带数据
  const tempSendData = ref<{
    method: string
    url: string
    headers: Record<string, string>
    body: string
  } | null>(null)

  // 性能优化：debounce saveToCache 到 3 秒（原 2s），降低高频 SSE 推送下的攒批触发频率
  // 实际 stringify 操作用 requestIdleCallback 延迟到空闲期执行，避免阻塞 SSE onmessage
  // requestIdleCallback timeout 从 5000 降至 3000：空闲机会更多，强制执行时也避开 SSE 关键帧
  // dirty 标记：仅当列表实际变更时才序列化，避免空闲 timer 重复 stringify 同一份数据
  let saveTimer: number | null = null
  let saveDirty = false
  function saveToCache() {
    saveDirty = true
    if (saveTimer != null) {
      window.clearTimeout(saveTimer)
    }
    saveTimer = window.setTimeout(() => {
      saveTimer = null
      if (!saveDirty) return
      // 用 requestIdleCallback 把 stringify 移到空闲期，避免阻塞 SSE 消息处理
      let wasTrimmed = false
      const doSave = () => {
        try {
          const threshold = parseFloat(localStorage.getItem(CACHE_THRESHOLD_KEY) || '10')
          const autoClean = localStorage.getItem(CACHE_AUTOCLEAN_KEY) !== 'false'
          let source = flows.value
          // 性能优化：超阈值时先截断源数组，避免对 5000+ flows 全量 toLiteFlow
          if (autoClean && threshold > 0) {
            const thresholdBytes = threshold * 1024 * 1024
            const estimated = source.length * 350
            if (estimated > thresholdBytes || source.length >= 5000) {
              source = source.slice(0, Math.floor(source.length * 0.7))
              wasTrimmed = true
            }
          }
          const liteFlows = source.map(toLiteFlow)
          localStorage.setItem(CACHE_KEY, JSON.stringify(liteFlows))
          saveDirty = false
          // 缓存裁剪后通知用户
          if (wasTrimmed) {
            ElMessage.info(i18n.global.t('capture.cacheTrimmedNote', { pct: '70' }))
          }
        } catch {
          /* localStorage 满了，忽略 */
          saveDirty = false
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
    saveDirty = false
    try {
      let source = flows.value
      // 超阈值时先截断，避免全量 toLiteFlow
      if (source.length >= 5000) {
        source = source.slice(0, Math.floor(source.length * 0.7))
      }
      const liteFlows = source.map(toLiteFlow)
      localStorage.setItem(CACHE_KEY, JSON.stringify(liteFlows))
    } catch {
      /* ignore */
    }
  }

  // 从 localStorage 恢复
  // 性能优化：超大缓存（>2MB）跳过同步恢复，改由 loadAllFlows 从后端拉取，
  // 避免 5000+ flows 的 JSON.parse 阻塞主线程 100-300ms 导致页面加载卡顿
  function restoreFromCache() {
    try {
      const data = localStorage.getItem(CACHE_KEY)
      if (data) {
        // 超大缓存跳过同步恢复（5MB ≈ 1.2万 lite flows，解析约30-50ms 可接受）
        if (data.length > 5 * 1024 * 1024) {
          // 向用户确认是否删除过大缓存
          const sizeMB = (data.length / (1024 * 1024)).toFixed(1)
          const sizeStr = `${sizeMB} MB`
          ElMessageBox.confirm(
            i18n.global.t('capture.cacheLargeConfirm', { size: sizeStr }),
            i18n.global.t('capture.clearConfirmTitle'),
            { confirmButtonText: i18n.global.t('capture.clearButton'), cancelButtonText: i18n.global.t('capture.cancelButton'), type: 'warning' }
          ).then(() => {
            localStorage.removeItem(CACHE_KEY)
          }).catch(() => {
            /* 用户取消，不处理 */
          })
          return
        }
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
    if (polling) {
      initialLoading.value = false
      return
    }
    polling = true
    initialLoading.value = flows.value.length === 0
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
      initialLoading.value = false
    }
  }

  // 跨会话加载最近 N 条流量（用于抓包页面显示历史流量，不依赖活动会话）
  async function loadAllFlows(limit: number = 300) {
    if (clearing.value) return
    if (polling) {
      // polling 锁住时确保 initialLoading 不卡在 true（修复 v-loading 持续显示）
      initialLoading.value = false
      return
    }
    polling = true
    // 不显示 loading 转圈遮罩：用户要求任何场景下都不转圈，列表为空时直接显示空状态
    initialLoading.value = false
    try {
      // 关键修复：在发起请求前，先同步 flush SSE batch，避免竞态条件导致漏包
      // 场景：SSE 推送 #51/#52/#53，但 #52/#53 还在 sseBatch（16ms 延迟）中等待 flush，
      // 而 loadAllFlows 拿到后端 list（最大 id=50），直接用 list 覆盖 flows.value，
      // 导致 #51/#52/#53 丢失。同步 flush 可确保 loadAllFlows 执行时 sseBatch 已合并。
      if (sseBatch.length > 0) {
        flushSSEBatch()
      }
      const res: any = await api.getAllFlows({ limit, offset: 0 })
      const list: Flow[] = res.flows || []
      // P1-PF-005: 后端 get_all_flows 已 ORDER BY id DESC，前端 sort 冗余。
      // 改用 O(n) 一次遍历计算 listMaxId，不依赖返回顺序（防御性，避免 sort O(n log n)）。
      let listMaxId = 0
      for (const f of list) if (f.id > listMaxId) listMaxId = f.id
      // 合并：保留 SSE 在 loadAllFlows 期间推送的流量（id 大于 listMaxId 的）
      const sseExtras = flows.value.filter(f => f.id > listMaxId)
      flows.value = sseExtras.length ? [...sseExtras, ...list] : list
      rebuildIndex()
      total.value = res.total || 0
      // maxFlowId 取合并后列表的最大 id（不依赖顺序，避免 pollNewFlows 基线丢失）
      let curMax = 0
      for (const f of flows.value) if (f.id > curMax) curMax = f.id
      maxFlowId.value = curMax
      saveToCache()
    } catch (e) {
      /* 静默 */
    } finally {
      polling = false
      initialLoading.value = false
    }
  }

  // 增量轮询：用 since_id 拉取 maxFlowId 之后的新流量，插入列表顶部
  // 返回新增条数（用于触发自动滚动）
  // force=true：绕过 polling 锁，用于 SSE init 补齐（必须执行，flowIndex 去重保证不重复）
  async function pollNewFlows(force: boolean = false): Promise<number> {
    if (clearing.value) return 0
    if (!maxFlowId.value) return 0
    if (polling && !force) return 0
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
      // 性能优化：替换 flows.value 引用（而非 unshift 原地修改 + triggerRef）：
      // shallowRef.value 赋值自动触发响应，新引用确保下游 computed 检测到变化。
      // 修复实时出包：原 unshift 修改原数组，displayFlows computed 返回同一引用，
      // Object.is 比较认为值未变，下游 visibleItems 不重新计算，新流量不渲染。
      flows.value = [...deduped, ...flows.value]
      for (const f of deduped) flowIndex.set(f.id, f)
      enforceMaxFlows()
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
      // 检查是否需要补齐完整数据：使用 _is_lite 标记区分 lite flow
      // lite flow 缺少 request_body/response_body/request_headers 等字段
      if (f && (f as any)._is_lite) {
        // lite flow 或预入库 flow，异步拉取完整数据并原地更新
        api.getFlow(id).then((full: Flow) => {
          // 原地 mutate flow 对象（shallowRef 模式下需 triggerRef）
          Object.assign(f, full)
          triggerRef(flows)
        }).catch(() => {
          // UX3 修复：选中详情拉取失败时给出轻提示，避免 Inspector 静默空白
          ElMessage.error(i18n.global.t('flowList.loadDetailFailed'))
          // 拉取失败时清除选中，避免 Inspector 显示不完整数据
          // 仅当该 flow 仍在选中时清除（避免竞态：用户可能已选中其他 flow）
          if (selectedId.value === id) {
            selectedId.value = null
          }
        })
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
      // 注入的 flow id 更大时同步 maxFlowId，避免后续增量轮询漏掉中间的包
      if (flow.id > maxFlowId.value) maxFlowId.value = flow.id
      enforceMaxFlows()
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
    sseBatchIndex = new Map()
    pendingSseUpdates.clear()
    sseBatchScheduled = false
    if (sseBatchTimer) { clearTimeout(sseBatchTimer); sseBatchTimer = null }
    // 重启 SSE 连接：丢弃浏览器缓冲中的旧事件，重新同步基线
    stopSSE()
    startSSE()
    clearFlowCache()
  }

  // 仅清空本地内存 + 断开 SSE（不重连、不删后端）。用于「清空」按钮的乐观即时反馈：
  // 先让用户立刻看到空列表，且「先停 SSE」可避免在后端删除完成前重连 SSE，
  // 否则新 SSE 的 init 会在删除前返回旧 max_id，触发 pollNewFlows 把旧包重新拉回
  //（现象：点清空后过一会旧包又弹出来）。
  function clearLocal() {
    clearing.value = true
    flows.value = []
    flowIndex.clear()
    total.value = 0
    maxFlowId.value = 0
    selectedId.value = null
    selectedFlowOverride.value = null
    sseBatch = []
    sseBatchIndex = new Map()
    pendingSseUpdates.clear()
    sseBatchScheduled = false
    if (sseBatchTimer) { clearTimeout(sseBatchTimer); sseBatchTimer = null }
    initialLoading.value = false
    stopSSE()
    clearFlowCache()
  }

  // 后端清空（或失败恢复）之后才重连 SSE：此时 init 的 max_id 已被后端 reset 为 0，
  // 不会触发 pollNewFlows 把已删除的旧包重新拉回。
  function reconnectSSE() {
    clearing.value = false
    stopSSE()
    startSSE()
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

  /** O(1) 判断指定 id 的 flow 是否在当前列表中（替代 flows.some(x => x.id === id)） */
  function hasFlow(id: number): boolean {
    return flowIndex.has(id)
  }

  /** O(1) 取指定 id 的 flow 对象，不存在返回 undefined（替代 flows.find O(n)） */
  function getFlow(id: number): Flow | undefined {
    return flowIndex.get(id)
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
  // sseBatch 索引：id -> 在 sseBatch 中的下标，避免 O(n²) 线性查找
  // （突发高并发时 batch 可能数百条，原 for 循环累计阻塞主线程）
  let sseBatchIndex: Map<number, number> = new Map()
  // 暂存「update 先到、预入库 flow 还未到」的响应字段更新，key=flow_id
  const pendingSseUpdates = new Map<number, Partial<Flow>>()
  let sseBatchScheduled = false
  let sseBatchTimer: ReturnType<typeof setTimeout> | null = null
  /** 按 id 合并两个 flow 对象：字段更完整的优先（有 method/host 的视作更完整），后到的补充缺失字段。 */
  function mergeFlow(target: any, src: any): Flow {
    if (!target) return { ...src }
    const out: any = { ...target }
    for (const k of Object.keys(src)) {
      const v = src[k]
      // 仅在 out[k] 为空/undefined/null 或 src[k] 是更有信息量的值时覆盖
      if (v == null || v === '') {
        if (out[k] != null && out[k] !== '') continue
      }
      // method/host/path/url 等关键字段：out 中已有有效值就不用 src 的空值覆盖
      if (out[k] != null && out[k] !== '' && (v == null || v === '')) continue
      out[k] = v
    }
    return out
  }
  function flushSSEBatch() {
    sseBatchScheduled = false
    sseBatchTimer = null
    if (!sseBatch.length) return
    const batch = sseBatch
    sseBatch = []
    // 必须同步清空索引：否则残留上一批的下标，下一批消息会按旧下标
    // 覆盖 sseBatch 中不相关的条目（数据错乱）或写入越界位置（稀疏数组）
    sseBatchIndex.clear()
    // ---- 修复：先在 batch 内部按 id 去重合并 ----
    // 同一 id 的多条（完整预入库 + 5字段更新 等）合并成一条，避免空行/重复行
    const byId = new Map<number, Flow>()
    for (const f of batch) {
      if (!f || typeof f.id !== 'number') continue
      const existing = byId.get(f.id)
      byId.set(f.id, existing ? mergeFlow(existing, f) as Flow : { ...f } as Flow)
    }
    // 应用 pending updates（响应更新先到、预入库 flow 也在本 batch 内时的场景）
    for (const [id, patch] of pendingSseUpdates) {
      if (byId.has(id)) {
        byId.set(id, mergeFlow(byId.get(id), patch) as Flow)
        pendingSseUpdates.delete(id)
      }
    }
    // ---- 再按 flowIndex 去重（已经加入列表的不再重复加） ----
    const deduped: Flow[] = []
    for (const f of byId.values()) {
      if (flowIndex.has(f.id)) {
        // 已在列表中：merge 字段并触发响应式（补全可能的响应更新）
        const existing = flowIndex.get(f.id)!
        const merged = mergeFlow(existing, f)
        let changed = false
        for (const k of Object.keys(merged)) {
          if ((existing as any)[k] !== (merged as any)[k]) { changed = true; break }
        }
        if (changed) {
          Object.assign(existing, merged)
          // 如果合并的是当前选中 flow，递增 revision 让 Inspector 立即刷新
          if (selectedId.value === f.id) selectedFlowRevision.value++
          triggerRef(flows)
        }
        continue
      }
      deduped.push(f)
    }
    if (!deduped.length) {
      // 清理 pendingSseUpdates 中已在 flowIndex 中的条目（避免内存泄漏）
      for (const id of pendingSseUpdates.keys()) {
        if (flowIndex.has(id)) {
          pendingSseUpdates.delete(id)
        }
      }
      return
    }
    deduped.sort((a, b) => b.id - a.id)
    // 替换 flows.value 引用（而非 unshift 原地修改）：
    // shallowRef.value 赋值会自动触发响应，且新引用确保下游 computed
    // （displayFlows/useVirtualList）检测到变化并重新计算，修复实时出包。
    flows.value = [...deduped, ...flows.value]
    for (const f of deduped) flowIndex.set(f.id, f)
    enforceMaxFlows()
    const newMax = deduped[0].id
    if (newMax > maxFlowId.value) maxFlowId.value = newMax
    saveToCache()
  }
  function scheduleSSEBatch() {
    if (sseBatchScheduled) return
    sseBatchScheduled = true
    // 帧级攒批：16ms（~1帧）窗口内的 SSE 消息合并为一次渲染，
    // 高并发时减少 Vue 响应式重算次数，低流量时单条也在 16ms 内渲染（体感实时）。
    // 同时用 microtask 作为兜底：如果事件循环空闲，立即 flush 不浪费帧时间。
    Promise.resolve().then(() => {
      if (!sseBatchTimer) {
        sseBatchTimer = setTimeout(flushSSEBatch, 16)
      }
    })
  }
  function startSSE() {
    if (sseSource) return
    try {
      sseSource = new EventSource('/api/flows/stream')
      sseSource.onopen = () => {
        sseActive.value = true
        // 连接成功即视为有活动，避免因「连着但没消息」被误判假死
        lastSseActivityAt.value = Date.now()
      }
      sseSource.onmessage = (ev) => {
        try {
          // 任意消息都刷新活跃时间戳（init/flow/ping 统一定义为「连接健康」）
          lastSseActivityAt.value = Date.now()
          const msg = JSON.parse(ev.data)
          if (msg.type === 'init') {
            // 后端推送当前 max_id，用于检测 SSE 连接期间漏掉的 flow
            const serverMax = msg.max_id || 0
            if (serverMax > maxFlowId.value) {
              if (maxFlowId.value === 0) {
                // 前端无基线：后端已有历史 flow，必须全量加载。
                // 只升级 maxFlowId 会导致 pollFlows 因 SSE 健康而不触发 loadAllFlows，
                // 历史 flow 永远不被加载（列表为空）。
                maxFlowId.value = serverMax
                loadAllFlows()
              } else {
                // 有基线：SSE 重连前漏掉的 flow 用增量查询补齐
                pollNewFlows(true)
              }
            }
          } else if (msg.type === 'flow') {
            const flow: any = msg.flow
            if (!flow || typeof flow.id !== 'number') return
            const isUpdate = !!flow._is_update
            const existing = flowIndex.get(flow.id)
            if (existing) {
              // ---- 修复：已有 flow 时总是 merge 字段，不再局限 status_code 从 null 变非 null ----
              // 支持：流式传输完成后更新 size/duration_ms、后续内容更新 tags 等各种场景
              const prevStatusCodeNull = (existing as any).status_code == null
              const merged = mergeFlow(existing, flow)
              let changed = false
              for (const k of Object.keys(merged)) {
                if ((existing as any)[k] !== (merged as any)[k]) { changed = true; break }
              }
              if (changed) {
                Object.assign(existing, merged)
                // 选中 flow 字段被更新时递增 revision，确保 Inspector 立即刷新
                if (selectedId.value === flow.id) selectedFlowRevision.value++
                triggerRef(flows)
                // 若该 flow 当前被选中：
                // - status_code 从 null 变非 null（响应刚完成）
                // - 或 request_body/response_body 仍为 null（lite SSE 未补全 body）
                // 则重新拉取完整数据以获取 body/headers 等详情
                if (selectedId.value === flow.id &&
                    ((prevStatusCodeNull && flow.status_code != null) ||
                     (existing as any).request_body == null ||
                     (existing as any).response_body == null ||
                     (existing as any).request_headers == null)) {
                  api.getFlow(flow.id).then((full: Flow) => {
                    Object.assign(existing!, full)
                    selectedFlowRevision.value++  // 拉取完整数据后再次递增 revision
                    triggerRef(flows)
                  }).catch(() => { /* ignore */ })
                }
              }
              return
            }
            // ---- 修复：_is_update 标记的响应更新绝不被当成新 flow 插入 ----
            if (isUpdate) {
              // 暂存 pendingUpdates，等预入库 flow 到达时再 merge
              pendingSseUpdates.set(flow.id, mergeFlow(pendingSseUpdates.get(flow.id), flow) as Partial<Flow>)
              return
            }
            // ---- 修复：新 flow 到达时先应用已暂存的 pending updates ----
            const patch = pendingSseUpdates.get(flow.id)
            let toPush: Flow = flow as Flow
            if (patch) {
              toPush = mergeFlow(flow, patch) as Flow
              pendingSseUpdates.delete(flow.id)
            }
            // ---- 修复：若 sseBatch 中已有该 id，先 merge，不重复 push ----
            // O(1) Map 查找替代原来的 for 循环
            const existingIdx = sseBatchIndex.get(toPush.id)
            if (existingIdx !== undefined) {
              sseBatch[existingIdx] = mergeFlow(sseBatch[existingIdx], toPush) as Flow
            } else {
              sseBatchIndex.set(toPush.id, sseBatch.length)
              sseBatch.push(toPush)
            }
            scheduleSSEBatch()
          }
          // msg.type === 'ping'：心跳保活，仅刷新活跃时间戳（上面已处理），不渲染
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

  // 延迟缓存恢复：等 capture store 的 fetchStatus 确认后端未重启后再恢复，
  // 避免后端重启后前端闪现上次的流量（旧 id 与后端重置后的 id 冲突）
  let _cacheRestored = false
  function tryRestoreFromCache() {
    if (_cacheRestored) return
    _cacheRestored = true
    restoreFromCache()
  }

  // 超时保护：如果 5 秒内没收到后端确认事件（API 不可用），
  // 乐观恢复缓存避免页面长时间空白
  setTimeout(tryRestoreFromCache, 5000)

  // 后端确认未重启 → 恢复缓存
  window.addEventListener('telnix:backend-confirmed', tryRestoreFromCache)

  // 监听缓存清空事件（后端重启时 capture store 触发），清空内存列表
  function onCacheCleared() {
    _cacheRestored = true  // 标记已处理，阻止后续 tryRestoreFromCache
    flows.value = []
    flowIndex.clear()
    total.value = 0
    maxFlowId.value = 0
    selectedId.value = null
    selectedFlowOverride.value = null
  }
  window.addEventListener('telnix:flows-cache-cleared', onCacheCleared)
  // 后端重启 → 不恢复缓存（onCacheCleared 会清空）
  window.addEventListener('telnix:backend-restarted', onCacheCleared)

  // 断点放行后立即触发增量轮询，确保高亮/详情立即刷新
  const onBreakpointReleased = () => { pollNewFlows(true) }
  window.addEventListener('telnix:breakpoint-released', onBreakpointReleased)

  // SSE 清理：关闭 EventSource + 移除 window 事件监听器
  // 供 Vue 组件在 onBeforeUnmount 中调用，避免内存泄漏
  function cleanup() {
    stopSSE()
    // 清理 batch 定时器
    if (sseBatchTimer) { clearTimeout(sseBatchTimer); sseBatchTimer = null }
    // 移除 window 事件监听器（防止 store 实例销毁后仍被调用）
    window.removeEventListener('telnix:backend-confirmed', tryRestoreFromCache)
    window.removeEventListener('telnix:flows-cache-cleared', onCacheCleared)
    window.removeEventListener('telnix:backend-restarted', onCacheCleared)
    window.removeEventListener('telnix:breakpoint-released', onBreakpointReleased)
  }

  return {
    flows,
    total,
    maxFlowId,
    selectedId,
    selectedFlow,
    lastPage,
    rememberPage,
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
    initialLoading,
    skipNextAutoScroll,
    pendingDslFilter,
    persistAutoScroll,
    aiFlowIds,
    tempSendData,
    loadFlows,
    loadAllFlows,
    pollNewFlows,
    select,
    selectFlow,
    clear,
    clearLocal,
    reconnectSSE,
    removeFlows,
    saveToCache,
    saveToCacheNow,
    restoreFromCache,
    touchFlows,
    patchFlows,
    startSSE,
    stopSSE,
    cleanup,
    sseActive,
    sseStale,
    hasFlow,
    getFlow,
  }
})
