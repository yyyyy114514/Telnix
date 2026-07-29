"""SQLite database: sessions / flows / auto_reply_rules / settings / ignored_processes tables.

Each operation creates an independent connection, commits, and closes, combined with
WAL mode and busy_timeout for concurrency handling.
"""

import asyncio
import base64
import concurrent.futures
import json
import logging
import os
import queue
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .config import get_db_path

_logger = logging.getLogger("telnix.db")


# ---------- SQL 列名白名单校验（防 SQL 注入）----------
# 动态 SQL 构建器（insert_flow / insert_rule / update_rule 等）把 dict 的 key
# 直接拼接到 SQL 语句中。虽然这些 dict 的 key 通常由应用代码控制，
# 但作为深度防御，校验列名只包含合法字符（字母/数字/下划线），
# 防止任何意外的 SQL 注入（如 key 含引号、分号、-- 等元字符）。
_VALID_COL_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _validate_column_names(cols) -> None:
    """Validate that column names contain only legal characters (letters/digits/underscore, first char not a digit).

    Security: prevents SQL metacharacters from sneaking in via dict keys (e.g. \"id; DROP TABLE--\").
    Legal column name examples: id, session_id, request_headers, http_version
    """
    for c in cols:
        if not isinstance(c, str) or not _VALID_COL_NAME.match(c):
            raise ValueError(
                f"Invalid column name: {c!r} (only letters/digits/underscore allowed, first char must not be a digit)"
            )

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
    """Get the current thread's reused connection (created on first call, reused afterwards).

    PRAGMA busy_timeout only needs to be set once when the connection is first
    established (connection-level setting). row_factory also only needs to be set once.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(get_db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _local.conn = conn
    return conn


@contextmanager
def get_connection():
    """Get a database connection (threading.local reuse, auto-commit).

    Performance optimization: each worker thread reuses a long-lived connection,
    avoiding connect/close on every operation.
    WAL mode is a persistent setting (set once in init_db), no need to set per connection.
    busy_timeout is a connection-level setting, set once when first established.
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
    """Initialize the database and tables, writing default settings.

    By default, the flows table is cleared and the auto-increment sequence is
    reset on startup, to avoid IDs accumulating indefinitely.
    Set the environment variable TELNIX_KEEP_FLOWS=1 to keep historical flows.

    Performance optimization: when clearing flows, DROP TABLE + CREATE TABLE is
    used instead of DELETE FROM flows. DELETE removes rows one by one and writes
    WAL logs, taking seconds when there are many rows; DROP+CREATE releases pages
    directly without writing WAL, completing instantly (< 1ms). The same applies
    to the sessions table and sqlite_sequence.
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
        # 性能优化：DROP+CREATE 比 DELETE 快几个数量级（不写 WAL，不逐行删除）
        if os.environ.get("TELNIX_KEEP_FLOWS") != "1":
            # DROP + CREATE flows（含所有字段，与 SCHEMA_TABLES 中定义一致）
            conn.execute("DROP TABLE IF EXISTS flows")
            conn.execute("""
                CREATE TABLE flows (
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
                    http_version TEXT DEFAULT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # 重建索引（DROP TABLE 会删掉所有索引）
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_session ON flows(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_host ON flows(host)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_process ON flows(process_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_protocol ON flows(protocol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_method ON flows(method)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_ip_region ON flows(ip_region)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_remote_ip ON flows(remote_ip)")
            # DROP + CREATE sessions
            conn.execute("DROP TABLE IF EXISTS sessions")
            conn.execute("""
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                )
            """)
            # 删除 sqlite_sequence 残留（DROP TABLE 后该表条目会自动删，但保险）
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('flows', 'sessions')")
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
    """Database migration: add missing new columns. Failure means it already exists, ignored.

    Safety: only swallows "duplicate column" / "already exists" errors (meaning the
    column already exists); other errors (disk full, DB corruption, permission errors)
    propagate up, to avoid silent migration failures that would cause subsequent
    queries to crash due to missing columns.
    """

    def _safe_alter(sql: str):
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            # 仅在"字段/表已存在"时静默，其他 OperationalError 向上抛
            if "duplicate column" not in msg and "already exists" not in msg:
                raise
        # 其他异常（如 ProgrammingError）不吞，直接向上抛

    _safe_alter("ALTER TABLE auto_reply_rules ADD COLUMN note TEXT DEFAULT ''")
    # flows 表加 protocol 字段（http/tcp/udp），兼容旧数据默认 http
    _safe_alter("ALTER TABLE flows ADD COLUMN protocol TEXT DEFAULT 'http'")
    # flows 表加 raw_data 字段（TCP/UDP 原始字节，base64）
    _safe_alter("ALTER TABLE flows ADD COLUMN raw_data TEXT DEFAULT NULL")
    # flows 表加 src_port/dst_port 字段（TCP/UDP 用）
    _safe_alter("ALTER TABLE flows ADD COLUMN src_port INTEGER DEFAULT NULL")
    _safe_alter("ALTER TABLE flows ADD COLUMN dst_port INTEGER DEFAULT NULL")
    # flows 表加 remote_ip/ip_region 字段（IP 属地分析）
    _safe_alter("ALTER TABLE flows ADD COLUMN remote_ip TEXT DEFAULT NULL")
    _safe_alter("ALTER TABLE flows ADD COLUMN ip_region TEXT DEFAULT NULL")
    # flows 表加 cert_info 字段（TLS 证书信息 JSON）
    _safe_alter("ALTER TABLE flows ADD COLUMN cert_info TEXT DEFAULT NULL")
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
        _safe_alter(
            f"ALTER TABLE auto_reply_rules ADD COLUMN {col} TEXT DEFAULT ''"
        )
    # auto_reply_rules 表加命中统计字段（§3.2 intercept 命中计数）
    _safe_alter(
        "ALTER TABLE auto_reply_rules ADD COLUMN hit_count INTEGER DEFAULT 0"
    )
    _safe_alter(
        "ALTER TABLE auto_reply_rules ADD COLUMN last_hit_at TEXT DEFAULT ''"
    )
    _safe_alter(
        "ALTER TABLE auto_reply_rules ADD COLUMN last_hit_flow_id INTEGER DEFAULT NULL"
    )
    # auto_reply_rules 表加 mock_request 相关字段（写死请求：用预设请求转发到目标服务器）
    # mock_method=HTTP方法(默认GET)，mock_url=完整请求URL（含https://）
    _safe_alter(
        "ALTER TABLE auto_reply_rules ADD COLUMN mock_method TEXT DEFAULT 'GET'"
    )
    _safe_alter(
        "ALTER TABLE auto_reply_rules ADD COLUMN mock_url TEXT DEFAULT ''"
    )
    # auto_reply_rules 表加 delay_ms/throttle_kbps 字段（网络模拟：延迟注入 + 限速）
    _safe_alter(
        "ALTER TABLE auto_reply_rules ADD COLUMN delay_ms INTEGER DEFAULT 0"
    )
    _safe_alter(
        "ALTER TABLE auto_reply_rules ADD COLUMN throttle_kbps INTEGER DEFAULT 0"
    )
    # flow_groups 表（§3.13 流量分组）
    _safe_alter(
        """
        CREATE TABLE IF NOT EXISTS flow_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            flow_ids TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
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
    except Exception as e:  # noqa: BLE001
        # 修复审计 6.1：原代码完全静默吞掉迁移异常，DB 可能处于不一致状态
        # （v2 已建但旧表未删，或旧表已删但 RENAME 失败），后续查询会崩溃
        # 记录 error 便于诊断；不抛出避免阻断启动
        try:
            _logger.error("ignored_processes table migration failed: %s: %s",
                          type(e).__name__, e)
        except Exception:  # noqa: BLE001
            pass


# ---------- settings ----------
# 设置统一存到 <data_dir>/settings.json（用户可查看编辑），SQLite 表仅作迁移源
from . import settings_store  # noqa: E402


def _sqlite_get_all_settings() -> dict:
    """Read all key-values from the SQLite settings table (migration only)."""
    with get_connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def get_setting(key: str, default: str = "") -> str:
    """Read a single setting (from settings.json)."""
    v = settings_store.get_setting(key, default)
    # 兼容旧调用：返回字符串
    if isinstance(v, (list, dict)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return str(v)
    return str(v) if v is not None else default


def set_setting(key: str, value: str):
    """Write a single setting to settings.json."""
    settings_store.set_setting(key, value)


def get_all_settings() -> dict:
    """Read all settings (from settings.json)."""
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
    """Delete a session and all its flows. Returns the number of deleted flows."""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM flows WHERE session_id=?", (session_id,))
        deleted = cur.rowcount
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return deleted


# ---------- flows ----------

def insert_flow(flow: dict) -> int:
    """Insert a single flow record. flow is a field-name-to-value mapping. Returns the auto-increment id."""
    cols = list(flow.keys())
    _validate_column_names(cols)  # 安全：校验列名防 SQL 注入
    placeholders = ",".join("?" * len(cols))
    col_str = ",".join(cols)
    with get_connection() as conn:
        cur = conn.execute(
            f"INSERT INTO flows({col_str}) VALUES({placeholders})",
            list(flow.values()),
        )
        return cur.lastrowid


def insert_flows_batch(flows: list[dict]) -> int:
    """Batch insert flows (single transaction commit + SAVEPOINT failure isolation),
    much faster than inserting one by one.

    Returns the number of successfully inserted rows; a single row failure only
    skips that row and does not affect the others.
    """
    if not flows:
        return 0
    inserted = 0
    with get_connection() as conn:
        for flow in flows:
            cols = list(flow.keys())
            try:
                _validate_column_names(cols)  # 安全：校验列名防 SQL 注入
            except ValueError:
                continue  # 跳过含非法列名的条目
            placeholders = ",".join("?" * len(cols))
            col_str = ",".join(cols)
            try:
                conn.execute("SAVEPOINT batch_ins")
                conn.execute(
                    f"INSERT INTO flows({col_str}) VALUES({placeholders})",
                    list(flow.values()),
                )
                conn.execute("RELEASE batch_ins")
                inserted += 1
            except Exception:  # noqa: BLE001
                conn.execute("ROLLBACK TO batch_ins")
                conn.execute("RELEASE batch_ins")
                continue
    return inserted


def get_flows_by_ids(ids: list[int]) -> dict[int, dict]:
    """Batch-get flows by id list, returning an {id: flow} mapping, eliminating the N+1 of per-id queries."""
    if not ids:
        return {}
    unique = list(dict.fromkeys(ids))
    placeholders = ",".join("?" * len(unique))
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM flows WHERE id IN ({placeholders})", unique
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


# 性能优化：异步批量写入 flows，减少高并发下 SQLite 写锁竞争
# 代理线程把 flow 数据入队（非阻塞），后台线程批量 INSERT + COMMIT
# 队列加上限 50000，防止极端场景下内存无限增长；满时丢弃最旧条目并计数
_flow_queue: "queue.Queue[dict | None]" = queue.Queue(maxsize=50000)
_flow_flush_event = threading.Event()
_flow_flush_done = threading.Event()
_flow_writer_started = False
_flow_writer_lock = threading.Lock()
# 内存级 max_flow_id 缓存：_flush_batch 插入后更新，避免 SSE 连接时查 SELECT MAX(id)
_max_flow_id_cache = 0
# 保护 _max_flow_id_cache 的并发读写（flow-writer 线程写、SSE/删除路径读改写）
_max_flow_id_lock = threading.Lock()
# 队列满时丢弃的流量计数（在 /status 端点暴露，用于监控背压）
_flow_dropped_count = 0


def _start_flow_writer():
    """Start the background writer thread (lazily started on first call)."""
    global _flow_writer_started
    with _flow_writer_lock:
        if _flow_writer_started:
            return
        _flow_writer_started = True
    t = threading.Thread(target=_flow_writer_loop, daemon=True, name="flow-writer")
    t.start()


def _flow_writer_loop():
    """Background thread: takes flow data from the queue, batch INSERTs + COMMITs.

    Performance optimization: first-item trigger mode -- after receiving the first
    item, immediately uses get_nowait to pull subsequent items to fill the batch,
    flushing as soon as BATCH_SIZE is reached or no more data is available.
    First-batch latency drops from 20ms to <1ms.
    """
    conn = sqlite3.connect(get_db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    batch: list[dict] = []
    BATCH_SIZE = 200

    while True:
        try:
            # 阻塞等待第一条（无超时，立即响应）
            item = _flow_queue.get()
        except queue.Empty:
            pass

        if item is None:
            # 哨兵：退出前提交剩余数据
            if batch:
                _flush_batch(conn, batch)
            break

        batch.append(item)
        # 首条触发：立即用 get_nowait 拉取后续凑批
        while len(batch) < BATCH_SIZE:
            try:
                item = _flow_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                if batch:
                    _flush_batch(conn, batch)
                batch.clear()
                conn.close()
                return
            batch.append(item)

        if batch:
            _flush_batch(conn, batch)
            batch.clear()

        # 通知等待 flush 的线程
        if _flow_flush_event.is_set():
            _flow_flush_event.clear()
            _flow_flush_done.set()

    conn.close()


def _flush_batch(conn: sqlite3.Connection, batch: list[dict]):
    """Batch insert flows with a single COMMIT.

    Performance optimization: uses executemany for batch insertion (5-10x faster
    than per-row execute), and infers each flow's auto-increment id via
    last_insert_rowid() + rowcount. On failure, falls back to binary search to
    locate and skip bad data.
    SSE notification: after INSERT, backfills flow.id and notifies subscribers
    (the frontend immediately receives flows with ids).
    """
    if not batch:
        return
    cols = list(batch[0].keys())
    _validate_column_names(cols)  # 安全：校验列名防 SQL 注入
    placeholders = ",".join("?" * len(cols))
    col_str = ",".join(cols)
    sql = f"INSERT INTO flows({col_str}) VALUES({placeholders})"
    try:
        # executemany 批量插入：单次 SQL 解析 + 批量执行，比逐条 execute 快 5-10x
        values = [list(f.values()) for f in batch]
        cur = conn.executemany(sql, values)
        conn.commit()
        # 反推每条 flow 的自增 id（SQLite AUTOINCREMENT 保证连续递增）
        # lastrowid 是最后一条插入的 id，rowcount 是插入条数
        last_id = cur.lastrowid
        count = len(batch)
        for i, f in enumerate(batch):
            f["id"] = last_id - count + 1 + i
        # 更新内存缓存（避免 SSE 连接时查 SELECT MAX(id)）
        global _max_flow_id_cache
        with _max_flow_id_lock:
            if last_id and last_id > _max_flow_id_cache:
                _max_flow_id_cache = last_id
        # 批量通知 SSE 订阅者（移到专用线程，不阻塞 flow-writer 的下一批写入）
        _SSE_NOTIFY_EXECUTOR.submit(_notify_flow_subscribers_batch, list(batch))
    except Exception:  # noqa: BLE001
        conn.rollback()
        # 二分定位：找出坏数据跳过，好数据仍然批量插入
        _flush_batch_split(conn, sql, batch)


def _flush_batch_split(conn: sqlite3.Connection, sql: str, batch: list[dict]):
    """Binary search to locate and skip bad data, avoiding the N-fold latency of per-row commits.

    Strategy: bisect the batch; insert the good half in bulk, continue bisecting
    the bad half, until a single row still fails and is skipped. Total commit
    count = O(log N + bad-data count), far better than N.

    Fix: successfully inserted sub-segments must backfill ids and trigger SSE
    notifications, otherwise the frontend won't see real-time packets.
    """
    if not batch:
        return
    if len(batch) == 1:
        # 单条仍失败：跳过，记日志
        try:
            cur = conn.execute(sql, list(batch[0].values()))
            conn.commit()
            # 回填 id 并 SSE 通知（单条成功也要推送）
            if cur.lastrowid:
                batch[0]["id"] = cur.lastrowid
                global _max_flow_id_cache
                with _max_flow_id_lock:
                    if cur.lastrowid > _max_flow_id_cache:
                        _max_flow_id_cache = cur.lastrowid
                _SSE_NOTIFY_EXECUTOR.submit(_notify_flow_subscribers_batch, [batch[0]])
        except Exception:  # noqa: BLE001
            conn.rollback()
        return
    mid = len(batch) // 2
    left, right = batch[:mid], batch[mid:]
    try:
        cur = conn.executemany(sql, [list(f.values()) for f in left])
        conn.commit()
        # 回填 id 并 SSE 通知
        _backfill_ids_and_notify(left, cur)
    except Exception:  # noqa: BLE001
        conn.rollback()
        _flush_batch_split(conn, sql, left)
    try:
        cur = conn.executemany(sql, [list(f.values()) for f in right])
        conn.commit()
        # 回填 id 并 SSE 通知
        _backfill_ids_and_notify(right, cur)
    except Exception:  # noqa: BLE001
        conn.rollback()
        _flush_batch_split(conn, sql, right)


def _backfill_ids_and_notify(sub_batch: list[dict], cur) -> None:
    """Backfill auto-increment ids and trigger batch SSE notification after a sub-segment insert succeeds.

    SQLite executemany's lastrowid is the id of the last inserted row, and
    rowcount is the number of inserted rows; each flow's id is inferred from these.
    """
    if not sub_batch:
        return
    last_id = cur.lastrowid
    count = len(sub_batch)
    if last_id:
        for i, f in enumerate(sub_batch):
            f["id"] = last_id - count + 1 + i
        global _max_flow_id_cache
        with _max_flow_id_lock:
            if last_id > _max_flow_id_cache:
                _max_flow_id_cache = last_id
        _SSE_NOTIFY_EXECUTOR.submit(_notify_flow_subscribers_batch, list(sub_batch))


def insert_flow_async(flow: dict):
    """Asynchronously insert a flow (enqueue, background batch write). Does not return flow_id.

    Used in non-breakpoint scenarios (_record_flow); the proxy thread does not
    need to wait for the DB write to complete.
    SSE notification is triggered in _flush_batch after INSERT (with flow.id),
    not when enqueuing here.

    When the queue is full, the oldest entry is dropped (get_nowait then put) and
    counted, to avoid blocking the proxy thread.
    """
    global _flow_dropped_count
    _start_flow_writer()
    try:
        _flow_queue.put_nowait(flow)
    except queue.Full:
        # 队列满：丢弃最旧条目腾出空间，记录丢弃计数
        try:
            _flow_queue.get_nowait()
            _flow_queue.put_nowait(flow)
            _flow_dropped_count += 1
        except queue.Empty:  # noqa: BLE001
            # 极端竞态：其他线程已取走，直接 put
            _flow_queue.put_nowait(flow)
        except queue.Full:  # noqa: BLE001
            # 仍然满：放弃本条并计数
            _flow_dropped_count += 1


# ---------- SSE 流量推送 ----------
# 性能优化：前端用 SSE 替代 500ms 轮询，新 flow 入库后立即推送给前端，
# UI 延迟从 500ms 降到 <50ms，后端查询负载降 90%。
# 存储 (queue, loop) 对：代理线程通过 loop.call_soon_threadsafe 跨线程安全投递。
_flow_subscribers: list = []  # list[(asyncio.Queue, asyncio.AbstractEventLoop)]
_flow_subscribers_lock = threading.Lock()

# 性能优化：SSE 通知在专用线程执行，不阻塞 flow-writer 线程的下一批 INSERT
# 4 workers 应对多订阅者 + 高频批量通知场景（2→4）
_SSE_NOTIFY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="sse-notify")


def register_flow_subscriber(q: "asyncio.Queue", loop: "asyncio.AbstractEventLoop") -> None:
    """Register an SSE subscriber (asyncio.Queue + its owning event loop)."""
    with _flow_subscribers_lock:
        _flow_subscribers.append((q, loop))


def unregister_flow_subscriber(q: "asyncio.Queue") -> None:
    """Unregister an SSE subscriber."""
    with _flow_subscribers_lock:
        _flow_subscribers[:] = [(q2, l) for (q2, l) in _flow_subscribers if q2 is not q]


def _notify_flow_subscribers_batch(flows: list[dict]) -> None:
    """Batch-notify all SSE subscribers in a dedicated thread (lock acquired only once, does not block flow-writer).

    Performance optimization: each subscriber gets the entire lite list delivered
    via a single call_soon_threadsafe, avoiding the N*M cross-thread scheduling
    overhead of per-item call_soon_threadsafe.
    """
    if not _flow_subscribers or not flows:
        return
    with _flow_subscribers_lock:
        subs = list(_flow_subscribers)
    if not subs:
        return
    lites = [_flow_to_lite(f) for f in flows]
    for q, loop in subs:
        try:
            # 一次性投递整个列表，事件循环线程内批量 put_nowait
            loop.call_soon_threadsafe(_batch_put_nowait, q, lites)
        except Exception:  # noqa: BLE001
            pass  # loop 已关闭，忽略


def _notify_flow_subscribers(flow: dict) -> None:
    """Notify all SSE subscribers of a new flow (single item, kept for low-volume scenarios like breakpoints)."""
    if not _flow_subscribers:
        return
    lite = _flow_to_lite(flow)
    with _flow_subscribers_lock:
        subs = list(_flow_subscribers)
    for q, loop in subs:
        try:
            loop.call_soon_threadsafe(_safe_put_nowait, q, lite)
        except Exception:  # noqa: BLE001
            pass


def _batch_put_nowait(q: "asyncio.Queue", items: list) -> None:
    """Batch put_nowait in the event loop thread; drops the rest if the queue is full."""
    for item in items:
        try:
            q.put_nowait(item)
        except Exception:  # noqa: BLE001
            pass  # QueueFull，丢弃剩余


def _safe_put_nowait(q: "asyncio.Queue", item: dict) -> None:
    """Safely put_nowait in the event loop thread; drops the item if the queue is full."""
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
    """Extract the lite fields of a flow (for SSE push, to reduce transfer size)."""
    return {k: flow.get(k) for k in _SSE_LITE_FIELDS if k in flow}


def flush_pending_flows(timeout: float = 2.0):
    """Wait for all pending flows in the queue to be written (for scenarios requiring consistency, e.g. flows/clear)."""
    if not _flow_writer_started:
        return
    # 入队一个 flush 标记，等待后台线程处理到此处
    _flow_flush_done.clear()
    _flow_flush_event.set()
    _flow_flush_done.wait(timeout=timeout)


def update_flow_response(flow_id: int, status_code, response_headers, response_body,
                         duration_ms, size):
    """Update the response fields of a flow record."""
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
    """Start the update background thread (lazily started)."""
    global _update_writer_started
    with _update_writer_lock:
        if _update_writer_started:
            return
        _update_writer_started = True
    t = threading.Thread(target=_update_writer_loop, daemon=True, name="flow-update")
    t.start()


def _update_writer_loop():
    """Background thread: batch-executes UPDATE operations.

    Performance optimization: merges multiple UPDATEs in the same batch into a
    single COMMIT, reducing write-lock contention.
    UPDATE operations are idempotent (keyed by flow_id); repeated execution has
    no side effects.
    """
    conn = sqlite3.connect(get_db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    batch: list[dict] = []
    BATCH_SIZE = 50
    FLUSH_TIMEOUT = 0.02  # 20ms（与 insert 对齐，响应字段更新更及时）

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
    """Batch-execute UPDATEs (each row executed separately, a single commit at the end)."""
    fail_count = 0
    for item in batch:
        try:
            sql = item["sql"]
            params = item["params"]
            conn.execute(sql, params)
        except Exception as e:  # noqa: BLE001
            # 修复审计 6.2：原代码完全静默吞掉所有 UPDATE 错误，导致响应字段更新
            # 持续失败时无法诊断。这里记录 warning 便于排查（不抛出避免阻塞写入线程）
            fail_count += 1
            if fail_count <= 3:  # 限制日志量，避免大批量失败时刷屏
                try:
                    _logger.warning(
                        "flush_update_batch SQL execution failed: %s: %s, params=%r",
                        type(e).__name__, e, params,
                    )
                except Exception:  # noqa: BLE001
                    pass
    if fail_count > 3:
        try:
            _logger.warning("flush_update_batch has %d more unrecorded failures", fail_count - 3)
        except Exception:  # noqa: BLE001
            pass
    try:
        conn.commit()
    except Exception as e:  # noqa: BLE001
        # 修复审计 6.2：commit 失败也记录日志
        try:
            _logger.warning("flush_update_batch commit failed: %s: %s",
                            type(e).__name__, e)
        except Exception:  # noqa: BLE001
            pass
        conn.rollback()


def update_flow_response_async(flow_id: int, status_code, response_headers, response_body,
                                duration_ms, size):
    """Asynchronously update response fields (enqueue, background batch execution).
    Does not block the proxy thread.

    Used for response recording in non-breakpoint scenarios. Breakpoint scenarios
    require synchronous visibility and still use update_flow_response.
    """
    _start_update_writer()
    _update_queue.put({
        "sql": "UPDATE flows SET status_code=?, response_headers=?, response_body=?, "
               "duration_ms=?, size=? WHERE id=?",
        "params": (status_code, response_headers, response_body, duration_ms, size, flow_id),
    })


def update_flow_breakpoint(flow_id: int, status: str | None):
    """Update the breakpoint status (synchronous; breakpoint status must be immediately visible to the frontend)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE flows SET breakpoint_status=? WHERE id=?", (status, flow_id)
        )


def update_flow_request(flow_id: int, method: str, url: str, host: str, path: str,
                        request_headers: str, request_body: str):
    """Update request fields on breakpoint release (with user-modified values)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE flows SET method=?, url=?, host=?, path=?, request_headers=?, "
            "request_body=? WHERE id=?",
            (method, url, host, path, request_headers, request_body, flow_id),
        )


def update_flow_response_fields(flow_id: int, status_code, response_headers: str,
                                response_body: str):
    """Update response fields on breakpoint release (with user-modified values)."""
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
              tag: str | None = None,
              lite: bool = False,
              has_tags: bool = False) -> list[dict]:
    """Get the flow list for a session, supporting filtering by host/process/status code/method/incremental/protocol/tag.

    When lite=True, the SELECT does not include request_body / response_body /
    raw_data fields, used for list acceleration (details are fetched separately
    via GET /flows/{id} when selected).
    """
    # lite 模式：排除大字段（request_body/response_body/raw_data），只返回列表展示所需字段
    select_cols = (
        "id, session_id, timestamp, pid, process_name, method, url, scheme, "
        "host, path, request_headers, status_code, response_headers, "
        "duration_ms, size, breakpoint_status, tags, tag_note, protocol, "
        "src_port, dst_port, remote_ip, ip_region, cert_info, http_version"
        if lite else "*"
    )
    sql = f"SELECT {select_cols} FROM flows WHERE session_id=?"
    args: list = [session_id]
    if host:
        sql += " AND host LIKE ?"
        args.append(f"%{host}%")
    if process:
        sql += " AND process_name LIKE ?"
        args.append(f"%{process}%")
    if status_code is not None:
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
    if has_tags:
        sql += " AND tags IS NOT NULL AND tags != ''"
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
    """Search flows by body regex/binary. session_id=0 means search across all sessions.

    Performance optimization: exact field filters (method/status/pid/process_name)
    are pushed down to SQL WHERE, and only the columns needed for search are
    projected (excluding large fields like raw_data), avoiding loading all flows
    with all columns into memory.
    """
    # 只投影搜索所需列，避免 SELECT * 拉取 raw_data 等大字段
    proj = ("id, session_id, method, status_code, pid, process_name, "
            "url, path, request_body, response_body, request_headers, response_headers")
    clauses = []
    args = []
    if session_id and session_id > 0:
        clauses.append("session_id=?")
        args.append(session_id)
    if method:
        clauses.append("UPPER(method)=UPPER(?)")
        args.append(method)
    if status_code is not None:
        clauses.append("status_code=?")
        args.append(status_code)
    if pid:
        clauses.append("pid=?")
        args.append(pid)
    if process_name:
        clauses.append("process_name=?")
        args.append(process_name)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT {proj} FROM flows{where} ORDER BY id DESC LIMIT ?"
    args.append(10000)
    # P2-4 修复：分批游标替代一次性 fetchall(10000)。
    # 原实现把最多 10000 行（含 request_body/response_body 大字段）全量载入内存再
    # Python 逐行正则匹配，大 body 场景内存峰值可达数百 MB、CPU 耗时百 ms~秒级，
    # 且即便命中 limit(200) 条也拉满了 10000 行。改为 fetchmany(500) 分批：
    # - 内存峰值降至 500 行；
    # - 命中达到 limit 即停止后续扫描，避免无谓拉取。
    # SQL 仍带 LIMIT 10000 限制总扫描量，正确性不受影响（结果上限仍为 limit）。
    rx = re.compile(body_regex) if body_regex else None
    hrx = re.compile(header_regex) if header_regex else None
    needle = None
    if binary_hex:
        try:
            needle = bytes.fromhex(binary_hex.replace(" ", "").replace("0x", ""))
        except ValueError:
            pass
    results = []
    with get_connection() as conn:
        cur = conn.execute(sql, args)
        while len(results) < limit:
            batch = cur.fetchmany(500)
            if not batch:
                break
            for r in batch:
                f = dict(r)
                matched = False
                if rx:
                    for field in ("request_body", "response_body", "url", "path"):
                        val = f.get(field) or ""
                        if val.startswith("base64:"):
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
    """Group statistics by host/method/status."""
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
    """Get the maximum flow id within a session (used as the baseline for incremental queries)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(id) as m FROM flows WHERE session_id=?", (session_id,)
        ).fetchone()
        return row["m"] or 0


def get_max_flow_id_all() -> int:
    """Get the global maximum flow id (used for SSE initial baseline + incremental polling).

    Performance optimization: prefers reading the in-memory cache (updated by
    _flush_batch on write), avoiding a SQLite query on every SSE connection.
    """
    global _max_flow_id_cache
    with _max_flow_id_lock:
        if _max_flow_id_cache > 0:
            return _max_flow_id_cache
        with get_connection() as conn:
            row = conn.execute("SELECT MAX(id) as m FROM flows").fetchone()
            _max_flow_id_cache = row["m"] or 0
            return _max_flow_id_cache


def count_flows(session_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM flows WHERE session_id=?", (session_id,)
        ).fetchone()
        return row["c"]


def count_flows_batch(session_ids: list[int]) -> dict[int, int]:
    """Batch-count flows for multiple sessions. Returns {session_id: count}."""
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
    """Global tag statistics: parses the tags field (comma-separated) of all flows,
    returns [{tag, count}] sorted by count descending.
    """
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
    """Clear all flows of a session."""
    with get_connection() as conn:
        conn.execute("DELETE FROM flows WHERE session_id=?", (session_id,))


def get_all_flows(limit: int = 200, offset: int = 0,
                  host: str | None = None, process: str | None = None,
                  status_code: int | None = None, method: str | None = None,
                  protocol: str | None = None, since_id: int = 0,
                  path: str | None = None, url: str | None = None,
                  tag: str | None = None,
                  lite: bool = False,
                  skip_total: bool = False,
                  has_tags: bool = False) -> tuple[list[dict], int | None]:
    """Query all flows across sessions (for global analysis), returns (flows, total).

    When lite=True, only lightweight fields are returned (excluding
    request_body/response_body/request_headers/response_headers/raw_data), used
    for global analysis list acceleration (details are fetched separately via
    GET /flows/{id} when selected).
    """
    where = []
    args: list = []
    if host:
        where.append("host LIKE ?")
        args.append(f"%{host}%")
    if process:
        where.append("process_name LIKE ?")
        args.append(f"%{process}%")
    if status_code is not None:
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
    if has_tags:
        where.append("tags IS NOT NULL AND tags != ''")
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
        if skip_total:
            total = None
        else:
            total = conn.execute(f"SELECT COUNT(*) FROM flows{where_sql}", args).fetchone()[0]
        sql = f"SELECT {select_cols} FROM flows{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?"
        rows = conn.execute(sql, args + [limit, offset]).fetchall()
        return [dict(r) for r in rows], total


def delete_all_flows() -> int:
    """Clear all flows (across sessions), returns the number of deleted rows.

    Audit fix 7.4: the original code set _max_flow_id_cache=0 before DELETE,
    creating a window: other threads calling get_max_flow_id_all during this
    window would query the DB and get the old max, then cache it; after DELETE
    the cache holds a stale value. Changed to DELETE first, then set to 0.
    """
    global _max_flow_id_cache
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM flows")
        rowcount = cur.rowcount
    # DELETE 成功后再置 0，避免并发查询读到旧 max 并缓存
    with _max_flow_id_lock:
        _max_flow_id_cache = 0
    return rowcount


def reset_max_flow_id():
    """Reset the in-memory max_flow_id cache (called after clearing flows, so the next query re-reads from DB)."""
    global _max_flow_id_cache
    with _max_flow_id_lock:
        _max_flow_id_cache = 0


def get_flows_stats(group_by: str = "host",
                    host: str | None = None, process: str | None = None) -> list[dict]:
    """Full-volume flow group statistics (unpaginated), used for the stats chart to show the proportion of all data.

    group_by: host / process / content_type / status_code / method
    Returns [{key, label, count}] sorted by count descending.
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

    # 安全护栏：group_by 对应的列名只允许来自固定白名单，
    # 杜绝任何经由该列名拼接进 SQL 的结构化注入（纵深防御）。
    _ALLOWED_STATS_COLS = {"host", "process_name", "method", "ip_region"}
    if col is not None and col not in _ALLOWED_STATS_COLS:
        col = None

    with get_connection() as conn:
        if col is not None:
            sql = (
                f"SELECT COALESCE(NULLIF({col}, ''), '{null_label}') AS k, COUNT(*) AS c "
                f"FROM flows{where_sql} GROUP BY k ORDER BY c DESC"
            )
            rows = conn.execute(sql, args).fetchall()
            return [{"key": r["k"], "label": r["k"], "count": r["c"]} for r in rows]
        # status_code：SQL 聚合，避免全量读取到内存
        if group_by == "status_code":
            extra_clauses = list(where)
            extra_clauses.append("status_code IS NOT NULL AND status_code != 0")
            extra_sql = " WHERE " + " AND ".join(extra_clauses) if extra_clauses else ""
            sql = (
                f"SELECT CASE "
                f"WHEN status_code < 200 THEN '1xx 信息' "
                f"WHEN status_code < 300 THEN '2xx 成功' "
                f"WHEN status_code < 400 THEN '3xx 重定向' "
                f"WHEN status_code < 500 THEN '4xx 客户端错误' "
                f"ELSE '5xx 服务器错误' END AS k, COUNT(*) AS c "
                f"FROM flows{extra_sql} GROUP BY 1 ORDER BY c DESC"
            )
            rows = conn.execute(sql, args).fetchall()
            return [{"key": r["k"], "label": r["k"], "count": r["c"]} for r in rows]
        # content_type：Python 聚合（response_headers 是 JSON 文本，SQL 无法直接提取）
        sql = f"SELECT method, status_code, response_headers FROM flows{where_sql}"
        rows = conn.execute(sql, args).fetchall()
        buckets: dict[str, int] = {}
        for r in rows:
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
    """Cross-session multi-dimensional aggregate statistics (returns all dimensions at once),
    for the CoolUI dashboard.

    Returns {total, total_bytes, incoming_bytes, outgoing_bytes,
             success_count, error_count, avg_duration_ms,
             by_protocol, by_method, by_status_range,
             by_host, by_process, by_ip_region}
    Each group is a [{key, count, bytes?}] list, sorted by count descending, top 20 entries.
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
            if code == 0:
                status_buckets["other"] += r["c"]
            elif code < 200:
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
    """Delete all flows with id < flow_id (clean up old data by id). Returns the number of deleted rows."""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM flows WHERE id < ?", (flow_id,))
        return cur.rowcount


def delete_flow(flow_id: int):
    """Delete a single flow."""
    with get_connection() as conn:
        conn.execute("DELETE FROM flows WHERE id=?", (flow_id,))


def delete_flow_batch(flow_ids: list[int]):
    """Batch-delete flows."""
    if not flow_ids:
        return
    with get_connection() as conn:
        placeholders = ",".join("?" * len(flow_ids))
        conn.execute(f"DELETE FROM flows WHERE id IN ({placeholders})", flow_ids)


def get_pending_flow_ids(flow_ids: list[int]) -> list[int]:
    """From the given id list, return the ids that have a breakpoint status (breakpoint_status IS NOT NULL).
    A single SQL query, avoiding the N+1 queries during batch release.
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
    """Get all flows currently paused at a breakpoint."""
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
    _validate_column_names(cols)  # 安全：校验列名防 SQL 注入
    placeholders = ",".join("?" * len(cols))
    col_str = ",".join(cols)
    with get_connection() as conn:
        conn.execute(
            f"INSERT INTO auto_reply_rules({col_str}) VALUES({placeholders})",
            list(rule.values()),
        )


def add_rule(rule: dict) -> str:
    """Insert a rule and return the generated ID."""
    import uuid
    rule_id = str(uuid.uuid4())[:8]
    rule["id"] = rule_id
    now = datetime.now().isoformat()
    rule.setdefault("created_at", now)
    rule.setdefault("updated_at", now)
    insert_rule(rule)
    return rule_id


def update_rule(rule_id: str, updates: dict):
    _validate_column_names(updates.keys())  # 安全：校验列名防 SQL 注入
    sets = ",".join(f"{k}=?" for k in updates.keys())
    values = list(updates.values()) + [rule_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE auto_reply_rules SET {sets} WHERE id=?", values)


def delete_rule(rule_id: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM auto_reply_rules WHERE id=?", (rule_id,))


def increment_rule_hit(rule_id: str, flow_id: int | None = None):
    """§3.2 Hit counting: increments hit_count, updates last_hit_at and last_hit_flow_id."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE auto_reply_rules SET hit_count = hit_count + 1, "
            "last_hit_at = ?, last_hit_flow_id = ? WHERE id = ?",
            (now, flow_id, rule_id),
        )


def delete_all_rules() -> int:
    """Clear all auto-reply rules (called before environment snapshot import). Returns the number of deleted rows."""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM auto_reply_rules")
        return cur.rowcount


# ---------- flow tags（§3.1 流量标签）----------

def update_flow_tags(flow_id: int, tags: str, tag_note: str | None = None):
    """Update flow tags (comma-separated string) and optional note.

    When tag_note is None, the note field is not modified (keeps its original value).
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
    """Query all flows with the given tag (across sessions). tag exactly matches one item in the comma-separated list."""
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
    """Create a flow group, returns the auto-increment id. flow_ids are stored as a comma-separated string."""
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
    """Update a group. name or flow_ids being None means that field is not updated."""
    updates = {}
    if name is not None:
        updates["name"] = name
    if flow_ids is not None:
        updates["flow_ids"] = ",".join(str(i) for i in flow_ids)
    if not updates:
        return
    _validate_column_names(updates.keys())  # 安全：校验列名防 SQL 注入
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
    """List of ignored host wildcards (sorted by creation time descending)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ignored_hosts ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def add_ignored_host(host_pattern: str) -> dict:
    """Add an ignored host wildcard (ignored if duplicate)."""
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
    """Create an AI analysis record, returns the chat id."""
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
    """Add a chat message, returns the message id."""
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
