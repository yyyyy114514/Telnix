<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount, shallowRef, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { EditorState, Compartment } from '@codemirror/state'
import { EditorView, highlightActiveLine, keymap, drawSelection, placeholder as cmPlaceholder } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import { bracketMatching, indentOnInput, indentUnit, syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language'
import { autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete'
import { linter } from '@codemirror/lint'
import { json, jsonParseLinter } from '@codemirror/lang-json'
import { xml } from '@codemirror/lang-xml'
import { oneDark } from '@codemirror/theme-one-dark'
import BigCodeEditor from './BigCodeEditor.vue'

const { t } = useI18n()

// 亮色主题（自定义，匹配项目亮色主题）
const lightTheme = EditorView.theme({
  '&': { color: '#1f2328', backgroundColor: 'transparent' },
  '.cm-content': { caretColor: '#0d9488' },
  '.cm-activeLine': { backgroundColor: 'rgba(0,0,0,0.03)' },
  '.cm-selectionBackground, ::selection': { backgroundColor: 'rgba(13,148,136,0.15)' },
  '.cm-cursor': { borderLeftColor: '#0d9488' },
  '.cm-gutters': { backgroundColor: 'transparent', color: '#6e7681', border: 'none' },
}, { dark: false })

// 主题切换辅助
function themeExt(): any {
  return document.documentElement.classList.contains('dark') ? oneDark : lightTheme
}

/**
 * CodeEditor：基于 CodeMirror 6 的紧凑代码编辑器（无行号）
 *
 * 内置功能：
 * - 实时语法高亮（JSON / XML / plaintext）
 * - 括号配对、引号配对、Tab/Shift+Tab 缩进、Enter 自动缩进
 * - 错误标注（JSON 语法错误波浪线，750ms debounce）
 * - 代码折叠、历史记录（Ctrl+Z/Y）
 * - 暗色主题（oneDark）
 *
 * 设计说明：小编辑器不显示行号（节省宽度、更美观），
 * 需要行号请点击放大按钮打开 BigCodeEditor 全屏编辑。
 */
const props = defineProps<{
  modelValue: string | null | undefined
  language?: string  // 'json' | 'xml' | 'plaintext' | 'http'
  placeholder?: string
  minHeight?: string  // 如 '200px'
  /** 放大编辑器标题（可选，默认 "代码编辑"） */
  expandTitle?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [string] }>()

const editorHost = ref<HTMLDivElement | null>(null)
const view = shallowRef<EditorView | null>(null)
const langCompartment = new Compartment()
const lintCompartment = new Compartment()
const themeCompartment = new Compartment()
let internalUpdate = false

// 大文本跳过 lint 阈值（避免 JSON.parse 万行大文本卡顿）
const LINT_LIMIT = 500000

// 根据语言构造 extension
function langExt(): any[] {
  const lang = props.language || 'plaintext'
  if (lang === 'json') return [json()]
  if (lang === 'xml') return [xml()]
  return []
}

// lint extension：仅 json 且文本不太大时启用
function lintExt(): any[] {
  const lang = props.language || 'plaintext'
  if (lang !== 'json') return []
  const doc = props.modelValue || ''
  if (doc.length > LINT_LIMIT) return []
  return [linter(jsonParseLinter())]
}

// 创建 EditorState
function createState(doc: string): EditorState {
  const extensions: any[] = [
    history(),
    drawSelection(),
    indentOnInput(),
    indentUnit.of('  '),
    bracketMatching(),
    closeBrackets(),
    autocompletion(),
    highlightActiveLine(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    keymap.of([
      ...closeBracketsKeymap,
      ...defaultKeymap,
      ...historyKeymap,
      ...completionKeymap,
      indentWithTab,
    ]),
    langCompartment.of(langExt()),
    lintCompartment.of(lintExt()),
    themeCompartment.of(themeExt()),
    EditorView.lineWrapping,
    EditorView.theme({
      '&': { fontSize: '12.5px', height: '100%', backgroundColor: 'transparent' },
      '.cm-content': {
        fontFamily: 'var(--on-font-mono, Menlo, Consolas, monospace)',
        lineHeight: '1.6',
        padding: '4px 8px',
      },
      // 无行号无 gutter：完全隐藏 gutter 区域，避免灰色块
      '.cm-gutters': {
        display: 'none',
      },
      '.cm-activeLine': { backgroundColor: 'rgba(255,255,255,0.03)' },
      '&.cm-focused': { outline: 'none' },
      '.cm-cursor': { borderLeftColor: 'var(--on-accent, #2dd4bf)' },
    }),
    EditorView.updateListener.of((u) => {
      if (u.docChanged && !internalUpdate) {
        emit('update:modelValue', u.state.doc.toString())
      }
    }),
  ]
  if (props.placeholder) {
    extensions.push(cmPlaceholder(props.placeholder))
  }
  return EditorState.create({ doc, extensions })
}

onMounted(() => {
  if (!editorHost.value) return
  view.value = new EditorView({
    state: createState(props.modelValue || ''),
    parent: editorHost.value,
  })
  // 监听主题切换（<html> class 变化）并重新应用主题
  themeObserver = new MutationObserver(() => {
    const v = view.value
    if (!v) return
    v.dispatch({ effects: themeCompartment.reconfigure(themeExt()) })
  })
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
})

// 外部 modelValue 变化时同步到编辑器
watch(() => props.modelValue, (newText) => {
  const v = view.value
  if (!v) return
  const cur = v.state.doc.toString()
  const next = newText ?? ''
  if (next !== cur) {
    internalUpdate = true
    v.dispatch({ changes: { from: 0, to: cur.length, insert: next } })
    internalUpdate = false
  }
})

// 语言变化时重新加载语言 + lint extension
watch(() => props.language, () => {
  const v = view.value
  if (!v) return
  v.dispatch({
    effects: [
      langCompartment.reconfigure(langExt()),
      lintCompartment.reconfigure(lintExt()),
    ],
  })
})

// 监听主题切换（<html> class 变化）并重新应用主题
let themeObserver: MutationObserver | null = null

onBeforeUnmount(() => {
  themeObserver?.disconnect()
  themeObserver = null
  if (view.value) {
    view.value.destroy()
    view.value = null
  }
})

// ---------- 放大编辑器 ----------
const bigVisible = ref(false)
function openBigEditor() {
  bigVisible.value = true
}
</script>

<template>
  <div class="code-editor mono" :style="{ minHeight: minHeight || '120px' }">
    <button
      type="button"
      class="ce-expand-btn"
      :title="t('codeEditor.expandTitle')"
      @click="openBigEditor"
    >
      <svg t="1784560989469" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="2601" width="14" height="14" aria-hidden="true">
        <path d="M781.312 243.6096l0.512 220.9792 0.3072 210.7392-158.5152-173.056-12.288-13.312s-20.6848 2.56-48.7424 13.312c-43.8272 16.9984-105.472 54.0672-134.9632 133.0176-42.496 135.7824 73.4208 219.136 115.8144 237.2608 0 0-234.1888 10.5472-293.9904-205.824-48.3328-236.544 147.6608-345.8048 195.2768-358.4L300.2368 151.3472h480.9728l0.2048 92.16z" fill="currentColor"></path>
      </svg>
    </button>
    <div ref="editorHost" class="ce-host"></div>
    <BigCodeEditor
      v-model="bigVisible"
      :title="expandTitle || t('codeEditor.defaultTitle')"
      :text="modelValue || ''"
      :language="language || 'plaintext'"
      @update:text="(v: string) => emit('update:modelValue', v)"
    />
  </div>
</template>

<style scoped>
.code-editor {
  position: relative;
  width: 100%;
  background: transparent;
  border: 1px solid var(--on-border-light, #333);
  border-radius: 4px;
  overflow: hidden;
}
.ce-host {
  width: 100%;
  height: 100%;
  min-height: v-bind(minHeight || '120px');
}
.ce-host :deep(.cm-editor) {
  height: 100%;
  background: transparent;
}
.ce-host :deep(.cm-scroller) {
  overflow: auto;
  max-height: 600px;
}
.ce-expand-btn {
  position: absolute;
  top: 4px;
  right: 4px;
  width: 22px;
  height: 22px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--on-border-light, #444);
  border-radius: 3px;
  color: var(--on-text-dim, #888);
  cursor: pointer;
  z-index: 2;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.ce-expand-btn:hover {
  background: rgba(45, 212, 191, 0.12);
  color: var(--on-accent, #2dd4bf);
  border-color: var(--on-accent, #2dd4bf);
}
.ce-expand-btn:focus-visible {
  outline: 2px solid var(--on-accent, #2dd4bf);
  outline-offset: 1px;
}
</style>
