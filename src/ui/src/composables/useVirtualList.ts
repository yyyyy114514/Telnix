import { ref, computed, onMounted, onBeforeUnmount, type Ref } from 'vue'

/**
 * 轻量固定行高虚拟滚动 composable（无第三方依赖）。
 *
 * 用于大列表（数千条）窗口化渲染：仅渲染可视区 + overscan 行，
 * 通过顶部/底部占位 div 维持滚动高度，避免一次性创建全部 DOM 节点。
 *
 * 性能优化：
 * 1. onScroll 用 rAF 节流，避免高频滚动事件触发过多响应式更新
 * 2. visibleItems 缓存：startIndex/endIndex 未变时返回同一引用，避免 v-memo 失效
 * 3. items() 引用缓存：避免每次 computed 调用都执行 items() 函数
 *
 * 性能收益：FlowList/RawCaptureView/WebSocketView 在 MAX_FLOWS=5000 时，
 * DOM 节点从 5000×N 降至约 (可视行数+overscan) 行，滚动/重渲染帧率显著提升。
 */
export function useVirtualList<T>(opts: {
  containerRef: Ref<HTMLElement | null>
  items: () => T[]
  itemHeight: number
  overscan?: number
}) {
  const scrollTop = ref(0)
  const viewportHeight = ref(0)
  const overscan = opts.overscan ?? 8
  const total = computed(() => opts.items().length)

  // rAF 节流：滚动事件高频触发，用 requestAnimationFrame 合并到每帧一次
  let rafPending = false
  let lastScrollTop = 0
  function onScroll(e: Event) {
    lastScrollTop = (e.target as HTMLElement).scrollTop
    if (rafPending) return
    rafPending = true
    requestAnimationFrame(() => {
      rafPending = false
      scrollTop.value = lastScrollTop
    })
  }

  let ro: ResizeObserver | null = null
  onMounted(() => {
    const el = opts.containerRef.value
    if (el) {
      viewportHeight.value = el.clientHeight
      ro = new ResizeObserver(() => {
        const cur = opts.containerRef.value
        if (cur) viewportHeight.value = cur.clientHeight
      })
      ro.observe(el)
    }
  })
  onBeforeUnmount(() => {
    ro?.disconnect()
    if (rafPending) cancelAnimationFrame(rafPending as unknown as number)
  })

  const startIndex = computed(() =>
    Math.max(0, Math.floor(scrollTop.value / opts.itemHeight) - overscan),
  )
  const endIndex = computed(() =>
    Math.min(total.value, Math.ceil((scrollTop.value + viewportHeight.value) / opts.itemHeight) + overscan),
  )

  // visibleItems 缓存：startIndex/endIndex 未变时返回同一数组引用，
  // 避免 v-memo 依赖 visibleItems 时因 slice 产生新引用而失效。
  let _cachedStart = -1
  let _cachedEnd = -1
  let _cachedItems: T[] = []
  let _cachedSource: T[] | null = null
  const visibleItems = computed(() => {
    const s = startIndex.value
    const e = endIndex.value
    const src = opts.items()
    // 引用未变且索引未变：直接返回缓存
    if (src === _cachedSource && s === _cachedStart && e === _cachedEnd) {
      return _cachedItems
    }
    _cachedSource = src
    _cachedStart = s
    _cachedEnd = e
    _cachedItems = src.slice(s, e)
    return _cachedItems
  })

  const topPad = computed(() => startIndex.value * opts.itemHeight)
  const bottomPad = computed(() => Math.max(0, (total.value - endIndex.value) * opts.itemHeight))

  return { onScroll, visibleItems, topPad, bottomPad }
}
