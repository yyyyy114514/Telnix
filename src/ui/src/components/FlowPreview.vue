<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { type Flow } from '../api/client'

const props = defineProps<{
  flow: Flow | null
  position: { x: number; y: number }
}>()

const emit = defineEmits<{
  'close': []
}>()

const { t } = useI18n()

// 内容类型检测
const contentType = computed(() => {
  if (!props.flow?.response_headers) return ''
  try {
    const headers = JSON.parse(props.flow.response_headers)
    return headers['Content-Type'] || headers['content-type'] || ''
  } catch {
    return ''
  }
})

const isImage = computed(() => {
  return contentType.value.startsWith('image/')
})

const isJson = computed(() => {
  return contentType.value.includes('json') ||
         (props.flow?.response_body && tryParseJson(props.flow.response_body) !== null)
})

const isHtml = computed(() => {
  return contentType.value.includes('html')
})

const isText = computed(() => {
  const ct = contentType.value
  return ct.startsWith('text/') || ct.includes('xml') || ct.includes('javascript')
})

// 解析 JSON
function tryParseJson(str: string): any {
  try {
    return JSON.parse(str)
  } catch {
    return null
  }
}

// 格式化 JSON 用于预览
const formattedJson = computed(() => {
  if (!props.flow?.response_body) return null
  const parsed = tryParseJson(props.flow.response_body)
  if (parsed === null) return null
  return JSON.stringify(parsed, null, 2)
})

// 提取关键字段（JSON 预览）
const jsonPreview = computed(() => {
  if (!props.flow?.response_body) return []
  const parsed = tryParseJson(props.flow.response_body)
  if (parsed === null || typeof parsed !== 'object') return []

  const entries: { key: string; value: string; type: string }[] = []
  const maxEntries = 10

  const addEntries = (obj: any, prefix = '') => {
    if (entries.length >= maxEntries) return
    if (Array.isArray(obj)) {
      obj.slice(0, maxEntries).forEach((item, idx) => {
        if (typeof item === 'object') {
          addEntries(item, `${prefix}[${idx}]`)
        } else {
          entries.push({
            key: `${prefix}[${idx}]`,
            value: String(item).slice(0, 100),
            type: typeof item,
          })
        }
      })
    } else if (typeof obj === 'object' && obj !== null) {
      Object.entries(obj).slice(0, maxEntries).forEach(([key, val]) => {
        if (typeof val === 'object') {
          addEntries(val as any, prefix ? `${prefix}.${key}` : key)
        } else {
          entries.push({
            key: prefix ? `${prefix}.${key}` : key,
            value: String(val).slice(0, 100),
            type: typeof val,
          })
        }
      })
    }
  }

  addEntries(parsed)
  return entries
})

// 图片预览 URL
const imageUrl = computed(() => {
  const body = props.flow?.response_body || ''
  if (!body.startsWith('base64:')) return ''
  const b64 = body.slice(7)
  const ct = contentType.value || 'image/png'
  const mime = ct.split(';')[0]
  return `data:${mime};base64,${b64}`
})

// 文本预览（HTML/纯文本）
const textPreview = computed(() => {
  if (!props.flow?.response_body) return ''
  // 截取前 2000 字符
  const body = props.flow.response_body
  if (body.length > 2000) {
    return body.slice(0, 2000) + '\n... (truncated)'
  }
  return body
})

// 复制内容
function copyContent() {
  if (!props.flow) return
  const content = props.flow.response_body || props.flow.request_body || ''
  navigator.clipboard.writeText(content).then(() => {
    ElMessage.success(t('common.copied'))
  }).catch(() => {
    ElMessage.error(t('common.copyFailed'))
  })
}

// 保存图片
function saveImage() {
  if (!imageUrl.value || !props.flow) return
  const a = document.createElement('a')
  a.href = imageUrl.value
  const ct = contentType.value || 'image.png'
  const ext = ct.split('/')[1]?.split(';')[0] || 'png'
  a.download = `preview_${props.flow.id}.${ext}`
  a.click()
}

// 关闭
function handleClose() {
  emit('close')
}

// 点击外部关闭
function handleClickOutside(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.closest('.flow-preview')) {
    handleClose()
  }
}

onMounted(() => {
  document.addEventListener('click', handleClickOutside, true)
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside, true)
})
</script>

<template>
  <div
    v-if="flow"
    class="flow-preview"
    :style="{
      left: position.x + 'px',
      top: position.y + 'px',
    }"
    @click.stop
  >
    <!-- 头部 -->
    <div class="preview-header">
      <div class="preview-title">
        <span class="preview-method">{{ flow.method }}</span>
        <span class="preview-path">{{ flow.host }}{{ flow.path }}</span>
      </div>
      <div class="preview-actions">
        <el-button size="small" text @click="copyContent" :title="t('common.copy')">
          <el-icon><CopyDocument /></el-icon>
        </el-button>
        <el-button v-if="isImage" size="small" text @click="saveImage" :title="t('common.save')">
          <el-icon><Download /></el-icon>
        </el-button>
        <el-button size="small" text @click="handleClose">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- 内容区 -->
    <div class="preview-body">
      <!-- 图片预览 -->
      <template v-if="isImage && imageUrl">
        <div class="preview-image-container">
          <img :src="imageUrl" :alt="t('flowPreview.imagePreview')" class="preview-image" />
        </div>
        <div class="preview-image-info">
          <span>{{ contentType }}</span>
          <span>{{ flow.size ? (flow.size / 1024).toFixed(1) + ' KB' : '' }}</span>
        </div>
      </template>

      <!-- JSON 预览 -->
      <template v-else-if="isJson && jsonPreview.length > 0">
        <div class="preview-json">
          <div class="preview-json-title">{{ t('flowPreview.jsonPreview') }}</div>
          <div class="preview-json-list">
            <div
              v-for="(entry, idx) in jsonPreview"
              :key="idx"
              class="preview-json-entry"
            >
              <span class="json-key">{{ entry.key }}</span>
              <span class="json-colon">:</span>
              <span class="json-value" :class="'type-' + entry.type">{{ entry.value }}</span>
            </div>
          </div>
          <div v-if="formattedJson" class="preview-json-full">
            <pre>{{ formattedJson.slice(0, 500) }}{{ formattedJson.length > 500 ? '\n...' : '' }}</pre>
          </div>
        </div>
      </template>

      <!-- 文本预览 -->
      <template v-else-if="isText || flow.response_body">
        <div class="preview-text">
          <div class="preview-text-title">{{ t('flowPreview.textPreview') }}</div>
          <pre class="preview-text-content">{{ textPreview }}</pre>
        </div>
      </template>

      <!-- 无内容 -->
      <template v-else>
        <div class="preview-empty">
          {{ t('flowPreview.noPreview') }}
        </div>
      </template>
    </div>

    <!-- 状态信息 -->
    <div class="preview-footer">
      <span class="preview-status" :class="'status-' + (flow.status_code ? Math.floor(flow.status_code / 100) + 'xx' : 'null')">
        {{ flow.status_code || '...' }}
      </span>
      <span class="preview-size">{{ flow.size ? (flow.size > 1024 ? (flow.size / 1024).toFixed(1) + ' KB' : flow.size + ' B') : '-' }}</span>
      <span class="preview-time">{{ flow.duration_ms ? flow.duration_ms + 'ms' : '-' }}</span>
    </div>
  </div>
</template>

<style scoped>
.flow-preview {
  position: fixed;
  z-index: 9999;
  width: 560px;
  max-height: 400px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border);
  border-radius: var(--on-radius-lg);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: var(--on-bg);
  border-bottom: 1px solid var(--on-border-light);
  flex-shrink: 0;
}

.preview-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  overflow: hidden;
}

.preview-method {
  font-weight: 600;
  color: var(--on-accent);
  font-family: var(--on-font-mono, monospace);
  flex-shrink: 0;
}

.preview-path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-text);
}

.preview-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}

.preview-body {
  flex: 1;
  overflow: auto;
  min-height: 0;
}

/* 图片预览 */
.preview-image-container {
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 12px;
  background: repeating-conic-gradient(#333 0% 25%, #444 0% 50%) 50% / 16px 16px;
  min-height: 150px;
}

.preview-image {
  max-width: 100%;
  max-height: 250px;
  object-fit: contain;
  border-radius: 4px;
}

.preview-image-info {
  display: flex;
  justify-content: space-between;
  padding: 6px 12px;
  font-size: 11px;
  color: var(--on-text-muted);
  background: var(--on-bg);
  border-top: 1px solid var(--on-border-light);
}

/* JSON 预览 */
.preview-json {
  padding: 12px;
}

.preview-json-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--on-text-muted);
  margin-bottom: 8px;
}

.preview-json-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.preview-json-entry {
  display: flex;
  align-items: baseline;
  gap: 4px;
  font-family: var(--on-font-mono, monospace);
  font-size: 11px;
  line-height: 1.4;
}

.json-key {
  color: var(--el-color-primary);
  flex-shrink: 0;
}

.json-colon {
  color: var(--on-text-muted);
}

.json-value {
  color: var(--on-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.json-value.type-string {
  color: var(--el-color-success);
}

.json-value.type-number {
  color: var(--el-color-warning);
}

.json-value.type-boolean {
  color: var(--el-color-info);
}

.preview-json-full {
  margin-top: 8px;
  padding: 8px;
  background: var(--on-bg);
  border-radius: 4px;
  max-height: 150px;
  overflow: auto;
}

.preview-json-full pre {
  margin: 0;
  font-family: var(--on-font-mono, monospace);
  font-size: 10px;
  color: var(--on-text-muted);
  white-space: pre-wrap;
  word-break: break-all;
}

/* 文本预览 */
.preview-text {
  padding: 12px;
}

.preview-text-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--on-text-muted);
  margin-bottom: 8px;
}

.preview-text-content {
  margin: 0;
  padding: 8px;
  background: var(--on-bg);
  border-radius: 4px;
  font-family: var(--on-font-mono, monospace);
  font-size: 11px;
  line-height: 1.5;
  max-height: 200px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

/* 空状态 */
.preview-empty {
  text-align: center;
  padding: 30px;
  color: var(--on-text-muted);
  font-size: 12px;
}

/* 底部状态栏 */
.preview-footer {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 12px;
  background: var(--on-bg);
  border-top: 1px solid var(--on-border-light);
  font-size: 11px;
  flex-shrink: 0;
}

.preview-status {
  font-weight: 600;
  font-family: var(--on-font-mono, monospace);
}

.preview-status.status-2xx {
  color: var(--el-color-success);
}

.preview-status.status-3xx {
  color: var(--el-color-primary);
}

.preview-status.status-4xx {
  color: var(--el-color-warning);
}

.preview-status.status-5xx {
  color: var(--el-color-danger);
}

.preview-size,
.preview-time {
  color: var(--on-text-muted);
}
</style>
