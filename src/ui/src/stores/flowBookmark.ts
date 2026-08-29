import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export interface BookmarkGroup {
  id: string
  name: string
  color: string
  created_at: string
}

export interface FlowBookmark {
  id: string
  name: string
  flow_id: number
  group_id: string
  note: string
  created_at: string
}

const GROUPS_KEY = 'telnix_bookmark_groups'
const BOOKMARKS_KEY = 'telnix_bookmarks'

export const useFlowBookmarkStore = defineStore('flowBookmark', () => {
  // 书签组
  const groups = ref<BookmarkGroup[]>([])

  // 书签列表
  const bookmarks = ref<FlowBookmark[]>([])

  // 持久化
  function persist() {
    localStorage.setItem(GROUPS_KEY, JSON.stringify(groups.value))
    localStorage.setItem(BOOKMARKS_KEY, JSON.stringify(bookmarks.value))
  }

  // 加载
  function load() {
    try {
      const savedGroups = localStorage.getItem(GROUPS_KEY)
      if (savedGroups) {
        groups.value = JSON.parse(savedGroups)
      } else {
        // 默认组
        groups.value = [
          { id: 'default', name: '默认', color: '#3b82f6', created_at: new Date().toISOString() },
          { id: 'important', name: '重要', color: '#f43f5e', created_at: new Date().toISOString() },
        ]
      }
      const savedBookmarks = localStorage.getItem(BOOKMARKS_KEY)
      if (savedBookmarks) {
        bookmarks.value = JSON.parse(savedBookmarks)
      }
    } catch { /* ignore */ }
  }

  // 组管理
  function addGroup(name: string, color: string = '#3b82f6'): BookmarkGroup {
    const group: BookmarkGroup = {
      id: 'group_' + Date.now(),
      name,
      color,
      created_at: new Date().toISOString(),
    }
    groups.value.push(group)
    persist()
    return group
  }

  function updateGroup(id: string, updates: Partial<Omit<BookmarkGroup, 'id' | 'created_at'>>) {
    const group = groups.value.find(g => g.id === id)
    if (group) {
      Object.assign(group, updates)
      persist()
    }
  }

  function deleteGroup(id: string) {
    // 不允许删除默认组
    if (id === 'default') return
    groups.value = groups.value.filter(g => g.id !== id)
    // 同时删除组内的书签
    bookmarks.value = bookmarks.value.filter(b => b.group_id !== id)
    persist()
  }

  // 书签管理
  function addBookmark(name: string, flowId: number, groupId: string = 'default', note: string = ''): FlowBookmark {
    // 检查是否已存在该 flow 的书签
    const existing = bookmarks.value.find(b => b.flow_id === flowId)
    if (existing) {
      // 更新已有书签
      existing.name = name
      existing.group_id = groupId
      existing.note = note
      persist()
      return existing
    }

    const bookmark: FlowBookmark = {
      id: 'bookmark_' + Date.now(),
      name,
      flow_id: flowId,
      group_id: groupId,
      note,
      created_at: new Date().toISOString(),
    }
    bookmarks.value.push(bookmark)
    persist()
    return bookmark
  }

  function updateBookmark(id: string, updates: Partial<Omit<FlowBookmark, 'id' | 'flow_id' | 'created_at'>>) {
    const bookmark = bookmarks.value.find(b => b.id === id)
    if (bookmark) {
      Object.assign(bookmark, updates)
      persist()
    }
  }

  function deleteBookmark(id: string) {
    bookmarks.value = bookmarks.value.filter(b => b.id !== id)
    persist()
  }

  // 移动书签到组
  function moveBookmarkToGroup(bookmarkId: string, groupId: string) {
    const bookmark = bookmarks.value.find(b => b.id === bookmarkId)
    if (bookmark) {
      bookmark.group_id = groupId
      persist()
    }
  }

  // 检查 flow 是否已书签
  function isFlowBookmarked(flowId: number): boolean {
    return bookmarks.value.some(b => b.flow_id === flowId)
  }

  // 获取 flow 的书签
  function getFlowBookmark(flowId: number): FlowBookmark | undefined {
    return bookmarks.value.find(b => b.flow_id === flowId)
  }

  // 获取组的书签
  function getGroupBookmarks(groupId: string): FlowBookmark[] {
    return bookmarks.value.filter(b => b.group_id === groupId)
  }

  // 计算属性
  const bookmarkCount = computed(() => bookmarks.value.length)

  // 初始化
  load()

  return {
    groups,
    bookmarks,
    addGroup,
    updateGroup,
    deleteGroup,
    addBookmark,
    updateBookmark,
    deleteBookmark,
    moveBookmarkToGroup,
    isFlowBookmarked,
    getFlowBookmark,
    getGroupBookmarks,
    bookmarkCount,
  }
})
