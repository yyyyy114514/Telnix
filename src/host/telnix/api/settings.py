"""Settings API + certificate management API."""

import json
import os
import socket
import sys

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

from .. import db, logger, settings_store
from ..logger import _capture_log
from . import err, ok
from .auth import is_loopback

router = APIRouter()


@router.get("/settings")
async def get_settings(request: Request):
    """Get settings. JSON array types like inspector_tabs / flow_columns are auto-deserialized."""
    raw = db.get_all_settings()
    # 自动反序列化 JSON 数组类型的设置项
    json_keys = {"inspector_tabs", "flow_columns"}
    for k in json_keys:
        v = raw.get(k)
        if v and isinstance(v, str):
            try:
                raw[k] = json.loads(v)
            except Exception as e:

                _capture_log("error", "API exception", extra={"exc": repr(e)})

                pass
    # 注入默认数据路径：用户未设置时返回实际数据目录，便于前端直观显示
    if not raw.get("data_path"):
        from ..config import get_data_dir
        raw["data_path"] = get_data_dir()
    # 注入 settings.json 文件路径（前端"打开设置文件"按钮用）
    raw["settings_file_path"] = settings_store.get_settings_path()
    # 注入 mitmproxy 可用性（前端据此决定是否禁用 mitmproxy 引擎选项）
    try:
        from ..proxy.mitmproxy_engine import MITMPROXY_AVAILABLE
        raw["mitmproxy_available"] = MITMPROXY_AVAILABLE
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in settings.py", extra={"exc": repr(e)})
        raw["mitmproxy_available"] = False
    # 安全：不向客户端回传敏感凭据（避免被非回环来源读取 / 被写入 localStorage）
    # - api_token：API 鉴权令牌本身，泄露即绕过鉴权，任何来源都不回传
    # - clash_secret：Mihomo 控制面密钥，仅回环保留，非回环来源替换为 has_clash_secret 布尔值
    raw.pop("api_token", None)
    client = request.client
    client_host = client.host if client is not None else ""
    if not is_loopback(client_host):
        raw["has_clash_secret"] = bool(raw.get("clash_secret"))
        raw.pop("clash_secret", None)
    return ok(raw)


@router.put("/settings")
async def update_settings(body: dict):
    """Update settings. list/dict types auto JSON serialized, bool converted to 1/0.

    Supports any key: user preferences (column sorting, navigation order, theme, etc.) are also written to settings.json via this endpoint.

    Performance: batch-merge all keys in one write, instead of calling set_setting
    per key (each set_setting reads+writes the entire JSON file, causing 5+ second
    delays when settings.json is large or has many keys).
    """
    from .. import secure_storage
    json_keys = {"inspector_tabs", "flow_columns"}
    # 安全修复：敏感字段自动加密存储
    encrypted_keys = {"clash_secret", "deepseek_api_key"}
    updates: dict[str, str] = {}
    for k, v in body.items():
        if v is None:
            continue  # 跳过前端未设置的 null 值
        # clash_secret 为空字符串时跳过：非回环访问时 GET 不返回密钥，
        # 前端会设为空字符串，如果保存则覆盖真实密钥导致 Clash 401
        if k == "clash_secret" and v == "":
            continue
        if k in json_keys:
            updates[k] = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            updates[k] = "1" if v else "0"
        elif isinstance(v, (list, dict)):
            updates[k] = json.dumps(v, ensure_ascii=False)
        else:
            val_str = str(v)
            # 敏感字段：写入时加密
            if k in encrypted_keys and val_str:
                val_str = secure_storage.encrypt(val_str)
            updates[k] = val_str
    if updates:
        # 批量写入：一次读+一次写，避免 N 次 IO
        from .. import settings_store
        settings_store.set_all_settings(updates)
    return ok(db.get_all_settings(), "Settings updated")


# ---------- Performance Configuration ----------

@router.get("/settings/performance")
async def get_performance_config():
    """Get performance configuration."""
    from .. import settings_store
    return ok({
        "max_body_size": settings_store.get_setting_int("max_body_size", 10 * 1024 * 1024),
        "decompress_threshold": settings_store.get_setting_int("decompress_threshold", 1024),
        "ssl_context_cache_size": settings_store.get_setting_int("ssl_context_cache_size", 256),
        "max_connections": settings_store.get_setting_int("max_connections", 200),
    })


@router.put("/settings/performance")
async def update_performance_config(body: dict):
    """Update performance configuration."""
    from .. import settings_store
    updates = {}
    if "max_body_size" in body:
        updates["max_body_size"] = str(int(body["max_body_size"]))
    if "decompress_threshold" in body:
        updates["decompress_threshold"] = str(int(body["decompress_threshold"]))
    if "ssl_context_cache_size" in body:
        updates["ssl_context_cache_size"] = str(int(body["ssl_context_cache_size"]))
    if "max_connections" in body:
        updates["max_connections"] = str(int(body["max_connections"]))
    if updates:
        settings_store.set_all_settings(updates)
    return ok(updates, "Performance config updated")


@router.post("/settings/performance/preset")
async def apply_performance_preset(body: dict):
    """Apply a performance preset (light/standard/high_performance)."""
    preset_name = body.get("preset", "standard")
    from ..settings_store import PERFORMANCE_PRESETS
    if preset_name not in PERFORMANCE_PRESETS:
        return err(f"Unknown preset: {preset_name}")
    preset = PERFORMANCE_PRESETS[preset_name]
    from .. import settings_store
    updates = {
        "max_body_size": str(preset["max_body_size"]),
        "decompress_threshold": str(preset["decompress_threshold"]),
        "ssl_context_cache_size": str(preset["ssl_context_cache_size"]),
        "max_connections": str(preset["max_connections"]),
    }
    settings_store.set_all_settings(updates)
    return ok(updates, f"Applied preset: {preset_name}")


# ---------- SSL/TLS Configuration ----------

@router.get("/settings/ssl")
async def get_ssl_config():
    """Get SSL/TLS configuration."""
    from .. import settings_store
    return ok({
        "min_tls_version": settings_store.get_setting("min_tls_version", "TLS 1.2"),
        "cipher_suites": settings_store.get_setting("cipher_suites", "DEFAULT"),
        "sni_spoofing": settings_store.get_setting("sni_spoofing", "0") == "1",
    })


@router.put("/settings/ssl")
async def update_ssl_config(body: dict):
    """Update SSL/TLS configuration."""
    from .. import settings_store
    updates = {}
    if "min_tls_version" in body:
        updates["min_tls_version"] = str(body["min_tls_version"])
    if "cipher_suites" in body:
        updates["cipher_suites"] = str(body["cipher_suites"])
    if "sni_spoofing" in body:
        updates["sni_spoofing"] = "1" if body["sni_spoofing"] else "0"
    if updates:
        settings_store.set_all_settings(updates)
    return ok(updates, "SSL config updated")


@router.post("/settings/open-file")
async def open_settings_file():
    """Open settings.json file with Notepad."""
    path = settings_store.get_settings_path()
    if not os.path.exists(path):
        return err(f"Settings file not found: {path}")
    try:
        if sys.platform == "win32":
            # 用记事本打开（不阻塞，记事本独立进程）
            import subprocess
            subprocess.Popen(["notepad.exe", path])  # noqa: S603
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])  # noqa: S603
        logger.info("settings", f"Open settings file: {path}")
        return ok({"opened": True, "path": path})
    except Exception as e:
        logger.error("settings", f"Failed to open settings file: {path}", str(e))
        return err(f"Open failed: {e}")


# ---------- Certificate management ----------

def _get_lan_ip() -> str:
    """Get local LAN IP (for phone proxy configuration/QR code certificate download).

    Principle: open a UDP socket "connecting" to a public IP (no data sent, just let OS choose route),
    read socket's local address. Unreachable is fine, OS will select appropriate local IP per routing table.
    Fallback to 127.0.0.1 on failure.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 不真正发包，只是让 OS 选路由
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


@router.get("/cert/root.pem")
async def download_root_cert(request: Request):
    """Download Telnix root certificate (.pem format), for phone browser QR code download and install.

    Route designed as /api/cert/root.pem (note: same prefix as /api/cert/status),
    phone scans QR code and directly visits http://computer_IP:18901/api/cert/root.pem to download.
    """
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("Proxy not started")
    path = proxy.ssl_bump.root_cert_path
    if not os.path.exists(path):
        return err(f"Root cert not found: {path}")
    return FileResponse(path, media_type="application/x-pem-file",
                        filename="telnix_root.pem")


def _android_cert_filename(cert_path: str) -> str:
    """Calculate Android system certificate filename (subject_hash_old + .0).

    Android 7+ requires placing certificate in /system/etc/security/cacerts/, filename must be
    "<subject_hash_old>.0". Prefer using openssl command (most accurate),
    fallback to cryptography library (better compatibility) on failure.
    """
    # 方式1：openssl x509 -inform PEM -subject_hash_old
    import subprocess
    try:
        r = subprocess.run(
            ["openssl", "x509", "-inform", "PEM", "-subject_hash_old", "-in", cert_path],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            h = r.stdout.strip().splitlines()[0]
            if h and len(h) == 8:
                return f"{h}.0"
    except Exception as e:

        _capture_log("error", "API exception", extra={"exc": repr(e)})

        pass

    # 方式2：用 cryptography 库手动计算（OpenSSL 兼容的 subject_hash_old 算法）
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        import hashlib
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        # subject_hash_old = MD5(subject) 截断前 4 字节小端
        # 但不同 OpenSSL 版本有差异，最稳的是用 cert.fingerprint(hashes.SHA1())
        # 这里用 MD5(subject_der) 前 4 字节小端，兼容老版本 OpenSSL subject_hash_old
        from cryptography.hazmat.primitives.serialization import Encoding
        subject_der = cert.subject.public_bytes()
        md5 = hashlib.md5(subject_der).digest()
        # 取前 4 字节，按小端序组合成 uint32
        val = md5[0] | (md5[1] << 8) | (md5[2] << 16) | (md5[3] << 24)
        # 强制最高位为 0（与 OpenSSL 一致）
        val = val & 0x7FFFFFFF
        return f"{val:08x}.0"
    except Exception as e:

        _capture_log("error", "API exception", extra={"exc": repr(e)})

        pass

    # 方式3：兜底，用固定名（不推荐但保证不报错）
    return "telnix_root.0"


@router.get("/cert/android.pem")
async def download_android_cert(request: Request):
    """Download Android 7+ system certificate format (filename is <subject_hash_old>.0).

    Android 7+ user certificates are not trusted, need to push certificate to /system/etc/security/cacerts/,
    and filename must be "<subject_hash_old>.0". This endpoint directly returns certificate with computed filename,
    saving user from running openssl on computer.

    Usage (rooted phone):
        adb push <hash>.0 /sdcard/
        adb shell su -c "mount -o rw,remount /system && \\
                         mv /sdcard/<hash>.0 /system/etc/security/cacerts/ && \\
                         chmod 644 /system/etc/security/cacerts/<hash>.0"
    """
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("Proxy not started")
    path = proxy.ssl_bump.root_cert_path
    if not os.path.exists(path):
        return err(f"Root cert not found: {path}")
    filename = _android_cert_filename(path)
    return FileResponse(path, media_type="application/x-pem-file",
                        filename=filename)


@router.get("/mobile/setup")
async def mobile_setup(request: Request):
    """Mobile capture configuration info: local IP, proxy port, API port, certificate download URL.

    For frontend "phone capture" wizard to display QR code: QR code content is certificate download URL,
    phone scans and browser opens it to directly download .pem.
    """
    lan_ip = _get_lan_ip()
    proxy_port = 8888
    try:
        from ..config import get_proxy_port
        proxy_port = get_proxy_port()
    except Exception as e:

        _capture_log("error", "API exception", extra={"exc": repr(e)})

        pass
    api_port = 18901
    try:
        from ..config import get_port
        api_port = get_port()
    except Exception as e:

        _capture_log("error", "API exception", extra={"exc": repr(e)})

        pass
    # API 已加 Token 鉴权（回环免鉴权，非回环需 Token）。手机通过局域网 IP
    # 访问属于非回环来源，必须在证书下载链接中附带 Token，否则会被 401 拒绝。
    try:
        from . import auth
        token = auth.get_api_token()
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in settings.py", extra={"exc": repr(e)})
        token = ""
    base = f"http://{lan_ip}:{api_port}/api"
    return ok({
        "lan_ip": lan_ip,
        "proxy_host": lan_ip,
        "proxy_port": proxy_port,
        "api_port": api_port,
        "cert_download_url": f"{base}/cert/root.pem?token={token}",
        "android_cert_url": f"{base}/cert/android.pem?token={token}",
    })


@router.get("/cert/status")
async def cert_status(request: Request):
    """Certificate status (installed/not installed)."""
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("Proxy not started")
    installed = proxy.ssl_bump.is_root_cert_installed()
    proxy.cert_installed = installed
    return ok({
        "installed": installed,
        "root_cert_path": proxy.ssl_bump.root_cert_path,
        "thumbprint": proxy.ssl_bump.root_thumbprint(),
    })


@router.get("/cert/details")
async def cert_details(request: Request):
    """Get detailed certificate information including expiry countdown and cache stats."""
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("Proxy not started")

    from datetime import datetime, timezone
    import os

    root_cert_path = proxy.ssl_bump.root_cert_path
    expiry_countdown = None
    issued_date = None
    expiry_date = None
    serial_number = None

    # 读取证书信息
    if os.path.exists(root_cert_path):
        try:
            from cryptography import x509
            with open(root_cert_path, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read())
            issued_date = cert.not_valid_before_utc.isoformat() if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before.isoformat()
            expiry_date = cert.not_valid_after_utc.isoformat() if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.isoformat()
            # 计算到期天数
            expiry = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after
            now = datetime.now(timezone.utc)
            delta = expiry.replace(tzinfo=timezone.utc) - now
            expiry_countdown = delta.days
            # 序列号
            serial_number = str(cert.serial_number)
        except Exception as e:
            _capture_log("error", "cert details parse error", extra={"exc": repr(e)})

    # 获取叶证书缓存数量（如果有缓存统计）
    leaf_cert_count = 0
    if hasattr(proxy.ssl_bump, "get_leaf_cert_count"):
        try:
            leaf_cert_count = proxy.ssl_bump.get_leaf_cert_count()
        except Exception:
            pass

    # 证书过期告警开关
    from .. import settings_store
    cert_expiry_alert = settings_store.get_setting("cert_expiry_alert", "1") == "1"

    return ok({
        "root_cert_path": root_cert_path,
        "installed": proxy.ssl_bump.is_root_cert_installed(),
        "thumbprint": proxy.ssl_bump.root_thumbprint(),
        "issued_date": issued_date,
        "expiry_date": expiry_date,
        "expiry_countdown": expiry_countdown,
        "serial_number": serial_number,
        "leaf_cert_count": leaf_cert_count,
        "cert_expiry_alert": cert_expiry_alert,
    })


@router.put("/cert/expiry-alert")
async def cert_expiry_alert(body: dict):
    """Update certificate expiry alert setting."""
    enabled = body.get("enabled", True)
    from .. import settings_store
    settings_store.set_setting("cert_expiry_alert", "1" if enabled else "0")
    return ok({"cert_expiry_alert": enabled})


@router.post("/cert/regenerate")
async def cert_regenerate(request: Request):
    """Regenerate root certificate (dangerous operation)."""
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("Proxy not started")
    try:
        # 重新生成根证书
        proxy.ssl_bump.generate_root_cert()
        proxy.cert_installed = False  # 需要重新安装
        return ok({"regenerated": True}, "Certificate regenerated. Please reinstall the root certificate.")
    except Exception as e:
        return err(f"Failed to regenerate certificate: {e}")


@router.post("/cert/install")
async def cert_install(request: Request):
    """Install root certificate to system trust store (Windows requires UAC, will popup)."""
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("Proxy not started")

    expected_thumbprint = proxy.ssl_bump.root_thumbprint()
    result = proxy.ssl_bump.install_root_cert()

    # 关键：不信任 install_root_cert 的 exit_code，主动用指纹复核
    verified = proxy.ssl_bump.is_root_cert_installed()
    proxy.cert_installed = verified

    if result.get("installed") and verified:
        # 安装成功后清空 SSL bump 失败集合，让用户重装后立即生效
        if hasattr(proxy, "clear_ssl_bump_failed_hosts"):
            proxy.clear_ssl_bump_failed_hosts()
        return ok(
            {**result, "thumbprint": expected_thumbprint, "verified": True},
            "Root certificate installed and verified",
        )
    elif result.get("installed") and not verified:
        return err(
            "Certificate verification failed after install: certutil returned success but the certificate with the matching fingerprint was not found in the trust store. "
            "Possible causes: (1) Windows CTL cache delay, please wait 30 seconds and retry; "
            "(2) UAC prompt not confirmed or blocked by group policy; "
            "(3) Certificate file quarantined by antivirus software.",
            data={**result, "verified": False, "thumbprint": expected_thumbprint},
        )
    else:
        error_msg = result.get("error", "")
        hint = ""
        if "cancelled UAC" in error_msg:
            hint = "Please click 'Yes' in the UAC prompt. If no prompt appears, check UAC policy settings."
        elif "timeout" in error_msg:
            hint = "UAC wait timed out, please click install again and confirm the UAC prompt within 2 minutes."
        return err(
            f"Install failed (exit_code={result.get('exit_code')}) {hint}",
            data=result,
        )


@router.post("/cert/remove")
async def cert_remove(request: Request):
    """Remove root certificate (requires UAC)."""
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("Proxy not started")
    result = proxy.ssl_bump.remove_root_cert()
    if result.get("removed"):
        proxy.cert_installed = False
        return ok(result, "Root certificate removed")
    return err(f"Remove failed (exit_code={result.get('exit_code')})", data=result)


# ---------- Path operations ----------

def _get_allowed_base_dirs():
    """Return list of root directories allowed for open-path/list-dirs access."""
    from ..config import get_cert_dir, get_data_dir
    dirs = [get_data_dir(), get_cert_dir()]
    # 可选：UI dist 目录
    try:
        from ..config import get_ui_dist_dir
        ui_dir = get_ui_dist_dir()
        if ui_dir and os.path.isdir(ui_dir):
            dirs.append(ui_dir)
    except Exception as e:

        _capture_log("error", "API exception", extra={"exc": repr(e)})

        pass
    return [os.path.realpath(d) for d in dirs if os.path.isdir(d)]


# Windows 禁止打开的可执行扩展名
_BLOCKED_EXTS = {".exe", ".bat", ".cmd", ".js", ".vbs", ".ps1", ".scr", ".com", ".msi"}


@router.post("/settings/open-path")
async def open_path(body: dict):
    """Open specified path in system file manager. When path is empty, uses default data directory."""
    from ..config import get_data_dir
    path = (body.get("path") or "").strip()
    if not path:
        # 默认打开数据目录
        path = get_data_dir()

    # 路径遍历防护
    try:
        real_path = os.path.realpath(path)
    except Exception as e:  # noqa: BLE001
        return err(f"Invalid path: {path} ({e})")

    allowed_dirs = _get_allowed_base_dirs()
    if not any(real_path == d or real_path.startswith(d + os.sep) for d in allowed_dirs):
        return err("Opening this path is forbidden (only data dir and cert dir are allowed)")

    if not os.path.exists(real_path):
        return err(f"Path not found: {real_path}")

    # Windows 上禁止打开可执行文件
    if sys.platform == "win32":
        _, ext = os.path.splitext(real_path)
        if ext.lower() in _BLOCKED_EXTS:
            return err(f"Opening executable files is forbidden: {ext}")
        try:
            os.startfile(real_path)  # noqa: S606
        except OSError as e:
            return err(f"Open failed: {e}")
    else:
        import subprocess
        try:
            subprocess.Popen(["xdg-open", real_path])  # noqa: S603
        except OSError as e:
            return err(f"Open failed: {e}")
    logger.info("settings", f"Open path: {real_path}")
    return ok({"opened": True, "path": real_path})


@router.get("/settings/list-dirs")
async def list_dirs(path: str = Query("")):
    """List subdirectories under specified path, for frontend directory selector."""
    if not path:
        # 返回系统根/家目录
        if sys.platform == "win32":
            # 列出盘符
            import string
            drives = []
            for c in string.ascii_uppercase:
                drv = f"{c}:\\"
                if os.path.exists(drv):
                    drives.append(drv)
            return ok({"current": "", "dirs": drives, "parent": ""})
        else:
            from ..config import get_data_dir
            path = get_data_dir()
    try:
        path = os.path.abspath(path)
        # 路径遍历防护：白名单校验
        real_path = os.path.realpath(path)
        allowed_dirs = _get_allowed_base_dirs()
        # 允许根目录、盘符根（用户首次选择时）以及白名单内目录
        is_root_or_drive = (
            path in ("/", "", ".")
            or (sys.platform == "win32" and len(path) <= 3 and path.endswith(":"))
            or (sys.platform == "win32" and path.endswith(":\\"))
            or (sys.platform != "win32" and path == "/")
        )
        if not is_root_or_drive:
            if not any(real_path == d or real_path.startswith(d + os.sep)
                       for d in allowed_dirs):
                return err("Listing this path is forbidden (only data dir and cert dir are allowed)")
        if not os.path.isdir(real_path):
            return err(f"Not a directory: {real_path}")
        dirs = []
        for name in sorted(os.listdir(real_path)):
            full = os.path.join(real_path, name)
            if os.path.isdir(full):
                dirs.append(name)
        parent = os.path.dirname(path)
        if parent == path:
            parent = ""
        return ok({"current": path, "dirs": dirs, "parent": parent})
    except Exception as e:
        logger.error("settings", f"List dirs failed: {path}", str(e))
        return err(f"Read failed: {e}")
