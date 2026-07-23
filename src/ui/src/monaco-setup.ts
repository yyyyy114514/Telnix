/**
 * Monaco Editor 运行时配置：无 worker + 预加载 Python 语言定义。
 *
 * 两个问题：
 * 1. Worker 加载失败：monaco-editor 0.56 的 exports 字段干扰 Vite 的 ?worker 路径解析。
 *    → getWorker 返回空 worker（类型要求返回 Worker，不能返回 null），
 *      Monaco 退化为无 worker 模式（语法高亮在主线程运行）。
 * 2. 语言定义动态 import 失败：Python 的 Monarch tokenizer 通过 `import('./python.js')`
 *    动态加载，Vite 打包后生成单独 chunk，浏览器缓存或路径问题导致 404。
 *    → 静态 import Python 语言定义，手动注册 tokens provider + language configuration，
 *    绕过动态 import。
 *
 * 必须在 `import * as monaco from 'monaco-editor'` 之前执行此文件。
 */
import * as monaco from 'monaco-editor'
// 静态导入 Python 语言定义（Monarch tokenizer + 语言配置），避免动态 import chunk 404
// @ts-expect-error: monaco-editor 的 exports 字段导致 TS 无法解析子路径模块声明
import { conf as pythonConf, language as pythonLanguage } from 'monaco-editor/esm/vs/languages/definitions/python/python.js'

// 1. 无 worker 模式：语法高亮在主线程运行，不依赖 worker
//    类型要求返回 Worker，实际返回空对象（不会真正使用）
self.MonacoEnvironment = {
  getWorker(): any {
    return null as any
  },
}

// 2. 手动注册 Python 语言定义（绕过动态 import）
//    monaco-editor 的 register.all.js 会通过 import('./python.js') 动态加载，
//    但在 Vite 打包后可能因 chunk 路径问题失败。
//    这里在模块加载时立即注册，确保 tokenizer 可用。
monaco.languages.registerTokensProviderFactory('python', {
  create: async () => pythonLanguage,
})
monaco.languages.setLanguageConfiguration('python', pythonConf)
