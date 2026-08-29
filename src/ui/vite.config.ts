import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// Vite 配置：开发代理 /api 到后端 FastAPI
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: [
      // 把 lodash-es 重定向到 lodash（CJS 单文件），避免 esbuild 逐个转换几百个 ESM 小文件。
      { find: 'lodash-es', replacement: 'lodash' },
      // 把 element-plus 重定向到 dist 完整打包版本（单文件），避免 esbuild 逐个转换
      // element-plus/es/ 下成百上千个 .mjs 组件文件（在 Windows 上间歇性卡死）。
      // 产物会稍大，但构建可稳定完成。完整打包已含所有组件和 locale，API 完全一致。
      { find: 'element-plus', replacement: 'element-plus/dist/index.full.mjs' },
      // Monaco Python 语言定义：绕过 monaco-editor 的 exports 字段
      //（exports 把 `./*` 映射到 `./esm/vs/*.js`，导致 Rollup 无法解析子路径）
      {
        find: 'monaco-editor/esm/vs/languages/definitions/python/python.js',
        replacement: path.resolve(__dirname, 'node_modules/monaco-editor/esm/vs/languages/definitions/python/python.js'),
      },
      // Monaco editor worker：同样的 exports 字段问题。
      // `monaco-editor/esm/vs/editor/editor.worker` 会被 exports 映射到
      // `./esm/vs/esm/vs/editor/editor.worker.js`（双重 esm/vs 前缀），文件不存在。
      // 必须用正则别名：字符串别名无法匹配带 `?worker` 查询后缀的 import。
      // 正则捕获可选的 `?worker` 后缀并拼接到真实文件的绝对路径后，
      // 让 Vite 的 worker 插件在别名解析后正常处理。
      {
        find: /^(monaco-editor\/esm\/vs\/editor\/editor\.worker)(\?.*)?$/,
        replacement: path.resolve(__dirname, 'node_modules/monaco-editor/esm/vs/editor/editor.worker.js') + '$2',
      },
    ],
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18901',
        changeOrigin: true,
      },
    },
  },
  optimizeDeps: {
    include: ['lodash', 'element-plus', 'vue', 'vue-router', 'pinia'],
    // monaco-editor 的 worker 入口带 ?worker 后缀，Vite dep optimizer 无法处理
    // （TypeError: Cannot read properties of undefined (reading 'imports')），
    // 必须排除，否则整个 dep 优化失败，所有依赖返回 504 Outdated Optimize Dep → 白屏。
    exclude: ['monaco-editor'],
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    minify: 'esbuild',
    chunkSizeWarningLimit: 1500,
    commonjsOptions: {
      // lodash 是 CJS，需要 transformMixedEsModules 以正确处理混合模块
      transformMixedEsModules: true,
    },
    // monaco-editor 改为本地打包（见 monaco-setup.ts：loader.config({ monaco }) 注入本地实例），
    // 不再从 CDN 加载，消除 5 秒级延迟。必须参与构建（不能 external），否则运行时会回退 CDN。
    rollupOptions: {
      // external: ['monaco-editor'],  // 已禁用：本地打包
      output: {
        // 手动分包：把体积大、变动频率不同的依赖拆成独立 chunk，
        // 提升浏览器缓存命中率与并行加载速度（本地桌面端加载也更平滑）。
        manualChunks(id: string) {
          if (id.includes('node_modules')) {
            if (id.includes('monaco-editor')) return 'monaco'
            if (id.includes('echarts') || id.includes('zrender')) return 'echarts'
            if (id.includes('codemirror') || id.includes('@codemirror')) return 'codemirror'
            if (id.includes('element-plus')) return 'element-plus'
            if (
              id.includes('vue/') || id.includes('@vue/') ||
              id.includes('vue-router') || id.includes('pinia') ||
              id.includes('@vueuse/')
            ) return 'vue-core'
          }
        },
      },
    },
  },
})
