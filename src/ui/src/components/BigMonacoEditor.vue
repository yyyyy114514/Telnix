<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount, shallowRef, nextTick } from 'vue'
import { loader } from '@guolao/vue-monaco-editor'
import type * as Monaco from 'monaco-editor'
import { setupMonaco } from '../monaco-setup'

// 确保 CDN loader 已配置 + Python 语言已注册（幂等）
setupMonaco()

/**
 * BigMonacoEditor：放大版 Monaco 编辑器（全屏对话框）
 *
 * 支持可选 #test-panel 插槽，展开时左右分栏（编辑器 70% + 测试面板 30%）。
 */
const props = defineProps<{
  modelValue: boolean
  title?: string
  text: string
  language?: string // 默认 'python'
  /** 是否显示测试面板插槽 */
  showTestPanel?: boolean
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  'update:text': [string]
  'update:showTestPanel': [boolean]
  'save': []
}>()

const visible = ref(props.modelValue)
watch(() => props.modelValue, (v) => { visible.value = v })
watch(visible, (v) => { emit('update:modelValue', v) })

const editorHost = ref<HTMLDivElement | null>(null)
const editorRef = shallowRef<Monaco.editor.IStandaloneCodeEditor | null>(null)
let themeObserver: MutationObserver | null = null
let monacoInstance: typeof Monaco | null = null

function defineCustomTheme(monaco: typeof Monaco) {
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

async function mountEditor() {
  if (!editorHost.value) return
  if (editorRef.value) return
  const monaco = await loader.init()
  monacoInstance = monaco
  defineCustomTheme(monaco)
  const { registerPythonIntelliSense } = await import('./pythonIntelliSense')
  registerPythonIntelliSense(monaco)
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
  monacoInstance = null
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
          <el-button
            v-if="$slots['test-panel']"
            size="small"
            :type="showTestPanel ? 'success' : 'primary'"
            plain
            @click="$emit('update:showTestPanel', !showTestPanel)"
          >
            {{ showTestPanel ? '收起测试' : '展开测试' }}
          </el-button>
          <span class="bm-hint text-dim">Esc 关闭</span>
        </div>
      </div>
    </template>
    <div class="bm-body" :class="{ 'with-test': showTestPanel && $slots['test-panel'] }">
      <div ref="editorHost" class="bm-editor-host"></div>
      <div v-if="showTestPanel && $slots['test-panel']" class="bm-test-panel">
        <slot name="test-panel"></slot>
      </div>
    </div>
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
  height: 100%;
  width: 100%;
  min-height: 0;  /* flex 子项允许收缩 */
  border: 1px solid var(--on-border-light, #333);
  border-radius: 4px;
  overflow: hidden;
}

/* 全屏 dialog body 去掉 padding，让 bm-body 撑满 */
.big-monaco-dialog :deep(.el-dialog__body) {
  padding: 0;
}
/* 带 test-panel 时左右分栏：70% 编辑器 + 30% 测试面板 */
.bm-body {
  display: block;
  height: calc(100vh - 55px);  /* 减去 header 高度 */
  overflow: hidden;  /* 整体页面不滚动 */
  padding: 10px;
  box-sizing: border-box;
}
.bm-body.with-test {
  display: flex;
  gap: 10px;
  align-items: stretch;  /* 两侧等高，撑满 bm-body 高度 */
}
.bm-body.with-test .bm-editor-host {
  flex: 0 0 68%;
  min-width: 0;
}
.bm-test-panel {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--on-border, #333);
  border-radius: 6px;
  background: var(--on-bg-elevated, var(--on-bg, #fff));
  overflow: hidden;
  display: flex;
  flex-direction: column;
  min-height: 0;  /* flex 子项允许收缩，让内部可滚动 */
}
.bm-test-panel :deep(.test-panel-header) {
  flex: 0 0 auto;
}
/* 全屏模式下：测试面板内容超出时只滚动 body 内部，编辑器固定不滚动 */
.bm-test-panel :deep(.test-panel-body) {
  flex: 1;
  overflow-y: auto !important;
  min-height: 0;
}
</style>
