/**
 * IndexedDB-backed storage adapter for Zustand persist middleware.
 * 替代 localStorage，突破 5MB 上限，支持存储大量章节内容。
 *
 * ## 过期策略（可配置）
 * 为避免长期未打开导致数据丢失，过期策略设计如下：
 * - 只有「编辑器大内容」（key === 'nc-editor' 且 value.length > EDITOR_TTL_MIN_SIZE）
 *   才设置过期时间，用于清理长期未使用项目遗留的巨型编辑器数据；
 * - 其余数据（项目、聊天、设置等）永不过期；
 * - 默认有效期为 365 天（原为 30 天），可通过 createIndexedDBStorage({ editorTtlMs }) 调整，
 *   传 0 表示永不过期。
 *
 * ## 备份导出
 * exportStorageData() / downloadStorageBackup() 可将全部数据导出为 JSON 供手动备份，
 * 导出时会自动过滤敏感字段（如 API 密钥）。导出不改变任何现有数据结构与存取接口。
 */

interface StoredItem {
  value: string;
  expiry?: number;
}

/** 编辑器大内容默认有效期：365 天 */
export const DEFAULT_EDITOR_TTL_MS = 365 * 24 * 60 * 60 * 1000;

/** 超过该字节数的编辑器内容才启用过期清理 */
export const EDITOR_TTL_MIN_SIZE = 500000;

/** 导出备份时需要过滤的敏感字段（递归删除） */
const SENSITIVE_KEYS = ['apiKey'];

const DB_NAME = 'novelcraft-storage';
const STORE_NAME = 'zustand-persist';
const DB_VERSION = 1;

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function withStore(
  mode: IDBTransactionMode,
  callback: (store: IDBObjectStore) => IDBRequest | void,
): Promise<unknown> {
  // 先 await 打开连接，再交回同步的 Promise executor。
  // 原写法 `new Promise(async (resolve, reject) => …)` 依赖 try/catch 兜底，
  // 一旦以后有人在 try 之外提前 return/再抛错，Promise 会永远 pending（ESLint
  // `no-async-promise-executor` 就是拦这个）。这里把异步部分移出 executor，
  // 语义等价且不再依赖那块 try/catch。
  return openDB().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, mode);
        const store = tx.objectStore(STORE_NAME);
        const request = callback(store);
        tx.oncomplete = () => resolve(request ? (request as IDBRequest).result : undefined);
        tx.onerror = () => reject(tx.error);
      }),
  );
}

export interface IndexedDBStorageOptions {
  /** 编辑器大内容的有效期（毫秒）。默认 365 天；传 0 表示永不过期。 */
  editorTtlMs?: number;
}

/**
 * 创建一个兼容 Storage API 的 IndexedDB 存储对象，
 * 可直接传入 Zustand persist 的 createJSONStorage()。
 */
export function createIndexedDBStorage(options: IndexedDBStorageOptions = {}): {
  getItem: (key: string) => Promise<string | null>;
  setItem: (key: string, value: string) => Promise<void>;
  removeItem: (key: string) => Promise<void>;
} {
  const editorTtlMs = options.editorTtlMs ?? DEFAULT_EDITOR_TTL_MS;

  return {
    async getItem(key: string): Promise<string | null> {
      try {
        const result = await withStore('readonly', (store) => store.get(key));
        if (result === undefined || result === null) return null;
        const item = result as StoredItem;
        if (item.expiry && Date.now() > item.expiry) {
          await withStore('readwrite', (store) => store.delete(key));
          return null;
        }
        return item.value;
      } catch {
        return null;
      }
    },

    async setItem(key: string, value: string): Promise<void> {
      try {
        const item: StoredItem = { value };
        // 仅对编辑器大内容设置过期；editorTtlMs 传 0 表示永不过期
        if (key === 'nc-editor' && value.length > EDITOR_TTL_MIN_SIZE && editorTtlMs > 0) {
          item.expiry = Date.now() + editorTtlMs;
        }
        await withStore('readwrite', (store) => store.put(item, key));
      } catch {
        // 静默失败，store 仍可在内存中运行
      }
    },

    async removeItem(key: string): Promise<void> {
      try {
        await withStore('readwrite', (store) => store.delete(key));
      } catch {
        // 静默失败
      }
    },
  };
}

/** 递归删除对象中的敏感字段（就地修改，不影响 IndexedDB 原始数据）。 */
function removeSensitiveKeys(node: unknown): void {
  if (Array.isArray(node)) {
    node.forEach((item) => removeSensitiveKeys(item));
    return;
  }
  if (node && typeof node === 'object') {
    const record = node as Record<string, unknown>;
    for (const key of Object.keys(record)) {
      if (SENSITIVE_KEYS.includes(key)) {
        delete record[key];
      } else {
        removeSensitiveKeys(record[key]);
      }
    }
  }
}

/** 过滤单条值的敏感字段；非 JSON 数据原样返回。 */
function sanitizeValue(value: string): string {
  try {
    const parsed = JSON.parse(value);
    removeSensitiveKeys(parsed);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return value;
  }
}

/**
 * 导出全部存储数据（key -> 原始 JSON 字符串）。
 * 返回的副本已过滤敏感字段（如 API 密钥），不改变 IndexedDB 中的原始数据。
 */
export async function exportStorageData(): Promise<Record<string, string>> {
  const data: Record<string, string> = {};
  const db = await openDB();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const request = store.openCursor();
    request.onsuccess = () => {
      const cursor = request.result;
      if (cursor) {
        const item = cursor.value as StoredItem;
        if (item && typeof item.value === 'string') {
          data[String(cursor.key)] = sanitizeValue(item.value);
        }
        cursor.continue();
      } else {
        resolve();
      }
    };
    request.onerror = () => reject(request.error);
  });
  return data;
}

/**
 * 将全部数据导出为 JSON 文件并触发浏览器下载（手动备份）。
 * 文件名默认 novelcraft-backup-YYYYMMDD.json。
 */
export async function downloadStorageBackup(filename?: string): Promise<void> {
  const data = await exportStorageData();
  const date = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename || `novelcraft-backup-${date}.json`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}
