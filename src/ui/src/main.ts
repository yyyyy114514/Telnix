import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
// 按需导入实际用到的图标（原为 `import *` 全量循环注册数百个图标，启动/内存/打包体积开销大）。
// 注意：项目中大量图标通过 `<component :is="'IconName'">` 字符串动态解析，
// 必须保持「按名称的全局注册」，因此这里显式列出所有静态 + 动态引用到的图标并注册。
// 新增图标用法时，请把对应图标补进下面的 usedIcons。
import {
  ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Back, Bottom, Top, Right,
  Check, Close, Delete, Plus, Refresh, RefreshLeft, RefreshRight, Search,
  Sort, Upload, Download, Edit, Filter, RemoveFilled, Open, TurnOff, Star,
  CircleCheck, CircleCheckFilled, CircleClose, CircleCloseFilled, WarningFilled,
  InfoFilled, QuestionFilled, Loading, VideoPlay, VideoPause, VideoCamera,
  Document, DocumentCopy, CopyDocument, Folder, FolderOpened, Connection, Link,
  Promotion, ChatDotRound, ChatLineRound, ChatLineSquare, Setting, SetUp, Cpu,
  Monitor, Iphone, Key, Lock, SwitchButton, Switch, Aim, Brush, Clock, Coffee,
  Coin, DataAnalysis, DataLine, Histogram, MagicStick, Share, Timer, View, Bowl,
  TrendCharts,
} from '@element-plus/icons-vue'

const usedIcons = {
  ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Back, Bottom, Top, Right,
  Check, Close, Delete, Plus, Refresh, RefreshLeft, RefreshRight, Search,
  Sort, Upload, Download, Edit, Filter, RemoveFilled, Open, TurnOff, Star,
  CircleCheck, CircleCheckFilled, CircleClose, CircleCloseFilled, WarningFilled,
  InfoFilled, QuestionFilled, Loading, VideoPlay, VideoPause, VideoCamera,
  Document, DocumentCopy, CopyDocument, Folder, FolderOpened, Connection, Link,
  Promotion, ChatDotRound, ChatLineRound, ChatLineSquare, Setting, SetUp, Cpu,
  Monitor, Iphone, Key, Lock, SwitchButton, Switch, Aim, Brush, Clock, Coffee,
  Coin, DataAnalysis, DataLine, Histogram, MagicStick, Share, Timer, View, Bowl,
  TrendCharts,
}
// highlight.js 代码高亮（深色主题）
import 'highlight.js/styles/atom-one-dark.css'
// 使用 core 而非 common，避免与内置语言冲突导致运行时崩溃
import hljs from 'highlight.js/lib/core'
import hljsVuePlugin from '@highlightjs/vue-plugin'
// 注册常用语言
import json from 'highlight.js/lib/languages/json'
import xml from 'highlight.js/lib/languages/xml'
import http from 'highlight.js/lib/languages/http'
import css from 'highlight.js/lib/languages/css'
import javascript from 'highlight.js/lib/languages/javascript'
import plaintext from 'highlight.js/lib/languages/plaintext'
hljs.registerLanguage('json', json)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('http', http)
hljs.registerLanguage('css', css)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('plaintext', plaintext)
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import ClashIcon from './components/ClashIcon.vue'
import ToolsIcon from './components/ToolsIcon.vue'
import CookieIcon from './components/CookieIcon.vue'
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

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(i18n)
app.use(ElementPlus)
app.use(hljsVuePlugin)

// 按名称全局注册用到的图标（供 `<component :is="'IconName'">` 字符串动态解析使用）
for (const [key, component] of Object.entries(usedIcons)) {
  app.component(key, component as any)
}

// 全局注册自定义 Clash 图标（替代与 TCP 页重复的 Connection 图标）
app.component('ClashIcon', ClashIcon)
// 全局注册自定义工具页图标（四方块 + 菱形 SVG）
app.component('ToolsIcon', ToolsIcon)
// 全局注册自定义 Cookie 图标
app.component('CookieIcon', CookieIcon)

// 全局可用 hljs 实例（组件内高亮原始文本用）
app.provide('hljs', hljs)

// 注册 v-code-assist 指令：textarea IDE 辅助（Tab 缩进 / 括号配对 / 引号配对）
app.directive('code-assist', vCodeAssist)

app.mount('#app')

// 启动用户偏好同步：把 localStorage 的 GUI 偏好同步到 settings.json
initPrefsSync()
