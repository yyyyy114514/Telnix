import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
// highlight.js 代码高亮（深色主题）
import 'highlight.js/styles/atom-one-dark.css'
import hljs from 'highlight.js/lib/common'
import hljsVuePlugin from '@highlightjs/vue-plugin'
// Monaco Editor worker 配置：必须在 monaco-editor 导入之前执行
import './monaco-setup'
import App from './App.vue'
import router from './router'
import ClashIcon from './components/ClashIcon.vue'
import './styles/main.css'
import { vCodeAssist } from './directives/codeAssist'
import { initPrefsSync } from './stores/prefs'

// 主题切换：从 localStorage 读取，默认 dark（保留原深色体验）
const savedTheme = localStorage.getItem('telnix_theme') || 'dark'
if (savedTheme === 'dark') {
  document.documentElement.classList.add('dark')
} else {
  document.documentElement.classList.remove('dark')
}

// 流量列表禁选文字：从 localStorage 读取，默认开启
// 设置页可调，开启后双击包不会选中文字（详细信息仍可选）
if (localStorage.getItem('telnix_list_no_select') !== 'false') {
  document.documentElement.classList.add('list-no-select')
}

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus)
app.use(hljsVuePlugin)

// 注册全部图标
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component as any)
}

// 全局注册自定义 Clash 图标（替代与 TCP 页重复的 Connection 图标）
app.component('ClashIcon', ClashIcon)

// 全局可用 hljs 实例（组件内高亮原始文本用）
app.provide('hljs', hljs)

// 注册 v-code-assist 指令：textarea IDE 辅助（Tab 缩进 / 括号配对 / 引号配对）
app.directive('code-assist', vCodeAssist)

app.mount('#app')

// 启动用户偏好同步：把 localStorage 的 GUI 偏好同步到 settings.json
initPrefsSync()
