"""设置 API + 证书管理 API。"""

import json
import os
import socket
import sys

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

from .. import db, logger, settings_store
from . import err, ok

router = APIRouter()


@router.get("/settings")
async def get_settings():
    """获取设置。inspector_tabs / flow_columns 等 JSON 数组类型自动反序列化。"""
    raw = db.get_all_settings()
    # 自动反序列化 JSON 数组类型的设置项
    json_keys = {"inspector_tabs", "flow_columns"}
    for k in json_keys:
        v = raw.get(k)
        if v and isinstance(v, str):
            try:
                raw[k] = json.loads(v)
            except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        raw["mitmproxy_available"] = False
    # 安全：不向客户端回传敏感凭据（避免被非回环来源读取 / 被写入 localStorage）
    # - api_token：API 鉴权令牌本身，泄露即绕过鉴权
    # - clash_secret：Mihomo 控制面密钥
    for _secret_key in ("api_token", "clash_secret"):
        raw.pop(_secret_key, None)
    return ok(raw)


@router.put("/settings")
async def update_settings(body: dict):
    """更新设置。list/dict 类型自动 JSON 序列化，bool 转 1/0。

    支持任意 key：用户偏好（列排序、导航顺序、主题等）也通过此接口写入 settings.json。
    """
    json_keys = {"inspector_tabs", "flow_columns"}
    for k, v in body.items():
        if v is None:
            continue  # 跳过前端未设置的 null 值
        if k in json_keys:
            db.set_setting(k, json.dumps(v, ensure_ascii=False))
        elif isinstance(v, bool):
            db.set_setting(k, "1" if v else "0")
        elif isinstance(v, (list, dict)):
            db.set_setting(k, json.dumps(v, ensure_ascii=False))
        else:
            db.set_setting(k, str(v))
    return ok(db.get_all_settings(), "设置已更新")


@router.post("/settings/open-file")
async def open_settings_file():
    """用记事本打开 settings.json 文件。"""
    path = settings_store.get_settings_path()
    if not os.path.exists(path):
        return err(f"设置文件不存在: {path}")
    try:
        if sys.platform == "win32":
            # 用记事本打开（不阻塞，记事本独立进程）
            import subprocess
            subprocess.Popen(["notepad.exe", path])  # noqa: S603
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])  # noqa: S603
        logger.info("settings", f"打开设置文件: {path}")
        return ok({"opened": True, "path": path})
    except Exception as e:
        logger.error("settings", f"打开设置文件失败: {path}", str(e))
        return err(f"打开失败: {e}")


# ---------- 证书管理 ----------

def _get_lan_ip() -> str:
    """获取本机局域网 IP（用于手机配代理/扫码下载证书）。

    原理：开一个 UDP socket "连接" 公网 IP（不发数据，只让 OS 选路由），
    读 socket 的本地地址。不可达也没关系，OS 会按路由表选合适的本地 IP。
    失败回退 127.0.0.1。
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
    """下载 Telnix 根证书（.pem 格式），供手机浏览器扫码下载安装。

    路由设计为 /api/cert/root.pem（注意：和 /api/cert/status 同前缀），
    手机扫码直接访问 http://电脑IP:18901/api/cert/root.pem 即可下载。
    """
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("代理未启动")
    path = proxy.ssl_bump.root_cert_path
    if not os.path.exists(path):
        return err(f"根证书不存在: {path}")
    return FileResponse(path, media_type="application/x-pem-file",
                        filename="telnix_root.pem")


def _android_cert_filename(cert_path: str) -> str:
    """计算安卓系统证书文件名（subject_hash_old + .0）。

    安卓 7+ 需把证书放到 /system/etc/security/cacerts/，文件名必须是
    "<subject_hash_old>.0"。优先用 openssl 命令计算（最准），
    失败回退到 cryptography 库（兼容性较好）。
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
    except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        pass

    # 方式3：兜底，用固定名（不推荐但保证不报错）
    return "telnix_root.0"


@router.get("/cert/android.pem")
async def download_android_cert(request: Request):
    """下载安卓 7+ 系统证书格式（文件名为 <subject_hash_old>.0）。

    安卓 7+ 用户证书不被信任，需要把证书推到 /system/etc/security/cacerts/，
    且文件名必须是 "<subject_hash_old>.0"。本接口直接返回计算好文件名的证书，
    省去用户在电脑上跑 openssl 的步骤。

    用法（root 手机）：
        adb push <hash>.0 /sdcard/
        adb shell su -c "mount -o rw,remount /system && \\
                         mv /sdcard/<hash>.0 /system/etc/security/cacerts/ && \\
                         chmod 644 /system/etc/security/cacerts/<hash>.0"
    """
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("代理未启动")
    path = proxy.ssl_bump.root_cert_path
    if not os.path.exists(path):
        return err(f"根证书不存在: {path}")
    filename = _android_cert_filename(path)
    return FileResponse(path, media_type="application/x-pem-file",
                        filename=filename)


@router.get("/mobile/setup")
async def mobile_setup(request: Request):
    """移动端抓包配置信息：本机 IP、代理端口、API 端口、证书下载 URL。

    供前端"手机抓包"向导显示二维码用：二维码内容是证书下载 URL，
    手机扫码后浏览器打开直接下载 .pem。
    """
    lan_ip = _get_lan_ip()
    proxy_port = 8888
    try:
        from ..config import get_proxy_port
        proxy_port = get_proxy_port()
    except Exception:  # noqa: BLE001
        pass
    api_port = 18901
    try:
        from ..config import get_port
        api_port = get_port()
    except Exception:  # noqa: BLE001
        pass
    # API 已加 Token 鉴权（回环免鉴权，非回环需 Token）。手机通过局域网 IP
    # 访问属于非回环来源，必须在证书下载链接中附带 Token，否则会被 401 拒绝。
    try:
        from . import auth
        token = auth.get_api_token()
    except Exception:  # noqa: BLE001
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
    """证书状态（已装/未装）。"""
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("代理未启动")
    installed = proxy.ssl_bump.is_root_cert_installed()
    proxy.cert_installed = installed
    return ok({
        "installed": installed,
        "root_cert_path": proxy.ssl_bump.root_cert_path,
        "thumbprint": proxy.ssl_bump.root_thumbprint(),
    })


@router.post("/cert/install")
async def cert_install(request: Request):
    """安装根证书到系统信任库（Windows 需 UAC，会弹窗）。"""
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("代理未启动")

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
            "根证书已安装并通过验证",
        )
    elif result.get("installed") and not verified:
        return err(
            "证书安装后验证失败：certutil 返回成功但未在信任库中找到对应指纹的证书。"
            "可能原因：(1) Windows CTL 缓存延迟，请等待 30 秒后重试；"
            "(2) UAC 弹窗未确认或被组策略拦截；"
            "(3) 证书文件被杀毒软件隔离。",
            data={**result, "verified": False, "thumbprint": expected_thumbprint},
        )
    else:
        error_msg = result.get("error", "")
        hint = ""
        if "用户取消了 UAC" in error_msg:
            hint = "请在 UAC 弹窗中点击'是'。若未出现弹窗，请检查 UAC 策略设置。"
        elif "超时" in error_msg:
            hint = "UAC 等待超时，请重新点击安装并在 2 分钟内确认 UAC 弹窗。"
        return err(
            f"安装失败（exit_code={result.get('exit_code')}）{hint}",
            data=result,
        )


@router.post("/cert/remove")
async def cert_remove(request: Request):
    """移除根证书（需 UAC）。"""
    proxy = request.app.state.telnix.proxy
    if proxy is None or proxy.ssl_bump is None:
        return err("代理未启动")
    result = proxy.ssl_bump.remove_root_cert()
    if result.get("removed"):
        proxy.cert_installed = False
        return ok(result, "根证书已移除")
    return err(f"移除失败（exit_code={result.get('exit_code')}）", data=result)


# ---------- 路径操作 ----------

def _get_allowed_base_dirs():
    """返回允许 open-path/list-dirs 访问的根目录列表。"""
    from ..config import get_cert_dir, get_data_dir
    dirs = [get_data_dir(), get_cert_dir()]
    # 可选：UI dist 目录
    try:
        from ..config import get_ui_dist_dir
        ui_dir = get_ui_dist_dir()
        if ui_dir and os.path.isdir(ui_dir):
            dirs.append(ui_dir)
    except Exception:  # noqa: BLE001
        pass
    return [os.path.realpath(d) for d in dirs if os.path.isdir(d)]


# Windows 禁止打开的可执行扩展名
_BLOCKED_EXTS = {".exe", ".bat", ".cmd", ".js", ".vbs", ".ps1", ".scr", ".com", ".msi"}


@router.post("/settings/open-path")
async def open_path(body: dict):
    """在系统文件管理器中打开指定路径。路径为空时使用默认数据目录。"""
    from ..config import get_data_dir
    path = (body.get("path") or "").strip()
    if not path:
        # 默认打开数据目录
        path = get_data_dir()

    # 路径遍历防护
    try:
        real_path = os.path.realpath(path)
    except Exception as e:  # noqa: BLE001
        return err(f"路径无效: {path} ({e})")

    allowed_dirs = _get_allowed_base_dirs()
    if not any(real_path == d or real_path.startswith(d + os.sep) for d in allowed_dirs):
        return err("禁止打开此路径（仅允许数据目录和证书目录）")

    if not os.path.exists(real_path):
        return err(f"路径不存在: {real_path}")

    # Windows 上禁止打开可执行文件
    if sys.platform == "win32":
        _, ext = os.path.splitext(real_path)
        if ext.lower() in _BLOCKED_EXTS:
            return err(f"禁止打开可执行文件: {ext}")
        try:
            os.startfile(real_path)  # noqa: S606
        except OSError as e:
            return err(f"打开失败: {e}")
    else:
        import subprocess
        try:
            subprocess.Popen(["xdg-open", real_path])  # noqa: S603
        except OSError as e:
            return err(f"打开失败: {e}")
    logger.info("settings", f"打开路径: {real_path}")
    return ok({"opened": True, "path": real_path})


@router.get("/settings/list-dirs")
async def list_dirs(path: str = Query("")):
    """列出指定路径下的子目录，用于前端目录选择器。"""
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
                return err("禁止列出此路径（仅允许数据目录和证书目录）")
        if not os.path.isdir(real_path):
            return err(f"不是目录: {real_path}")
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
        logger.error("settings", f"列目录失败: {path}", str(e))
        return err(f"读取失败: {e}")
