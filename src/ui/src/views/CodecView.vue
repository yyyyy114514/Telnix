<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'

// 编解码工具：选择类型 + 加密/解密按钮（哈希只有加密）
const input = ref('')
const output = ref('')
const type = ref('base64')

const types = [
  { value: 'base64', label: 'Base64', hasDecode: true },
  { value: 'url', label: 'URL', hasDecode: true },
  { value: 'hex', label: 'Hex', hasDecode: true },
  { value: 'md5', label: 'MD5', hasDecode: false },
  { value: 'sha256', label: 'SHA256', hasDecode: false },
]

const currentType = computed(() => types.find(t => t.value === type.value) || types[0])

function utf8ToBytes(str: string): number[] {
  const encoder = new TextEncoder()
  return Array.from(encoder.encode(str))
}
function bytesToUtf8(bytes: number[]): string {
  const decoder = new TextDecoder()
  return decoder.decode(new Uint8Array(bytes))
}

function toHex(bytes: number[]): string {
  return bytes.map((b) => b.toString(16).padStart(2, '0')).join('')
}
function fromHex(str: string): number[] {
  const clean = str.replace(/\s+/g, '')
  const out: number[] = []
  for (let i = 0; i < clean.length; i += 2) {
    out.push(parseInt(clean.slice(i, i + 2), 16))
  }
  return out
}

async function hash(algo: string): Promise<string> {
  const bytes = utf8ToBytes(input.value)
  const alg = algo === 'md5' ? 'MD5' : 'SHA-256'
  try {
    const buf = await crypto.subtle.digest(alg as any, new Uint8Array(bytes))
    return toHex(Array.from(new Uint8Array(buf)))
  } catch {
    if (algo === 'md5') return md5(input.value)
    throw new Error('不支持该哈希算法')
  }
}

// MD5 纯实现（公共领域算法）
function md5(str: string): string {
  function toBytes(s: string): number[] {
    return utf8ToBytes(s)
  }
  const bytes = toBytes(str)
  function rl(n: number, c: number): number {
    return (n << c) | (n >>> (32 - c))
  }
  function add32(a: number, b: number): number {
    return (a + b) & 0xffffffff
  }
  function cmn(q: number, a: number, b: number, x: number, s: number, t: number): number {
    a = add32(add32(a, q), add32(x, t))
    return add32(rl(a, s), b)
  }
  function ff(a: number, b: number, c: number, d: number, x: number, s: number, t: number): number {
    return cmn((b & c) | (~b & d), a, b, x, s, t)
  }
  function gg(a: number, b: number, c: number, d: number, x: number, s: number, t: number): number {
    return cmn((b & d) | (c & ~d), a, b, x, s, t)
  }
  function hh(a: number, b: number, c: number, d: number, x: number, s: number, t: number): number {
    return cmn(b ^ c ^ d, a, b, x, s, t)
  }
  function ii(a: number, b: number, c: number, d: number, x: number, s: number, t: number): number {
    return cmn(c ^ (b | ~d), a, b, x, s, t)
  }
  function toHexStr(n: number): string {
    let s = ''
    for (let j = 0; j < 4; j++) {
      s += ((n >> (j * 8 + 4)) & 0xf).toString(16) + ((n >> (j * 8)) & 0xf).toString(16)
    }
    return s
  }

  const origLen = bytes.length
  bytes.push(0x80)
  while (bytes.length % 64 !== 56) bytes.push(0)
  const bits = origLen * 8
  for (let i = 0; i < 8; i++) {
    bytes.push((bits >>> (i * 8)) & 0xff)
  }

  let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476
  const S: number[] = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
  ]
  const K: number[] = []
  for (let i = 0; i < 64; i++) K.push(Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32))

  for (let off = 0; off < bytes.length; off += 64) {
    const M: number[] = []
    for (let j = 0; j < 16; j++) {
      M.push(
        (bytes[off + j * 4]) |
          (bytes[off + j * 4 + 1] << 8) |
          (bytes[off + j * 4 + 2] << 16) |
          (bytes[off + j * 4 + 3] << 24)
      )
    }
    let A = a0, B = b0, C = c0, D = d0
    for (let i = 0; i < 64; i++) {
      let F: number, g: number
      if (i < 16) { F = ff(A, B, C, D, M[g = i], S[i], K[i]) }
      else if (i < 32) { F = gg(A, B, C, D, M[g = (5 * i + 1) % 16], S[i], K[i]) }
      else if (i < 48) { F = hh(A, B, C, D, M[g = (3 * i + 5) % 16], S[i], K[i]) }
      else { F = ii(A, B, C, D, M[g = (7 * i) % 16], S[i], K[i]) }
      A = D; D = C; C = B; B = F
    }
    a0 = add32(a0, A)
    b0 = add32(b0, B)
    c0 = add32(c0, C)
    d0 = add32(d0, D)
  }
  return toHexStr(a0) + toHexStr(b0) + toHexStr(c0) + toHexStr(d0)
}

// 加密（编码）
async function encode() {
  try {
    const t = type.value
    if (t === 'base64') {
      // btoa 不支持非 Latin1 字符（如中文），需先用 TextEncoder 编码为 UTF-8 字节再转 Latin1 字符串
      const bytes = new TextEncoder().encode(input.value)
      let binStr = ''
      for (let i = 0; i < bytes.length; i++) binStr += String.fromCharCode(bytes[i])
      output.value = btoa(binStr)
    } else if (t === 'url') {
      output.value = encodeURIComponent(input.value)
    } else if (t === 'hex') {
      output.value = toHex(utf8ToBytes(input.value))
    } else if (t === 'md5') {
      output.value = await hash('md5')
    } else if (t === 'sha256') {
      output.value = await hash('sha256')
    }
  } catch (e: any) {
    ElMessage.error('编码失败：' + (e?.message || e))
    output.value = ''
  }
}

// 解密（解码）
async function decode() {
  try {
    const t = type.value
    if (t === 'base64') {
      output.value = bytesToUtf8(Array.from(Uint8Array.from(atob(input.value), (c) => c.charCodeAt(0))))
    } else if (t === 'url') {
      output.value = decodeURIComponent(input.value)
    } else if (t === 'hex') {
      output.value = bytesToUtf8(fromHex(input.value))
    } else {
      ElMessage.warning(currentType.value.label + ' 不支持解码')
    }
  } catch (e: any) {
    ElMessage.error('解码失败：' + (e?.message || e))
    output.value = ''
  }
}

function swap() {
  const t = input.value
  input.value = output.value
  output.value = t
}

function clearAll() {
  input.value = ''
  output.value = ''
}

function copyOutput() {
  navigator.clipboard.writeText(output.value)
  ElMessage.success('已复制')
}
</script>

<template>
  <div class="codec-view full flex flex-col">
    <div class="page-header">
      <div class="page-title"><el-icon><Key /></el-icon>&nbsp;编解码工具</div>
      <el-button size="small" @click="clearAll">清空</el-button>
    </div>

    <div class="flex-1 overflow-auto codec-body">
      <div class="codec-controls">
        <span class="text-muted">类型：</span>
        <el-select v-model="type" style="width: 140px" size="default">
          <el-option v-for="t in types" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>
        <el-button type="primary" @click="encode">
          <el-icon><Top /></el-icon>&nbsp;{{ currentType.hasDecode ? '加密' : '计算' }}
        </el-button>
        <el-button v-if="currentType.hasDecode" type="success" @click="decode">
          <el-icon><Bottom /></el-icon>&nbsp;解密
        </el-button>
        <el-button @click="swap">⇅ 交换</el-button>
        <div class="flex-1"></div>
        <el-button size="small" @click="copyOutput" :disabled="!output">复制结果</el-button>
      </div>

      <div class="codec-panes">
        <div class="codec-pane">
          <div class="pane-label">输入</div>
          <el-input v-model="input" type="textarea" :rows="14" resize="vertical" class="mono" placeholder="在此输入要编解码的文本…" />
        </div>
        <div class="codec-pane">
          <div class="pane-label">输出</div>
          <el-input :model-value="output" type="textarea" :rows="14" readonly resize="vertical" class="mono" placeholder="结果…" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.codec-view { background: var(--on-bg); }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--on-border-light);
}
.page-title { font-size: 15px; font-weight: 600; display: flex; align-items: center; }
.codec-body { padding: 16px; }
.codec-controls { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.codec-panes { display: flex; gap: 16px; }
.codec-pane { flex: 1; display: flex; flex-direction: column; }
.pane-label { font-size: 12px; color: var(--on-text-muted); margin-bottom: 6px; }
.codec-pane :deep(.el-textarea__inner) { font-family: var(--on-font-mono); font-size: 12.5px; }
</style>
