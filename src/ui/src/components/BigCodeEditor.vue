<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount, shallowRef, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { EditorState, Compartment } from '@codemirror/state'
import { EditorView, lineNumbers, highlightActiveLine, highlightActiveLineGutter, keymap, drawSelection } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import { bracketMatching, foldGutter, indentOnInput, indentUnit, syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language'
import { autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete'
import { linter, lintGutter } from '@codemirror/lint'
import { json, jsonParseLinter } from '@codemirror/lang-json'
import { xml } from '@codemirror/lang-xml'
import { oneDark } from '@codemirror/theme-one-dark'

const { t } = useI18n()

// 亮色主题（自定义，匹配项目亮色主题）
const lightTheme = EditorView.theme({
  '&': { color: '#1f2328', backgroundColor: '#ffffff' },
  '.cm-content': { caretColor: '#0d9488' },
  '.cm-activeLine': { backgroundColor: 'rgba(0,0,0,0.03)' },
  '.cm-selectionBackground, ::selection': { backgroundColor: 'rgba(13,148,136,0.15)' },
  '.cm-cursor': { borderLeftColor: '#0d9488' },
  '.cm-gutters': { backgroundColor: '#f6f8fa', color: '#6e7681', border: 'none' },
}, { dark: false })

function themeExt(): any {
  return document.documentElement.classList.contains('dark') ? oneDark : lightTheme
}

/**
 * BigCodeEditor：放大版代码编辑器（CodeMirror 6）
 *
 * 功能：
 * - 行号显示
 * - 括号配对（输入即补全）
 * - 自动缩进（Enter 继承 + 块开括号加一层）
 * - 实时语法高亮（JSON / XML / plaintext）
 * - 错误标注（JSON 解析错误波浪线 + gutter 警示）
 * - 暗色主题（oneDark）
 *
 * 通过 v-model 双向同步文本。语言由 prop 控制，json 时启用 lint。
 */
const props = defineProps<{
  modelValue: boolean
  title?: string
  text: string
  language?: string  // 'json' | 'xml' | 'plaintext'
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  'update:text': [string]
}>()

const visible = ref(props.modelValue)
watch(() => props.modelValue, (v) => { visible.value = v })
watch(visible, (v) => { emit('update:modelValue', v) })

// 容器 ref（CodeMirror 挂载点）
const editorHost = ref<HTMLDivElement | null>(null)
// EditorView 实例（shallowRef 避免 Vue 深度代理）
const view = shallowRef<EditorView | null>(null)
// 语言 compartment（动态切换语言用）
const langCompartment = new Compartment()
// lint compartment（仅 json 启用）
const lintCompartment = new Compartment()
// 主题 compartment（动态切换主题用）
const themeCompartment = new Compartment()

// 当前文本（避免循环更新）
let internalUpdate = false

// 主题监听 Observer（在 mountEditor 中创建，destroyEditor 中销毁）
let themeObserver: MutationObserver | null = null

// 根据语言构造 extension
function langExt(): any[] {
  const lang = props.language || 'plaintext'
  if (lang === 'json') {
    return [json(), linter(jsonParseLinter())]
  }
  if (lang === 'xml') {
    return [xml()]
  }
  return []
}

// 创建 EditorState
function createState(doc: string): EditorState {
  return EditorState.create({
    doc,
    extensions: [
      lineNumbers(),
      history(),
      foldGutter(),
      drawSelection(),
      indentOnInput(),
      indentUnit.of('  '),
      bracketMatching(),
      closeBrackets(),
      autocompletion(),
      highlightActiveLine(),
      highlightActiveLineGutter(),
      syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
      keymap.of([
        ...closeBracketsKeymap,
        ...defaultKeymap,
        ...historyKeymap,
        ...completionKeymap,
        indentWithTab,
      ]),
      lintGutter(),
      langCompartment.of(langExt()),
      lintCompartment.of(props.language === 'json' ? linter(jsonParseLinter()) : []),
      themeCompartment.of(themeExt()),
      EditorView.lineWrapping,
      EditorView.theme({
        '&': { fontSize: '13px', height: '100%' },
        '.cm-content': { fontFamily: 'var(--on-font-mono, Menlo, Consolas, monospace)', lineHeight: '1.6' },
        '.cm-gutters': { fontSize: '12px', border: 'none' },
        // 行号列紧凑：右侧 padding 极小，仅留 2px 间隙
        '.cm-lineNumbers .cm-gutterElement': { padding: '0 2px 0 6px', textAlign: 'right' },
        '.cm-foldGutter': { width: '14px', minWidth: '14px' },
      }),
      EditorView.updateListener.of((u) => {
        if (u.docChanged && !internalUpdate) {
          emit('update:text', u.state.doc.toString())
        }
      }),
    ],
  })
}

// 安全引用 searchKeymap（@codemirror/search 包，未装则跳过）
// 已移除：defaultKeymap 已包含 Ctrl+F 搜索

// 挂载 CodeMirror（el-dialog 是 lazy 渲染，visible=true 时才渲染内部，
// 所以不能只在 onMounted 挂载，必须在 visible 变 true 时再挂载）
function mountEditor() {
  if (!editorHost.value) return
  // 已挂载则不重复
  if (view.value) return
  const state = createState(props.text || '')
  view.value = new EditorView({ state, parent: editorHost.value })
  // 监听主题切换
  themeObserver = new MutationObserver(() => {
    const v = view.value
    if (!v) return
    v.dispatch({ effects: themeCompartment.reconfigure(themeExt()) })
  })
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
}

function destroyEditor() {
  themeObserver?.disconnect()
  themeObserver = null
  if (view.value) {
    view.value.destroy()
    view.value = null
  }
}

onMounted(() => {
  // 如果 dialog 初始就是打开的（不太可能），立即挂载
  if (visible.value) {
    nextTick(mountEditor)
  }
})

// visible 变 true 时挂载，变 false 时销毁
watch(visible, (v) => {
  if (v) {
    nextTick(mountEditor)
  } else {
    destroyEditor()
  }
})

onBeforeUnmount(destroyEditor)

// 外部 text 变化时同步到编辑器（避免光标跳动）
watch(() => props.text, (newText) => {
  const v = view.value
  if (!v) return
  const cur = v.state.doc.toString()
  if (newText !== cur) {
    internalUpdate = true
    v.dispatch({
      changes: { from: 0, to: cur.length, insert: newText || '' },
    })
    internalUpdate = false
  }
})

// 语言变化时重新加载语言 extension
watch(() => props.language, () => {
  const v = view.value
  if (!v) return
  v.dispatch({
    effects: [
      langCompartment.reconfigure(langExt()),
      lintCompartment.reconfigure(props.language === 'json' ? linter(jsonParseLinter()) : []),
    ],
  })
})

// 快捷键：Ctrl+Enter / Esc 关闭
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    visible.value = false
  }
}

// 格式化 JSON
function formatJson() {
  const v = view.value
  if (!v) return
  const txt = v.state.doc.toString()
  try {
    const obj = JSON.parse(txt)
    const formatted = JSON.stringify(obj, null, 2)
    internalUpdate = true
    v.dispatch({
      changes: { from: 0, to: txt.length, insert: formatted },
    })
    internalUpdate = false
    emit('update:text', formatted)
  } catch (e: any) {
    // 不强制弹窗，lint 已经标红了
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="title || t('bigCodeEditor.defaultTitle')"
    :fullscreen="true"
    :close-on-click-modal="false"
    class="big-code-dialog"
    @keydown="onKeydown"
  >
    <template #header>
      <div class="bc-header">
        <span class="bc-title">{{ title || t('bigCodeEditor.defaultTitle') }}</span>
        <div class="bc-actions">
          <span class="bc-lang text-dim">{{ language || 'plaintext' }}</span>
          <el-button v-if="language === 'json'" size="small" @click="formatJson">
            {{ t('bigCodeEditor.formatJson') }}
          </el-button>
          <span class="bc-hint text-dim">{{ t('bigCodeEditor.escClose') }}</span>
        </div>
      </div>
    </template>
    <div ref="editorHost" class="bc-editor-host"></div>
  </el-dialog>
</template>

<style scoped>
.bc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding-right: 32px;
}
.bc-title {
  font-weight: 600;
  font-size: 14px;
}
.bc-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.bc-lang {
  font-size: 11px;
  padding: 2px 8px;
  border: 1px solid var(--on-border-light, #333);
  border-radius: 3px;
  text-transform: uppercase;
}
.bc-hint {
  font-size: 11px;
}
.bc-editor-host {
  height: calc(100vh - 120px);
  width: 100%;
  border: 1px solid var(--on-border-light, #333);
  border-radius: 4px;
  overflow: hidden;
}
.bc-editor-host :deep(.cm-editor) {
  height: 100%;
}
.bc-editor-host :deep(.cm-scroller) {
  overflow: auto;
}
</style>
