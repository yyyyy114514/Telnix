import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// Vite 配置：开发代理 /api 到后端 FastAPI
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      // 把 lodash-es 重定向到 lodash（CJS 单文件），避免 esbuild 逐个转换几百个 ESM 小文件。
      'lodash-es': 'lodash',
      // 把 element-plus 重定向到 dist 完整打包版本（单文件），避免 esbuild 逐个转换
      // element-plus/es/ 下成百上千个 .mjs 组件文件（在 Windows 上间歇性卡死）。
      // 产物会稍大，但构建可稳定完成。完整打包已含所有组件和 locale，API 完全一致。
      'element-plus': 'element-plus/dist/index.full.mjs',
      // Monaco Python 语言定义：绕过 monaco-editor 的 exports 字段
      //（exports 把 `./*` 映射到 `./esm/vs/*.js`，导致 Rollup 无法解析子路径）
      'monaco-editor/esm/vs/languages/definitions/python/python.js':
        path.resolve(__dirname, 'node_modules/monaco-editor/esm/vs/languages/definitions/python/python.js'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18899',
        changeOrigin: true,
      },
    },
  },
  optimizeDeps: {
    include: ['lodash', 'element-plus', 'vue', 'vue-router', 'pinia'],
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
    // monaco-editor 通过 @guolao/vue-monaco-editor 的 loader 从 CDN 加载，
    // 不参与构建（几千个模块会严重拖慢 rollup）。
    // 只排除运行时模块（monaco-editor），保留类型导入（import type）。
    rollupOptions: {
      external: ['monaco-editor'],
      output: {
        manualChunks: undefined,
      },
    },
  },
})
