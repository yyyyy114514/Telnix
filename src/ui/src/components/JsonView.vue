<script setup lang="ts">
import { computed, watch, ref } from 'vue'
import TextSearch from './TextSearch.vue'
import CodeEditor from './CodeEditor.vue'

// JSON 标签页：格式化 + 搜索，支持编辑（带语法高亮 + IDE 辅助）
const props = defineProps<{
  modelValue: string | null
  editable?: boolean
  lang?: 'json' | 'xml'
}>()
const emit = defineEmits<{ 'update:modelValue': [string] }>()

// 尝试美化 JSON（超长 JSON 跳过美化，避免主线程卡顿）
// 性能优化：阈值与 TextSearch HIGHLIGHT_LIMIT 对齐（100000），避免美化后超出高亮上限
const PRETTY_LIMIT = 100000
function pretty(str: string | null): string {
  if (!str) return ''
  // 超过 100KB 的 JSON 跳过美化，直接返回原文（TextSearch 会截断高亮）
  if (str.length > PRETTY_LIMIT) return str
  try {
    return JSON.stringify(JSON.parse(str), null, 2)
  } catch {
    return str
  }
}

const text = ref(pretty(props.modelValue))
watch(
  () => props.modelValue,
  (v) => {
    // 切换 flow 时外部 modelValue 变化，更新 text（CodeEditor 通过自己的 watch 同步）
    text.value = pretty(v)
  }
)

function onInput(v: string) {
  emit('update:modelValue', v)
}
</script>

<template>
  <div class="json-view full">
    <CodeEditor
      v-if="editable"
      :model-value="text"
      :language="lang || 'json'"
      :min-height="'180px'"
      @update:model-value="onInput"
    />
    <TextSearch v-else :text="text" searchable :language="lang || 'json'" />
  </div>
</template>

<style scoped>
.json-view { padding: 8px; }
</style>
