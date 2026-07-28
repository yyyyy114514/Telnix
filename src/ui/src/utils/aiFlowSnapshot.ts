/**
 * AI 流量快照本地持久化（IndexedDB）
 *
 * 用途：用户给 AI 发送流量后，即使后端重启导致抓包列表清空，
 * 前端 AI 页面右侧「包信息面板」仍能从本地快照恢复显示当时的流量内容。
 *
 * 设计：
 * - key = flow_id（number），value = 完整 Flow 对象 + saved_at 时间戳
 * - 发送流量到 AI 前先存快照
 * - loadChatFlows 时若 api.getFlow 失败则回退到快照
 * - 超过 7 天的快照自动清理，避免无限增长
 */

import type { Flow } from '../api/client'

const DB_NAME = 'telnix-ai'
const STORE_NAME = 'flow-snapshots'
// v2: 新增 saved_at 索引，用于 cleanupExpired 用 IDBKeyRange + cursor 批量删除
const DB_VERSION = 2
const TTL_MS = 7 * 24 * 60 * 60 * 1000  // 7 天

let dbPromise: Promise<IDBDatabase> | null = null

function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') {
      reject(new Error('IndexedDB not supported'))
      return
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        // 全新创建：直接带 saved_at 索引
        const store = db.createObjectStore(STORE_NAME, { keyPath: 'flow_id' })
        store.createIndex('saved_at', 'saved_at', { unique: false })
      } else {
        // v1 升级到 v2：store 已存在但缺少 saved_at 索引，补建
        const tx = req.transaction
        if (tx) {
          const store = tx.objectStore(STORE_NAME)
          if (!store.indexNames.contains('saved_at')) {
            store.createIndex('saved_at', 'saved_at', { unique: false })
          }
        }
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
  return dbPromise
}

export async function saveFlowSnapshot(flow: Flow): Promise<void> {
  try {
    const db = await openDb()
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      const store = tx.objectStore(STORE_NAME)
      store.put({
        flow_id: flow.id,
        flow,
        saved_at: Date.now(),
      })
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
    // 顺便清理过期
    cleanupExpired().catch(() => { /* ignore */ })
  } catch {
    /* 静默失败：快照是 best-effort，不应阻塞主流程 */
  }
}

export async function getFlowSnapshot(flowId: number): Promise<Flow | null> {
  try {
    const db = await openDb()
    return await new Promise<Flow | null>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readonly')
      const store = tx.objectStore(STORE_NAME)
      const req = store.get(flowId)
      req.onsuccess = () => {
        const result = req.result
        if (!result || !result.flow || typeof result.flow.id !== 'number') {
          // 快照数据缺失或结构被污染时视为无快照，避免渲染异常对象
          resolve(null)
          return
        }
        resolve(result.flow as Flow)
      }
      req.onerror = () => reject(req.error)
    })
  } catch {
    return null
  }
}

async function cleanupExpired(): Promise<void> {
  try {
    const db = await openDb()
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      const store = tx.objectStore(STORE_NAME)
      const cutoff = Date.now() - TTL_MS
      // 性能优化：用 saved_at 索引 + IDBKeyRange 上界 + cursor 批量删除，
      // 避免原 getAll() 把全部快照读进内存再逐条 delete
      const idx = store.index('saved_at')
      const range = IDBKeyRange.upperBound(cutoff)
      const cursorReq = idx.openCursor(range)
      cursorReq.onsuccess = () => {
        const cursor = cursorReq.result
        if (cursor) {
          cursor.delete()
          cursor.continue()
        }
      }
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } catch {
    /* 静默失败：清理是 best-effort，不应阻塞主流程 */
  }
}
