import { ref, computed, onMounted, onBeforeUnmount, type Ref } from 'vue'

/**
 * 轻量固定行高虚拟滚动 composable（无第三方依赖）。
 *
 * 用于大列表（数千条）窗口化渲染：仅渲染可视区 + overscan 行，
 * 通过顶部/底部占位 div 维持滚动高度，避免一次性创建全部 DOM 节点。
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

  function onScroll(e: Event) {
    scrollTop.value = (e.target as HTMLElement).scrollTop
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
  onBeforeUnmount(() => ro?.disconnect())

  const startIndex = computed(() =>
    Math.max(0, Math.floor(scrollTop.value / opts.itemHeight) - overscan),
  )
  const endIndex = computed(() =>
    Math.min(total.value, Math.ceil((scrollTop.value + viewportHeight.value) / opts.itemHeight) + overscan),
  )
  const visibleItems = computed(() => opts.items().slice(startIndex.value, endIndex.value))
  const topPad = computed(() => startIndex.value * opts.itemHeight)
  const bottomPad = computed(() => Math.max(0, (total.value - endIndex.value) * opts.itemHeight))

  return { onScroll, visibleItems, topPad, bottomPad }
}
