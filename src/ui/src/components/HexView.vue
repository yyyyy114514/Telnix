<script setup lang="ts">
import { computed, ref, shallowRef, watch, triggerRef, nextTick } from 'vue'

// 十六进制 + ASCII 对照视图（三色高亮 + 搜索）
const props = defineProps<{
  data: string | null
}>()

// 性能优化：
// 1. MAX_BYTES 256KB→64KB：4000 行已足够大多数场景，256KB 会生成 16384 行导致卡死
// 2. 用 Array.push + join('') 替代字符串 += 拼接（O(n) vs O(n²)）
// 3. hexLines 用 shallowRef + 手动计算，避免深响应式追踪 4000 个对象

const MAX_BYTES = 64 * 1024  // 64KB = 4000 行

interface HexLine {
  offset: string
  hex: string
  ascii: string
}

const hexLines = shallowRef<HexLine[]>([])
const truncated = shallowRef(false)
const totalBytes = shallowRef(0)

function computeHexLines() {
  const str = props.data || ''
  if (!str) {
    hexLines.value = []
    truncated.value = false
    totalBytes.value = 0
    return
  }
  let bytes: Uint8Array
  try {
    bytes = new TextEncoder().encode(str)
  } catch {
    hexLines.value = []
    return
  }
  totalBytes.value = bytes.length
  const isTruncated = bytes.length > MAX_BYTES
  truncated.value = isTruncated
  if (isTruncated) {
    bytes = bytes.subarray(0, MAX_BYTES)
  }
  const lineCount = Math.ceil(bytes.length / 16)
  const lines: HexLine[] = new Array(lineCount)
  const hexParts: string[] = new Array(16)
  const asciiParts: string[] = new Array(16)
  for (let i = 0, li = 0; i < bytes.length; i += 16, li++) {
    const slice = bytes.subarray(i, i + 16)
    const len = slice.length
    for (let j = 0; j < len; j++) {
      const b = slice[j]
      hexParts[j] = (b < 16 ? '0' : '') + b.toString(16)
      asciiParts[j] = (b >= 32 && b <= 126) ? String.fromCharCode(b) : '·'
    }
    for (let j = len; j < 16; j++) {
      hexParts[j] = '  '
      asciiParts[j] = ' '
    }
    lines[li] = {
      offset: i.toString(16).padStart(8, '0'),
      hex: hexParts.slice(0, len).join(' ').padEnd(47, ' '),
      ascii: asciiParts.slice(0, len).join(''),
    }
  }
  hexLines.value = lines
  triggerRef(hexLines)
}

watch(() => props.data, computeHexLines, { immediate: true })

// ---------- 搜索 ----------
const searchQuery = ref('')
const currentMatch = ref(0)
const matchCount = ref(0)
const showSearchBar = ref(false)
const searchInputRef = ref<any>(null)
const contentRef = ref<HTMLElement | null>(null)
const containerRef = ref<HTMLElement | null>(null)

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// 计算高亮后的 HTML（三色 + 搜索高亮）
const highlightedHtml = computed(() => {
  const lines = hexLines.value
  if (!lines.length) return ''
  const q = searchQuery.value
  const hasSearch = q.length > 0
  let rx: RegExp | null = null
  if (hasSearch) {
    try { rx = new RegExp(escapeRegex(q), 'gi') } catch { rx = null }
  }
  const parts: string[] = new Array(lines.length)
  let matchIdx = 0
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]
    // offset：淡蓝色
    let lineHtml = `<span class="hx-off">${l.offset}</span>  `
    if (hasSearch && rx) {
      // hex 区搜索高亮（在 hex 字符串中查找匹配）
      lineHtml += `<span class="hx-bytes">${highlightMatches(l.hex, rx, matchIdx, currentMatch.value)}</span>  `
      // 计算 hex 区新增的匹配数
      rx.lastIndex = 0
      let m: RegExpExecArray | null
      while ((m = rx.exec(l.hex)) !== null) {
        matchIdx++
        if (m[0].length === 0) rx.lastIndex++
      }
      rx.lastIndex = 0
      // ascii 区搜索高亮
      lineHtml += `<span class="hx-ascii">${highlightMatches(l.ascii, rx, matchIdx, currentMatch.value)}</span>`
      rx.lastIndex = 0
      while ((m = rx.exec(l.ascii)) !== null) {
        matchIdx++
        if (m[0].length === 0) rx.lastIndex++
      }
    } else {
      // 无搜索：hex 默认色，ascii 绿色（可打印）/ 暗色（不可打印 ·）
      lineHtml += `<span class="hx-bytes">${escapeHtml(l.hex)}</span>  `
      lineHtml += `<span class="hx-ascii">${colorizeAscii(l.ascii)}</span>`
    }
    parts[i] = lineHtml
  }
  return parts.join('\n')
})

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// ascii 区着色：可打印字符绿色，不可打印 · 用暗色
function colorizeAscii(ascii: string): string {
  let result = ''
  for (let i = 0; i < ascii.length; i++) {
    const c = ascii[i]
    if (c === '·') {
      result += `<span class="hx-dim">${c}</span>`
    } else if (c === ' ') {
      result += ' '
    } else {
      result += `<span class="hx-print">${c}</span>`
    }
  }
  return result
}

// 在文本中高亮匹配项
function highlightMatches(text: string, rx: RegExp, startIdx: number, currentIdx: number): string {
  const escaped = escapeHtml(text)
  // 在转义后的文本上重新匹配（简化处理：直接在原文匹配再转义）
  rx.lastIndex = 0
  let result = ''
  let lastIdx = 0
  let localIdx = 0
  let m: RegExpExecArray | null
  // 在原文上匹配
  const srcRx = new RegExp(rx.source, 'gi')
  while ((m = srcRx.exec(text)) !== null) {
    // 转义匹配前的部分
    result += escapeHtml(text.slice(lastIdx, m.index))
    const globalIdx = startIdx + localIdx
    const isCurrent = globalIdx + 1 === currentIdx
    result += `<mark class="hx-mark${isCurrent ? ' hx-mark-current' : ''}">${escapeHtml(m[0])}</mark>`
    lastIdx = m.index + m[0].length
    localIdx++
    if (m[0].length === 0) srcRx.lastIndex++
  }
  result += escapeHtml(text.slice(lastIdx))
  return result
}

// 搜索计数 debounce
let searchCountTimer: number | null = null
watch(searchQuery, () => {
  currentMatch.value = 1
  if (searchCountTimer !== null) clearTimeout(searchCountTimer)
  searchCountTimer = window.setTimeout(() => {
    const q = searchQuery.value
    const lines = hexLines.value
    if (!q || !lines.length) {
      matchCount.value = 0
      return
    }
    let count = 0
    const rx = new RegExp(escapeRegex(q), 'gi')
    for (const l of lines) {
      rx.lastIndex = 0
      let m: RegExpExecArray | null
      while ((m = rx.exec(l.hex)) !== null) { count++; if (m[0].length === 0) rx.lastIndex++ }
      rx.lastIndex = 0
      while ((m = rx.exec(l.ascii)) !== null) { count++; if (m[0].length === 0) rx.lastIndex++ }
    }
    matchCount.value = count
  }, 300)
})

function openSearch() {
  showSearchBar.value = true
  nextTick(() => {
    searchInputRef.value?.focus?.()
    searchInputRef.value?.select?.()
  })
}
function closeSearch() {
  searchQuery.value = ''
  showSearchBar.value = false
}
function nextMatch() {
  if (matchCount.value === 0) return
  currentMatch.value = currentMatch.value >= matchCount.value ? 1 : currentMatch.value + 1
  scrollToCurrentMatch()
}
function prevMatch() {
  if (matchCount.value === 0) return
  currentMatch.value = currentMatch.value <= 1 ? matchCount.value : currentMatch.value - 1
  scrollToCurrentMatch()
}
function scrollToCurrentMatch() {
  nextTick(() => {
    const el = contentRef.value
    if (!el) return
    const current = el.querySelector('.hx-mark-current') as HTMLElement | null
    if (!current) return
    // 手动计算 scrollTop，避免 scrollIntoView 冒泡到 body 导致整页上移
    const cRect = el.getBoundingClientRect()
    const mRect = current.getBoundingClientRect()
    const matchTop = mRect.top - cRect.top + el.scrollTop
    const target = matchTop - el.clientHeight / 2 + mRect.height / 2
    el.scrollTo({ top: Math.max(0, target), behavior: 'smooth' })
  })
}
function onSearchKeydown(e: Event) {
  const ke = e as KeyboardEvent
  if (ke.key === 'Enter') {
    ke.preventDefault()
    if (ke.shiftKey) prevMatch()
    else nextMatch()
  } else if (ke.key === 'Escape') {
    closeSearch()
  }
}
function onContainerKeydown(e: Event) {
  const ke = e as KeyboardEvent
  if ((ke.ctrlKey || ke.metaKey) && ke.key === 'f') {
    ke.preventDefault()
    ke.stopPropagation()
    openSearch()
  }
}
</script>

<template>
  <div ref="containerRef" class="hex-view full" tabindex="0" @keydown="onContainerKeydown">
    <!-- 右上角搜索按钮 + 搜索栏 -->
    <button v-if="!showSearchBar" class="hx-fab" title="搜索 (Ctrl+F)" @click="openSearch">
      <el-icon><Search /></el-icon>
    </button>
    <transition name="hx-pop">
      <div v-if="showSearchBar" class="hx-bar">
        <el-input
          ref="searchInputRef"
          v-model="searchQuery"
          size="small"
          placeholder="搜索 Hex/ASCII..."
          clearable
          @keydown="onSearchKeydown"
          style="width: 200px"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <span class="hx-count" v-if="searchQuery">{{ matchCount ? `${currentMatch}/${matchCount}` : '0/0' }}</span>
        <el-button size="small" circle @click="prevMatch" :disabled="!matchCount" title="上一个 (Shift+Enter)">
          <el-icon><ArrowUp /></el-icon>
        </el-button>
        <el-button size="small" circle @click="nextMatch" :disabled="!matchCount" title="下一个 (Enter)">
          <el-icon><ArrowDown /></el-icon>
        </el-button>
        <el-button size="small" circle @click="closeSearch" title="关闭 (Esc)">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>
    </transition>

    <!-- Hex 内容 -->
    <div ref="contentRef" class="hx-content">
      <pre class="mono hx-pre" v-if="hexLines.length" v-html="highlightedHtml"></pre>
      <div v-else class="empty-text text-dim">（无数据）</div>
    </div>
    <div v-if="truncated" class="trunc-notice">
      （内容过大 {{ (totalBytes / 1024).toFixed(1) }}KB，已截断显示前 {{ MAX_BYTES / 1024 }}KB）
    </div>
  </div>
</template>

<style scoped>
.hex-view {
  padding: 8px;
  position: relative;
  outline: none;
  height: 100%;
  overflow: hidden;
}
.hx-fab {
  position: absolute;
  top: 6px;
  right: 8px;
  z-index: 100;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 6px;
  background: var(--on-bg-elevated, #2a2a3a);
  color: var(--on-text);
  cursor: pointer;
  opacity: 0.75;
  transition: opacity .15s ease, background .15s ease;
  font-size: 14px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}
.hx-fab:hover { opacity: 1; background: var(--on-bg-hover, #353548); }
.hx-bar {
  position: absolute;
  top: 6px;
  right: 8px;
  z-index: 100;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  background: var(--on-bg-elevated, #2a2a3a);
  border: 1px solid var(--on-border, #444);
  border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
}
.hx-count {
  font-size: 11px; color: var(--on-text);
  min-width: 40px; text-align: center;
  font-family: var(--on-font-mono);
}
.hx-content {
  height: 100%;
  overflow: auto;
  padding: 4px 6px;
}
.hx-pre {
  margin: 0; font-size: 12.5px; line-height: 1.6;
  white-space: pre; color: var(--on-text);
}
.empty-text { text-align: center; padding: 18px; }
.trunc-notice {
  padding: 6px 10px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--on-text-muted, #888);
  background: var(--on-bg-elevated, #2a2a3a);
  border: 1px dashed var(--on-border, #444);
  border-radius: 4px;
}

/* 三色高亮 */
:deep(.hx-off) { color: var(--on-text-dim, #6a8759); }
:deep(.hx-bytes) { color: var(--on-text, #d4d4d4); }
:deep(.hx-ascii) { color: var(--on-text-muted, #aaa); }
:deep(.hx-print) { color: var(--on-ok, #6abf69); }
:deep(.hx-dim) { color: var(--on-text-dim, #555); }

:deep(.hx-mark) {
  background: rgba(255, 235, 59, 0.3);
  color: inherit;
  border-radius: 2px;
  padding: 0 1px;
}
:deep(.hx-mark-current) {
  background: rgba(255, 152, 0, 0.6);
  color: #fff;
}

.hx-pop-enter-active, .hx-pop-leave-active {
  transition: opacity .15s ease, transform .15s ease;
}
.hx-pop-enter-from, .hx-pop-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
