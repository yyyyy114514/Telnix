<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Flow } from '../api/client'
import TextSearch from './TextSearch.vue'
import CodeEditor from './CodeEditor.vue'

// 原始报文标签页：请求行+头+空行+体 / 状态行+头+空行+体
const props = defineProps<{
  flow: Flow
  type: 'request' | 'response' | 'ws'
  editableBody?: string
}>()
const emit = defineEmits<{ 'update:editableBody': [string] }>()

const editBody = ref(props.editableBody ?? '')
watch(
  () => props.editableBody,
  (v) => {
    editBody.value = v ?? ''
  }
)

function parseHeaders(str: string | null): string {
  if (!str) return ''
  try {
    const obj = JSON.parse(str)
    if (obj && typeof obj === 'object') {
      return Object.entries(obj)
        .map(([k, v]) => `${k}: ${v}`)
        .join('\r\n')
    }
  } catch {
    /* 非 JSON */
  }
  return str
}

const rawText = computed(() => {
  const f = props.flow
  if (props.type === 'request') {
    const line = `${f.method} ${f.path || '/'} HTTP/1.1`
    const headers = parseHeaders(f.request_headers)
    const body = f.request_body || ''
    return [line, headers, '', body].filter((_, i) => i < 3 || body).join('\r\n')
  } else {
    const code = f.status_code ?? 0
    const line = `HTTP/1.1 ${code} ${statusText(code)}`
    const headers = parseHeaders(f.response_headers)
    const body = f.response_body || ''
    return [line, headers, '', body].filter((_, i) => i < 3 || body).join('\r\n')
  }
})

function statusText(code: number): string {
  const map: Record<number, string> = {
    200: 'OK', 201: 'Created', 204: 'No Content', 301: 'Moved Permanently',
    302: 'Found', 304: 'Not Modified', 400: 'Bad Request', 401: 'Unauthorized',
    403: 'Forbidden', 404: 'Not Found', 500: 'Internal Server Error',
    502: 'Bad Gateway', 503: 'Service Unavailable',
  }
  return map[code] || ''
}

function onInput(v: string) {
  emit('update:editableBody', v)
}
</script>

<template>
  <div class="raw-view full">
    <CodeEditor
      v-if="editableBody !== undefined"
      :model-value="editBody"
      language="http"
      :min-height="'200px'"
      @update:model-value="onInput"
    />
    <TextSearch v-else :text="rawText" searchable language="http" />
  </div>
</template>

<style scoped>
.raw-view { padding: 8px; height: 100%; }
</style>
