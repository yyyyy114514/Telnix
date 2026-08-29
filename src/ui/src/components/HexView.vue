<script setup lang="ts">
import { computed, shallowRef, watch, triggerRef } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

// 十六进制 + ASCII 对照视图（三色高亮，无搜索）
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

// 计算高亮后的 HTML（三色，无搜索高亮）
const highlightedHtml = computed(() => {
  const lines = hexLines.value
  if (!lines.length) return ''
  const parts: string[] = new Array(lines.length)
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]
    // offset：淡蓝色
    let lineHtml = `<span class="hx-off">${l.offset}</span>  `
    lineHtml += `<span class="hx-bytes">${escapeHtml(l.hex)}</span>  `
    lineHtml += `<span class="hx-ascii">${colorizeAscii(l.ascii)}</span>`
    parts[i] = lineHtml
  }
  return parts.join('\n')
})

// 禁用 Ctrl+F 搜索（hex 无搜索功能）
function onKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
    e.preventDefault()
    e.stopPropagation()
  }
}
</script>

<template>
  <div class="hex-view full" tabindex="0" @keydown="onKeydown">
    <!-- Hex 内容 -->
    <div class="hx-content">
      <pre class="mono hx-pre" v-if="hexLines.length" v-html="highlightedHtml"></pre>
      <div v-else class="empty-text text-dim">{{ t('hexView.noData') }}</div>
    </div>
    <div v-if="truncated" class="trunc-notice">
      {{ t('hexView.truncated', { total: (totalBytes / 1024).toFixed(1), max: MAX_BYTES / 1024 }) }}
    </div>
  </div>
</template>

<style scoped>
.hex-view {
  padding: 8px;
  position: relative;
  outline: none;
  /* 确保 focus 时的 outline 不干扰布局 */
  overflow: hidden;
  height: 100%;
  overflow: hidden;
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
</style>
