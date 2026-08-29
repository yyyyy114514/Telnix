import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

// 搜索历史记录数据结构
export interface SearchHistoryItem {
  id: string        // 唯一标识（时间戳+随机数）
  timestamp: number // 记录时间
  // 搜索条件快照
  bodyRegex: string
  binaryHex: string
  filterMethod: string
  filterStatusCode: number | null
  filterStatusMin: number | null
  filterStatusMax: number | null
  filterHost: string
  filterPid: number | null
  filterProcess: string
  filterHeaderRegex: string
  // 搜索条件描述（用于显示）
  displayText: string
}

// localStorage 持久化 key
const STORAGE_KEY = 'telnix_search_history'
// 最大历史记录数
const MAX_HISTORY = 50

/**
 * 搜索历史 Store
 * - 保存搜索条件到 localStorage
 * - 支持添加、删除、清空历史记录
 * - 支持快速回填搜索条件
 */
export const useSearchHistoryStore = defineStore('searchHistory', () => {
  // 搜索历史列表（按时间倒序，最新的在前）
  const history = ref<SearchHistoryItem[]>([])

  // 从 localStorage 加载历史记录
  function loadFromStorage() {
    try {
      const data = localStorage.getItem(STORAGE_KEY)
      if (data) {
        const parsed = JSON.parse(data)
        if (Array.isArray(parsed)) {
          history.value = parsed
        }
      }
    } catch {
      // 忽略解析错误
    }
  }

  // 保存历史记录到 localStorage
  function saveToStorage() {
    try {
      // 限制最大条数
      const toSave = history.value.slice(0, MAX_HISTORY)
      localStorage.setItem(STORAGE_KEY, JSON.stringify(toSave))
    } catch {
      // localStorage 满时忽略
    }
  }

  // 初始化：加载历史记录
  loadFromStorage()

  // 监听变化自动保存
  watch(history, saveToStorage, { deep: true })

  /**
   * 添加新的搜索历史记录
   * @param params 搜索参数
   * @returns 新记录的 ID
   */
  function addHistory(params: {
    bodyRegex?: string
    binaryHex?: string
    filterMethod?: string
    filterStatusCode?: number | null
    filterStatusMin?: number | null
    filterStatusMax?: number | null
    filterHost?: string
    filterPid?: number | null
    filterProcess?: string
    filterHeaderRegex?: string
  }): string {
    // 生成显示文本
    const parts: string[] = []
    if (params.bodyRegex) parts.push(`正则: ${params.bodyRegex}`)
    if (params.binaryHex) parts.push(`Hex: ${params.binaryHex}`)
    if (params.filterHost) parts.push(`Host: ${params.filterHost}`)
    if (params.filterMethod) parts.push(`Method: ${params.filterMethod}`)
    if (params.filterStatusCode !== undefined && params.filterStatusCode !== null) {
      parts.push(`Status: ${params.filterStatusCode}`)
    }
    if (params.filterStatusMin !== undefined && params.filterStatusMin !== null) {
      parts.push(`Status ${params.filterStatusMin}-${params.filterStatusMax ?? ''}`)
    }
    if (params.filterProcess) parts.push(`Process: ${params.filterProcess}`)
    if (params.filterHeaderRegex) parts.push(`Header: ${params.filterHeaderRegex}`)
    if (params.filterPid !== undefined && params.filterPid !== null) {
      parts.push(`PID: ${params.filterPid}`)
    }

    const displayText = parts.length > 0 ? parts.join(' | ') : '(空条件)'

    // 检查是否与最近一条完全相同（避免重复）
    if (history.value.length > 0) {
      const last = history.value[0]
      if (last.bodyRegex === (params.bodyRegex || '') &&
          last.binaryHex === (params.binaryHex || '') &&
          last.filterMethod === (params.filterMethod || '') &&
          last.filterStatusCode === (params.filterStatusCode ?? null) &&
          last.filterStatusMin === (params.filterStatusMin ?? null) &&
          last.filterStatusMax === (params.filterStatusMax ?? null) &&
          last.filterHost === (params.filterHost || '') &&
          last.filterPid === (params.filterPid ?? null) &&
          last.filterProcess === (params.filterProcess || '') &&
          last.filterHeaderRegex === (params.filterHeaderRegex || '')) {
        // 更新最新记录的时间戳
        last.timestamp = Date.now()
        return last.id
      }
    }

    // 创建新记录
    const newItem: SearchHistoryItem = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      timestamp: Date.now(),
      bodyRegex: params.bodyRegex || '',
      binaryHex: params.binaryHex || '',
      filterMethod: params.filterMethod || '',
      filterStatusCode: params.filterStatusCode ?? null,
      filterStatusMin: params.filterStatusMin ?? null,
      filterStatusMax: params.filterStatusMax ?? null,
      filterHost: params.filterHost || '',
      filterPid: params.filterPid ?? null,
      filterProcess: params.filterProcess || '',
      filterHeaderRegex: params.filterHeaderRegex || '',
      displayText,
    }

    // 插入到列表头部
    history.value.unshift(newItem)

    // 限制最大条数
    if (history.value.length > MAX_HISTORY) {
      history.value = history.value.slice(0, MAX_HISTORY)
    }

    return newItem.id
  }

  /**
   * 删除单条历史记录
   * @param id 历史记录 ID
   */
  function removeHistory(id: string) {
    const idx = history.value.findIndex(h => h.id === id)
    if (idx !== -1) {
      history.value.splice(idx, 1)
    }
  }

  /**
   * 清空所有历史记录
   */
  function clearHistory() {
    history.value = []
  }

  /**
   * 根据 ID 获取历史记录
   * @param id 历史记录 ID
   */
  function getHistory(id: string): SearchHistoryItem | undefined {
    return history.value.find(h => h.id === id)
  }

  return {
    history,
    addHistory,
    removeHistory,
    clearHistory,
    getHistory,
  }
})
