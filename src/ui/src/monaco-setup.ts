/**
 * Monaco Editor 加载器（本地打包模式）
 *
 * 之前用 @guolao/vue-monaco-editor 的 loader 从 CDN（jsdelivr）加载 monaco-editor，
 * 体积 5MB+，首次打开编辑器要等 5 秒以上（尤其国内网络访问 jsdelivr 不稳定）。
 *
 * 改为本地打包：monaco-editor 已在 package.json 依赖中，这里直接 import 本地实例，
 * 通过 loader.config({ monaco }) 注入，不再走网络。配合 vite.config.ts 中把
 * monaco-editor 移出 rollup external（参与构建），彻底消除 CDN 等待。
 *
 * Python 语言定义：monaco-editor 的官方 python.js 体积大且在 CDN 下不好访问，
 * 仍用自己注册的简化 Monarch tokenizer（见下方 registerPythonLanguage）。
 */

import * as monaco from 'monaco-editor'
// 基础 editor worker（本地导入，Vite 会打包为独立 worker chunk）。
// Python 语法高亮走主线程 Monarch tokenizer，不依赖额外语言 worker。
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { loader } from '@guolao/vue-monaco-editor'

// 配置 monaco worker 环境（本地，不走 CDN）
;(self as unknown as { MonacoEnvironment: unknown }).MonacoEnvironment = {
  getWorker() {
    return new editorWorker() as unknown as Worker
  },
}

let configured = false

/**
 * 配置 Monaco loader（使用本地 monaco 实例）+ 注册 Python 语言定义。
 * 幂等，多次调用安全。
 */
export function setupMonaco() {
  if (configured) return
  configured = true

  // 注入本地 monaco 实例，避免 loader 从 CDN 拉取
  loader.config({ monaco })

  // Python Monarch tokenizer：注册自定义语言定义（不依赖 monaco-editor 的 python.js）
  registerPythonLanguage(monaco)
}

/**
 * 注册 Python 语言定义（Monarch tokenizer + 语言配置）。
 * 简化版，覆盖常见 Python 语法高亮需求。
 */
function registerPythonLanguage(monaco: typeof import('monaco-editor')) {
  // 如果已注册则跳过
  try {
    // 语言配置：括号配对、自动缩进、注释
    monaco.languages.setLanguageConfiguration('python', {
      comments: {
        lineComment: '#',
        blockComment: ['"""', '"""'],
      },
      brackets: [
        ['{', '}'],
        ['[', ']'],
        ['(', ')'],
      ],
      autoClosingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"', notIn: ['string'] },
        { open: "'", close: "'", notIn: ['string', 'comment'] },
        { open: '"""', close: '"""', notIn: ['string', 'comment'] },
        { open: "'''", close: "'''", notIn: ['string', 'comment'] },
      ],
      surroundingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"' },
        { open: "'", close: "'" },
        { open: '"""', close: '"""' },
        { open: "'''", close: "'''" },
      ],
      onEnterRules: [
        {
          beforeText: /^\s*(?:def|class|for|if|elif|else|while|try|except|finally|with|async\s+def)\b.*:\s*$/,
          action: { indentAction: monaco.languages.IndentAction.Indent },
        },
      ],
      folding: {
        offSide: true,
        markers: {
          start: /^\s*#region\b/,
          end: /^\s*#endregion\b/,
        },
      },
    })

    // Monarch tokenizer：Python 语法高亮规则
    monaco.languages.registerTokensProviderFactory('python', {
      create: async () => ({
        defaultToken: '',
        tokenPostfix: '.python',
        keywords: [
          'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue',
          'def', 'del', 'elif', 'else', 'except', 'False', 'finally', 'for',
          'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'None',
          'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'True', 'try',
          'while', 'with', 'yield', 'self', 'cls',
        ],
        operators: [
          '+', '-', '*', '**', '/', '//', '%', '@', '<<', '>>', '&', '|', '^',
          '~', '<', '>', '<=', '>=', '==', '!=', '=', '+=', '-=', '*=', '/=',
          '//=', '%=', '**=', '>>=', '<<=', '&=', '|=', '^=', '@=', ':=',
        ],
        symbols: /[=><!~?:&|+\-*/^%]+/,
        escapes: /\\(?:[abfnrtv\\"']|x[0-9A-Fa-f]{1,4}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})/,
        digits: /\d+(_+\d+)*/,
        octaldigits: /[0-7]?(_+[0-7])*$/,
        binarydigits: /[0-1]?(_+[0-1])*$/,
        hexdigits: /[[0-9a-fA-F]?(_+[0-9a-fA-F])*$/,
        tokenizer: {
          root: [
            [/[a-zA-Z_]\w*/, {
              cases: {
                '@keywords': 'keyword',
                '@default': 'identifier',
              },
            }],
            { include: '@whitespace' },
            [/\d+(\.\d+)?([eE][+-]?\d+)?/, 'number'],
            [/0[xX][0-9a-fA-F]+/, 'number.hex'],
            [/0[oO][0-7]+/, 'number.octal'],
            [/0[bB][01]+/, 'number.binary'],
            [/"""/, { token: 'string', next: '@stringTriple' }],
            [/'''/, { token: 'string', next: '@stringTripleSingle' }],
            [/"/, { token: 'string', next: '@string' }],
            [/'/, { token: 'string', next: '@stringSingle' }],
            [/[{}()[\]]/, '@brackets'],
            [/@symbols/, {
              cases: {
                '@operators': 'operator',
                '@default': '',
              },
            }],
            [/#region\b/, 'comment'],
            [/#endregion\b/, 'comment'],
          ],
          whitespace: [
            [/\s+/, 'white'],
            [/#.*$/, 'comment'],
          ],
          string: [
            [/[^\\"]+/, 'string'],
            [/@escapes/, 'string.escape'],
            [/\\./, 'string.escape.invalid'],
            [/"/, { token: 'string', next: '@pop' }],
          ],
          stringSingle: [
            [/[^\\']+/, 'string'],
            [/@escapes/, 'string.escape'],
            [/\\./, 'string.escape.invalid'],
            [/'/, { token: 'string', next: '@pop' }],
          ],
          stringTriple: [
            [/[^\\"]+/, 'string'],
            [/@escapes/, 'string.escape'],
            [/\\./, 'string.escape.invalid'],
            [/"""/, { token: 'string', next: '@pop' }],
            [/"/, 'string'],
          ],
          stringTripleSingle: [
            [/[^\\']+/, 'string'],
            [/@escapes/, 'string.escape'],
            [/\\./, 'string.escape.invalid'],
            [/'''/, { token: 'string', next: '@pop' }],
            [/'/, 'string'],
          ],
        },
      }),
    })
  } catch {
    // 已注册则忽略
  }
}
