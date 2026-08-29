<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted, inject, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
// 可选注入 hljs 实例（由 main.ts provide），用于代码语法高亮
const hljs: any = inject('hljs', null)
const { t } = useI18n()

// 通用文本搜索组件：右上角放大镜图标 + 悬浮搜索窗 + 高亮 + 上一个/下一个 + Ctrl+F
// 右键选中文本可解码（Base64/URL/Hex 等），结果在独立悬浮窗显示
const props = defineProps<{
  text: string          // 原始文本
  searchable?: boolean  // 是否启用搜索（默认 true）
  language?: string     // 代码语言（如 'json'/'xml'/'http'），传入则启用语法高亮
}>()

const searchQuery = ref('')
const currentMatch = ref(0)  // 当前匹配索引（1-based）
const matchCount = ref(0)
const searchInputRef = ref<any>(null)
const containerRef = ref<HTMLElement | null>(null)
const contentRef = ref<HTMLElement | null>(null)
// 搜索悬浮窗默认隐藏，点放大镜或 Ctrl+F 打开
const showSearchBar = ref(false)

// 性能优化（异步高亮，避免大文本 hljs.highlight 阻塞主线程）：
// - PLAIN_LIMIT：立即显示的字符数（纯转义 HTML，主线程同步执行 < 5ms）
// - HIGHLIGHT_LIMIT：异步 hljs 高亮最大字符数（超过则只显示纯转义）
// 阶段1（同步）：纯转义 HTML（无搜索高亮），立即可见
// 阶段2（异步）：requestIdleCallback 中做 hljs.highlight()，得到语法高亮 HTML（不依赖搜索词）
// 阶段3（同步 computed）：基于 hljs HTML（或纯转义）做搜索高亮 —— 搜索时仍保留语法颜色
const PLAIN_LIMIT = 50000
const HIGHLIGHT_LIMIT = 100000

// 转义正则特殊字符
function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// 截断提示
function truncNoticeHtml(rawLen: number): string {
  if (rawLen <= PLAIN_LIMIT) return ''
  return `<div class="ts-truncated">${t('textSearch.truncated', { shown: PLAIN_LIMIT, total: rawLen })}</div>`
}

// 阶段1：纯转义 HTML（无搜索高亮）—— 立即同步计算
const escapedHtml = computed(() => {
  const raw = props.text || ''
  if (!raw) return `<span class="ts-empty">${t('textSearch.empty')}</span>`
  const truncated = raw.length > PLAIN_LIMIT
  const text = truncated ? raw.slice(0, PLAIN_LIMIT) : raw
  return escapeHtml(text) + truncNoticeHtml(raw.length)
})

// 阶段2：异步 hljs 高亮（不依赖 searchQuery，text/language 变化时重算）
const hljsHtml = shallowRef<string>('')
let hljsTimer: number | null = null
let hljsToken = 0  // 并发令牌：新请求使旧请求结果失效

function scheduleHljsHighlight() {
  // 清除上一次的异步任务
  if (hljsTimer !== null) {
    ;(cancelIdleCallback as any)(hljsTimer as number)
    hljsTimer = null
  }
  const raw = props.text || ''
  // 无 language / hljs 未注入 / 文本过大 → 不做语法高亮（用纯转义）
  if (!raw || !props.language || !hljs || raw.length > HIGHLIGHT_LIMIT) {
    hljsHtml.value = ''
    return
  }
  const myToken = ++hljsToken
  const truncated = raw.length > PLAIN_LIMIT
  const text = truncated ? raw.slice(0, PLAIN_LIMIT) : raw
  const notice = truncNoticeHtml(raw.length)
  // 用 requestIdleCallback 在浏览器空闲时做高亮（避免阻塞输入和滚动）
  const ric = (window as any).requestIdleCallback || ((cb: any) => setTimeout(cb, 50))
  hljsTimer = ric((deadline: any) => {
    hljsTimer = null
    if (myToken !== hljsToken) return  // 已被新请求取代
    try {
      const result = hljs.highlight(text, { language: props.language, ignoreIllegals: true })
      if (myToken !== hljsToken) return
      hljsHtml.value = result.value + notice
    } catch {
      // 语言不支持时回退到纯转义（escapedHtml 已显示）
    }
  }, { timeout: 200 } as any) as number
}

// 监听 text/language 变化重新调度异步高亮（注意：searchQuery 变化不触发 hljs 重算）
watch(
  () => [props.text, props.language],
  () => {
    hljsHtml.value = ''  // 先清空，回到 escaped 阶段
    scheduleHljsHighlight()
  },
  { immediate: true }
)

// 在 HTML 字符串中做搜索高亮：只在文本节点中替换，跳过 <...> 标签部分
// 这样可以在 hljs 高亮 HTML 上叠加搜索高亮，保留语法颜色
function highlightSearchInHtml(html: string, query: string, currentIdx: number): string {
  if (!query) return html
  const rx = new RegExp(escapeRegex(query), 'gi')
  let result = ''
  let count = 0
  let i = 0
  const len = html.length
  while (i < len) {
    if (html[i] === '<') {
      // 标签：原样复制到 '>'
      const end = html.indexOf('>', i)
      if (end === -1) {
        result += html.slice(i)
        break
      }
      result += html.slice(i, end + 1)
      i = end + 1
    } else {
      // 文本节点：找到下一个 '<' 或字符串末尾
      const nextTag = html.indexOf('<', i)
      const textEnd = nextTag === -1 ? len : nextTag
      const text = html.slice(i, textEnd)
      // 在文本中查找搜索词
      let lastIdx = 0
      let m: RegExpExecArray | null
      rx.lastIndex = 0
      while ((m = rx.exec(text)) !== null) {
        count++
        result += text.slice(lastIdx, m.index)
        const isCurrent = count === currentIdx
        result += `<mark class="ts-mark${isCurrent ? ' ts-mark-current' : ''}">${m[0]}</mark>`
        lastIdx = m.index + m[0].length
        if (m[0].length === 0) rx.lastIndex++
      }
      result += text.slice(lastIdx)
      i = textEnd
    }
  }
  return result
}

// 阶段3：最终显示 = base(hljs 或 escaped) + 搜索高亮
const highlightedHtml = computed(() => {
  const base = hljsHtml.value || escapedHtml.value
  if (searchQuery.value && props.searchable) {
    return highlightSearchInHtml(base, searchQuery.value, currentMatch.value)
  }
  return base
})

onUnmounted(() => {
  if (hljsTimer !== null) {
    ;(cancelIdleCallback as any)(hljsTimer as number)
    hljsTimer = null
  }
})

// 搜索计数 debounce：避免每次输入字符都全文本扫描
// 性能优化：限制扫描范围在 SEARCH_LIMIT 内，避免对几 MB 文本扫描导致卡顿
const SEARCH_LIMIT = 500000  // 50 万字符内扫描计数，超出标记为 ">"
const matchCountTruncated = ref(false)  // 计数是否被截断（true 时 UI 显示 ">N"）
let searchCountTimer: number | null = null
watch(searchQuery, () => {
  currentMatch.value = 1
  if (searchCountTimer !== null) clearTimeout(searchCountTimer)
  searchCountTimer = window.setTimeout(() => {
    const raw = props.text || ''
    if (!searchQuery.value || !raw) {
      matchCount.value = 0
      matchCountTruncated.value = false
      return
    }
    const isTruncated = raw.length > SEARCH_LIMIT
    const scanText = isTruncated ? raw.slice(0, SEARCH_LIMIT) : raw
    const rx = new RegExp(escapeRegex(searchQuery.value), 'gi')
    let count = 0
    let m: RegExpExecArray | null
    while ((m = rx.exec(scanText)) !== null) {
      count++
      if (m[0].length === 0) rx.lastIndex++
    }
    matchCount.value = count
    matchCountTruncated.value = isTruncated  // 截断时 UI 显示 ">N"
  }, 300)
})

// 搜索计数显示：截断时显示 ">N"，否则显示 "N"
const matchCountDisplay = computed(() => {
  if (!matchCount.value) return '0'
  return matchCountTruncated.value ? `>${matchCount.value}` : String(matchCount.value)
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
    const current = el.querySelector('.ts-mark-current') as HTMLElement | null
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

// ---------- 右键解密菜单 ----------
const menuVisible = ref(false)
const menuX = ref(0)
const menuY = ref(0)
const selectedText = ref('')
const menuRef = ref<HTMLElement | null>(null)

// 解密悬浮窗
const decodeVisible = ref(false)
const decodeResult = ref('')
const decodeTitle = ref('')
const decodeLang = ref('')

interface DecodeOption {
  label: string
  action: () => void
}

// 获取当前选中文本
function getSelectionText(): string {
  const sel = window.getSelection()
  return sel ? sel.toString().trim() : ''
}

function onContextMenu(e: MouseEvent) {
  const txt = getSelectionText()
  // 不可搜索且无选中文本：不弹菜单（让浏览器默认右键）
  if (props.searchable === false && !txt) return
  e.preventDefault()
  selectedText.value = txt
  // 先用点击位置定位，显示后 nextTick 测量菜单尺寸做边界回退
  menuX.value = e.clientX
  menuY.value = e.clientY
  menuVisible.value = true
  nextTick(() => {
    const el = menuRef.value
    if (!el) return
    const rect = el.getBoundingClientRect()
    const vw = window.innerWidth
    const vh = window.innerHeight
    let x = e.clientX
    let y = e.clientY
    if (x + rect.width > vw - 4) x = Math.max(4, vw - rect.width - 4)
    if (y + rect.height > vh - 4) y = Math.max(4, vh - rect.height - 4)
    menuX.value = x
    menuY.value = y
  })
}

// 右键菜单"搜索选项"：填入选中文字（若有）并打开搜索栏
function searchFromMenu() {
  closeMenu()
  if (selectedText.value) {
    searchQuery.value = selectedText.value
  }
  showSearchBar.value = true
  nextTick(() => {
    searchInputRef.value?.focus?.()
    if (!selectedText.value) searchInputRef.value?.select?.()
  })
}

// 截断长文本用于菜单展示
function truncateText(s: string, max = 30): string {
  if (!s) return ''
  const t = s.length > max ? s.slice(0, max) + '…' : s
  // 替换换行符避免菜单变形
  return t.replace(/\s+/g, ' ')
}

function closeMenu() {
  menuVisible.value = false
}

const decodeOptions = computed<DecodeOption[]>(() => [
  { label: t('textSearch.decodeBase64'), action: decodeBase64 },
  { label: t('textSearch.decodeUrl'), action: decodeUrl },
  { label: t('textSearch.decodeHex'), action: decodeHex },
  { label: t('textSearch.decodeHtmlEntity'), action: decodeHtmlEntity },
  { label: t('textSearch.decodeUnicode'), action: decodeUnicode },
  { label: t('textSearch.formatJson'), action: formatJson },
])

function showResult(title: string, result: string, lang = '') {
  decodeTitle.value = title
  decodeResult.value = result
  decodeLang.value = lang
  decodeVisible.value = true
  closeMenu()
}

function decodeBase64() {
  try {
    const txt = selectedText.value
    // 尝试 UTF-8 安全解码
    const bin = atob(txt)
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    const result = new TextDecoder('utf-8').decode(bytes)
    showResult(t('textSearch.decodeBase64'), result)
  } catch {
    showResult(t('textSearch.decodeBase64'), t('textSearch.decodeFailedBase64'))
  }
}

function decodeUrl() {
  try {
    showResult(t('textSearch.decodeUrl'), decodeURIComponent(selectedText.value))
  } catch {
    showResult(t('textSearch.decodeUrl'), t('textSearch.decodeFailedUrl'))
  }
}

function decodeHex() {
  try {
    const hex = selectedText.value.replace(/0x/gi, '').replace(/[\s,]/g, '')
    if (!/^[0-9a-fA-F]+$/.test(hex) || hex.length % 2 !== 0) {
      showResult(t('textSearch.decodeHex'), t('textSearch.decodeFailedHex'))
      return
    }
    let result = ''
    for (let i = 0; i < hex.length; i += 2) {
      const code = parseInt(hex.slice(i, i + 2), 16)
      result += String.fromCharCode(code)
    }
    showResult(t('textSearch.decodeHex'), result)
  } catch {
    showResult(t('textSearch.decodeHex'), t('textSearch.decodeFailed'))
  }
}

function decodeHtmlEntity() {
  const txt = document.createElement('textarea')
  txt.innerHTML = selectedText.value
  showResult(t('textSearch.decodeHtmlEntity'), txt.value)
}

function decodeUnicode() {
  try {
    // \uXXXX 形式
    const result = selectedText.value.replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
    showResult(t('textSearch.decodeUnicode'), result)
  } catch {
    showResult(t('textSearch.decodeUnicode'), t('textSearch.decodeFailed'))
  }
}

function formatJson() {
  try {
    const obj = JSON.parse(selectedText.value)
    showResult(t('textSearch.formatJson'), JSON.stringify(obj, null, 2), 'json')
  } catch {
    showResult(t('textSearch.formatJson'), t('textSearch.parseFailedJson'))
  }
}

function closeDecode() {
  decodeVisible.value = false
  decodeResult.value = ''
}

// 复制结果
function copyResult() {
  navigator.clipboard.writeText(decodeResult.value).then(() => {
    // 简单提示
  })
}

// 点击外部关闭菜单
function onGlobalClick() {
  if (menuVisible.value) closeMenu()
}

onMounted(() => {
  containerRef.value?.addEventListener('keydown', onContainerKeydown)
  window.addEventListener('click', onGlobalClick)
})
onUnmounted(() => {
  containerRef.value?.removeEventListener('keydown', onContainerKeydown)
  window.removeEventListener('click', onGlobalClick)
})

// 解密结果高亮 HTML
const decodeHighlighted = computed(() => {
  if (!decodeResult.value) return ''
  if (decodeLang.value && hljs) {
    try {
      return hljs.highlight(decodeResult.value, { language: decodeLang.value, ignoreIllegals: true }).value
    } catch {
      /* ignore */
    }
  }
  return decodeResult.value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
})
</script>

<template>
  <div ref="containerRef" class="text-search full" tabindex="0" @contextmenu="onContextMenu">
    <!-- 右上角悬浮放大镜图标 + 搜索窗（absolute 定位，不随内容滚动） -->
    <button
      v-if="searchable !== false && !showSearchBar"
      class="ts-fab"
      :title="t('textSearch.searchTitle')"
      @click="openSearch"
    >
      <el-icon><Search /></el-icon>
    </button>

    <transition name="ts-pop">
      <div v-if="showSearchBar && searchable !== false" class="ts-bar">
        <el-input
          ref="searchInputRef"
          v-model="searchQuery"
          size="small"
          :placeholder="t('textSearch.searchPlaceholder')"
          clearable
          @keydown="onSearchKeydown"
          style="width: 220px"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <span class="ts-count" v-if="searchQuery">
          {{ matchCount ? `${currentMatch}/${matchCountDisplay}` : '0/0' }}
        </span>
        <el-button size="small" circle @click="prevMatch" :disabled="!matchCount" :title="t('textSearch.prevTitle')">
          <el-icon><ArrowUp /></el-icon>
        </el-button>
        <el-button size="small" circle @click="nextMatch" :disabled="!matchCount" :title="t('textSearch.nextTitle')">
          <el-icon><ArrowDown /></el-icon>
        </el-button>
        <el-button size="small" circle @click="closeSearch" :title="t('textSearch.closeTitle')">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>
    </transition>

    <!-- 文本内容（可滚动） -->
    <div ref="contentRef" class="ts-content">
      <pre class="mono ts-pre" v-html="highlightedHtml"></pre>
    </div>

    <!-- 右键菜单：搜索 + 解码 -->
    <teleport to="body">
      <div
        v-if="menuVisible"
        ref="menuRef"
        class="ts-menu"
        :style="{ left: menuX + 'px', top: menuY + 'px' }"
        @click.stop
      >
        <!-- 搜索选项：始终显示（除非 searchable=false） -->
        <template v-if="searchable !== false">
          <div class="ts-menu-header">{{ t('textSearch.searchHeader') }}</div>
          <div class="ts-menu-item" @click.stop="searchFromMenu">
            <el-icon class="ts-menu-icon"><Search /></el-icon>
            <span>{{ selectedText ? t('textSearch.searchSelected', { text: truncateText(selectedText) }) : t('textSearch.searchDots') }}</span>
          </div>
        </template>
        <!-- 解码选项：仅选中文字时显示 -->
        <template v-if="selectedText">
          <div class="ts-menu-header">{{ t('textSearch.decodeHeader') }}</div>
          <div
            v-for="(opt, i) in decodeOptions"
            :key="i"
            class="ts-menu-item"
            @click.stop="opt.action"
          >{{ opt.label }}</div>
        </template>
      </div>
    </teleport>

    <!-- 解密结果悬浮窗（fixed 定位，不跟随滚动，只有点叉号才关闭） -->
    <teleport to="body">
      <div v-if="decodeVisible" class="decode-overlay">
        <div class="decode-window">
          <div class="decode-header">
            <span class="decode-title">{{ decodeTitle }}</span>
            <div class="decode-actions">
              <el-button size="small" @click="copyResult" :title="t('textSearch.copyResultTitle')">
                <el-icon><DocumentCopy /></el-icon>&nbsp;{{ t('common.copy') }}
              </el-button>
              <el-button size="small" circle @click="closeDecode" :title="t('textSearch.closeTitle')">
                <el-icon><Close /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="decode-body">
            <pre class="mono decode-pre" v-html="decodeHighlighted"></pre>
          </div>
        </div>
      </div>
    </teleport>
  </div>
</template>

<style scoped>
/* 容器：relative + overflow:hidden，工具栏用 absolute 固定在右上角不随内容滚动 */
.text-search {
  outline: none;
  position: relative;
  overflow: hidden;
  height: 100%;
}

/* 右上角悬浮放大镜按钮：absolute 相对 .text-search，不随 .ts-content 滚动 */
.ts-fab {
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
.ts-fab:hover {
  opacity: 1;
  background: var(--on-bg-hover, #353548);
}

/* 悬浮搜索窗：absolute 相对 .text-search，固定在右上角不随内容滚动 */
.ts-bar {
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

.ts-count {
  font-size: 11px; color: var(--on-text);
  min-width: 40px; text-align: center;
  font-family: var(--on-font-mono);
}

/* 内容区：自己滚动，工具栏在它之上（absolute）不动 */
.ts-content {
  height: 100%;
  overflow: auto;
  padding: 4px 6px;
}
.ts-pre {
  margin: 0; font-size: 12.5px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all; color: var(--on-text);
}
.ts-empty { color: var(--on-text-dim); }
:deep(.ts-truncated) {
  display: block;
  padding: 8px 12px;
  margin-top: 8px;
  background: var(--on-bg-elevated);
  border: 1px dashed var(--on-border);
  border-radius: 4px;
  font-size: 12px;
  color: var(--on-text-muted);
}

:deep(.ts-mark) {
  background: rgba(255, 235, 59, 0.3);
  color: inherit;
  border-radius: 2px;
  padding: 0 1px;
}
:deep(.ts-mark-current) {
  background: rgba(255, 152, 0, 0.6);
  color: #fff;
}

/* 悬浮窗淡入淡出 */
.ts-pop-enter-active, .ts-pop-leave-active {
  transition: opacity .15s ease, transform .15s ease;
}
.ts-pop-enter-from, .ts-pop-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>

<!-- 全局样式：右键菜单和解密悬浮窗（teleport to body，需非 scoped） -->
<style>
.ts-menu {
  position: fixed;
  z-index: 9999;
  min-width: 180px;
  background: var(--on-bg-elevated, #2a2a3a);
  border: 1px solid var(--on-border, #444);
  border-radius: 6px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.5);
  padding: 4px 0;
  font-size: 13px;
}
.ts-menu-header {
  padding: 6px 12px;
  color: var(--on-text-muted, #888);
  font-size: 11px;
  border-bottom: 1px solid var(--on-border-light, #333);
  margin-bottom: 2px;
}
.ts-menu-item {
  padding: 7px 12px;
  color: var(--on-text, #e0e0e0);
  cursor: pointer;
  transition: background .12s ease;
  display: flex;
  align-items: center;
  gap: 6px;
}
.ts-menu-item:hover {
  background: var(--on-bg-hover, #353548);
}
.ts-menu-icon {
  font-size: 13px;
  color: var(--on-text-muted, #888);
  flex-shrink: 0;
}

/* 解密悬浮窗：全屏遮罩 + 居中窗口，不跟随滚动 */
.decode-overlay {
  position: fixed;
  inset: 0;
  z-index: 10000;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
}
.decode-window {
  width: 70vw;
  max-width: 900px;
  height: 70vh;
  max-height: 700px;
  background: var(--on-bg-elevated, #2a2a3a);
  border: 1px solid var(--on-border, #444);
  border-radius: 8px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.6);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.decode-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--on-border-light, #333);
}
.decode-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--on-text, #e0e0e0);
}
.decode-actions {
  display: flex;
  gap: 6px;
}
.decode-body {
  flex: 1;
  overflow: auto;
  padding: 12px;
}
.decode-pre {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--on-text, #e0e0e0);
}
</style>
