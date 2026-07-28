/**
 * v-code-assist 指令：给 <textarea> 提供 IDE 风格的输入辅助
 * - Tab 键：插入 2 空格（不切换焦点）；有选区时整体缩进
 * - Shift+Tab：取消缩进（删除行首 2 空格）
 * - Enter 键：继承上一行缩进；上一行以 { [ ( 结尾则额外加一层
 * - 括号配对：输入 ( [ { 自动补 ) ] }
 * - 引号配对：输入 " ' 自动配对
 * - 退格删除配对：光标在成对符号中间时同时删除两侧
 *
 * 性能：零依赖，零运行时开销（仅在 keydown 时触发）
 * 限制：textarea 无法实时语法高亮（浏览器限制）
 */
import type { Directive } from 'vue'

const PAIRS: Record<string, string> = {
  '(': ')',
  '[': ']',
  '{': '}',
}

const QUOTES = new Set(['"', "'"])

const INDENT = '  ' // 2 空格缩进

function insertText(el: HTMLTextAreaElement, text: string, selectStart?: number, selectEnd?: number) {
  const start = el.selectionStart
  const end = el.selectionEnd
  const before = el.value.slice(0, start)
  const after = el.value.slice(end)
  el.value = before + text + after
  const newStart = selectStart ?? start + text.length
  const newEnd = selectEnd ?? newStart
  el.setSelectionRange(newStart, newEnd)
  // 触发 v-model 更新
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

function getLineIndent(text: string, pos: number): string {
  const lineStart = text.lastIndexOf('\n', pos - 1) + 1
  const lineText = text.slice(lineStart, pos)
  const m = lineText.match(/^[ \t]*/)
  return m ? m[0] : ''
}

function onKeydown(e: KeyboardEvent) {
  const el = e.target as HTMLTextAreaElement
  if (el.readOnly || el.disabled) return
  // 输入法合成中（中文/日文输入法）：不干预按键，避免与输入法自带配对冲突
  if (e.isComposing) return

  const start = el.selectionStart
  const end = el.selectionEnd
  const hasSelection = start !== end
  const before = el.value.slice(0, start)
  const after = el.value.slice(end)
  const prevChar = before.slice(-1)
  const nextChar = after.slice(0, 1)

  // 1. Tab 键：缩进 / 选区缩进
  if (e.key === 'Tab') {
    e.preventDefault()
    if (hasSelection) {
      // 多行选区：整体缩进/取消缩进
      const lineStart = before.lastIndexOf('\n', start - 1) + 1
      const selected = el.value.slice(lineStart, end)
      const lines = selected.split('\n')
      if (e.shiftKey) {
        // 取消缩进：每行删 1-2 个行首空格
        const dedented = lines.map((l) => l.replace(/^ {1,2}/, ''))
        const newText = dedented.join('\n')
        el.value = el.value.slice(0, lineStart) + newText + el.value.slice(end)
        el.setSelectionRange(lineStart, lineStart + newText.length)
      } else {
        const newText = lines.map((l) => INDENT + l).join('\n')
        el.value = el.value.slice(0, lineStart) + newText + el.value.slice(end)
        el.setSelectionRange(lineStart, lineStart + newText.length)
      }
      el.dispatchEvent(new Event('input', { bubbles: true }))
    } else {
      // 单点：插入 2 空格
      insertText(el, INDENT)
    }
    return
  }

  // 2. Enter 键：自动缩进
  if (e.key === 'Enter' && !e.shiftKey) {
    // 仅在普通 textarea（非 element-plus 增强）中处理
    // el-input type=textarea 的 Enter 默认换行，这里增强缩进
    const indent = getLineIndent(el.value, start)
    const opensBlock = /[{[(]\s*$/.test(before.slice(before.lastIndexOf('\n') + 1))
    if (indent || opensBlock) {
      e.preventDefault()
      const newIndent = indent + (opensBlock ? INDENT : '')
      // 如果下一行已经是闭合括号且开块，光标停在中间
      const insertText = '\n' + newIndent
      el.value = before + insertText + after
      const cursorPos = start + insertText.length
      el.setSelectionRange(cursorPos, cursorPos)
      el.dispatchEvent(new Event('input', { bubbles: true }))
      return
    }
    // 无缩进需求则走默认行为
    return
  }

  // 3. 括号配对
  if (PAIRS[e.key] && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const close = PAIRS[e.key]
    // 关键：输入法可能已自动补了闭合符号（如输入 { 后输入法插入了 }）
    // 检测：如果光标后紧接着就是对应的闭合符号，说明已配对，只移动光标
    if (!hasSelection && nextChar === close) {
      e.preventDefault()
      const np = start + 1
      el.setSelectionRange(np, np)
      return
    }
    e.preventDefault()
    // 有选区时：用括号包裹选区
    if (hasSelection) {
      el.value = before + e.key + el.value.slice(start, end) + close + after
      el.setSelectionRange(start + 1, end + 1)
      el.dispatchEvent(new Event('input', { bubbles: true }))
    } else {
      // 无选区：插入配对，光标在中间
      insertText(el, e.key + close, start + 1, start + 1)
    }
    return
  }

  // 4. 引号配对
  if (QUOTES.has(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey) {
    // 如果光标前是同款引号且后面也有同款引号（已在引号内），直接跳过
    if (prevChar === e.key && nextChar === e.key) {
      e.preventDefault()
      const newPos = start + 1
      el.setSelectionRange(newPos, newPos)
      return
    }
    // 有选区：用引号包裹选区
    if (hasSelection) {
      e.preventDefault()
      el.value = before + e.key + el.value.slice(start, end) + e.key + after
      el.setSelectionRange(start + 1, end + 1)
      el.dispatchEvent(new Event('input', { bubbles: true }))
      return
    }
    // 无选区：插入配对引号
    // 但如果前一个字符是字母/数字（如已经输入了 abc），不配对（避免干扰普通文本输入）
    if (/[a-zA-Z0-9_]/.test(prevChar)) {
      return // 走默认行为
    }
    e.preventDefault()
    insertText(el, e.key + e.key, start + 1, start + 1)
    return
  }

  // 5. 退格删除配对
  if (e.key === 'Backspace' && !e.ctrlKey && !e.metaKey && !e.altKey && !hasSelection) {
    const pairClose = PAIRS[prevChar]
    if (pairClose && nextChar === pairClose) {
      e.preventDefault()
      el.value = before.slice(0, -1) + after.slice(1)
      el.setSelectionRange(start - 1, start - 1)
      el.dispatchEvent(new Event('input', { bubbles: true }))
      return
    }
    if (QUOTES.has(prevChar) && prevChar === nextChar && prevChar) {
      e.preventDefault()
      el.value = before.slice(0, -1) + after.slice(1)
      el.setSelectionRange(start - 1, start - 1)
      el.dispatchEvent(new Event('input', { bubbles: true }))
      return
    }
  }
}

export const vCodeAssist: Directive = {
  mounted(el: HTMLElement) {
    // 支持 textarea、input 以及 el-input 包装（内部含 textarea 或 input）
    let ta: HTMLTextAreaElement | HTMLInputElement | null = null
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
      ta = el as HTMLTextAreaElement | HTMLInputElement
    } else {
      ta = (el.querySelector('textarea') || el.querySelector('input')) as HTMLTextAreaElement | HTMLInputElement | null
    }
    if (ta) {
      ta.addEventListener('keydown', onKeydown as EventListener)
      // 标记便于 unmounted 时移除
      ;(el as any).__codeAssistHandler = onKeydown
      ;(el as any).__codeAssistTarget = ta
    }
  },
  unmounted(el: HTMLElement) {
    const handler = (el as any).__codeAssistHandler
    const ta = (el as any).__codeAssistTarget
    if (handler && ta) {
      ta.removeEventListener('keydown', handler as EventListener)
    }
  },
}

export default vCodeAssist
