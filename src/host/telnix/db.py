"""SQLite 数据库：sessions / flows / auto_reply_rules / settings / ignored_processes 表。

每个操作创建独立连接并提交关闭，配合 WAL 模式与 busy_timeout 处理并发。
"""

import asyncio
import json
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .config import get_db_path

# 建表语句（不含 INDEX）：CREATE TABLE IF NOT EXISTS 不会修改已有表结构，
# 因此旧 db 文件需要靠 _migrate 补列。INDEX 语句单独放 SCHEMA_INDEXES，
# 在 _migrate 之后再执行（否则旧表缺列时 CREATE INDEX 会失败）。
SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    pid INTEGER,
    process_name TEXT,
    method TEXT,
    url TEXT,
    scheme TEXT,
    host TEXT,
    path TEXT,
    request_headers TEXT,
    request_body TEXT,
    status_code INTEGER,
    response_headers TEXT,
    response_body TEXT,
    duration_ms INTEGER,
    size INTEGER,
    breakpoint_status TEXT DEFAULT NULL,
    tags TEXT DEFAULT '',
    tag_note TEXT DEFAULT '',
    protocol TEXT DEFAULT 'http',
    raw_data TEXT DEFAULT NULL,
    src_port INTEGER DEFAULT NULL,
    dst_port INTEGER DEFAULT NULL,
    remote_ip TEXT DEFAULT NULL,
    ip_region TEXT DEFAULT NULL,
    cert_info TEXT DEFAULT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS auto_reply_rules (
    id TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    match_mode TEXT DEFAULT 'wildcard',
    pattern TEXT NOT NULL,
    action TEXT NOT NULL,
    mock_status INTEGER DEFAULT 200,
    mock_headers TEXT,
    mock_body TEXT,
    modify_rules TEXT,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delay_ms INTEGER DEFAULT 0,
    throttle_kbps INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ignored_processes (
    pid INTEGER PRIMARY KEY,
    process_name TEXT NOT NULL,
    ignored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ignored_hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_pattern TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    flow_ids TEXT NOT NULL,
    flow_context TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES ai_chats(id) ON DELETE CASCADE
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_flows_session ON flows(session_id);
CREATE INDEX IF NOT EXISTS idx_flows_host ON flows(host);
CREATE INDEX IF NOT EXISTS idx_flows_process ON flows(process_name);
CREATE INDEX IF NOT EXISTS idx_flows_protocol ON flows(protocol);
CREATE INDEX IF NOT EXISTS idx_flows_method ON flows(method);
CREATE INDEX IF NOT EXISTS idx_flows_ip_region ON flows(ip_region);
CREATE INDEX IF NOT EXISTS idx_flows_remote_ip ON flows(remote_ip);
CREATE INDEX IF NOT EXISTS idx_ai_messages_chat ON ai_messages(chat_id);
"""

# 默认设置项
DEFAULT_SETTINGS = {
    "deepseek_api_key": "",
    "deepseek_enabled": "0",
    "ai_consent": "0",
    "auto_reply_enabled": "0",
    "break_on_request": "0",
    "break_on_response": "0",
    "multi_select_bar_delay": "1",
    "show_raw_nav": "0",
    "show_search_nav": "0",
    "auto_switch_preview": "1",
    "auto_scroll": "1",
    "auto_scroll_delay": "10",
}


# 性能优化：threading.local 连接池，每个工作线程复用一个长连接
# 避免每次操作都 connect()+PRAGMA+commit()+close() 的开销（单次 1-3ms）
# WAL 模式 + synchronous=NORMAL 已在 init_db 持久设置，连接级只需设 busy_timeout
import threading

_local = threading.local()


def _get_thread_conn() -> sqlite3.Connection:
    """获取当前线程的复用连接（首次调用时建立，后续复用）。

    PRAGMA busy_timeout 只需在连接首次建立时设一次（连接级设置）。
    row_factory 也只需设一次。
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(get_db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _local.conn = conn
    return conn


@contextmanager
def get_connection():
    """获取数据库连接（threading.local 复用，自动提交）。

    性能优化：每个工作线程复用一个长连接，避免每次操作都 connect/close。
    WAL 模式是持久设置（init_db 时设一次），不需要每次连接都设。
    busy_timeout 是连接级设置，首次建立时设一次即可。
    """
    conn = _get_thread_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        # 出错时回滚但不关闭连接（连接可复用）
        conn.rollback()
        raise


def init_db():
    """初始化数据库与表，写入默认设置。

    默认启动时清空 flows 表并重置自增序列，避免 ID 无限累积。
    设置环境变量 TELNIX_KEEP_FLOWS=1 可保留历史流量。
    """
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # 先用独立连接设置 WAL 模式（持久设置，只需一次）
    _init_conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    try:
        _init_conn.execute("PRAGMA journal_mode=WAL")
        _init_conn.execute("PRAGMA synchronous=NORMAL")
        _init_conn.commit()
    finally:
        _init_conn.close()
    with get_connection() as conn:
        # 1. 先建表（IF NOT EXISTS 不改已有表，旧表缺列需要靠 _migrate 补）
        conn.executescript(SCHEMA_TABLES)
        # 2. 迁移：为旧表添加新列（CREATE TABLE IF NOT EXISTS 不会改已有表）
        _migrate(conn)
        # 3. 建索引（必须在 _migrate 之后，否则旧表缺 protocol 列时 CREATE INDEX 会失败）
        conn.executescript(SCHEMA_INDEXES)
        # 默认启动清空 flows 并重置自增序列（避免 ID 累积到上万）
        # 用环境变量 TELNIX_KEEP_FLOWS=1 可保留历史
        if os.environ.get("TELNIX_KEEP_FLOWS") != "1":
            conn.execute("DELETE FROM flows")
            # 重置 sqlite_sequence 让 id 从 1 重新开始
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name='flows'"
            )
            conn.execute("DELETE FROM sessions")
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name='sessions'"
            )
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v)
            )
    # 迁移 SQLite settings → settings.json（首次启动 JSON 不存在时）
    settings_store.migrate_from_sqlite_if_needed(_sqlite_get_all_settings)
    # 默认设置注入 settings.json（不覆盖用户已有值）
    _existing = settings_store.get_all_settings()
    for k, v in DEFAULT_SETTINGS.items():
        if k not in _existing or _existing.get(k) in (None, ""):
            settings_store.set_setting(k, v)


def _migrate(conn):
    """数据库迁移：补齐新字段。失败说明已存在，忽略。"""
    try:
        conn.execute("ALTER TABLE auto_reply_rules ADD COLUMN note TEXT DEFAULT ''")
    except Exception:  # noqa: BLE001
        pass
    # flows 表加 protocol 字段（http/tcp/udp），兼容旧数据默认 http
    try:
        conn.execute("ALTER TABLE flows ADD COLUMN protocol TEXT DEFAULT 'http'")
    except Exception:  # noqa: BLE001
        pass
    # flows 表加 raw_data 字段（TCP/UDP 原始字节，base64）
    try:
        conn.execute("ALTER TABLE flows ADD COLUMN raw_data TEXT DEFAULT NULL")
    except Exception:  # noqa: BLE001
        pass
    # flows 表加 src_port/dst_port 字段（TCP/UDP 用）
    try:
        conn.execute("ALTER TABLE flows ADD COLUMN src_port INTEGER DEFAULT NULL")
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.execute("ALTER TABLE flows ADD COLUMN dst_port INTEGER DEFAULT NULL")
    except Exception:  # noqa: BLE001
        pass
    # flows 表加 remote_ip/ip_region 字段（IP 属地分析）
    try:
        conn.execute("ALTER TABLE flows ADD COLUMN remote_ip TEXT DEFAULT NULL")
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.execute("ALTER TABLE flows ADD COLUMN ip_region TEXT DEFAULT NULL")
    except Exception:  # noqa: BLE001
        pass
    # flows 表加 cert_info 字段（TLS 证书信息 JSON）
    try:
        conn.execute("ALTER TABLE flows ADD COLUMN cert_info TEXT DEFAULT NULL")
    except Exception:  # noqa: BLE001
        pass
    # flows 表加 tags 字段（逗号分隔字符串，§3.1 流量标签）
    # 用 PRAGMA table_info 检查，避免依赖 ALTER TABLE 异常
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(flows)").fetchall()}
    if "tags" not in cols:
        conn.execute("ALTER TABLE flows ADD COLUMN tags TEXT DEFAULT ''")
    if "tag_note" not in cols:
        conn.execute("ALTER TABLE flows ADD COLUMN tag_note TEXT DEFAULT ''")
    # flows 表加 http_version 字段（HTTP/1.1 | HTTP/2）
    if "http_version" not in cols:
        conn.execute("ALTER TABLE flows ADD COLUMN http_version TEXT DEFAULT NULL")
    # auto_reply_rules 表加 4 个过滤字段（§4.1 规则匹配 method/status/pid/process 过滤）
    # 空字符串=不过滤，非空=必须匹配。method_filter/status_filter/pid_filter/process_filter
    # 支持逗号分隔多个值（如 method_filter="POST,PUT"）
    for col in ("method_filter", "status_filter", "pid_filter", "process_filter"):
        try:
            conn.execute(
                f"ALTER TABLE auto_reply_rules ADD COLUMN {col} TEXT DEFAULT ''"
            )
        except Exception:  # noqa: BLE001
            pass
    # auto_reply_rules 表加命中统计字段（§3.2 intercept 命中计数）
    try:
        conn.execute(
            "ALTER TABLE auto_reply_rules ADD COLUMN hit_count INTEGER DEFAULT 0"
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.execute(
            "ALTER TABLE auto_reply_rules ADD COLUMN last_hit_at TEXT DEFAULT ''"
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.execute(
            "ALTER TABLE auto_reply_rules ADD COLUMN last_hit_flow_id INTEGER DEFAULT NULL"
        )
    except Exception:  # noqa: BLE001
        pass
    # auto_reply_rules 表加 mock_request 相关字段（写死请求：用预设请求转发到目标服务器）
    # mock_method=HTTP方法(默认GET)，mock_url=完整请求URL（含https://）
    try:
        conn.execute(
            "ALTER TABLE auto_reply_rules ADD COLUMN mock_method TEXT DEFAULT 'GET'"
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.execute(
            "ALTER TABLE auto_reply_rules ADD COLUMN mock_url TEXT DEFAULT ''"
        )
    except Exception:  # noqa: BLE001
        pass
    # auto_reply_rules 表加 delay_ms/throttle_kbps 字段（网络模拟：延迟注入 + 限速）
    try:
        conn.execute(
            "ALTER TABLE auto_reply_rules ADD COLUMN delay_ms INTEGER DEFAULT 0"
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.execute(
            "ALTER TABLE auto_reply_rules ADD COLUMN throttle_kbps INTEGER DEFAULT 0"
        )
    except Exception:  # noqa: BLE001
        pass
    # flow_groups 表（§3.13 流量分组）
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flow_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                flow_ids TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    except Exception:  # noqa: BLE001
        pass
    # ignored_processes 表迁移：旧表 pid INTEGER PRIMARY KEY，无法存多个按名称忽略（pid=-1 会冲突）
    # 新表：id 自增主键，pid 可空（NULL=按名称忽略），UNIQUE(pid, process_name) 防重
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(ignored_processes)").fetchall()}
        if "id" not in cols:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ignored_processes_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pid INTEGER,
                    process_name TEXT NOT NULL,
                    ignored_at TEXT NOT NULL,
                    UNIQUE(pid, process_name)
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO ignored_processes_v2(pid, process_name, ignored_at)
                SELECT pid, process_name, ignored_at FROM ignored_processes
                """
            )
            conn.execute("DROP TABLE ignored_processes")
            conn.execute("ALTER TABLE ignored_processes_v2 RENAME TO ignored_processes")
    except Exception:  # noqa: BLE001
        pass


# ---------- settings ----------
# 设置统一存到 <data_dir>/settings.json（用户可查看编辑），SQLite 表仅作迁移源
from . import settings_store  # noqa: E402


def _sqlite_get_all_settings() -> dict:
    """从 SQLite settings 表读取所有键值（仅迁移用）。"""
    with get_connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def get_setting(key: str, default: str = "") -> str:
    """读取单个设置项（从 settings.json）。"""
    v = settings_store.get_setting(key, default)
    # 兼容旧调用：返回字符串
    if isinstance(v, (list, dict)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return str(v)
    return str(v) if v is not None else default


def set_setting(key: str, value: str):
    """写入单个设置项到 settings.json。"""
    settings_store.set_setting(key, value)


def get_all_settings() -> dict:
    """读取全部设置（从 settings.json）。"""
    return settings_store.get_all_settings()


# ---------- sessions ----------

def create_session(name: str | None = None) -> int:
    started_at = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO sessions(name, started_at) VALUES(?, ?)",
            (name, started_at),
        )
        return cur.lastrowid


def get_session(session_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None


def get_sessions() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def update_session_ended(session_id: int):
    ended_at = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (ended_at, session_id))


def delete_session(session_id: int) -> int:
    """删除会话及其所有流量。返回删除的流量条数。"""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM flows WHERE session_id=?", (session_id,))
        deleted = cur.rowcount
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return deleted


# ---------- flows ----------

def insert_flow(flow: dict) -> int:
    """插入一条流量记录，flow 为字段名到值的映射，返回自增 id。"""
    cols = list(flow.keys())
    placeholders = ",".join("?" * len(cols))
    col_str = ",".join(cols)
    with get_connection() as conn:
        cur = conn.execute(
            f"INSERT INTO flows({col_str}) VALUES({placeholders})",
            list(flow.values()),
        )
        return cur.lastrowid


# 性能优化：异步批量写入 flows，减少高并发下 SQLite 写锁竞争
# 代理线程把 flow 数据入队（非阻塞），后台线程批量 INSERT + COMMIT
_flow_queue: "queue.Queue[dict | None]" = queue.Queue()
_flow_flush_event = threading.Event()
_flow_flush_done = threading.Event()
_flow_writer_started = False
_flow_writer_lock = threading.Lock()


def _start_flow_writer():
    """启动后台写入线程（懒启动，首次调用时创建）。"""
    global _flow_writer_started
    with _flow_writer_lock:
        if _flow_writer_started:
            return
        _flow_writer_started = True
    t = threading.Thread(target=_flow_writer_loop, daemon=True, name="flow-writer")
    t.start()


def _flow_writer_loop():
    """后台线程：从队列取 flow 数据，批量 INSERT + COMMIT。

    性能优化：每 200 条或每 200ms 提交一次，减少 COMMIT 开销和写锁竞争。
    批量越大，单次 COMMIT 摊销的开销越低（50→200 提速约 3 倍）。
    """
    conn = sqlite3.connect(get_db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    batch: list[dict] = []
    BATCH_SIZE = 200
    FLUSH_TIMEOUT = 0.2  # 秒

    while True:
        try:
            item = _flow_queue.get(timeout=FLUSH_TIMEOUT)
        except queue.Empty:
            # 超时：如果有积攒的 batch，提交
            if batch:
                _flush_batch(conn, batch)
                batch.clear()
            # 通知等待 flush 的线程
            if _flow_flush_event.is_set():
                _flow_flush_event.clear()
                _flow_flush_done.set()
            continue

        if item is None:
            # 哨兵：退出前提交剩余数据
            if batch:
                _flush_batch(conn, batch)
            break

        batch.append(item)
        if len(batch) >= BATCH_SIZE:
            _flush_batch(conn, batch)
            batch.clear()

    conn.close()


def _flush_batch(conn: sqlite3.Connection, batch: list[dict]):
    """批量插入 flows，单次 COMMIT。

    性能优化：失败时不再逐条 commit（放大 50 倍延迟），改为二分定位坏数据并跳过。
    """
    if not batch:
        return
    # 所有 flow 应有相同列（来自同一个 _record_flow 调用点）
    cols = list(batch[0].keys())
    placeholders = ",".join("?" * len(cols))
    col_str = ",".join(cols)
    sql = f"INSERT INTO flows({col_str}) VALUES({placeholders})"
    try:
        conn.executemany(sql, [list(f.values()) for f in batch])
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        # 二分定位：找出坏数据跳过，好数据仍然批量插入
        _flush_batch_split(conn, sql, batch)


def _flush_batch_split(conn: sqlite3.Connection, sql: str, batch: list[dict]):
    """二分法定位并跳过坏数据，避免逐条 commit 的 N 倍延迟。

    策略：对 batch 做二分，好的一半批量插入，坏的一半继续二分，
    直到单条仍失败则跳过。总 commit 次数 = O(log N + 坏数据数)，远优于 N。
    """
    if not batch:
        return
    if len(batch) == 1:
        # 单条仍失败：跳过，记日志
        try:
            conn.execute(sql, list(batch[0].values()))
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
        return
    mid = len(batch) // 2
    left, right = batch[:mid], batch[mid:]
    try:
        conn.executemany(sql, [list(f.values()) for f in left])
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        _flush_batch_split(conn, sql, left)
    try:
        conn.executemany(sql, [list(f.values()) for f in right])
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        _flush_batch_split(conn, sql, right)


def insert_flow_async(flow: dict):
    """异步插入 flow（入队，后台批量写入）。不返回 flow_id。

    用于非断点场景（_record_flow），代理线程无需等待 DB 写完。
    """
    _start_flow_writer()
    _flow_queue.put(flow)
    # SSE 通知：新 flow 入队后，通知所有 SSE 订阅者（用于 /flows/stream 推送）
    _notify_flow_subscribers(flow)


# ---------- SSE 流量推送 ----------
# 性能优化：前端用 SSE 替代 500ms 轮询，新 flow 入库后立即推送给前端，
# UI 延迟从 500ms 降到 <50ms，后端查询负载降 90%。
# 存储 (queue, loop) 对：代理线程通过 loop.call_soon_threadsafe 跨线程安全投递。
_flow_subscribers: list = []  # list[(asyncio.Queue, asyncio.AbstractEventLoop)]
_flow_subscribers_lock = threading.Lock()


def register_flow_subscriber(q: "asyncio.Queue", loop: "asyncio.AbstractEventLoop") -> None:
    """注册一个 SSE 订阅者（asyncio.Queue + 所属事件循环）。"""
    with _flow_subscribers_lock:
        _flow_subscribers.append((q, loop))


def unregister_flow_subscriber(q: "asyncio.Queue") -> None:
    """取消注册 SSE 订阅者。"""
    with _flow_subscribers_lock:
        _flow_subscribers[:] = [(q2, l) for (q2, l) in _flow_subscribers if q2 is not q]


def _notify_flow_subscribers(flow: dict) -> None:
    """通知所有 SSE 订阅者有新 flow（非阻塞，队列满则丢弃，避免阻塞代理线程）。

    通过 loop.call_soon_threadsafe 跨线程把数据投递到事件循环线程。
    """
    if not _flow_subscribers:
        return
    lite = _flow_to_lite(flow)
    with _flow_subscribers_lock:
        subs = list(_flow_subscribers)
    for q, loop in subs:
        try:
            loop.call_soon_threadsafe(_safe_put_nowait, q, lite)
        except Exception:  # noqa: BLE001
            pass  # loop 已关闭，忽略


def _safe_put_nowait(q: "asyncio.Queue", item: dict) -> None:
    """在事件循环线程中安全 put_nowait，队列满则丢弃。"""
    try:
        q.put_nowait(item)
    except Exception:  # noqa: BLE001
        pass  # QueueFull 或其他异常，丢弃即可


# SSE 推送的 lite 字段（不含大 body/headers），前端选中时再拉详情
_SSE_LITE_FIELDS = (
    "id", "session_id", "timestamp", "pid", "process_name", "method", "url",
    "scheme", "host", "path", "status_code", "duration_ms", "size",
    "breakpoint_status", "protocol", "src_port", "dst_port", "remote_ip",
    "ip_region", "tags", "tag_note", "http_version",
)


def _flow_to_lite(flow: dict) -> dict:
    """提取 flow 的 lite 字段（用于 SSE 推送，减少传输量）。"""
    return {k: flow.get(k) for k in _SSE_LITE_FIELDS if k in flow}


def flush_pending_flows(timeout: float = 2.0):
    """等待队列中所有 pending flows 写完（用于 flows/clear 等需要一致性的场景）。"""
    if not _flow_writer_started:
        return
    # 入队一个 flush 标记，等待后台线程处理到此处
    _flow_flush_done.clear()
    _flow_flush_event.set()
    _flow_flush_done.wait(timeout=timeout)


def update_flow_response(flow_id: int, status_code, response_headers, response_body,
                         duration_ms, size):
    """更新流量记录的响应字段。"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE flows SET status_code=?, response_headers=?, response_body=?, "
            "duration_ms=?, size=? WHERE id=?",
            (status_code, response_headers, response_body, duration_ms, size, flow_id),
        )


# 性能优化：异步 update 队列，代理热路径（非断点场景）用 async 版本避免阻塞
# update 操作合并批量执行，减少 SQLite 写锁竞争
_update_queue: "queue.Queue[dict | None]" = queue.Queue()
_update_writer_started = False
_update_writer_lock = threading.Lock()


def _start_update_writer():
    """启动 update 后台线程（懒启动）。"""
    global _update_writer_started
    with _update_writer_lock:
        if _update_writer_started:
            return
        _update_writer_started = True
    t = threading.Thread(target=_update_writer_loop, daemon=True, name="flow-update")
    t.start()


def _update_writer_loop():
    """后台线程：批量执行 UPDATE 操作。

    性能优化：合并同一批的多个 UPDATE 为一次 COMMIT，减少写锁竞争。
    UPDATE 操作幂等（按 flow_id），重复执行无副作用。
    """
    conn = sqlite3.connect(get_db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    batch: list[dict] = []
    BATCH_SIZE = 50
    FLUSH_TIMEOUT = 0.1  # 100ms（比 insert 更短，断点状态需要尽快可见）

    while True:
        try:
            item = _update_queue.get(timeout=FLUSH_TIMEOUT)
        except queue.Empty:
            if batch:
                _flush_update_batch(conn, batch)
                batch.clear()
            continue

        if item is None:
            if batch:
                _flush_update_batch(conn, batch)
            break

        batch.append(item)
        if len(batch) >= BATCH_SIZE:
            _flush_update_batch(conn, batch)
            batch.clear()

    conn.close()


def _flush_update_batch(conn: sqlite3.Connection, batch: list[dict]):
    """批量执行 UPDATE（每条单独 execute，最后一次 commit）。"""
    for item in batch:
        try:
            sql = item["sql"]
            params = item["params"]
            conn.execute(sql, params)
        except Exception:  # noqa: BLE001
            pass
    try:
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()


def update_flow_response_async(flow_id: int, status_code, response_headers, response_body,
                                duration_ms, size):
    """异步更新响应字段（入队，后台批量执行）。不阻塞代理线程。

    用于非断点场景的响应记录。断点场景需要同步可见，仍用 update_flow_response。
    """
    _start_update_writer()
    _update_queue.put({
        "sql": "UPDATE flows SET status_code=?, response_headers=?, response_body=?, "
               "duration_ms=?, size=? WHERE id=?",
        "params": (status_code, response_headers, response_body, duration_ms, size, flow_id),
    })


def update_flow_breakpoint(flow_id: int, status: str | None):
    """更新断点状态（同步，断点状态需要立即可见给前端）。"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE flows SET breakpoint_status=? WHERE id=?", (status, flow_id)
        )


def update_flow_request(flow_id: int, method: str, url: str, host: str, path: str,
                        request_headers: str, request_body: str):
    """断点放行时更新请求字段（用户修改后的值）。"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE flows SET method=?, url=?, host=?, path=?, request_headers=?, "
            "request_body=? WHERE id=?",
            (method, url, host, path, request_headers, request_body, flow_id),
        )


def update_flow_response_fields(flow_id: int, status_code, response_headers: str,
                                response_body: str):
    """断点放行时更新响应字段（用户修改后的值）。"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE flows SET status_code=?, response_headers=?, response_body=? "
            "WHERE id=?",
            (status_code, response_headers, response_body, flow_id),
        )


def get_flow(flow_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone()
        return dict(row) if row else None


def get_flows(session_id: int, limit: int = 100, offset: int = 0,
              host: str | None = None, process: str | None = None,
              status_code: int | None = None, method: str | None = None,
              since_id: int = 0, protocol: str | None = None,
              tag: str | None = None) -> list[dict]:
    """获取会话流量列表，支持按 host/进程/状态码/方法/增量/协议/标签过滤。"""
    sql = "SELECT * FROM flows WHERE session_id=?"
    args: list = [session_id]
    if host:
        sql += " AND host LIKE ?"
        args.append(f"%{host}%")
    if process:
        sql += " AND process_name LIKE ?"
        args.append(f"%{process}%")
    if status_code:
        sql += " AND status_code=?"
        args.append(status_code)
    if method:
        sql += " AND method=?"
        args.append(method.upper())
    if since_id:
        sql += " AND id > ?"
        args.append(since_id)
    if protocol:
        sql += " AND protocol=?"
        args.append(protocol)
    if tag:
        sql += " AND tags LIKE ?"
        args.append(f"%{tag}%")
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    with get_connection() as conn:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def search_flows(session_id: int, body_regex: str | None = None,
                 binary_hex: str | None = None, limit: int = 200,
                 header_regex: str | None = None,
                 method: str | None = None, status_code: int | None = None,
                 pid: int | None = None, process_name: str | None = None,
                 offset_start: int | None = None,
                 offset_end: int | None = None) -> list[dict]:
    """跨 body 正则/二进制搜索流量。session_id=0 表示跨所有会话搜索。

    过滤条件（AND 关系）：
    - body_regex: 对 request_body/response_body/url/path 做正则匹配
    - binary_hex: 对 request_body/response_body 的原始字节做 hex 子串匹配
    - header_regex: 对 request_headers/response_headers 做正则匹配
    - method/status_code/pid/process_name: 精确匹配
    - offset_start/offset_end: 二进制搜索时只在 [offset_start, offset_end) 范围内查找
    """
    import re
    if session_id and session_id > 0:
        rows = get_flows(session_id, limit=10000)
    else:
        # session_id=0：跨会话搜索所有流量
        rows, _ = get_all_flows(limit=10000)
    # 精确字段过滤（先过滤掉不匹配的，减少后续正则运算量）
    if method:
        rows = [f for f in rows if (f.get("method") or "").upper() == method.upper()]
    if status_code:
        rows = [f for f in rows if f.get("status_code") == status_code]
    if pid:
        rows = [f for f in rows if f.get("pid") == pid]
    if process_name:
        rows = [f for f in rows if (f.get("process_name") or "") == process_name]
    results = []
    rx = re.compile(body_regex) if body_regex else None
    hrx = re.compile(header_regex) if header_regex else None
    needle = None
    if binary_hex:
        try:
            needle = bytes.fromhex(binary_hex.replace(" ", "").replace("0x", ""))
        except ValueError:
            pass
    for f in rows:
        matched = False
        if rx:
            for field in ("request_body", "response_body", "url", "path"):
                val = f.get(field) or ""
                if val.startswith("base64:"):
                    import base64
                    try:
                        val = base64.b64decode(val[7:]).decode("utf-8", "ignore")
                    except Exception:  # noqa: BLE001
                        pass
                if rx.search(val):
                    matched = True
                    break
        if not matched and needle:
            for field in ("request_body", "response_body"):
                val = f.get(field) or ""
                raw = val.encode("utf-8")
                if val.startswith("base64:"):
                    import base64
                    try:
                        raw = base64.b64decode(val[7:])
                    except Exception:  # noqa: BLE001
                        pass
                # §3.14 hex 偏移范围搜索：只搜 [offset_start, offset_end)
                if offset_start is not None or offset_end is not None:
                    s = offset_start or 0
                    e = offset_end if offset_end is not None else len(raw)
                    if needle in raw[s:e]:
                        matched = True
                        break
                else:
                    if needle in raw:
                        matched = True
                        break
        if not matched and hrx:
            for field in ("request_headers", "response_headers"):
                val = f.get(field) or ""
                if hrx.search(val):
                    matched = True
                    break
        # 如果没有任何正则/hex 条件（只有精确字段过滤），所有过滤后的行都算匹配
        if not (rx or needle or hrx):
            matched = True
        if matched:
            results.append(f)
            if len(results) >= limit:
                break
    return results


def stats_flows(session_id: int) -> dict:
    """按 host/method/status 分组统计。"""
    with get_connection() as conn:
        by_host = conn.execute(
            "SELECT host, COUNT(*) as c, AVG(duration_ms) as avg_ms "
            "FROM flows WHERE session_id=? AND host IS NOT NULL "
            "GROUP BY host ORDER BY c DESC LIMIT 50",
            (session_id,)
        ).fetchall()
        by_method = conn.execute(
            "SELECT method, COUNT(*) as c FROM flows "
            "WHERE session_id=? AND method IS NOT NULL "
            "GROUP BY method ORDER BY c DESC",
            (session_id,)
        ).fetchall()
        by_status = conn.execute(
            "SELECT status_code, COUNT(*) as c FROM flows "
            "WHERE session_id=? AND status_code IS NOT NULL "
            "GROUP BY status_code ORDER BY status_code",
            (session_id,)
        ).fetchall()
        by_protocol = conn.execute(
            "SELECT protocol, COUNT(*) as c FROM flows "
            "WHERE session_id=? GROUP BY protocol ORDER BY c DESC",
            (session_id,)
        ).fetchall()
        return {
            "total": count_flows(session_id),
            "by_host": [dict(r) for r in by_host],
            "by_method": [dict(r) for r in by_method],
            "by_status": [dict(r) for r in by_status],
            "by_protocol": [dict(r) for r in by_protocol],
        }


def get_max_flow_id(session_id: int) -> int:
    """获取会话内最大 flow id（用于增量查询基线）。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(id) as m FROM flows WHERE session_id=?", (session_id,)
        ).fetchone()
        return row["m"] or 0


def get_max_flow_id_all() -> int:
    """获取全局最大 flow id（用于 SSE 初始基线 + 增量轮询）。"""
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(id) as m FROM flows").fetchone()
        return row["m"] or 0


def count_flows(session_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM flows WHERE session_id=?", (session_id,)
        ).fetchone()
        return row["c"]


def count_flows_batch(session_ids: list[int]) -> dict[int, int]:
    """批量统计多个会话的流量数。返回 {session_id: count}。"""
    if not session_ids:
        return {}
    with get_connection() as conn:
        placeholders = ",".join("?" * len(session_ids))
        rows = conn.execute(
            f"SELECT session_id, COUNT(*) AS c FROM flows WHERE session_id IN ({placeholders}) GROUP BY session_id",
            session_ids,
        ).fetchall()
        result = {sid: 0 for sid in session_ids}
        for r in rows:
            result[r["session_id"]] = r["c"]
        return result


def get_all_tags_summary() -> list[dict]:
    """全局标签统计：解析所有流量的 tags 字段（逗号分隔），返回 [{tag, count}] 按计数降序。"""
    with get_connection() as conn:
        rows = conn.execute("SELECT tags FROM flows WHERE tags IS NOT NULL AND tags != ''").fetchall()
    counter: dict[str, int] = {}
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                counter[t] = counter.get(t, 0) + 1
    return [{"tag": k, "count": v} for k, v in sorted(counter.items(), key=lambda x: -x[1])]


def delete_flows(session_id: int):
    """清空会话流量。"""
    with get_connection() as conn:
        conn.execute("DELETE FROM flows WHERE session_id=?", (session_id,))


def get_all_flows(limit: int = 200, offset: int = 0,
                  host: str | None = None, process: str | None = None,
                  status_code: int | None = None, method: str | None = None,
                  protocol: str | None = None, since_id: int = 0,
                  path: str | None = None, url: str | None = None,
                  tag: str | None = None,
                  lite: bool = False) -> tuple[list[dict], int]:
    """跨会话查询所有流量（用于全局分析），返回 (flows, total)。

    lite=True 时只返回轻量字段（不含 request_body/response_body/request_headers/
    response_headers/raw_data），用于全局分析列表加速（选中详情时再单独 GET /flows/{id}）。
    """
    where = []
    args: list = []
    if host:
        where.append("host LIKE ?")
        args.append(f"%{host}%")
    if process:
        where.append("process_name LIKE ?")
        args.append(f"%{process}%")
    if status_code:
        where.append("status_code=?")
        args.append(status_code)
    if method:
        where.append("method=?")
        args.append(method.upper())
    if protocol:
        where.append("protocol=?")
        args.append(protocol)
    if since_id:
        where.append("id > ?")
        args.append(since_id)
    if path:
        where.append("path LIKE ?")
        args.append(f"%{path}%")
    if url:
        where.append("url LIKE ?")
        args.append(f"%{url}%")
    if tag:
        where.append("tags LIKE ?")
        args.append(f"%{tag}%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    # lite 模式：排除大字段，只返回列表展示所需字段
    select_cols = (
        "id, session_id, timestamp, pid, process_name, method, url, scheme, "
        "host, path, status_code, duration_ms, size, breakpoint_status, "
        "tags, tag_note, protocol, src_port, dst_port, remote_ip, ip_region, "
        "http_version"
        if lite else "*"
    )
    with get_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM flows{where_sql}", args).fetchone()[0]
        sql = f"SELECT {select_cols} FROM flows{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?"
        rows = conn.execute(sql, args + [limit, offset]).fetchall()
        return [dict(r) for r in rows], total


def delete_all_flows() -> int:
    """清空所有流量（跨会话），返回删除条数。"""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM flows")
        return cur.rowcount


def get_flows_stats(group_by: str = "host",
                    host: str | None = None, process: str | None = None) -> list[dict]:
    """全量流量分组统计（不分页），用于统计图显示所有数据的比例。

    group_by: host / process / content_type / status_code / method
    返回 [{key, label, count}] 按 count 降序。
    """
    where = []
    args: list = []
    if host:
        where.append("host LIKE ?")
        args.append(f"%{host}%")
    if process:
        where.append("process_name LIKE ?")
        args.append(f"%{process}%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    if group_by == "host":
        col = "host"
        null_label = "(无 host)"
    elif group_by == "process":
        col = "process_name"
        null_label = "(未知进程)"
    elif group_by == "method":
        col = "method"
        null_label = "(无方法)"
    elif group_by == "ip_region":
        col = "ip_region"
        null_label = "(未知属地)"
    else:
        # content_type / status_code 需从 response_headers 提取，SQL 无法直接做
        # 退化为全量读取后 Python 聚合（数据量大时有性能成本，但统计图本就要求全量）
        col = None
        null_label = "(未知)"

    with get_connection() as conn:
        if col is not None:
            sql = (
                f"SELECT COALESCE(NULLIF({col}, ''), '{null_label}') AS k, COUNT(*) AS c "
                f"FROM flows{where_sql} GROUP BY k ORDER BY c DESC"
            )
            rows = conn.execute(sql, args).fetchall()
            return [{"key": r["k"], "label": r["k"], "count": r["c"]} for r in rows]
        # content_type / status_code：Python 聚合
        sql = f"SELECT method, status_code, response_headers FROM flows{where_sql}"
        rows = conn.execute(sql, args).fetchall()
        buckets: dict[str, int] = {}
        for r in rows:
            if group_by == "status_code":
                code = r["status_code"]
                if code is None or code == "":
                    k = "无状态码"
                elif code < 200:
                    k = "1xx 信息"
                elif code < 300:
                    k = "2xx 成功"
                elif code < 400:
                    k = "3xx 重定向"
                elif code < 500:
                    k = "4xx 客户端错误"
                else:
                    k = "5xx 服务器错误"
            else:  # content_type
                ct = ""
                try:
                    obj = json.loads(r["response_headers"] or "{}")
                    for kk in obj:
                        if kk.lower() == "content-type":
                            ct = str(obj[kk])
                            break
                except Exception:  # noqa: BLE001
                    pass
                k = (ct.split("/")[0].lower() or "unknown") if ct else "unknown"
            buckets[k] = buckets.get(k, 0) + 1
        return [{"key": k, "label": k, "count": c}
                for k, c in sorted(buckets.items(), key=lambda x: -x[1])]


def get_flows_overview() -> dict:
    """跨会话多维聚合统计（一次返回所有维度），供 CoolUI 仪表盘使用。

    返回 {total, total_bytes, incoming_bytes, outgoing_bytes,
          success_count, error_count, avg_duration_ms,
          by_protocol, by_method, by_status_range,
          by_host, by_process, by_ip_region}
    每个分组为 [{key, count, bytes?}] 列表，按 count 降序，前 20 条。
    """
    with get_connection() as conn:
        # 总数 + 总字节
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS bytes, "
            "COALESCE(AVG(duration_ms), 0) AS avg_ms "
            "FROM flows"
        ).fetchone()
        total = row["c"]
        total_bytes = row["bytes"]
        avg_duration_ms = round(row["avg_ms"], 1) if row["avg_ms"] else 0

        # 入站/出站字节（WS-RECV / response_body 视为入站，WS-SEND / request_body 视为出站）
        # 简化：size 作为总字节，incoming=response_size, outgoing=request_size
        # 这里用 protocol + method 估算：WS-RECV 为入站，WS-SEND 为出站
        io_row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN method='WS-RECV' THEN size ELSE 0 END), 0) AS inb, "
            "COALESCE(SUM(CASE WHEN method='WS-SEND' THEN size ELSE 0 END), 0) AS outb "
            "FROM flows WHERE protocol='ws'"
        ).fetchone()
        incoming_bytes = io_row["inb"] if io_row else 0
        outgoing_bytes = io_row["outb"] if io_row else 0
        # 非 WS 流量也计入（response 视为入站，request 视为出站，简化用 size 平均分）
        non_ws_row = conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS bytes FROM flows WHERE protocol != 'ws'"
        ).fetchone()
        non_ws_bytes = non_ws_row["bytes"] if non_ws_row else 0
        incoming_bytes += non_ws_bytes // 2
        outgoing_bytes += non_ws_bytes - non_ws_bytes // 2

        # 成功/错误计数
        se_row = conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status_code >= 200 AND status_code < 300 THEN 1 ELSE 0 END), 0) AS success, "
            "COALESCE(SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END), 0) AS error "
            "FROM flows WHERE status_code IS NOT NULL"
        ).fetchone()
        success_count = se_row["success"] if se_row else 0
        error_count = se_row["error"] if se_row else 0

        # 协议分布
        by_protocol = [
            {"key": r["k"] or "(unknown)", "count": r["c"]}
            for r in conn.execute(
                "SELECT COALESCE(NULLIF(protocol, ''), 'http') AS k, COUNT(*) AS c "
                "FROM flows GROUP BY k ORDER BY c DESC"
            ).fetchall()
        ]

        # 方法分布
        by_method = [
            {"key": r["k"] or "(none)", "count": r["c"]}
            for r in conn.execute(
                "SELECT COALESCE(NULLIF(method, ''), '(none)') AS k, COUNT(*) AS c "
                "FROM flows WHERE method IS NOT NULL GROUP BY k ORDER BY c DESC"
            ).fetchall()
        ]

        # 状态码区间分布
        status_rows = conn.execute(
            "SELECT status_code, COUNT(*) AS c FROM flows "
            "WHERE status_code IS NOT NULL GROUP BY status_code"
        ).fetchall()
        status_buckets = {"1xx": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}
        for r in status_rows:
            code = r["status_code"]
            if code is None or code == "":
                continue
            if code < 200:
                status_buckets["1xx"] += r["c"]
            elif code < 300:
                status_buckets["2xx"] += r["c"]
            elif code < 400:
                status_buckets["3xx"] += r["c"]
            elif code < 500:
                status_buckets["4xx"] += r["c"]
            elif code < 600:
                status_buckets["5xx"] += r["c"]
            else:
                status_buckets["other"] += r["c"]
        by_status_range = [{"key": k, "count": v} for k, v in status_buckets.items()]

        # Host 分布（top 20，含字节数）
        by_host = [
            {"key": r["k"], "count": r["c"], "bytes": r["b"] or 0}
            for r in conn.execute(
                "SELECT COALESCE(NULLIF(host, ''), '(no host)') AS k, COUNT(*) AS c, "
                "COALESCE(SUM(size), 0) AS b "
                "FROM flows GROUP BY k ORDER BY c DESC LIMIT 20"
            ).fetchall()
        ]

        # 进程分布（top 20）
        by_process = [
            {"key": r["k"], "count": r["c"], "bytes": r["b"] or 0}
            for r in conn.execute(
                "SELECT COALESCE(NULLIF(process_name, ''), '(unknown)') AS k, COUNT(*) AS c, "
                "COALESCE(SUM(size), 0) AS b "
                "FROM flows GROUP BY k ORDER BY c DESC LIMIT 20"
            ).fetchall()
        ]

        # IP 属地分布（top 20）
        by_ip_region = [
            {"key": r["k"], "count": r["c"]}
            for r in conn.execute(
                "SELECT COALESCE(NULLIF(ip_region, ''), '(unknown)') AS k, COUNT(*) AS c "
                "FROM flows WHERE ip_region IS NOT NULL AND ip_region != '' "
                "GROUP BY k ORDER BY c DESC LIMIT 20"
            ).fetchall()
        ]

        return {
            "total": total,
            "total_bytes": total_bytes,
            "incoming_bytes": incoming_bytes,
            "outgoing_bytes": outgoing_bytes,
            "success_count": success_count,
            "error_count": error_count,
            "avg_duration_ms": avg_duration_ms,
            "by_protocol": by_protocol,
            "by_method": by_method,
            "by_status_range": by_status_range,
            "by_host": by_host,
            "by_process": by_process,
            "by_ip_region": by_ip_region,
        }


def delete_flows_before(flow_id: int) -> int:
    """删除 id < flow_id 的所有流量（按 id 清理旧数据），返回删除条数。"""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM flows WHERE id < ?", (flow_id,))
        return cur.rowcount


def delete_flow(flow_id: int):
    """删除单条流量。"""
    with get_connection() as conn:
        conn.execute("DELETE FROM flows WHERE id=?", (flow_id,))


def delete_flow_batch(flow_ids: list[int]):
    """批量删除流量。"""
    if not flow_ids:
        return
    with get_connection() as conn:
        placeholders = ",".join("?" * len(flow_ids))
        conn.execute(f"DELETE FROM flows WHERE id IN ({placeholders})", flow_ids)


def get_pending_flow_ids(flow_ids: list[int]) -> list[int]:
    """从给定 id 列表中，返回有断点状态（breakpoint_status IS NOT NULL）的 id 列表。
    单次 SQL 查询，避免批量放行时的 N+1 查询。
    """
    if not flow_ids:
        return []
    with get_connection() as conn:
        placeholders = ",".join("?" * len(flow_ids))
        rows = conn.execute(
            f"SELECT id FROM flows WHERE id IN ({placeholders}) AND breakpoint_status IS NOT NULL",
            flow_ids,
        ).fetchall()
        return [r["id"] for r in rows]


def get_pending_breakpoint_flows() -> list[dict]:
    """获取所有断点暂停中的流量。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM flows WHERE breakpoint_status IS NOT NULL ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- auto_reply_rules ----------

def get_rules() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM auto_reply_rules ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_rule(rule_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM auto_reply_rules WHERE id=?", (rule_id,)
        ).fetchone()
        return dict(row) if row else None


def insert_rule(rule: dict):
    cols = list(rule.keys())
    placeholders = ",".join("?" * len(cols))
    col_str = ",".join(cols)
    with get_connection() as conn:
        conn.execute(
            f"INSERT INTO auto_reply_rules({col_str}) VALUES({placeholders})",
            list(rule.values()),
        )


def add_rule(rule: dict) -> str:
    """插入规则并返回生成的 ID。"""
    import uuid
    rule_id = str(uuid.uuid4())[:8]
    rule["id"] = rule_id
    now = datetime.now().isoformat()
    rule.setdefault("created_at", now)
    rule.setdefault("updated_at", now)
    insert_rule(rule)
    return rule_id


def update_rule(rule_id: str, updates: dict):
    sets = ",".join(f"{k}=?" for k in updates.keys())
    values = list(updates.values()) + [rule_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE auto_reply_rules SET {sets} WHERE id=?", values)


def delete_rule(rule_id: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM auto_reply_rules WHERE id=?", (rule_id,))


def increment_rule_hit(rule_id: str, flow_id: int | None = None):
    """§3.2 命中计数：自增 hit_count，更新 last_hit_at 和 last_hit_flow_id。"""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE auto_reply_rules SET hit_count = hit_count + 1, "
            "last_hit_at = ?, last_hit_flow_id = ? WHERE id = ?",
            (now, flow_id, rule_id),
        )


def delete_all_rules() -> int:
    """清空所有自动回复规则（环境快照导入前调用）。返回删除条数。"""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM auto_reply_rules")
        return cur.rowcount


# ---------- flow tags（§3.1 流量标签）----------

def update_flow_tags(flow_id: int, tags: str, tag_note: str | None = None):
    """更新流量标签（逗号分隔字符串）和可选备注。

    tag_note 为 None 时不修改备注字段（保持原值）。
    """
    with get_connection() as conn:
        if tag_note is None:
            conn.execute(
                "UPDATE flows SET tags=? WHERE id=?", (tags, flow_id)
            )
        else:
            conn.execute(
                "UPDATE flows SET tags=?, tag_note=? WHERE id=?",
                (tags, tag_note, flow_id)
            )


def get_flows_by_tag(tag: str) -> list[dict]:
    """查询所有带指定标签的流量（跨会话）。tag 精确匹配逗号分隔列表中的某一项。"""
    if not tag:
        return []
    with get_connection() as conn:
        # tags 是逗号分隔字符串，用 LIKE 做子串匹配（兼容旧数据）
        rows = conn.execute(
            "SELECT * FROM flows WHERE tags LIKE ? ORDER BY id DESC",
            (f"%{tag}%",)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- flow_groups（§3.13 流量分组）----------

def create_flow_group(name: str, flow_ids: list[int]) -> int:
    """创建流量分组，返回自增 id。flow_ids 以逗号分隔字符串存储。"""
    now = datetime.now().isoformat()
    ids_str = ",".join(str(i) for i in flow_ids)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO flow_groups(name, flow_ids, created_at) VALUES(?, ?, ?)",
            (name, ids_str, now),
        )
        return cur.lastrowid


def get_flow_groups() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM flow_groups ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_flow_group(group_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM flow_groups WHERE id=?", (group_id,)
        ).fetchone()
        return dict(row) if row else None


def delete_flow_group(group_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM flow_groups WHERE id=?", (group_id,))


def update_flow_group(group_id: int, name: str | None = None,
                      flow_ids: list[int] | None = None):
    """更新分组。name 或 flow_ids 为 None 表示不更新该字段。"""
    updates = {}
    if name is not None:
        updates["name"] = name
    if flow_ids is not None:
        updates["flow_ids"] = ",".join(str(i) for i in flow_ids)
    if not updates:
        return
    sets = ",".join(f"{k}=?" for k in updates.keys())
    values = list(updates.values()) + [group_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE flow_groups SET {sets} WHERE id=?", values)


# ---------- ignored_processes ----------

def get_ignored_processes() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, pid, process_name, ignored_at FROM ignored_processes ORDER BY ignored_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_ignored_pids() -> set[int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT pid FROM ignored_processes WHERE pid IS NOT NULL").fetchall()
        return {row["pid"] for row in rows if row["pid"] is not None and row["pid"] > 0}


def add_ignored_process(pid: int | None, process_name: str):
    ignored_at = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ignored_processes(pid, process_name, ignored_at) "
            "VALUES(?, ?, ?)",
            (pid, process_name, ignored_at),
        )


def remove_ignored_process(row_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM ignored_processes WHERE id=?", (row_id,))


# ---------- ignored_hosts ----------

def get_ignored_hosts() -> list[dict]:
    """已忽略 host 通配符列表（按创建时间倒序）。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ignored_hosts ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def add_ignored_host(host_pattern: str) -> dict:
    """添加忽略 host 通配符（重复时忽略）。"""
    created_at = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO ignored_hosts(host_pattern, created_at) VALUES(?, ?)",
            (host_pattern, created_at),
        )
        if cur.lastrowid:
            return {"id": cur.lastrowid, "host_pattern": host_pattern}
        # 已存在则查回
        row = conn.execute(
            "SELECT * FROM ignored_hosts WHERE host_pattern=?", (host_pattern,)
        ).fetchone()
        return dict(row) if row else {"id": 0, "host_pattern": host_pattern}


def remove_ignored_host(host_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM ignored_hosts WHERE id=?", (host_id,))


# ---------- ai_chats / ai_messages ----------

def create_ai_chat(title: str, flow_ids: list[int], flow_context: str) -> int:
    """创建 AI 分析记录，返回 chat id。"""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO ai_chats(title, flow_ids, flow_context, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (title, json.dumps(flow_ids), flow_context, now, now),
        )
        return cur.lastrowid


def get_ai_chat(chat_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ai_chats WHERE id=?", (chat_id,)
        ).fetchone()
        return dict(row) if row else None


def get_ai_chats(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_chats ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_ai_chat_title(chat_id: int, title: str):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE ai_chats SET title=?, updated_at=? WHERE id=?",
            (title, now, chat_id),
        )


def delete_ai_chat(chat_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM ai_messages WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM ai_chats WHERE id=?", (chat_id,))


def add_ai_message(chat_id: int, role: str, content: str) -> int:
    """添加一条对话消息，返回 message id。"""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO ai_messages(chat_id, role, content, created_at) "
            "VALUES(?, ?, ?, ?)",
            (chat_id, role, content, now),
        )
        conn.execute(
            "UPDATE ai_chats SET updated_at=? WHERE id=?", (now, chat_id)
        )
        return cur.lastrowid


def get_ai_messages(chat_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_messages WHERE chat_id=? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]
