<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount, shallowRef } from 'vue'
import { loader } from '@guolao/vue-monaco-editor'
import * as monaco from 'monaco-editor'
import BigMonacoEditor from './BigMonacoEditor.vue'
import { registerPythonIntelliSense } from './pythonIntelliSense'

loader.config({ monaco })

/**
 * MonacoEditor：基于 Monaco 的 Python 脚本编辑器（紧凑版）
 *
 * 特性：
 * - Python 语法高亮 + IntelliSense（ctx API 补全、关键字、内置函数）
 * - 亮/暗主题自动切换（监听 <html>.dark）
 * - 行号、折叠、括号配对、自动缩进
 * - Ctrl+S 触发 save 事件
 * - 右上角放大按钮 → 打开 BigMonacoEditor 全屏编辑
 */
const props = defineProps<{
  modelValue: string
  language?: string // 默认 'python'
  theme?: string // 'vs' | 'vs-dark' | 'hc-black'，默认跟随系统
  height?: string // 默认 '400px'
  readOnly?: boolean
  /** 放大编辑器标题 */
  expandTitle?: string
}>()
const emit = defineEmits<{
  'update:modelValue': [string]
  'save': []
}>()

const containerRef = ref<HTMLElement | null>(null)
const editorRef = shallowRef<monaco.editor.IStandaloneCodeEditor | null>(null)

function defineCustomTheme() {
  monaco.editor.defineTheme('telnix-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': '#00000000',
      'editorGutter.background': '#00000000',
      'editorLineNumber.foreground': '#6e7681',
      'editorLineNumber.activeForeground': '#0d9488',
      'editorCursor.foreground': '#0d9488',
      'editor.selectionBackground': '#0d948833',
    },
  })
  monaco.editor.defineTheme('telnix-light', {
    base: 'vs',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': '#00000000',
      'editorGutter.background': '#00000000',
      'editorLineNumber.foreground': '#6e7681',
      'editorLineNumber.activeForeground': '#0d9488',
      'editorCursor.foreground': '#0d9488',
      'editor.selectionBackground': '#0d948833',
    },
  })
}

function currentTheme(): string {
  return document.documentElement.classList.contains('dark') ? 'telnix-dark' : 'telnix-light'
}

let themeObserver: MutationObserver | null = null

onMounted(() => {
  defineCustomTheme()
  registerPythonIntelliSense()
  if (!containerRef.value) return
  const lang = props.language || 'python'
  const editor = monaco.editor.create(containerRef.value, {
    value: props.modelValue || '',
    language: lang,
    theme: currentTheme(),
    automaticLayout: true,
    minimap: { enabled: false },
    fontSize: 13,
    fontFamily: 'Consolas, Monaco, "Courier New", monospace',
    lineHeight: 20,
    padding: { top: 8, bottom: 8 },
    scrollBeyondLastLine: false,
    smoothScrolling: true,
    cursorBlinking: 'smooth',
    renderWhitespace: 'selection',
    bracketPairColorization: { enabled: true },
    tabSize: 4,
    insertSpaces: true,
    readOnly: props.readOnly || false,
    scrollbar: {
      verticalScrollbarSize: 8,
      horizontalScrollbarSize: 8,
    },
    quickSuggestions: { other: true, comments: false, strings: true },
    suggestOnTriggerCharacters: true,
    parameterHints: { enabled: true },
    hover: { enabled: true } as any,
  })
  editorRef.value = editor

  editor.onDidChangeModelContent(() => {
    const v = editor.getValue()
    if (v !== props.modelValue) {
      emit('update:modelValue', v)
    }
  })

  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
    emit('save')
  })

  themeObserver = new MutationObserver(() => {
    monaco.editor.setTheme(currentTheme())
  })
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
})

onBeforeUnmount(() => {
  themeObserver?.disconnect()
  editorRef.value?.dispose()
  editorRef.value = null
})

watch(() => props.modelValue, (newVal) => {
  if (editorRef.value && editorRef.value.getValue() !== newVal) {
    editorRef.value.setValue(newVal || '')
  }
})

watch(() => props.readOnly, (newVal) => {
  editorRef.value?.updateOptions({ readOnly: newVal || false })
})

// ---------- 放大编辑器 ----------
const bigVisible = ref(false)
function openBigEditor() {
  bigVisible.value = true
}
</script>

<template>
  <div class="monaco-wrap" :style="{ height: height || '400px', width: '100%' }">
    <button
      type="button"
      class="me-expand-btn"
      title="放大编辑（全屏 Monaco + IntelliSense）"
      @click="openBigEditor"
    >
      <svg t="1784560989469" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="2601" width="14" height="14" aria-hidden="true">
        <path d="M781.312 243.6096l0.512 220.9792 0.3072 210.7392-158.5152-173.056-12.288-13.312s-20.6848 2.56-48.7424 13.312c-43.8272 16.9984-105.472 54.0672-134.9632 133.0176-42.496 135.7824 73.4208 219.136 115.8144 237.2608 0 0-234.1888 10.5472-293.9904-205.824-48.3328-236.544 147.6608-345.8048 195.2768-358.4L300.2368 151.3472h480.9728l0.2048 92.16z" fill="currentColor"></path>
      </svg>
    </button>
    <div
      ref="containerRef"
      class="monaco-editor-container"
      :style="{ height: '100%', width: '100%' }"
    ></div>
    <BigMonacoEditor
      v-model="bigVisible"
      :title="expandTitle || 'Python 脚本编辑'"
      :text="modelValue || ''"
      :language="language || 'python'"
      @update:text="(v: string) => emit('update:modelValue', v)"
    />
  </div>
</template>

<style scoped>
.monaco-wrap {
  position: relative;
  border: 1px solid var(--on-border, #333);
  border-radius: 4px;
  background: var(--on-bg, #1e1e2e);
  overflow: hidden;
}
.monaco-editor-container {
  background: transparent;
}
.me-expand-btn {
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
.me-expand-btn:hover {
  background: rgba(45, 212, 191, 0.12);
  color: var(--on-accent, #2dd4bf);
  border-color: var(--on-accent, #2dd4bf);
}
.me-expand-btn:focus-visible {
  outline: 2px solid var(--on-accent, #2dd4bf);
  outline-offset: 1px;
}
</style>
