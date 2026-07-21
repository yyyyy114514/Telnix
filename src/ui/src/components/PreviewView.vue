<script setup lang="ts">
import { computed } from 'vue'
import TextSearch from './TextSearch.vue'
import CodeEditor from './CodeEditor.vue'

// 响应体预览：根据 Content-Type 自动选择渲染方式
// 二进制内容（图片/PDF/视频等）后端以 "base64:xxx" 形式存储
// JSON/XML/CSS/JS/text 使用 TextSearch 组件：代码高亮 + 搜索 + 右键解密
// 断点时（editable=true）对文本类内容用 CodeEditor 直接编辑
const props = defineProps<{
  body: string
  contentType: string
  editable?: boolean
}>()
const emit = defineEmits<{ 'update:body': [string] }>()

function onInput(v: string) {
  emit('update:body', v)
}

// 解析 Content-Type：主类型/子类型 + charset
const parsed = computed(() => {
  const ct = (props.contentType || '').toLowerCase().trim()
  const semi = ct.split(';')
  const main = (semi[0] || '').trim()
  let charset = ''
  for (let i = 1; i < semi.length; i++) {
    const part = semi[i].trim()
    if (part.startsWith('charset=')) {
      charset = part.slice(8).replace(/["']/g, '')
    }
  }
  const [type, subtype] = main.split('/')
  return { type: type || '', subtype: subtype || '', charset, main }
})

// 二进制 body：去 base64: 前缀
const isBase64 = computed(() => (props.body || '').startsWith('base64:'))

// 文本 body（非 base64）
const textBody = computed(() => (isBase64.value ? '' : props.body || ''))

// 构造 data URL（用于图片/PDF/视频/音频）
// 性能优化：props.body 本身就是 base64:xxx，直接拼接，跳过 atob+btoa 重复编解码
const dataUrl = computed(() => {
  if (!isBase64.value) return ''
  const b64 = props.body.slice(7)
  return `data:${parsed.value.main};base64,${b64}`
})

// 渲染类别
type RenderKind =
  | 'image'
  | 'html'
  | 'json'
  | 'xml'
  | 'text'
  | 'css'
  | 'js'
  | 'video'
  | 'audio'
  | 'pdf'
  | 'binary'
  | 'empty'

const kind = computed<RenderKind>(() => {
  if (!props.body) return 'empty'
  const { type, subtype } = parsed.value
  if (type === 'image') return 'image'
  if (type === 'video') return 'video'
  if (type === 'audio') return 'audio'
  if (parsed.value.main === 'application/pdf' || subtype === 'pdf') return 'pdf'
  if (parsed.value.main === 'text/html' || subtype === 'html') return 'html'
  if (parsed.value.main === 'application/json' || subtype === 'json') return 'json'
  if (parsed.value.main === 'application/xml' || subtype === 'xml' || type === 'text' && subtype === 'xml') return 'xml'
  if (parsed.value.main === 'text/css' || subtype === 'css') return 'css'
  if (parsed.value.main === 'text/javascript' || subtype === 'javascript' || subtype === 'x-javascript') return 'js'
  if (type === 'text' || subtype === 'plain' || subtype === 'csv') return 'text'
  return 'binary'
})

// TextSearch 用的语言标识（用于代码高亮）：json/xml/css/javascript，纯文本返回空（不高亮）
const codeLanguage = computed<string>(() => {
  switch (kind.value) {
    case 'json': return 'json'
    case 'xml': return 'xml'
    case 'css': return 'css'
    case 'js': return 'javascript'
    default: return ''
  }
})

// 可编辑的文本类型：editable=true 时对这些类型用 CodeEditor 直接编辑
// CodeEditor 支持 json/xml/plaintext，css/js 暂回退到 plaintext（仍可编辑，仅无语法高亮）
const editableLang = computed<string>(() => {
  if (!props.editable) return ''
  switch (kind.value) {
    case 'json': return 'json'
    case 'xml': return 'xml'
    case 'css':
    case 'js':
    case 'text': return 'plaintext'
    default: return ''  // 二进制/图片等不可编辑
  }
})

// JSON 编辑前美化（同 JsonView 逻辑）
// 性能优化：阈值与 TextSearch HIGHLIGHT_LIMIT 对齐（100000），避免美化后超出高亮上限
const PRETTY_LIMIT = 100000
const editBody = computed(() => {
  if (kind.value !== 'json') return textBody.value
  const str = textBody.value
  if (!str || str.length > PRETTY_LIMIT) return str
  try {
    return JSON.stringify(JSON.parse(str), null, 2)
  } catch {
    return str
  }
})

// 格式化大小
function fmtSize(n: number): string {
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(2) + ' MB'
}

const bodySize = computed(() => {
  if (isBase64.value) {
    // base64 字符串长度 → 原始字节数（减去 padding）
    const b64 = props.body.slice(7)
    const padding = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0
    return Math.floor(b64.length * 3 / 4) - padding
  }
  return new Blob([textBody.value]).size
})
</script>

<template>
  <div class="preview-view full overflow-auto">
    <!-- 空 -->
    <div v-if="kind === 'empty'" class="empty-text text-dim">（无响应体）</div>

    <!-- 图片 -->
    <div v-else-if="kind === 'image'" class="preview-center">
      <img :src="dataUrl" class="preview-img" :alt="`图片 ${parsed.subtype}`" />
      <div class="preview-meta text-dim">
        {{ parsed.subtype.toUpperCase() }} · {{ fmtSize(bodySize) }}
      </div>
    </div>

    <!-- HTML：iframe 沙箱渲染（allow-scripts 让内联脚本执行） -->
    <div v-else-if="kind === 'html'" class="preview-html">
      <iframe
        :srcdoc="textBody"
        class="html-frame"
        sandbox="allow-same-origin allow-scripts"
        referrerpolicy="no-referrer"
      ></iframe>
    </div>

    <!-- 视频 -->
    <div v-else-if="kind === 'video'" class="preview-center">
      <video :src="dataUrl" controls class="preview-video"></video>
      <div class="preview-meta text-dim">{{ parsed.subtype.toUpperCase() }} · {{ fmtSize(bodySize) }}</div>
    </div>

    <!-- 音频 -->
    <div v-else-if="kind === 'audio'" class="preview-center">
      <audio :src="dataUrl" controls></audio>
      <div class="preview-meta text-dim">{{ parsed.subtype.toUpperCase() }} · {{ fmtSize(bodySize) }}</div>
    </div>

    <!-- PDF -->
    <div v-else-if="kind === 'pdf'" class="preview-pdf">
      <embed :src="dataUrl" type="application/pdf" class="pdf-embed" />
    </div>

    <!-- JSON：editable 时用 CodeEditor，否则 TextSearch -->
    <CodeEditor
      v-else-if="kind === 'json' && editableLang"
      :model-value="editBody"
      :language="editableLang"
      :min-height="'200px'"
      class="preview-code-wrap"
      @update:model-value="onInput"
    />
    <TextSearch
      v-else-if="kind === 'json'"
      :text="textBody"
      searchable
      :language="codeLanguage"
      class="preview-code-wrap"
    />

    <!-- XML / CSS / JS / 纯文本：editable 时用 CodeEditor，否则 TextSearch -->
    <CodeEditor
      v-else-if="(kind === 'xml' || kind === 'css' || kind === 'js' || kind === 'text') && editableLang"
      :model-value="textBody"
      :language="editableLang"
      :min-height="'200px'"
      class="preview-code-wrap"
      @update:model-value="onInput"
    />
    <TextSearch
      v-else-if="kind === 'xml' || kind === 'css' || kind === 'js' || kind === 'text'"
      :text="textBody"
      searchable
      :language="codeLanguage"
      class="preview-code-wrap"
    />

    <!-- 其他二进制 -->
    <div v-else class="preview-center">
      <el-icon :size="40"><Document /></el-icon>
      <div class="text-dim" style="margin-top: 10px">无法预览此内容类型</div>
      <div class="preview-meta text-dim">{{ parsed.main || '未知类型' }} · {{ fmtSize(bodySize) }}</div>
      <div class="text-dim" style="margin-top: 6px; font-size: 11px">请切换到 Hex 标签查看原始字节</div>
    </div>
  </div>
</template>

<style scoped>
.preview-view { padding: 10px; background: var(--on-bg); height: 100%; }
.empty-text { text-align: center; padding: 24px; }

.preview-center {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  min-height: 200px; gap: 8px;
}
.preview-img {
  max-width: 100%; max-height: 70vh;
  border: 1px solid var(--on-border-light); border-radius: 4px;
  background: #fff;
}
.preview-video { max-width: 100%; max-height: 70vh; }
.preview-meta { font-size: 12px; margin-top: 4px; }

.preview-html { height: 100%; display: flex; }
.html-frame {
  flex: 1; width: 100%; height: 100%;
  border: 1px solid var(--on-border-light); border-radius: 4px;
  background: #fff;
}

.preview-pdf { height: 100%; display: flex; }
.pdf-embed { flex: 1; width: 100%; height: 70vh; border: 1px solid var(--on-border-light); }

/* TextSearch 代码块容器：填满父容器，内部自带滚动+高亮+搜索悬浮窗 */
.preview-code-wrap {
  height: 100%;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: 4px;
}
</style>
