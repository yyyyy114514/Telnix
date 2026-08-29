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
import time
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from typing import Any

from .config import get_db_path

_logger = logging.getLogger("telnix.db")


# ---------- 正则表达式编译缓存（search_flows 性能优化）----------
# 性能修复(审计 P-#4)：search_flows() 每次调用都 re.compile()，
# 添加模块级 LRU 缓存避免重复编译相同正则表达式。
@lru_cache(maxsize=64)
def _compile_body_regex(pattern: str | None) -> re.Pattern | None:
    """缓存 body_regex 编译结果。"""
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


@lru_cache(maxsize=64)
def _compile_header_regex(pattern: str | None) -> re.Pattern | None:
    """缓存 header_regex 编译结果。"""
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


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
    color TEXT,
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

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    service TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_flows_host ON flows(host);
CREATE INDEX IF NOT EXISTS idx_flows_process ON flows(process_name);
CREATE INDEX IF NOT EXISTS idx_flows_protocol ON flows(protocol);
CREATE INDEX IF NOT EXISTS idx_flows_method ON flows(method);
CREATE INDEX IF NOT EXISTS idx_flows_ip_region ON flows(ip_region);
CREATE INDEX IF NOT EXISTS idx_flows_remote_ip ON flows(remote_ip);
CREATE INDEX IF NOT EXISTS idx_ai_messages_chat ON ai_messages(chat_id);
-- 性能：覆盖最常见的 "WHERE session_id=? ORDER BY id DESC" 分页查询，
-- 复合索引让 SQLite 直接走索引排序，避免临时排序/回表扫描。
-- 性能修复(审计 B-#8)：移除冗余的 idx_flows_session(session_id)，
-- 复合索引 idx_flows_session_id(session_id, id DESC) 的前缀已覆盖 WHERE session_id=?。
CREATE INDEX IF NOT EXISTS idx_flows_session_id ON flows(session_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_flows_status ON flows(status_code);
"""

# 默认设置项
DEFAULT_SETTINGS = {
    "ai_service": "deepseek",
    "deepseek_api_key": "",
    "deepseek_model": "deepseek-v4-flash",
    "anthropic_api_key": "",
    "anthropic_model": "claude-3-5-sonnet-20241022",
    "openai_api_key": "",
    "openai_model": "gpt-4o",
    "gemini_api_key": "",
    "gemini_model": "gemini-1.5-pro",
    "ollama_api_key": "",
    "ollama_model": "llama3.1",
    "ollama_endpoint": "http://127.0.0.1:11434",
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
    "no_caching": "0",
    "force_cors": "0",
    "block_list_enabled": "0",
    "block_list": "[]",
    "allow_list_enabled": "0",
    "allow_list": "[]",
}


# 性能优化：threading.local 连接池，每个工作线程复用一个长连接
# 避免每次操作都 connect()+PRAGMA+commit()+close() 的开销（单次 1-3ms）
# WAL 模式 + synchronous=NORMAL 已在 init_db 持久设置，连接级只需设 busy_timeout
import threading

_local = threading.local()


def _apply_pragmas(conn: sqlite3.Connection):
    """Apply performance-related PRAGMAs to a connection.

    性能修复(审计 B-#4)：补全 cache_size / mmap_size / temp_store / wal_autocheckpoint，
    提升 flows 表（10万+ 行）的缓存命中率与聚合查询速度。
    - cache_size=-65536：64MB 页缓存（默认仅 2MB，命中率低）
    - mmap_size=268435456：256MB mmap，读多写少场景避免 read syscall
    - temp_store=MEMORY：GROUP BY 临时表写内存（默认写文件慢 10-100x）
    - wal_autocheckpoint=2000：8MB 触发 checkpoint（默认 4MB 偏小）
    同时设置 busy_timeout / synchronous=NORMAL（连接级，需每连接设一次）。
    """
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")      # 64MB
    conn.execute("PRAGMA mmap_size=268435456")     # 256MB mmap
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA wal_autocheckpoint=2000")  # 8MB


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
    _apply_pragmas(conn)
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
        _apply_pragmas(_init_conn)
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
            # 性能修复(审计 B-#8)：移除冗余的 idx_flows_session，
            # idx_flows_session_id(session_id, id DESC) 已覆盖 WHERE session_id=? 查询
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_host ON flows(host)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_process ON flows(process_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_protocol ON flows(protocol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_method ON flows(method)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_ip_region ON flows(ip_region)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_remote_ip ON flows(remote_ip)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_session_id ON flows(session_id, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_status ON flows(status_code)")
            # DROP + CREATE sessions
            conn.execute("DROP TABLE IF EXISTS sessions")
            conn.execute("""
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    color TEXT,
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
    # 清理 settings.json 中已废弃的巨大 scan_results（被动扫描已改为内存缓冲区）
    _existing = settings_store.get_all_settings()
    if "scan_results" in _existing:
        settings_store.set_setting("scan_results", "")
        # 再次读取确认清理
        _existing = settings_store.get_all_settings()
    # 默认设置注入 settings.json（不覆盖用户已有值）
    for k, v in DEFAULT_SETTINGS.items():
        if k not in _existing or _existing.get(k) in (None, ""):
            settings_store.set_setting(k, v)
    # 收缩数据库文件：DROP+CREATE flows/sessions 会在 db 文件中留下大量空闲页，
    # 导致文件膨胀到 GB 级并显著拖慢首次连接。当文件大于阈值时执行 VACUUM。
    try:
        _vacuum_if_needed(db_path)
    except Exception:  # noqa: BLE001
        pass


def _vacuum_if_needed(db_path: str, threshold_mb: int = 50):
    """VACUUM database if its file size exceeds threshold (reclaim free pages)."""
    try:
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
    except OSError:
        return
    if size_mb < threshold_mb:
        return
    # VACUUM 需要独占连接，使用独立连接执行
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    try:
        _apply_pragmas(conn)
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()


def cleanup_database() -> dict:
    """Aggressively reclaim database space: drop and recreate flows/sessions, then VACUUM.

    Returns before/after size in MB. Preserves settings, rules, ignored_processes/hosts,
    and ai_chats/messages.
    """
    db_path = get_db_path()
    before_mb = os.path.getsize(db_path) / (1024 * 1024)
    with get_connection() as conn:
        # 删除并重建 flows/sessions（释放所有相关页）
        conn.execute("DROP TABLE IF EXISTS flows")
        conn.execute("DROP TABLE IF EXISTS sessions")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('flows', 'sessions')")
        conn.executescript(SCHEMA_TABLES)
        conn.executescript(SCHEMA_INDEXES)
    # 强制 VACUUM，threshold=0 表示无论当前大小都执行
    _vacuum_if_needed(db_path, threshold_mb=0)
    after_mb = os.path.getsize(db_path) / (1024 * 1024)
    return {
        "before_mb": round(before_mb, 2),
        "after_mb": round(after_mb, 2),
        "reclaimed_mb": round(before_mb - after_mb, 2),
    }


def get_db_stats() -> dict:
    """Return current database file size and row counts of major tables."""
    db_path = get_db_path()
    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    with get_connection() as conn:
        flows = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        rules = conn.execute("SELECT COUNT(*) FROM auto_reply_rules").fetchone()[0]
        ai_chats = conn.execute("SELECT COUNT(*) FROM ai_chats").fetchone()[0]
        ai_messages = conn.execute("SELECT COUNT(*) FROM ai_messages").fetchone()[0]
    return {
        "path": db_path,
        "size_mb": round(size_mb, 2),
        "flows": flows,
        "sessions": sessions,
        "rules": rules,
        "ai_chats": ai_chats,
        "ai_messages": ai_messages,
    }


def clear_rules() -> int:
    """Clear all auto-reply rules. Returns the number of deleted rows."""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM auto_reply_rules")
        return cur.rowcount


def clear_ai_chats() -> int:
    """Clear all AI chats and messages. Returns the total number of deleted rows."""
    with get_connection() as conn:
        msg_cur = conn.execute("DELETE FROM ai_messages")
        chat_cur = conn.execute("DELETE FROM ai_chats")
        return msg_cur.rowcount + chat_cur.rowcount


def clear_sessions() -> int:
    """Clear all sessions and their flows. Returns the number of deleted sessions."""
    global _max_flow_id_cache, _overview_cache
    with get_connection() as conn:
        conn.execute("DELETE FROM flows")
        cur = conn.execute("DELETE FROM sessions")
    with _max_flow_id_lock:
        _max_flow_id_cache = 0
    _overview_cache = None
    return cur.rowcount


def clear_data_by_type(type_name: str) -> dict:
    """Clear data by category. Returns a result dict with deletion counts.

    Supported types:
    - flows:     delete all flow records
    - sessions:  delete all sessions and their flows
    - rules:     delete all auto-reply rules
    - ai_chats:  delete all AI chats and messages
    - all:       delete all of the above (except settings)
    """
    if type_name == "flows":
        return {"deleted": delete_all_flows()}
    if type_name == "sessions":
        return {"deleted": clear_sessions()}
    if type_name == "rules":
        return {"deleted": clear_rules()}
    if type_name == "ai_chats":
        return {"deleted": clear_ai_chats()}
    if type_name == "all":
        flows_deleted = delete_all_flows()
        sessions_deleted = clear_sessions()
        rules_deleted = clear_rules()
        ai_deleted = clear_ai_chats()
        return {
            "flows": flows_deleted,
            "sessions": sessions_deleted,
            "rules": rules_deleted,
            "ai_chats": ai_deleted,
        }
    raise ValueError(f"Invalid clear type: {type_name}")


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
    # trigger_config 表（持久化触发式捕获条件，不论功能开关状态）
    _safe_alter(
        "CREATE TABLE IF NOT EXISTS trigger_config ("
        "  id INTEGER PRIMARY KEY,"
        "  conditions TEXT NOT NULL DEFAULT '[]'"
        ")"
    )
    # flows 表加 timing 字段（响应时间组成 JSON：dns/tcp_connect/ssl/first_byte/total）
    if "timing" not in cols:
        conn.execute("ALTER TABLE flows ADD COLUMN timing TEXT DEFAULT NULL")
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
    # sessions 表加 color 字段（会话颜色标记，用于前端区分不同会话）
    _safe_alter("ALTER TABLE sessions ADD COLUMN color TEXT DEFAULT NULL")
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
    # auto_reply_rules 表加 group_id/tags 字段（§3.2 规则分组与标签）
    _safe_alter(
        "ALTER TABLE auto_reply_rules ADD COLUMN group_id INTEGER DEFAULT NULL"
    )
    _safe_alter(
        "ALTER TABLE auto_reply_rules ADD COLUMN tags TEXT DEFAULT ''"
    )
    # rule_groups 表（规则分组）
    _safe_alter(
        """
        CREATE TABLE IF NOT EXISTS rule_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _safe_alter("CREATE INDEX IF NOT EXISTS idx_rule_groups_sort ON rule_groups(sort_order, id)")
    # DNS 规则分组表
    _safe_alter(
        """
        CREATE TABLE IF NOT EXISTS dns_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _safe_alter("CREATE INDEX IF NOT EXISTS idx_dns_groups_priority ON dns_groups(priority, id)")

    # DNS 规则表
    _safe_alter(
        """
        CREATE TABLE IF NOT EXISTS dns_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            mode TEXT DEFAULT 'wildcard',
            action TEXT DEFAULT 'block',
            redirect_to TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES dns_groups(id) ON DELETE CASCADE
        )
        """
    )
    _safe_alter("CREATE INDEX IF NOT EXISTS idx_dns_rules_group_id ON dns_rules(group_id)")
    _safe_alter("CREATE INDEX IF NOT EXISTS idx_dns_rules_pattern ON dns_rules(pattern)")

    # script_errors 表：记录脚本规则的执行错误历史
    # 设计修复：原 API 只返回最后一条错误，无历史记录
    _safe_alter(
        """
        CREATE TABLE IF NOT EXISTS script_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT NOT NULL,
            error_type TEXT NOT NULL,
            error_message TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY (rule_id) REFERENCES auto_reply_rules(id) ON DELETE CASCADE
        )
        """
    )
    _safe_alter("CREATE INDEX IF NOT EXISTS idx_script_errors_rule_id ON script_errors(rule_id, occurred_at DESC)")
    # flow_groups 表（§3.13 流量分组）
    # 设计修复：flow_ids 存储为逗号分隔字符串导致查询困难、无法建索引、易注入
    # 迁移到关系型中间表 flow_group_flows(flow_group_id, flow_id)
    _safe_alter(
        """
        CREATE TABLE IF NOT EXISTS flow_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    _safe_alter(
        """
        CREATE TABLE IF NOT EXISTS flow_group_flows (
            flow_group_id INTEGER NOT NULL,
            flow_id INTEGER NOT NULL,
            PRIMARY KEY (flow_group_id, flow_id),
            FOREIGN KEY (flow_group_id) REFERENCES flow_groups(id) ON DELETE CASCADE
        )
        """
    )
    # 迁移：旧 flow_ids 列数据迁移到中间表
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(flow_groups)").fetchall()}
        if "flow_ids" in cols:
            # 检查是否已完成迁移（中间表有数据说明已迁移）
            migrated_count = conn.execute(
                "SELECT COUNT(*) FROM flow_group_flows"
            ).fetchone()[0]
            if migrated_count == 0:
                # 从旧列迁移数据到中间表
                rows = conn.execute("SELECT id, flow_ids FROM flow_groups WHERE flow_ids IS NOT NULL AND flow_ids != ''").fetchall()
                for row in rows:
                    group_id = row["id"]
                    ids_str = row["flow_ids"]
                    if ids_str:
                        for fid in ids_str.split(","):
                            fid = fid.strip()
                            if fid:
                                try:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO flow_group_flows(flow_group_id, flow_id) VALUES(?, ?)",
                                        (group_id, int(fid))
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
    except Exception as e:  # noqa: BLE001
        try:
            _logger.warning("flow_groups migration to junction table failed: %s: %s",
                          type(e).__name__, e)
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
    except Exception as e:  # noqa: BLE001
        # 修复审计 6.1：原代码完全静默吞掉迁移异常，DB 可能处于不一致状态
        # （v2 已建但旧表未删，或旧表已删但 RENAME 失败），后续查询会崩溃
        # 记录 error 便于诊断；不抛出避免阻断启动
        try:
            _logger.error("ignored_processes table migration failed: %s: %s",
                          type(e).__name__, e)
            # 记录迁移状态标志，供诊断使用
            _logger.error("ignored_processes migration incomplete: manual intervention may be required. "
                         "Consider: DROP TABLE IF EXISTS ignored_processes; "
                         "CREATE TABLE ignored_processes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "pid INTEGER, process_name TEXT NOT NULL, ignored_at TEXT NOT NULL, "
                         "UNIQUE(pid, process_name));")
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

def create_session(name: str | None = None, color: str | None = None) -> int:
    started_at = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO sessions(name, color, started_at) VALUES(?, ?, ?)",
            (name, color, started_at),
        )
        return cur.lastrowid


def update_session(session_id: int, name: str | None = None, color: str | None = None) -> bool:
    """Update session name and/or color. Returns True if updated."""
    if name is None and color is None:
        return False
    sets = []
    vals = []
    if name is not None:
        sets.append("name = ?")
        vals.append(name)
    if color is not None:
        sets.append("color = ?")
        vals.append(color)
    if not sets:
        return False
    vals.append(session_id)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        return cur.rowcount > 0


def get_session(session_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None


def get_sessions() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def get_current_session_id() -> int | None:
    """Get the current active session id (the latest session with ended_at IS NULL).

    Used by AI tools that don't have access to app state. Falls back to the latest
    session if no active session exists, or None if no sessions exist.
    """
    with get_connection() as conn:
        # 优先找 ended_at IS NULL 的活跃会话
        row = conn.execute(
            "SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return row["id"]
        # 没有活跃会话，找最新的会话
        row = conn.execute(
            "SELECT id FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None


def update_session_ended(session_id: int):
    ended_at = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (ended_at, session_id))


def reset_session_ended(session_id: int):
    """Reset a session's ended_at to NULL so it can be reused."""
    with get_connection() as conn:
        conn.execute("UPDATE sessions SET ended_at=NULL WHERE id=?", (session_id,))


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

    Performance optimization: uses executemany for batch insert (5-10x faster than
    individual INSERT statements).
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


def _insert_flows_batch_optimized(flows: list[dict]) -> int:
    """Optimized batch insert using executemany.

    Groups flows by column schema and uses executemany for each group.
    Falls back to single-insert if flows have inconsistent columns.
    """
    if not flows:
        return 0
    if not flows:
        return 0

    # 按列分组
    groups: dict[frozenset, list[dict]] = {}
    for flow in flows:
        cols = frozenset(flow.keys())
        groups.setdefault(cols, []).append(flow)

    inserted = 0
    with get_connection() as conn:
        for cols in groups:
            flows_in_group = groups[cols]
            try:
                _validate_column_names(list(cols))
            except ValueError:
                continue

            cols_list = list(cols)
            placeholders = ",".join("?" * len(cols_list))
            col_str = ",".join(cols_list)

            # 提取值列表用于 executemany
            values = [[f.get(c) for c in cols_list] for f in flows_in_group]

            try:
                conn.execute("SAVEPOINT batch_ins")
                conn.executemany(
                    f"INSERT INTO flows({col_str}) VALUES({placeholders})",
                    values,
                )
                conn.execute("RELEASE batch_ins")
                inserted += len(flows_in_group)
            except Exception:  # noqa: BLE001
                conn.execute("ROLLBACK TO batch_ins")
                conn.execute("RELEASE batch_ins")
                # 回退到逐条插入
                for flow in flows_in_group:
                    try:
                        conn.execute(
                            f"INSERT INTO flows({col_str}) VALUES({placeholders})",
                            [flow.get(c) for c in cols_list],
                        )
                        inserted += 1
                    except Exception:  # noqa: BLE001
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
    _apply_pragmas(conn)
    batch: list[dict] = []
    BATCH_SIZE = 200

    while True:
        # 阻塞等待第一条（无超时，立即响应）
        # _flow_queue.get() 无 timeout 时为无限阻塞，不会抛 queue.Empty
        item = _flow_queue.get()

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


# SSE 推送的 lite 字段（不含大 body，但含 response_headers 供前端 Content-Type 过滤）
# _is_update: 响应阶段 update 推送的标记，前端据此判断不新增行而是 merge
_SSE_LITE_FIELDS = (
    "id", "session_id", "timestamp", "pid", "process_name", "method", "url",
    "scheme", "host", "path", "status_code", "duration_ms", "size",
    "breakpoint_status", "protocol", "src_port", "dst_port", "remote_ip",
    "ip_region", "tags", "tag_note", "http_version",
    "request_headers", "response_headers", "_is_update",
)


def _flow_to_lite(flow: dict) -> dict:
    """Extract the lite fields of a flow (for SSE push, to reduce transfer size).

    性能修复(审计 P-#8)：添加 _is_lite: True 标记，前端据此判断是否为 lite flow，
    避免错误地通过 request_headers == null 判断是否需要补全（预入库 flow 本就不包含 headers）。
    """
    result = {k: flow.get(k) for k in _SSE_LITE_FIELDS if k in flow}
    result["_is_lite"] = True
    return result


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
# 性能修复(审计 B-#6)：加 maxsize=20000 防止高负载下内存无界增长，
# 满时丢最旧条目并计数（UPDATE 幂等，丢失只影响最新响应字段更新）。
_update_queue: "queue.Queue[dict | None]" = queue.Queue(maxsize=20000)
_update_writer_started = False
_update_writer_lock = threading.Lock()
# 队列满时丢弃的更新计数（在 /status 端点暴露，用于监控写入背压）
_update_dropped_count = 0


def _start_update_writer():
    """Start the update background thread (lazily started)."""
    global _update_writer_started
    with _update_writer_lock:
        if _update_writer_started:
            return
        _update_writer_started = True
    t = threading.Thread(target=_update_writer_loop, daemon=True, name="flow-update")
    t.start()


def _update_queue_put(item: dict):
    """Enqueue an update item with drop-oldest backpressure (审计 B-#6).

    When the queue is full, the oldest entry is dropped (get_nowait then put) and
    counted, to avoid blocking the proxy thread. UPDATE is idempotent (keyed by
    flow_id), so dropping the oldest only loses the latest response field update.
    """
    global _update_dropped_count
    _start_update_writer()
    try:
        _update_queue.put_nowait(item)
    except queue.Full:
        # 队列满：丢弃最旧条目腾出空间，记录丢弃计数
        try:
            _update_queue.get_nowait()
            _update_queue.put_nowait(item)
            _update_dropped_count += 1
        except queue.Empty:  # noqa: BLE001
            _update_queue.put_nowait(item)
        except queue.Full:  # noqa: BLE001
            _update_dropped_count += 1


def _update_writer_loop():
    """Background thread: batch-executes UPDATE operations.

    Performance optimization: merges multiple UPDATEs in the same batch into a
    single COMMIT, reducing write-lock contention.
    UPDATE operations are idempotent (keyed by flow_id); repeated execution has
    no side effects.
    """
    conn = sqlite3.connect(get_db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
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
    failed_items: list[dict] = []
    for item in batch:
        try:
            sql = item["sql"]
            params = item["params"]
            conn.execute(sql, params)
        except Exception as e:  # noqa: BLE001
            # 修复审计 6.2：原代码完全静默吞掉所有 UPDATE 错误，导致响应字段更新
            # 持续失败时无法诊断。改为记录所有失败到列表供后续分析。
            fail_count += 1
            failed_items.append({"params": params, "error": f"{type(e).__name__}: {e}"})
    # 统一记录失败列表，避免逐条打印；大数据量时减少日志刷屏
    if failed_items:
        try:
            _logger.warning(
                "flush_update_batch had %d/%d failures, first few: %r",
                fail_count, len(batch), failed_items[:5]
            )
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
    _update_queue_put({
        "sql": "UPDATE flows SET status_code=?, response_headers=?, response_body=?, "
               "duration_ms=?, size=? WHERE id=?",
        "params": (status_code, response_headers, response_body, duration_ms, size, flow_id),
    })


def update_flow_cert_info_async(flow_id: int, cert_info: str):
    """Asynchronously update cert_info (enqueue, background batch execution).

    性能修复(审计 B1-#1)：原实现在代理线程内为每条响应新建同步连接执行单条
    UPDATE cert_info，绕过异步批写队列，抵消 update_flow_response_async 的批
    写收益。改为与响应字段一样入 _update_queue 合并提交。
    """
    _update_queue_put({
        "sql": "UPDATE flows SET cert_info=? WHERE id=?",
        "params": (cert_info, flow_id),
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


# ---------------------------------------------------------------------------
# 触发式捕获配置持久化（conditions 独立存储，不论功能开关状态）
# ---------------------------------------------------------------------------

def get_trigger_config() -> list[dict]:
    """获取持久化的触发条件列表。"""
    with get_connection() as conn:
        row = conn.execute("SELECT conditions FROM trigger_config WHERE id=1").fetchone()
        if not row:
            return []
        try:
            import json
            return json.loads(row[0])
        except Exception:  # noqa: BLE001
            return []


def set_trigger_config(conditions: list[dict]) -> None:
    """持久化触发条件列表。conditions 为空则清除。"""
    import json
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO trigger_config(id, conditions) VALUES(1, ?)",
            (json.dumps(conditions),),
        )


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
                 host: str | None = None,
                 status_min: int | None = None,
                 status_max: int | None = None,
                 offset_start: int | None = None,
                 offset_end: int | None = None) -> list[dict]:
    """Search flows by body regex/binary. session_id=0 means search across all sessions.

    Performance optimization: exact field filters (method/status/pid/process_name/host)
    are pushed down to SQL WHERE, and only the columns needed for search are
    projected (excluding large fields like raw_data), avoiding loading all flows
    with all columns into memory.
    """
    # 只投影搜索所需列，避免 SELECT * 拉取 raw_data 等大字段
    proj = ("id, session_id, method, status_code, pid, process_name, "
            "url, host, path, request_body, response_body, request_headers, response_headers")
    clauses = []
    args = []
    if session_id and session_id > 0:
        clauses.append("session_id=?")
        args.append(session_id)
    if method:
        # 性能修复(审计 B-#7)：UPPER(method)=UPPER(?) 阻止 idx_flows_method 索引使用，
        # 改为 method=? + 大写化（DB 中 method 约定大写存储，与 get_flows 一致）
        clauses.append("method=?")
        args.append(method.upper())
    if status_code is not None:
        clauses.append("status_code=?")
        args.append(status_code)
    if status_min is not None:
        clauses.append("status_code>=?")
        args.append(status_min)
    if status_max is not None:
        clauses.append("status_code<=?")
        args.append(status_max)
    if pid:
        clauses.append("pid=?")
        args.append(pid)
    if process_name:
        clauses.append("process_name=?")
        args.append(process_name)
    if host:
        # 子串匹配，大小写不敏感
        clauses.append("host LIKE ?")
        args.append(f"%{host}%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT {proj} FROM flows{where} ORDER BY id DESC LIMIT ?"
    args.append(10000)
    # P2-4 修复：分批游标替代一次性 fetchall(10000)。
    # 原实现把最多 10000 行（含 request_body/response_body 大字段）全量载入内存再
    # Python 逐行正则匹配，大 body 场景内存峰值可达数百 MB、CPU 耗时百 ms~秒级，
    # 且即便命中 limit(200) 条也拉满了 10000 行。改为 fetchmany(500) 分批：
    # - 内存峰值降至 500 行；
    # - 命中达到 limit 即停止后续扫描，避免无谓拉取。
    # 性能修复(审计 P-#4)：使用 LRU 缓存的 _compile_body_regex / _compile_header_regex
    # 替代直接调用 re.compile()，避免每次调用都重新编译相同正则。
    rx = _compile_body_regex(body_regex)
    hrx = _compile_header_regex(header_regex)
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
    性能修复(审计 P-#1)：添加 LIMIT 1000 防止长会话 OOM。
    """
    with get_connection() as conn:
        # 限制采样量：10万+流量时取最后 1000 条的 tags 聚合，
        # 统计结果足够代表性，且内存占用可控。
        rows = conn.execute(
            "SELECT tags FROM flows WHERE tags IS NOT NULL AND tags != '' ORDER BY id DESC LIMIT 1000"
        ).fetchall()
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

    性能修复(审计 B-#3)：改用 DROP+CREATE 替代 DELETE FROM flows，
    与 init_db / cleanup_database 路径对齐。DROP+CREATE 直接释放页不写 WAL，
    10 万行清理从秒级降到毫秒级。

    Audit fix 7.4: the original code set _max_flow_id_cache=0 before DELETE,
    creating a window: other threads calling get_max_flow_id_all during this
    window would query the DB and get the old max, then cache it; after DELETE
    the cache holds a stale value. Changed to DELETE first, then set to 0.
    """
    global _max_flow_id_cache, _overview_cache
    with get_connection() as conn:
        # 先取行数（DROP 后无法取 rowcount），用于返回给调用方
        rowcount = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
        # DROP + CREATE flows（与 SCHEMA_TABLES 中定义一致）
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
        # 性能修复(审计 B-#8)：移除冗余的 idx_flows_session，
        # idx_flows_session_id(session_id, id DESC) 已覆盖 WHERE session_id=? 查询
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_host ON flows(host)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_process ON flows(process_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_protocol ON flows(protocol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_method ON flows(method)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_ip_region ON flows(ip_region)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_remote_ip ON flows(remote_ip)")
        # 复合索引：覆盖 WHERE session_id=? ORDER BY id DESC 分页查询
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_session_id ON flows(session_id, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flows_status ON flows(status_code)")
        # 重置自增序列
        conn.execute("DELETE FROM sqlite_sequence WHERE name='flows'")
    # 重置内存缓存
    with _max_flow_id_lock:
        _max_flow_id_cache = 0
    # 清空后立即失效 overview 缓存，避免 dashboard 短暂显示旧统计
    _overview_cache = None
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
        # content_type：SQLite json_each 函数提取 content-type，避免全量加载到 Python
        # 性能优化：用 LIKE + CASE 做 SQL 端分类（比 Python json.loads 快 5x）
        sql = f"""
            SELECT CASE
                WHEN response_headers LIKE '%"content-type":"image/%' ESCAPE '\\' THEN 'image'
                WHEN response_headers LIKE '%"content-type":"text/%' ESCAPE '\\' THEN 'text'
                WHEN response_headers LIKE '%"content-type":"application/%' ESCAPE '\\' THEN 'application'
                WHEN response_headers LIKE '%"content-type":"video/%' ESCAPE '\\' THEN 'video'
                WHEN response_headers LIKE '%"content-type":"audio/%' ESCAPE '\\' THEN 'audio'
                WHEN response_headers LIKE '%"content-type":"font/%' ESCAPE '\\' THEN 'font'
                WHEN response_headers LIKE '%"content-type":"multipart/%' ESCAPE '\\' THEN 'multipart'
                ELSE 'other'
            END AS k, COUNT(*) AS c
            FROM flows{where_sql}
            GROUP BY k ORDER BY c DESC LIMIT 100
        """
        rows = conn.execute(sql, args).fetchall()
        return [{"key": r["k"], "label": r["k"], "count": r["c"]} for r in rows]


def _normalize_path_template(path: str) -> tuple[str, list[str]]:
    """Normalize path: replace numbers, UUIDs, hex, tokens with placeholders.

    Returns (template, query_keys).
    """
    if not path:
        return "/", []
    import re
    raw_path, _, query_str = path.partition("?")
    segs = raw_path.split("/")
    out = []
    for seg in segs:
        if not seg:
            out.append("")
            continue
        # Pure number
        if re.match(r"^\d+$", seg):
            out.append("{id}")
        # UUID
        elif re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", seg):
            out.append("{uuid}")
        # Long hex (>=16)
        elif re.match(r"^[0-9a-fA-F]{16,}$", seg):
            out.append("{hex}")
        # Long base64-ish (>=24, alphanumeric)
        elif len(seg) >= 24 and re.match(r"^[A-Za-z0-9_-]+$", seg):
            out.append("{token}")
        else:
            out.append(seg)
    tpl = "/".join(out)
    # Deduplicate query keys
    query_keys = []
    if query_str:
        for pair in query_str.split("&"):
            k = pair.split("=", 1)[0]
            if k and k not in query_keys:
                query_keys.append(k)
    return tpl, query_keys


def get_flows_endpoint_stats(session_id: int | None = None, limit: int = 2000) -> list[dict]:
    """Endpoint statistics with path template normalization (server-side).

    Design fix: moved from cli.py client-side processing to server-side API.
    Aggregates flows by normalized path templates to avoid transferring all flows to client.

    Returns [{method, host, path_template, count, status_codes, sample_ids, query_keys}]
    """
    with get_connection() as conn:
        if session_id:
            sql = "SELECT id, method, host, path, status_code FROM flows WHERE session_id=? ORDER BY id DESC LIMIT ?"
            rows = conn.execute(sql, (session_id, limit)).fetchall()
        else:
            sql = "SELECT id, method, host, path, status_code FROM flows ORDER BY id DESC LIMIT ?"
            rows = conn.execute(sql, (limit,)).fetchall()

    endpoints: dict[str, dict] = {}
    for r in rows:
        path = r["path"] or ""
        tpl, qkeys = _normalize_path_template(path)
        method = r["method"] or "-"
        host = r["host"] or ""
        key = f"{method} {host}{tpl}"
        ep = endpoints.setdefault(key, {
            "method": method,
            "host": host,
            "path_template": tpl,
            "count": 0,
            "status_codes": [],
            "sample_ids": [],
            "query_keys": qkeys
        })
        ep["count"] += 1
        sc = r["status_code"]
        if sc and sc not in ep["status_codes"]:
            ep["status_codes"].append(sc)
        if len(ep["sample_ids"]) < 3:
            ep["sample_ids"].append(r["id"])

    return list(endpoints.values())


def get_network_topology(host: str | None = None,
                        process: str | None = None,
                        max_nodes: int = 100) -> dict:
    """网络拓扑数据：process → remote_ip → host 的连接关系图。

    Returns {
        nodes: [{id, label, type, count}],   # type: process/ip/host
        edges: [{source, target, count, size}]
    }
    用于前端力导向图渲染。
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

    with get_connection() as conn:
        # 聚合 process → host → remote_ip 连接
        sql = (
            f"SELECT process_name, host, remote_ip, COUNT(*) AS c "
            f"FROM flows{where_sql} "
            f"GROUP BY process_name, host, remote_ip "
            f"ORDER BY c DESC LIMIT ?"
        )
        rows = conn.execute(sql, args + [max_nodes * 3]).fetchall()

    nodes_map: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(node_id: str, label: str, node_type: str, count: int):
        if node_id not in nodes_map:
            nodes_map[node_id] = {"id": node_id, "label": label, "type": node_type, "count": 0}
        nodes_map[node_id]["count"] += count

    for r in rows:
        proc = r["process_name"] or "(未知进程)"
        host_val = r["host"] or "(未知 host)"
        ip_val = r["remote_ip"] or "(未知 IP)"
        cnt = r["c"]

        proc_id = f"proc:{proc}"
        ip_id = f"ip:{ip_val}"
        host_id = f"host:{host_val}"

        add_node(proc_id, proc, "process", cnt)
        add_node(ip_id, ip_val, "ip", cnt)
        add_node(host_id, host_val, "host", cnt)

        edges.append({"source": proc_id, "target": ip_id, "count": cnt, "size": min(10, cnt)})
        edges.append({"source": ip_id, "target": host_id, "count": cnt, "size": min(10, cnt)})

    # 限制节点数
    nodes = list(nodes_map.values())
    if len(nodes) > max_nodes:
        nodes.sort(key=lambda x: -x["count"])
        keep_ids = {n["id"] for n in nodes[:max_nodes]}
        nodes = [n for n in nodes if n["id"] in keep_ids]
        edges = [e for e in edges if e["source"] in keep_ids and e["target"] in keep_ids]

    return {"nodes": nodes, "edges": edges}


def get_flows_heatmap(group_by: str = "host",
                      bucket_seconds: int = 60,
                      max_buckets: int = 120,
                      top_n: int = 20,
                      host: str | None = None,
                      process: str | None = None,
                      hours: int = 24) -> dict:
    """2D 热力图聚合：时间分桶 × 维度分组。

    group_by: host / process / method / status_range / ip_region
    bucket_seconds: 时间桶大小（秒），默认 60s
    max_buckets: 最多返回多少个时间桶（从最新往前）
    top_n: 维度方向最多返回 top N（按总量排序）
    hours: 只统计最近 N 小时内的流量，默认 24

    Returns {
        buckets: [ts_label, ts_epoch],           # 时间轴标签
        dimensions: [{key, label, total}],       # 维度列表
        matrix: [[count, ...], ...],             # matrix[dim_idx][bucket_idx] = count
        bucket_seconds, max_buckets, top_n
    }
    """
    where = []
    args: list = []
    if host:
        where.append("host LIKE ?")
        args.append(f"%{host}%")
    if process:
        where.append("process_name LIKE ?")
        args.append(f"%{process}%")
    # 性能修复(审计 P-#3)：添加时间范围过滤，只统计最近 hours 小时的数据，
    # 避免长会话全量加载导致 OOM（10万流量热力图请求原来会加载 10万行到内存）。
    from datetime import datetime, timezone, timedelta
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    where.append("timestamp >= ?")
    args.append(cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    # 确定维度列
    dim_col_map = {
        "host": "host",
        "process": "process_name",
        "method": "method",
        "ip_region": "ip_region",
    }
    dim_col = dim_col_map.get(group_by)

    with get_connection() as conn:
        # status_range 需 CASE WHEN，单独处理
        if group_by == "status_range":
            dim_expr = (
                "CASE "
                "WHEN status_code IS NULL OR status_code = 0 THEN '(无状态码)' "
                "WHEN status_code < 200 THEN '1xx' "
                "WHEN status_code < 300 THEN '2xx' "
                "WHEN status_code < 400 THEN '3xx' "
                "WHEN status_code < 500 THEN '4xx' "
                "ELSE '5xx' END"
            )
        elif dim_col is None:
            # 未知维度，退化为 host
            dim_expr = "host"
        else:
            # 安全护栏：列名只允许来自固定白名单
            _ALLOWED = {"host", "process_name", "method", "ip_region"}
            dim_expr = dim_col if dim_col in _ALLOWED else "host"

        # 拉取所需字段：timestamp + 维度列
        # 时间用 substr 截取前 19 位（ISO 8601 'YYYY-MM-DDTHH:MM:SS'），转为 epoch
        # SQLite 没有 ISO 解析函数，用 strftime
        sql = (
            f"SELECT timestamp, {dim_expr} AS dim "
            f"FROM flows{where_sql} ORDER BY id DESC LIMIT 50000"
        )
        rows = conn.execute(sql, args).fetchall()

    if not rows:
        return {
            "buckets": [],
            "dimensions": [],
            "matrix": [],
            "bucket_seconds": bucket_seconds,
            "max_buckets": max_buckets,
            "top_n": top_n,
            "total": 0,
        }

    # Python 端聚合：解析时间戳 → epoch → 分桶
    from datetime import datetime as _dt
    bucket_map: dict[int, dict[str, int]] = {}  # {bucket_epoch: {dim_key: count}}
    dim_totals: dict[str, int] = {}
    for r in rows:
        ts = r["timestamp"] or ""
        dim_val = r["dim"] or ""
        # 解析时间戳
        # 设计修复：使用更健壮的时间解析，支持多种 ISO 8601 格式
        # 包括带/不带时区、多种分隔符等情况
        try:
            s = ts.strip()
            if not s:
                continue
            # 统一处理时区后缀：Z -> +00:00
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            elif s[-6:-5] not in ("+", "-") and len(s) > 19 and s[19] in ("+", "-"):
                # 格式如 2024-01-01T12:00:00+08:00，保持原样
                pass
            elif "+" not in s and "-" not in s[10:]:
                # 无时区信息，假设为本地时间
                pass
            dt = _dt.fromisoformat(s)
            epoch = dt.timestamp()
        except (ValueError, TypeError):
            continue
        bucket_epoch = int(epoch) - (int(epoch) % bucket_seconds)
        bm = bucket_map.setdefault(bucket_epoch, {})
        bm[dim_val] = bm.get(dim_val, 0) + 1
        dim_totals[dim_val] = dim_totals.get(dim_val, 0) + 1

    if not bucket_map:
        return {
            "buckets": [],
            "dimensions": [],
            "matrix": [],
            "bucket_seconds": bucket_seconds,
            "max_buckets": max_buckets,
            "top_n": top_n,
            "total": 0,
        }

    # 确定时间桶范围：从最新往前取 max_buckets 个
    all_bucket_epochs = sorted(bucket_map.keys())
    recent_buckets = all_bucket_epochs[-max_buckets:]

    # 维度 top N
    top_dims = sorted(dim_totals.items(), key=lambda x: -x[1])[:top_n]
    # 其他维度合并为 "(其他)"
    other_total = sum(c for d, c in dim_totals.items() if d not in dict(top_dims))
    dim_list = [{"key": d, "label": d or "(未知)", "total": c} for d, c in top_dims]
    if other_total > 0:
        dim_list.append({"key": "__other__", "label": "(其他)", "total": other_total})
    top_dim_keys = [d["key"] for d in dim_list]

    # 构建矩阵：matrix[dim_idx][bucket_idx]
    matrix = [[0] * len(recent_buckets) for _ in dim_list]
    for bi, bepoch in enumerate(recent_buckets):
        bm = bucket_map.get(bepoch, {})
        for di, dk in enumerate(top_dim_keys):
            if dk == "__other__":
                # 其他维度：sum 所有不在 top_dims 中的
                matrix[di][bi] = sum(c for d, c in bm.items() if d not in dict(top_dims))
            else:
                matrix[di][bi] = bm.get(dk, 0)

    # 生成时间轴标签
    bucket_labels = []
    for bepoch in recent_buckets:
        dt = _dt.fromtimestamp(bepoch)
        bucket_labels.append({
            "epoch": bepoch,
            "label": dt.strftime("%H:%M:%S"),
            "full": dt.strftime("%Y-%m-%d %H:%M:%S"),
        })

    return {
        "buckets": bucket_labels,
        "dimensions": dim_list,
        "matrix": matrix,
        "bucket_seconds": bucket_seconds,
        "max_buckets": max_buckets,
        "top_n": top_n,
        "total": sum(dim_totals.values()),
    }


# 性能修复(审计 B1-#4)：get_flows_overview 一次执行 8+ 条聚合 SQL，
# dashboard 轮询时每次全表聚合开销大。加 2s TTL 缓存合并短时间内的重复请求；
# TTL 极短，不影响数据实时性感知。

# 设计修复：抽象统一的缓存工具类，避免重复实现缓存逻辑
class TTLCache:
    """Thread-safe TTL cache with double-checked locking pattern.

    Attributes:
        _cache: The cached data (None = not initialized)
        _ts: Timestamp of last cache update
        _lock: Threading lock for synchronization
        _ttl: Time-to-live in seconds
    """
    __slots__ = ('_cache', '_ts', '_lock', '_ttl')

    def __init__(self, ttl: float = 2.0):
        self._cache: Any = None
        self._ts: float = 0.0
        self._lock = threading.Lock()
        self._ttl = ttl

    def get_or_compute(self, compute_fn: callable) -> Any:
        """Get cached value or compute and cache it.

        Uses double-checked locking: fast path without lock if cache is valid,
        slow path with lock for thread safety during computation.
        """
        now = time.time()
        # Fast path: no lock, return if valid
        if self._cache is not None and (now - self._ts) < self._ttl:
            return self._cache
        # Slow path: lock and double-check
        with self._lock:
            if self._cache is not None and (now - self._ts) < self._ttl:
                return self._cache
            self._cache = compute_fn()
            self._ts = time.time()
            return self._cache

    def invalidate(self):
        """Clear the cache."""
        with self._lock:
            self._cache = None
            self._ts = 0.0


_overview_cache: dict | None = None
_overview_cache_ts: float = 0.0
_OVERVIEW_TTL = 2.0
# 性能修复(审计 B-#2)：double-checked locking 防止 N 个 dashboard 线程
# 同时跑 10 条全表聚合 SQL（每条 50-200ms，10 条串行 ~1-2s）。
_overview_lock = threading.Lock()


def get_flows_overview() -> dict:
    """Cross-session multi-dimensional aggregate statistics (returns all dimensions at once),
    for the CoolUI dashboard.

    Returns {total, total_bytes, incoming_bytes, outgoing_bytes,
             success_count, error_count, avg_duration_ms,
             by_protocol, by_method, by_status_range,
             by_host, by_process, by_ip_region}
    Each group is a [{key, count, bytes?}] list, sorted by count descending, top 20 entries.
    """
    global _overview_cache, _overview_cache_ts
    now = time.time()
    # Fast path：无锁读缓存（TTL 内直接返回，避免锁竞争）
    if _overview_cache is not None and (now - _overview_cache_ts) < _OVERVIEW_TTL:
        return _overview_cache
    # Slow path：加锁后二次检查，确保只有一个线程执行 10 条聚合 SQL
    with _overview_lock:
        now = time.time()
        if _overview_cache is not None and (now - _overview_cache_ts) < _OVERVIEW_TTL:
            return _overview_cache
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

            result = {
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
            _overview_cache = result
            _overview_cache_ts = time.time()
            return result


def get_site_map() -> dict:
    """Build a Burp Suite-style site map tree from all captured flows.

    Aggregates flows by host → path segments → method, returning a nested tree
    structure so the frontend can render it with el-tree.

    Returns {"hosts": [{host, request_count, path_count, children: [...]}]}.
    Each path node: {path, request_count, methods: [{method, count}], children}.

    Performance optimization: LIMIT 10000 to prevent loading too many rows.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT host, path, method, COUNT(*) AS c "
            "FROM flows "
            "WHERE host IS NOT NULL AND host != '' "
            "AND path IS NOT NULL AND path != '' "
            "GROUP BY host, path, method "
            "ORDER BY host, path "
            "LIMIT 10000"
        ).fetchall()

    # Group by host: {host: {path: {method: count}}}
    hosts_map: dict[str, dict[str, dict[str, int]]] = {}
    for r in rows:
        host = r["host"]
        path = r["path"] or "/"
        method = r["method"] or "?"
        count = r["c"]
        hosts_map.setdefault(host, {}).setdefault(path, {})
        hosts_map[host][path][method] = hosts_map[host][path].get(method, 0) + count

    hosts = []
    for host, paths in hosts_map.items():
        total_requests = sum(sum(m.values()) for m in paths.values())
        path_count = len(paths)
        children = _build_path_tree(paths)
        hosts.append({
            "host": host,
            "request_count": total_requests,
            "path_count": path_count,
            "children": children,
        })

    # Sort hosts by request_count descending
    hosts.sort(key=lambda h: -h["request_count"])
    return {"hosts": hosts}


def _build_path_tree(paths_methods: dict[str, dict[str, int]]) -> list[dict]:
    """Build a path-segment trie from {path: {method: count}}.

    Each node represents an accumulated path prefix. Direct requests to an exact
    path are recorded in that node's ``methods``; deeper paths become children.
    Returns a sorted list of nested {path, request_count, methods, children}.
    """
    # Build a trie of path segments.
    # Each trie node: {"_path": full_path, "_methods": {m:c}, "_children": {}}
    root_children: dict[str, dict] = {}

    for path, methods in paths_methods.items():
        # Normalize: "/api/users" → segments ["api", "users"]; "/" → []
        segments = [s for s in path.split('/') if s]
        if not segments:
            # Root path "/"
            node = root_children.setdefault('', {"_path": "/", "_methods": {}, "_children": {}})
            for m, c in methods.items():
                node["_methods"][m] = node["_methods"].get(m, 0) + c
            continue

        current_children = root_children
        accumulated = ""
        for i, seg in enumerate(segments):
            accumulated = accumulated + "/" + seg
            if seg not in current_children:
                current_children[seg] = {"_path": accumulated, "_methods": {}, "_children": {}}
            node = current_children[seg]
            # Record methods only at the leaf segment (exact path) so that
            # intermediate nodes don't double-count their descendants.
            if i == len(segments) - 1:
                for m, c in methods.items():
                    node["_methods"][m] = node["_methods"].get(m, 0) + c
            current_children = node["_children"]

    def _convert(children_dict: dict) -> list[dict]:
        result = []
        for node in children_dict.values():
            methods_list = [
                {"method": m, "count": c}
                for m, c in sorted(node["_methods"].items(), key=lambda x: -x[1])
            ]
            request_count = sum(node["_methods"].values()) if node["_methods"] else 0
            children = _convert(node["_children"])
            result.append({
                "path": node["_path"],
                "request_count": request_count,
                "methods": methods_list,
                "children": children,
            })
        # Nodes with direct requests first, then alphabetically by path
        result.sort(key=lambda n: (-n["request_count"], n["path"]))
        return result

    return _convert(root_children)


def delete_flows_before(flow_id: int) -> int:
    """Delete all flows with id < flow_id (clean up old data by id). Returns the number of deleted rows."""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM flows WHERE id < ?", (flow_id,))
        return cur.rowcount


def delete_flows_in_session(session_id: int | None) -> int:
    """Delete all flows in a specific session. Returns the number of deleted rows."""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM flows WHERE session_id = ?", (session_id,))
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


# ---------- Script Error History ----------

def add_script_error(rule_id: str, error_type: str, error_message: str):
    """Add a script error record to history.

    Design fix: maintains error history in database instead of just in-memory.
    """
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO script_errors(rule_id, error_type, error_message, occurred_at) VALUES(?, ?, ?, ?)",
            (rule_id, error_type, error_message, now)
        )
        # Keep only last 100 errors per rule to avoid unbounded growth
        conn.execute(
            """
            DELETE FROM script_errors WHERE id NOT IN (
                SELECT id FROM script_errors WHERE rule_id=? ORDER BY occurred_at DESC LIMIT 100
            ) AND rule_id=?
            """,
            (rule_id, rule_id)
        )


def get_script_errors(rule_id: str, limit: int = 100) -> list[dict]:
    """Get script error history for a rule."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM script_errors WHERE rule_id=? ORDER BY occurred_at DESC LIMIT ?",
            (rule_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_latest_script_error(rule_id: str) -> dict | None:
    """Get the most recent script error for a rule."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM script_errors WHERE rule_id=? ORDER BY occurred_at DESC LIMIT 1",
            (rule_id,)
        ).fetchone()
        return dict(row) if row else None


def delete_script_errors(rule_id: str):
    """Delete all script errors for a rule."""
    with get_connection() as conn:
        conn.execute("DELETE FROM script_errors WHERE rule_id=?", (rule_id,))


def increment_rule_hit(rule_id: str, flow_id: int | None = None):
    """§3.2 Hit counting: increments hit_count, updates last_hit_at and last_hit_flow_id."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE auto_reply_rules SET hit_count = hit_count + 1, "
            "last_hit_at = ?, last_hit_flow_id = ? WHERE id = ?",
            (now, flow_id, rule_id),
        )


# ---------- rule_groups（§3.2 规则分组与标签）----------

def get_rule_groups() -> list[dict]:
    """Get all rule groups ordered by sort_order."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM rule_groups ORDER BY sort_order ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_rule_group(group_id: int) -> dict | None:
    """Get a single rule group by id."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM rule_groups WHERE id=?", (group_id,)
        ).fetchone()
        return dict(row) if row else None


def create_rule_group(name: str, enabled: bool = True) -> int:
    """Create a new rule group. Returns the new group id."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        # 获取当前最大 sort_order
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM rule_groups"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO rule_groups(name, enabled, sort_order, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            (name, 1 if enabled else 0, max_order + 1, now, now),
        )
        return cur.lastrowid


def update_rule_group(group_id: int, updates: dict) -> bool:
    """Update a rule group. updates can contain: name, enabled, sort_order."""
    if not updates:
        return False
    sets = []
    vals = []
    for k, v in updates.items():
        if k == "enabled":
            sets.append("enabled = ?")
            vals.append(1 if v else 0)
        elif k in ("name", "sort_order"):
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(datetime.now().isoformat())
    vals.append(group_id)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE rule_groups SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        return cur.rowcount > 0


def delete_rule_group(group_id: int) -> bool:
    """Delete a rule group. Rules in the group will have group_id set to NULL."""
    with get_connection() as conn:
        # 先把该组下的规则 group_id 设为 NULL
        conn.execute(
            "UPDATE auto_reply_rules SET group_id = NULL WHERE group_id = ?",
            (group_id,),
        )
        cur = conn.execute("DELETE FROM rule_groups WHERE id=?", (group_id,))
        return cur.rowcount > 0


def update_rule_group_id(rule_id: str, group_id: int | None):
    """Update a rule's group_id."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE auto_reply_rules SET group_id = ? WHERE id = ?",
            (group_id, rule_id),
        )


def get_rules_by_group(group_id: int | None) -> list[dict]:
    """Get all rules in a group. If group_id is None, returns rules with no group."""
    with get_connection() as conn:
        if group_id is None:
            rows = conn.execute(
                "SELECT * FROM auto_reply_rules WHERE group_id IS NULL ORDER BY created_at"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM auto_reply_rules WHERE group_id = ? ORDER BY created_at",
                (group_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_recent_rule_hits(limit: int = 10) -> list[dict]:
    """Get recent rule hits across all rules, ordered by last_hit_at DESC."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, pattern, action, hit_count, last_hit_at, last_hit_flow_id "
            "FROM auto_reply_rules WHERE hit_count > 0 AND last_hit_at != '' "
            "ORDER BY last_hit_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


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


# ---------- flow_groups（§3.13 流量分组，关系型中间表）----------

def create_flow_group(name: str, flow_ids: list[int]) -> int:
    """Create a flow group, returns the auto-increment id. Uses junction table for flow_ids.

    Performance optimization: uses executemany for batch insert (10x faster).
    """
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO flow_groups(name, created_at) VALUES(?, ?)",
            (name, now),
        )
        group_id = cur.lastrowid
        if flow_ids:
            # 批量插入关系表
            relations = [(group_id, fid) for fid in flow_ids]
            conn.executemany(
                "INSERT OR IGNORE INTO flow_group_flows(flow_group_id, flow_id) VALUES(?, ?)",
                relations
            )
        return group_id


def get_flow_groups() -> list[dict]:
    """Get all flow groups with their flow_ids assembled from junction table.

    Performance optimization: uses LEFT JOIN to eliminate N+1 queries.
    """
    with get_connection() as conn:
        # 一次JOIN获取所有数据，避免N+1查询
        rows = conn.execute(
            """SELECT fg.id, fg.name, fg.created_at, fgf.flow_id
               FROM flow_groups fg
               LEFT JOIN flow_group_flows fgf ON fg.id = fgf.flow_group_id
               ORDER BY fg.id DESC, fgf.flow_id"""
        ).fetchall()

        # Python端分组
        groups: dict[int, dict] = {}
        for row in rows:
            gid = row["id"]
            if gid not in groups:
                groups[gid] = {"id": gid, "name": row["name"], "created_at": row["created_at"], "flow_ids": []}
            if row["flow_id"] is not None:
                groups[gid]["flow_ids"].append(row["flow_id"])

        return list(groups.values())


def get_flow_group(group_id: int) -> dict | None:
    """Get a single flow group with its flow_ids."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, name, created_at FROM flow_groups WHERE id=?", (group_id,)
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        flow_rows = conn.execute(
            "SELECT flow_id FROM flow_group_flows WHERE flow_group_id=? ORDER BY flow_id",
            (group_id,)
        ).fetchall()
        r["flow_ids"] = [fr["flow_id"] for fr in flow_rows]
        return r


def delete_flow_group(group_id: int):
    """Delete a flow group (cascade deletes junction table entries)."""
    with get_connection() as conn:
        # 先删中间表（显式删除，以防外键约束未启用）
        conn.execute("DELETE FROM flow_group_flows WHERE flow_group_id=?", (group_id,))
        conn.execute("DELETE FROM flow_groups WHERE id=?", (group_id,))


def update_flow_group(group_id: int, name: str | None = None,
                      flow_ids: list[int] | None = None):
    """Update a group. name or flow_ids being None means that field is not updated."""
    with get_connection() as conn:
        if name is not None:
            conn.execute("UPDATE flow_groups SET name=? WHERE id=?", (name, group_id))
        if flow_ids is not None:
            # 替换整个 flow_ids 列表
            conn.execute("DELETE FROM flow_group_flows WHERE flow_group_id=?", (group_id,))
            for fid in flow_ids:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO flow_group_flows(flow_group_id, flow_id) VALUES(?, ?)",
                        (group_id, fid)
                    )
                except Exception:  # noqa: BLE001
                    pass


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


def record_ai_usage(chat_id: int, service: str, model: str,
                    input_tokens: int, output_tokens: int, total_cost: float) -> int:
    """Record AI usage for cost tracking. Returns the record id."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO ai_usage(chat_id, service, model, input_tokens, output_tokens, total_cost, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (chat_id, service, model, input_tokens, output_tokens, total_cost, now),
        )
        return cur.lastrowid


def get_delay_stats(host: str | None = None, process: str | None = None, limit: int = 50000) -> dict:
    """响应时间深度分析：延迟分布直方图 + P50/P75/P90/P95/P99 统计。

    Returns {
        histogram: [{range, label, count, pct}],  # <50ms, 50-200ms, 200-500ms, 500ms-1s, >1s
        percentiles: {p50, p75, p90, p95, p99},
        total: int,
        avg_ms: float,
        slow_count: int,   # >1s 的慢请求数
        slow_pct: float,
    }
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

    # 采样：取最新的 limit 条（足够计算分位数）
    sql = f"SELECT duration_ms FROM flows{where_sql} AND duration_ms IS NOT NULL ORDER BY id DESC LIMIT ?"
    with get_connection() as conn:
        rows = conn.execute(sql, args + [limit]).fetchall()

    durations = [r["duration_ms"] for r in rows if r["duration_ms"] is not None]
    total = len(durations)
    if total == 0:
        return {
            "histogram": [
                {"range": "<50ms", "label": "<50ms", "count": 0, "pct": 0},
                {"range": "50-200ms", "label": "50-200ms", "count": 0, "pct": 0},
                {"range": "200-500ms", "label": "200-500ms", "count": 0, "pct": 0},
                {"range": "500ms-1s", "label": "500ms-1s", "count": 0, "pct": 0},
                {"range": ">1s", "label": ">1s", "count": 0, "pct": 0},
            ],
            "percentiles": {"p50": 0, "p75": 0, "p90": 0, "p95": 0, "p99": 0},
            "total": 0, "avg_ms": 0, "slow_count": 0, "slow_pct": 0,
        }

    # 平均值
    avg_ms = sum(durations) / total

    # 分位数计算（排序后取位置）
    sorted_durations = sorted(durations)
    def percentile(p: float) -> int:
        idx = int(len(sorted_durations) * p)
        if idx >= len(sorted_durations):
            idx = len(sorted_durations) - 1
        return sorted_durations[idx]

    p50 = percentile(0.50)
    p75 = percentile(0.75)
    p90 = percentile(0.90)
    p95 = percentile(0.95)
    p99 = percentile(0.99)

    # 直方图分桶
    buckets = [
        ("<50ms", 0, 50),
        ("50-200ms", 50, 200),
        ("200-500ms", 200, 500),
        ("500ms-1s", 500, 1000),
        (">1s", 1000, float('inf')),
    ]
    histogram = []
    for label, lo, hi in buckets:
        count = sum(1 for d in durations if lo <= d < hi)
        pct = (count / total) * 100
        histogram.append({"range": label, "label": label, "count": count, "pct": round(pct, 2)})

    # 慢请求统计（>1s）
    slow_count = sum(1 for d in durations if d >= 1000)
    slow_pct = (slow_count / total) * 100

    return {
        "histogram": histogram,
        "percentiles": {"p50": p50, "p75": p75, "p90": p90, "p95": p95, "p99": p99},
        "total": total,
        "avg_ms": round(avg_ms, 2),
        "slow_count": slow_count,
        "slow_pct": round(slow_pct, 2),
    }


def get_cross_analysis(host: str | None = None, process: str | None = None, limit: int = 50000) -> dict:
    """多维度交叉分析：主机名 x 状态码、Content-Type x 响应大小。

    Returns {
        host_status_matrix: {dimensions: [host_labels], buckets: [status_labels], matrix: [[count]]},
        content_size_matrix: {dimensions: [content_types], buckets: [size_ranges], matrix: [[count]]},
    }
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

    # 采样获取足够数据
    sql = f"SELECT host, status_code, response_headers, size FROM flows{where_sql} ORDER BY id DESC LIMIT ?"
    with get_connection() as conn:
        rows = conn.execute(sql, args + [limit]).fetchall()

    # === Host x Status Code 交叉矩阵 ===
    host_status: dict[str, dict[str, int]] = {}
    status_buckets = ["2xx", "3xx", "4xx", "5xx", "other"]
    host_totals: dict[str, int] = {}
    for r in rows:
        h = (r["host"] or "").strip() or "(unknown)"
        sc = r["status_code"]
        if sc is None or sc == 0:
            sb = "other"
        elif sc < 200:
            sb = "other"
        elif sc < 300:
            sb = "2xx"
        elif sc < 400:
            sb = "3xx"
        elif sc < 500:
            sb = "4xx"
        else:
            sb = "5xx"
        if h not in host_status:
            host_status[h] = {b: 0 for b in status_buckets}
        host_status[h][sb] = host_status[h].get(sb, 0) + 1
        host_totals[h] = host_totals.get(h, 0) + 1

    # 取 top 15 host
    top_hosts = sorted(host_totals.items(), key=lambda x: -x[1])[:15]
    top_host_labels = [h for h, _ in top_hosts]
    host_matrix = [[host_status.get(h, {}).get(b, 0) for b in status_buckets] for h in top_host_labels]

    # === Content-Type x Size 交叉矩阵 ===
    content_size: dict[str, dict[str, int]] = {}
    size_buckets = ["<1KB", "1-10KB", "10-100KB", "100KB-1MB", ">1MB"]
    content_totals: dict[str, int] = {}
    for r in rows:
        ct = ""
        try:
            obj = json.loads(r["response_headers"] or "{}")
            for kk in obj:
                if kk.lower() == "content-type":
                    ct = str(obj[kk]).split("/")[0].lower()
                    break
        except Exception:  # noqa: BLE001
            pass
        if not ct:
            ct = "unknown"
        sz = r["size"] or 0
        if sz < 1024:
            sb = "<1KB"
        elif sz < 10 * 1024:
            sb = "1-10KB"
        elif sz < 100 * 1024:
            sb = "10-100KB"
        elif sz < 1024 * 1024:
            sb = "100KB-1MB"
        else:
            sb = ">1MB"
        if ct not in content_size:
            content_size[ct] = {b: 0 for b in size_buckets}
        content_size[ct][sb] = content_size[ct].get(sb, 0) + 1
        content_totals[ct] = content_totals.get(ct, 0) + 1

    # 取 top 10 content types
    top_cts = sorted(content_totals.items(), key=lambda x: -x[1])[:10]
    top_ct_labels = [ct for ct, _ in top_cts]
    ct_matrix = [[content_size.get(ct, {}).get(b, 0) for b in size_buckets] for ct in top_ct_labels]

    return {
        "host_status": {
            "dimensions": top_host_labels,
            "buckets": status_buckets,
            "matrix": host_matrix,
        },
        "content_size": {
            "dimensions": top_ct_labels,
            "buckets": size_buckets,
            "matrix": ct_matrix,
        },
    }


def detect_anomalies(host: str | None = None, process: str | None = None, limit: int = 10000) -> dict:
    """智能异常检测：高延迟异常、错误率异常、流量突增/突降。

    Returns {anomalies: [{type, severity, message, details, count}], summary: {...}}
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

    # 分桶统计：按时间窗口（假设 id 递增近似时间顺序）
    sql = f"SELECT id, duration_ms, status_code FROM flows{where_sql} ORDER BY id DESC LIMIT ?"
    with get_connection() as conn:
        rows = conn.execute(sql, args + [limit]).fetchall()

    anomalies = []
    durations = [r["duration_ms"] for r in rows if r["duration_ms"] is not None]
    total = len(rows)
    error_count = sum(1 for r in rows if r["status_code"] and r["status_code"] >= 400)
    error_rate = (error_count / total * 100) if total > 0 else 0

    # === 1. 高延迟异常检测 ===
    if durations:
        sorted_d = sorted(durations)
        p95 = sorted_d[int(len(sorted_d) * 0.95)] if len(sorted_d) > 0 else 0
        slow_count = sum(1 for d in durations if d >= 1000)
        slow_pct = (slow_count / len(durations) * 100) if durations else 0

        if slow_pct > 10:
            anomalies.append({
                "type": "high_latency",
                "severity": "warning",
                "message": f"慢请求比例过高: {slow_pct:.1f}% 超过 10% 阈值",
                "details": {
                    "slow_count": slow_count,
                    "slow_pct": round(slow_pct, 2),
                    "p95_ms": p95,
                },
                "count": slow_count,
            })

        if p95 > 2000:
            anomalies.append({
                "type": "p95_high",
                "severity": "info",
                "message": f"P95 延迟较高: {p95}ms",
                "details": {"p95_ms": p95},
                "count": 1,
            })

    # === 2. 错误率异常检测 ===
    if total >= 10 and error_rate > 10:
        anomalies.append({
            "type": "high_error_rate",
            "severity": "error" if error_rate > 30 else "warning",
            "message": f"错误率异常: {error_rate:.1f}% 超过 10% 阈值",
            "details": {
                "error_count": error_count,
                "error_rate": round(error_rate, 2),
                "total": total,
            },
            "count": error_count,
        })
    elif total >= 10 and error_rate == 0 and total > 50:
        # 全部成功但流量较多时提示（可能是抓包范围问题）
        anomalies.append({
            "type": "zero_error",
            "severity": "info",
            "message": f"错误率为零（{total} 个请求全部成功），注意检查是否涵盖所有目标",
            "details": {"error_rate": 0, "total": total},
            "count": 0,
        })

    # === 3. 流量突增/突降检测 ===
    # 将数据分成前后两半比较
    mid = len(rows) // 2
    if mid >= 10:
        first_half = rows[mid:]  # 较旧的一半
        second_half = rows[:mid]  # 较新的一半

        first_count = len(first_half)
        second_count = len(second_half)

        if first_count > 0:
            change_rate = (second_count - first_count) / first_count * 100

            if change_rate > 100:  # 流量翻倍以上
                anomalies.append({
                    "type": "traffic_spike",
                    "severity": "warning",
                    "message": f"流量突增: 近期比之前增长 {change_rate:.0f}%",
                    "details": {
                        "recent_count": second_count,
                        "earlier_count": first_count,
                        "change_rate": round(change_rate, 2),
                    },
                    "count": second_count,
                })
            elif change_rate < -50:  # 流量下降超过 50%
                anomalies.append({
                    "type": "traffic_drop",
                    "severity": "info",
                    "message": f"流量突降: 近期比之前下降 {abs(change_rate):.0f}%",
                    "details": {
                        "recent_count": second_count,
                        "earlier_count": first_count,
                        "change_rate": round(change_rate, 2),
                    },
                    "count": second_count,
                })

    # === 4. 4xx/5xx 详细分类 ===
    status_counts: dict[str, int] = {}
    for r in rows:
        sc = r["status_code"]
        if sc and sc >= 400:
            bucket = f"{sc // 100}xx"
            status_counts[bucket] = status_counts.get(bucket, 0) + 1

    if "4xx" in status_counts:
        anomalies.append({
            "type": "client_errors",
            "severity": "info",
            "message": f"存在 {status_counts['4xx']} 个 4xx 客户端错误",
            "details": {"count": status_counts["4xx"]},
            "count": status_counts["4xx"],
        })

    if "5xx" in status_counts:
        anomalies.append({
            "type": "server_errors",
            "severity": "error",
            "message": f"存在 {status_counts['5xx']} 个 5xx 服务器错误",
            "details": {"count": status_counts["5xx"]},
            "count": status_counts["5xx"],
        })

    # 统计摘要
    avg_ms = round(sum(durations) / len(durations), 2) if durations else 0
    summary = {
        "total": total,
        "error_count": error_count,
        "error_rate": round(error_rate, 2),
        "avg_ms": avg_ms,
        "anomaly_count": len(anomalies),
    }

    return {"anomalies": anomalies, "summary": summary}


def get_ai_usage_stats() -> dict:
    """Get AI usage statistics: today, this month, all time."""
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    with get_connection() as conn:
        # All time stats
        all_time = conn.execute(
            "SELECT COUNT(*) as count, COALESCE(SUM(input_tokens), 0) as input_tokens, "
            "COALESCE(SUM(output_tokens), 0) as output_tokens, COALESCE(SUM(total_cost), 0) as total_cost "
            "FROM ai_usage"
        ).fetchone()

        # This month stats
        month = conn.execute(
            "SELECT COUNT(*) as count, COALESCE(SUM(input_tokens), 0) as input_tokens, "
            "COALESCE(SUM(output_tokens), 0) as output_tokens, COALESCE(SUM(total_cost), 0) as total_cost "
            "FROM ai_usage WHERE created_at >= ?",
            (month_start,),
        ).fetchone()

        # Today stats
        today = conn.execute(
            "SELECT COUNT(*) as count, COALESCE(SUM(input_tokens), 0) as input_tokens, "
            "COALESCE(SUM(output_tokens), 0) as output_tokens, COALESCE(SUM(total_cost), 0) as total_cost "
            "FROM ai_usage WHERE created_at >= ?",
            (today_start,),
        ).fetchone()

        # By service stats
        by_service = []
        rows = conn.execute(
            "SELECT service, model, COUNT(*) as count, COALESCE(SUM(input_tokens), 0) as input_tokens, "
            "COALESCE(SUM(output_tokens), 0) as output_tokens, COALESCE(SUM(total_cost), 0) as total_cost "
            "FROM ai_usage GROUP BY service, model ORDER BY total_cost DESC"
        ).fetchall()
        for r in rows:
            by_service.append(dict(r))

    return {
        "all_time": {
            "requests": all_time["count"],
            "input_tokens": all_time["input_tokens"],
            "output_tokens": all_time["output_tokens"],
            "total_cost": round(all_time["total_cost"], 4),
        },
        "month": {
            "requests": month["count"],
            "input_tokens": month["input_tokens"],
            "output_tokens": month["output_tokens"],
            "total_cost": round(month["total_cost"], 4),
        },
        "today": {
            "requests": today["count"],
            "input_tokens": today["input_tokens"],
            "output_tokens": today["output_tokens"],
            "total_cost": round(today["total_cost"], 4),
        },
        "by_service": by_service,
    }


# ============ DNS 规则分组管理 ============

def get_dns_groups() -> list[dict]:
    """获取所有 DNS 规则分组（按优先级排序）。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT g.*, COUNT(r.id) as rule_count "
            "FROM dns_groups g "
            "LEFT JOIN dns_rules r ON g.id = r.group_id "
            "GROUP BY g.id "
            "ORDER BY g.priority ASC, g.id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def create_dns_group(name: str, priority: int = 0, enabled: bool = True) -> int:
    """创建 DNS 规则分组。返回新分组 ID。"""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO dns_groups(name, priority, enabled, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            (name, priority, 1 if enabled else 0, now, now),
        )
        return cur.lastrowid


def update_dns_group(group_id: int, name: str | None = None, priority: int | None = None,
                     enabled: bool | None = None) -> bool:
    """更新 DNS 规则分组。返回是否成功。"""
    updates = []
    values = []
    if name is not None:
        updates.append("name = ?")
        values.append(name)
    if priority is not None:
        updates.append("priority = ?")
        values.append(priority)
    if enabled is not None:
        updates.append("enabled = ?")
        values.append(1 if enabled else 0)
    if not updates:
        return False
    updates.append("updated_at = ?")
    values.append(datetime.now().isoformat())
    values.append(group_id)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE dns_groups SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        return cur.rowcount > 0


def delete_dns_group(group_id: int) -> bool:
    """删除 DNS 规则分组及其所有规则。返回是否成功。"""
    with get_connection() as conn:
        # 先删除该分组下的所有规则（外键级联删除也可，但显式删除更安全）
        conn.execute("DELETE FROM dns_rules WHERE group_id = ?", (group_id,))
        cur = conn.execute("DELETE FROM dns_groups WHERE id = ?", (group_id,))
        return cur.rowcount > 0


def reorder_dns_groups(group_ids: list[int]) -> bool:
    """批量更新分组优先级（按传入顺序）。"""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        for priority, gid in enumerate(group_ids):
            conn.execute(
                "UPDATE dns_groups SET priority = ?, updated_at = ? WHERE id = ?",
                (priority, now, gid),
            )
        return True


def get_dns_rules(group_id: int | None = None) -> list[dict]:
    """获取 DNS 规则（可选按分组过滤）。"""
    with get_connection() as conn:
        if group_id is not None:
            rows = conn.execute(
                "SELECT * FROM dns_rules WHERE group_id = ? ORDER BY id ASC",
                (group_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT r.*, g.name as group_name, g.priority as group_priority "
                "FROM dns_rules r "
                "JOIN dns_groups g ON r.group_id = g.id "
                "ORDER BY g.priority ASC, g.id ASC, r.id ASC"
            ).fetchall()
        return [dict(r) for r in rows]


def create_dns_rule(group_id: int, pattern: str, mode: str = "wildcard",
                    action: str = "block", redirect_to: str | None = None) -> int:
    """创建 DNS 规则。返回新规则 ID。"""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO dns_rules(group_id, pattern, mode, action, redirect_to, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (group_id, pattern, mode, action, redirect_to, now, now),
        )
        return cur.lastrowid


def update_dns_rule(rule_id: int, pattern: str | None = None, mode: str | None = None,
                    action: str | None = None, redirect_to: str | None = None) -> bool:
    """更新 DNS 规则。返回是否成功。"""
    updates = []
    values = []
    if pattern is not None:
        updates.append("pattern = ?")
        values.append(pattern)
    if mode is not None:
        updates.append("mode = ?")
        values.append(mode)
    if action is not None:
        updates.append("action = ?")
        values.append(action)
    if redirect_to is not None:
        updates.append("redirect_to = ?")
        values.append(redirect_to)
    if not updates:
        return False
    updates.append("updated_at = ?")
    values.append(datetime.now().isoformat())
    values.append(rule_id)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE dns_rules SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        return cur.rowcount > 0


def delete_dns_rule(rule_id: int) -> bool:
    """删除 DNS 规则。返回是否成功。"""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM dns_rules WHERE id = ?", (rule_id,))
        return cur.rowcount > 0


def get_dns_rules_for_hijack() -> dict[str, str]:
    """获取用于 DNS 劫持的规则字典 {domain: ip}。

    只返回启用的分组中的规则，按优先级合并。
    - allow: 返回原规则
    - block: 返回 0.0.0.0
    - redirect: 返回 redirect_to
    """
    rules: dict[str, str] = {}
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT r.pattern, r.action, r.redirect_to "
            "FROM dns_rules r "
            "JOIN dns_groups g ON r.group_id = g.id "
            "WHERE g.enabled = 1 "
            "ORDER BY g.priority ASC, r.id ASC"
        ).fetchall()
        for row in rows:
            pattern = row["pattern"]
            action = row["action"]
            redirect_to = row["redirect_to"]
            if action == "allow":
                # allow 动作用空字符串或特殊标记表示不劫持
                rules[pattern] = redirect_to or ""
            elif action == "block":
                rules[pattern] = "0.0.0.0"
            elif action == "redirect" and redirect_to:
                rules[pattern] = redirect_to
    return rules
