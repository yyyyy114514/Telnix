<script setup lang="ts">
// 通用导入按钮组件：读取本地 JSON/HAR 文件，覆盖导入流量
// 导入前询问是否保存当前会话（若当前有流量），导入时先清空所有流量再导入
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'
import { useFlowsStore } from '../stores/flows'
import { useCaptureStore } from '../stores/capture'

const emit = defineEmits<{ (e: 'imported', r: { session_id: number; imported: number }): void }>()
const flows = useFlowsStore()
const capture = useCaptureStore()

const fileInput = ref<HTMLInputElement | null>(null)
const importing = ref(false)

// 触发导入流程：先询问是否保存当前会话，再选文件覆盖导入
async function pickFile() {
  // 当前有流量时询问是否保存
  const hasFlows = flows.total > 0
  if (hasFlows) {
    try {
      await ElMessageBox.confirm(
        '导入新流量将覆盖当前所有流量。是否先保存当前会话？',
        '导入确认',
        {
          confirmButtonText: '保存并导入',
          cancelButtonText: '直接导入',
          distinguishCancelAndClose: true,
          type: 'warning',
        }
      )
      // 确认：保存并导入
      await saveCurrentSession()
    } catch (action) {
      // action === 'close'：用户点 X 关闭，中止整个导入
      if (action === 'close') return
      // action === 'cancel'：直接导入，继续选文件
    }
  }
  fileInput.value?.click()
}

// 保存当前会话为 JSON 文件下载
async function saveCurrentSession() {
  const sid = capture.status.session_id
  if (!sid) {
    ElMessage.warning('当前无活动会话，跳过保存')
    return
  }
  try {
    const res: any = await api.exportSession(sid, 'json')
    const content = res?.content ?? res?.data
    const text = typeof content === 'string' ? content : JSON.stringify(content, null, 2)
    const blob = new Blob([text], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const ts = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    const fname = `telnix_${ts.getFullYear()}${pad(ts.getMonth() + 1)}${pad(ts.getDate())}_${pad(ts.getHours())}${pad(ts.getMinutes())}${pad(ts.getSeconds())}.json`
    const a = document.createElement('a')
    a.href = url
    a.download = fname
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    ElMessage.success(`已保存当前会话：${fname}`)
  } catch (e: any) {
    ElMessage.error('保存会话失败：' + (e?.message || e))
  }
}

async function onFileChange(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  // 防重入：导入进行中时拒绝再次选择文件（input 仍可触发）
  if (importing.value) {
    target.value = ''
    return
  }
  // 根据扩展名判断格式
  const name = file.name.toLowerCase()
  let format = 'json'
  if (name.endsWith('.har')) format = 'har'
  else if (name.endsWith('.json')) format = 'json'
  else {
    ElMessage.warning('请选择 .json 或 .har 文件')
    target.value = ''
    return
  }
  // 读取文件内容
  const text = await file.text()
  importing.value = true
  try {
    // 先清空当前所有流量（覆盖导入）
    await api.clearAllFlows('all')
    // 前端立即清空列表，避免界面闪烁旧数据
    flows.clear()
    // 再导入新流量
    const r = await api.importFlows({ format, content: text, session_name: `导入 ${file.name}` })
    ElMessage.success(`已导入 ${r.imported} 条流量（覆盖原数据，会话 #${r.session_id}）`)
    // 刷新流量列表
    await flows.loadAllFlows()
    emit('imported', { session_id: r.session_id, imported: r.imported })
  } catch (e: any) {
    ElMessage.error('导入失败：' + (e?.message || e))
    // 失败时也刷新一次，保证界面与后端一致
    await flows.loadAllFlows()
  } finally {
    importing.value = false
    target.value = ''  // 允许重复导入同一文件
  }
}
</script>

<template>
  <el-button size="small" :loading="importing" @click="pickFile">
    <el-icon><Upload /></el-icon>&nbsp;导入
  </el-button>
  <input
    ref="fileInput"
    type="file"
    accept=".json,.har"
    style="display: none"
    @change="onFileChange"
  />
</template>
