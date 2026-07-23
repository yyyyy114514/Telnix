<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount, shallowRef, nextTick } from 'vue'
import { loader } from '@guolao/vue-monaco-editor'
import * as monaco from 'monaco-editor'
import { registerPythonIntelliSense } from './pythonIntelliSense'

loader.config({ monaco })

/**
 * BigMonacoEditor：放大版 Monaco 编辑器（全屏对话框）
 *
 * - Python 语法高亮 + IntelliSense（关键字、内置函数、ctx API 补全）
 * - 亮/暗主题自动切换
 * - 行号、minimap、折叠、括号配对、自动缩进
 * - Ctrl+S 触发 save 事件
 * - Esc 关闭
 */
const props = defineProps<{
  modelValue: boolean
  title?: string
  text: string
  language?: string // 默认 'python'
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  'update:text': [string]
  'save': []
}>()

const visible = ref(props.modelValue)
watch(() => props.modelValue, (v) => { visible.value = v })
watch(visible, (v) => { emit('update:modelValue', v) })

const editorHost = ref<HTMLDivElement | null>(null)
const editorRef = shallowRef<monaco.editor.IStandaloneCodeEditor | null>(null)
let themeObserver: MutationObserver | null = null

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

function mountEditor() {
  if (!editorHost.value) return
  if (editorRef.value) return
  defineCustomTheme()
  registerPythonIntelliSense()
  const lang = props.language || 'python'
  const editor = monaco.editor.create(editorHost.value, {
    value: props.text || '',
    language: lang,
    theme: currentTheme(),
    automaticLayout: true,
    minimap: { enabled: true },
    fontSize: 14,
    fontFamily: 'Consolas, Monaco, "Courier New", monospace',
    lineHeight: 22,
    padding: { top: 12, bottom: 12 },
    scrollBeyondLastLine: false,
    smoothScrolling: true,
    cursorBlinking: 'smooth',
    renderWhitespace: 'selection',
    bracketPairColorization: { enabled: true },
    guides: { bracketPairs: true, indentation: true },
    tabSize: 4,
    insertSpaces: true,
    scrollbar: {
      verticalScrollbarSize: 10,
      horizontalScrollbarSize: 10,
    },
    quickSuggestions: { other: true, comments: false, strings: true },
    suggestOnTriggerCharacters: true,
    parameterHints: { enabled: true },
    hover: { enabled: true } as any,
    formatOnPaste: true,
    formatOnType: true,
  })
  editorRef.value = editor

  editor.onDidChangeModelContent(() => {
    emit('update:text', editor.getValue())
  })

  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
    emit('save')
  })

  themeObserver = new MutationObserver(() => {
    monaco.editor.setTheme(currentTheme())
  })
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
}

function destroyEditor() {
  themeObserver?.disconnect()
  themeObserver = null
  if (editorRef.value) {
    editorRef.value.dispose()
    editorRef.value = null
  }
}

onMounted(() => {
  if (visible.value) {
    nextTick(mountEditor)
  }
})

watch(visible, (v) => {
  if (v) {
    nextTick(mountEditor)
  } else {
    destroyEditor()
  }
})

onBeforeUnmount(destroyEditor)

// 外部 text 变化时同步
watch(() => props.text, (newText) => {
  if (editorRef.value && editorRef.value.getValue() !== newText) {
    editorRef.value.setValue(newText || '')
  }
})

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    visible.value = false
  }
}

function formatDoc() {
  editorRef.value?.getAction('editor.action.formatDocument')?.run()
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="title || '代码编辑'"
    :fullscreen="true"
    :close-on-click-modal="false"
    class="big-monaco-dialog"
    @keydown="onKeydown"
  >
    <template #header>
      <div class="bm-header">
        <span class="bm-title">{{ title || '代码编辑' }}</span>
        <div class="bm-actions">
          <span class="bm-lang text-dim">{{ language || 'python' }}</span>
          <el-button size="small" @click="formatDoc">格式化</el-button>
          <span class="bm-hint text-dim">Esc 关闭</span>
        </div>
      </div>
    </template>
    <div ref="editorHost" class="bm-editor-host"></div>
  </el-dialog>
</template>

<style scoped>
.bm-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding-right: 32px;
}
.bm-title {
  font-weight: 600;
  font-size: 14px;
}
.bm-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.bm-lang {
  font-size: 11px;
  padding: 2px 8px;
  border: 1px solid var(--on-border-light, #333);
  border-radius: 3px;
  text-transform: uppercase;
}
.bm-hint {
  font-size: 11px;
}
.bm-editor-host {
  height: calc(100vh - 120px);
  width: 100%;
  border: 1px solid var(--on-border-light, #333);
  border-radius: 4px;
  overflow: hidden;
}
</style>
