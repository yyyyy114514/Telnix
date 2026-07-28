"""SSL bump：根证书管理 + 动态证书签发。

用 cryptography 库生成自签根证书，并为每个域名动态签发叶证书（用根证书签）。
证书缓存避免重复签发。安装/检查/移除根证书走 certutil（安装需 UAC）。

跨平台说明：
- Windows：用 certutil + ShellExecuteEx 提权安装到 Root 信任库。
- macOS：用 security add-trusted-cert 安装到钥匙串（需用户授权）。
- Linux：不自动安装（不同发行版证书库不同），提示用户手动安装。
"""

import os
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# 平台判断
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

ROOT_SUBJECT_CN = "Telnix Root CA"
ROOT_ORG = "Telnix"

# 证书缓存 LRU 上限
_CERT_CACHE_MAX = 2000
# UAC 提权等待超时（毫秒）
_WAIT_UAC_TIMEOUT_MS = 120000

# 合法主机名：DNS label / IPv4 / IPv6 字面量（不含路径分隔符，防路径遍历）
_HOST_RE = re.compile(r'^[A-Za-z0-9.\-:]+$')


def _strip_port(host: str) -> str:
    """去掉 host 末尾的端口，正确处理 IPv6 字面量。"""
    if host.startswith("["):
        idx = host.find("]")
        if idx != -1:
            return host[1:idx]
        return host
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


def _is_valid_host(host: str) -> bool:
    """校验 host 是否合法（防路径遍历：拒绝包含 / \\ .. 等字符的 host）。"""
    if not host or len(host) > 253:
        return False
    # 拒绝包含路径分隔符或 .. 段（防止写入 cert_dir 之外）
    if "/" in host or "\\" in host or ".." in host:
        return False
    return bool(_HOST_RE.match(host))


class SSLBumpManager:
    """SSL bump：根证书管理 + 动态证书签发。"""

    def __init__(self, cert_dir: str):
        self.cert_dir = cert_dir
        self.root_key_path = os.path.join(cert_dir, "telnix_root.key")
        self.root_cert_path = os.path.join(cert_dir, "telnix_root.crt")
        self._cert_cache: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        self._root_key = None
        self._root_cert = None
        # 性能优化：每域名独立锁，不同 host 的首次签发可并行 keygen
        # （原全局锁会把多域名首次访问串行化，每个 keygen 50-150ms）
        self._host_locks: "OrderedDict[str, threading.Lock]" = OrderedDict()
        self._host_locks_lock = threading.Lock()
        self._ensure_root_cert()

    def _get_host_lock(self, host: str) -> threading.Lock:
        """获取指定 host 的独立锁（懒创建，带 LRU 淘汰）。"""
        with self._host_locks_lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            else:
                self._host_locks.move_to_end(host)
            while len(self._host_locks) > _CERT_CACHE_MAX:
                self._host_locks.popitem(last=False)
            return lock

    # ---------- 根证书 ----------

    def _ensure_root_cert(self):
        """生成或加载根证书与密钥。"""
        if os.path.exists(self.root_cert_path) and os.path.exists(self.root_key_path):
            self._load_root()
            # 兼容性：老版本生成的根证书没有 SKI，重新生成（一次性迁移）
            try:
                self._root_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            except x509.ExtensionNotFound:
                try:
                    os.remove(self.root_cert_path)
                    os.remove(self.root_key_path)
                except OSError:
                    pass
                # 关键修复：根证书被重新生成时，必须清空所有旧叶证书缓存。
                # 旧叶证书由旧根的私钥签发，新根的公钥无法验证其签名，
                # 客户端收到后发出 certificate_unknown alert（46），
                # 导致所有 host 的 TLS 握手失败。
                self._purge_leaf_certs()
                self._root_key = None
                self._root_cert = None
                return self._ensure_root_cert()
            return
        # 全新生成根证书：清空可能残留的旧叶证书
        # （根证书文件可能被外部删除/损坏/手动清理后重建，旧叶证书与新根不匹配）
        self._purge_leaf_certs()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ROOT_ORG),
            x509.NameAttribute(NameOID.COMMON_NAME, ROOT_SUBJECT_CN),
        ])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                critical=False)
            .sign(key, hashes.SHA256())
        )
        # 根私钥以 0o600 权限直接创建，消除"先 open 后 chmod"之间的权限暴露窗口
        kfd = os.open(self.root_key_path,
                      os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(kfd, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(self.root_cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        os.chmod(self.root_cert_path, 0o644)
        self._root_key = key
        self._root_cert = cert

    def _load_root(self):
        with open(self.root_key_path, "rb") as f:
            self._root_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(self.root_cert_path, "rb") as f:
            self._root_cert = x509.load_pem_x509_certificate(f.read())

    # ---------- 动态证书签发 ----------

    def get_cert(self, host: str) -> tuple[str, str]:
        """获取指定域名的证书（有缓存），返回 (cert_path, key_path)。

        性能优化：每域名独立锁 — 同 host 互斥避免重复签发，
        不同 host 的 keygen 完全并行（原全局锁会串行化所有域名，每个 keygen 50-150ms）。

        安全：校验 host 合法性，防止路径遍历写入 cert_dir 之外。
        """
        # 去掉端口
        host = _strip_port(host)
        # 校验 host 合法性（防路径遍历：拒绝 / \ .. 等字符）
        if not _is_valid_host(host):
            raise ValueError(f"invalid host for cert: {host!r}")
        # 第一次无锁检查（命中则零开销）
        cached = self._cert_cache.get(host)
        if cached is not None:
            self._cert_cache.move_to_end(host)
            return cached
        # 每域名独立锁：锁内双检 + 签发
        with self._get_host_lock(host):
            cached = self._cert_cache.get(host)
            if cached is not None:
                self._cert_cache.move_to_end(host)
                return cached
            cert_path = os.path.join(self.cert_dir, f"{host}.crt")
            key_path = os.path.join(self.cert_dir, f"{host}.key")
            # 锁内签发（保证同 host 只有一个线程做 RSA keygen + 写文件）
            # 验证磁盘上的缓存叶证书是否仍有效：
            # 1. 文件不存在 → 签发
            # 2. 叶证书不是由当前根证书签发（根已重新生成，旧叶证书 stale）→ 重新签发
            # 3. 证书文件不包含完整链（旧格式只含 leaf，缺 root 链）→ 重新签发
            if not (os.path.exists(cert_path) and os.path.exists(key_path)) \
                    or self._needs_resign(cert_path):
                self._sign_host_cert(host, cert_path, key_path)
            self._cert_cache[host] = (cert_path, key_path)
            while len(self._cert_cache) > _CERT_CACHE_MAX:
                self._cert_cache.popitem(last=False)
            return cert_path, key_path

    def _sign_host_cert(self, host: str, cert_path: str, key_path: str):
        """用根证书为 host 签发叶证书。

        性能优化：子证书用 ECDSA P-256（keygen ~1-5ms），
        而非 RSA 2048（keygen ~50-150ms）。
        根证书保持 RSA（已安装），签发子证书时用 RSA 根密钥签名（跨算法签名合法）。
        浏览器加载 30 个新域名时，keygen 总耗时从 3 秒降到 150ms。
        """
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, host),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ROOT_ORG),
        ])
        now = datetime.now(timezone.utc)
        san = x509.SubjectAlternativeName([x509.DNSName(host)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._root_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(san, critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False),
                critical=True,
            )
            .add_extension(x509.OCSPNoCheck(), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                    self._root_cert.extensions.get_extension_for_class(
                        x509.SubjectKeyIdentifier).value),
                critical=False)
            .sign(self._root_key, hashes.SHA256())
        )
        # 叶私钥以 0o600 权限直接创建，消除"先 open 后 chmod"之间的权限暴露窗口
        lkfd = os.open(key_path,
                       os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(lkfd, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(cert_path, "wb") as f:
            # 写入完整证书链（leaf + root），确保 TLS 握手时 ServerHello 的
            # Certificate 消息包含到信任根的完整链路。
            # 某些 Windows 客户端（SChannel / Edge / Chrome）在只收到 leaf 时
            # 可能无法从信任库中找到对应 issuer 来构建链（即使根证书已安装），
            # 导致 certificate_unknown alert。发送 leaf + root 让客户端能直接
            # 从消息中构建链，再校验 root 是否在信任库中。
            # 顺序：leaf 在前（必须），root 在后（用作链构建辅助）
            f.write(cert.public_bytes(serialization.Encoding.PEM))
            f.write(self._root_cert.public_bytes(serialization.Encoding.PEM))
        os.chmod(key_path, 0o600)
        os.chmod(cert_path, 0o644)

    def _purge_leaf_certs(self):
        """清空所有缓存的叶证书文件（根证书重新生成时调用）。

        旧叶证书由旧根的私钥签发，与新根的公钥不匹配。如果不清空，get_cert
        会继续返回旧叶证书，客户端收到后无法用信任库中的新根验证签名，
        发出 certificate_unknown alert（46），导致所有 host TLS 握手失败。

        本方法在 _ensure_root_cert 重新生成根证书前调用（启动时，无并发）。
        """
        try:
            for name in os.listdir(self.cert_dir):
                # 跳过根证书本身及其 DER 变体
                if name in ("telnix_root.crt", "telnix_root.key",
                            "telnix_root.crt.der"):
                    continue
                if name.endswith(".crt") or name.endswith(".key") \
                        or name.endswith(".crt.der"):
                    try:
                        os.remove(os.path.join(self.cert_dir, name))
                    except OSError:
                        pass
        except OSError:
            pass
        # 清空内存中的叶证书缓存（启动时为空，防御性调用）
        self._cert_cache.clear()

    def _needs_resign(self, cert_path: str) -> bool:
        """检查磁盘上的叶证书是否需要重新签发。

        需要重新签发的情况：
        1. 叶证书签名无法用当前根公钥验证（根证书已重新生成，旧叶证书 stale）
        2. 证书文件不包含完整链（旧格式只含 leaf，缺少 root 证书链）

        返回 True 表示需要重新签发，False 表示可继续使用缓存。
        """
        try:
            with open(cert_path, "rb") as f:
                data = f.read()
            # 解析第一张证书（leaf）
            leaf = x509.load_pem_x509_certificate(data)
            # 用当前根公钥验证叶证书签名
            root_pubkey = self._root_cert.public_key()
            if isinstance(root_pubkey, rsa.RSAPublicKey):
                from cryptography.hazmat.primitives.asymmetric import padding
                root_pubkey.verify(
                    leaf.signature,
                    leaf.tbs_certificate_bytes,
                    padding.PKCS1v15(),
                    leaf.signature_hash_algorithm,
                )
            else:
                # 非 RSA 根（理论上不会出现），保守起见重新签发
                return True
            # 检查证书文件是否包含完整链（leaf + root）
            # 旧格式只含 1 张证书，需要重新签发以写入完整链
            if data.count(b"-----BEGIN CERTIFICATE-----") < 2:
                return True
            return False
        except Exception:
            # 任何解析/验证异常都视为需要重新签发
            return True

    # ---------- 证书库（跨平台） ----------

    def is_root_cert_installed(self) -> bool:
        """检查根证书是否已安装到系统信任库（按指纹精确匹配）。"""
        if IS_WINDOWS:
            return self._verify_root_cert_in_store_by_thumbprint()
        if IS_MACOS:
            try:
                r = subprocess.run(
                    ["security", "find-certificate", "-c", ROOT_SUBJECT_CN],
                    capture_output=True, text=True, timeout=10,
                )
                return r.returncode == 0
            except Exception:  # noqa: BLE001
                return False
        return False

    def _verify_root_cert_in_store_by_thumbprint(self) -> bool:
        """用 SHA1 指纹精确验证根证书是否在 Local Machine 或 Current User Root store 中。"""
        if not IS_WINDOWS:
            return False
        thumbprint = self.root_thumbprint().lower()
        # certutil 输出的指纹通常带空格（如 "a1 b2 c3 d4 ..."），
        # 需要去掉空格后再与本地计算的指纹（无空格）比较，
        # 否则 "a1b2c3d4" in "a1 b2 c3 d4" 永远为 False，导致验证误报未安装。
        for cmd in (["certutil", "-store", "Root", thumbprint],
                    ["certutil", "-user", "-store", "Root", thumbprint]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=10, encoding="utf-8", errors="ignore")
                if r.returncode != 0:
                    continue
                # 去空格比较，兼容 certutil 不同版本的输出格式
                stdout_nospace = (r.stdout or "").lower().replace(" ", "")
                if thumbprint in stdout_nospace:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def root_thumbprint(self) -> str:
        """计算根证书 SHA1 指纹（大写十六进制，无分隔）。"""
        return self._root_cert.fingerprint(hashes.SHA1()).hex().upper()

    def install_root_cert(self) -> dict:
        """安装根证书到系统信任库。"""
        if IS_WINDOWS:
            # 把 PEM 转 DER 临时文件（certutil 对 DER 兼容性更好）
            der_path = self.root_cert_path + ".der"
            try:
                der_bytes = self._root_cert.public_bytes(serialization.Encoding.DER)
                with open(der_path, "wb") as f:
                    f.write(der_bytes)
                os.chmod(der_path, 0o644)
                install_path = der_path
            except Exception:
                install_path = self.root_cert_path
            result = _run_elevated("certutil.exe",
                                   f'-addstore -f Root "{install_path}"')
            exit_code = result.get("exit_code", -1)
            error = result.get("error", "")
            if exit_code != 0:
                return {"installed": False, "exit_code": exit_code,
                        "error": error or f"certutil 退出码 {exit_code}"}
            # 关键：主动复核（按指纹），避免 certutil exit 0 但实际未安装
            time.sleep(0.5)
            if not self._verify_root_cert_in_store_by_thumbprint():
                return {"installed": False, "exit_code": 0,
                        "error": "certutil 返回成功但未在信任库中找到对应指纹的证书"}
            return {"installed": True, "exit_code": 0}
        if IS_MACOS:
            try:
                r = subprocess.run(
                    ["security", "add-trusted-cert", "-d",
                     "-r", "trustRoot",
                     "-k", "/Library/Keychains/System.keychain",
                     self.root_cert_path],
                    capture_output=True, text=True, timeout=60,
                )
                return {"installed": r.returncode == 0,
                        "exit_code": r.returncode,
                        "stderr": r.stderr[-400:] if r.stderr else ""}
            except Exception as e:  # noqa: BLE001
                return {"installed": False, "exit_code": -1,
                        "error": f"macOS 证书安装失败: {e}"}
        return {
            "installed": False,
            "exit_code": -1,
            "hint": ("Linux 上需手动安装根证书。Debian/Ubuntu: "
                     f"sudo cp '{self.root_cert_path}' /usr/local/share/ca-certificates/telnix_root.crt && "
                     "sudo update-ca-certificates。"
                     "RHEL/CentOS: sudo trust anchor '{self.root_cert_path}'"),
        }

    def remove_root_cert(self) -> dict:
        """从系统信任库移除根证书。"""
        if IS_WINDOWS:
            result = _run_elevated("certutil.exe",
                                   f'-delstore Root {self.root_thumbprint()}')
            exit_code = result.get("exit_code", -1)
            return {"removed": exit_code == 0, "exit_code": exit_code,
                    "error": result.get("error", "")}
        if IS_MACOS:
            try:
                r = subprocess.run(
                    ["security", "delete-certificate", "-Z", self.root_thumbprint()],
                    capture_output=True, text=True, timeout=30,
                )
                return {"removed": r.returncode == 0,
                        "exit_code": r.returncode}
            except Exception as e:  # noqa: BLE001
                return {"removed": False, "exit_code": -1,
                        "error": f"macOS 证书移除失败: {e}"}
        return {
            "removed": False,
            "exit_code": -1,
            "hint": "Linux 上需手动移除根证书（删除 /usr/local/share/ca-certificates/telnix_root.crt 后运行 update-ca-certificates）",
        }


# ---------- 提权运行 certutil（Windows 专属） ----------

# Windows 专属模块条件导入：ctypes/wintypes 仅 Windows 可用
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0


    class _SHELLEXECUTEINFO(ctypes.Structure):  # type: ignore[name-defined]
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", ctypes.c_ulong),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]


def _run_elevated(exe: str, args: str) -> dict:
    """以管理员权限运行 exe 并等待完成，返回 dict。

    返回:
      - exit_code: 进程退出码（-1 启动失败，-2 超时，-3 等待失败）
      - error: 错误描述（成功时为空）
    仅 Windows 调用。
    """
    if not IS_WINDOWS:
        return {"exit_code": -1, "error": "非 Windows 平台"}
    sei = _SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFO)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = args
    sei.nShow = SW_HIDE
    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok:
        err_code = ctypes.windll.kernel32.GetLastError()
        if err_code == 1223:
            return {"exit_code": -1, "error": "用户取消了 UAC 提权"}
        return {"exit_code": -1, "error": f"ShellExecuteExW 失败（GetLastError={err_code}）"}
    if not sei.hProcess:
        return {"exit_code": 0, "error": ""}
    wait_result = ctypes.windll.kernel32.WaitForSingleObject(
        sei.hProcess, _WAIT_UAC_TIMEOUT_MS)
    exit_code = ctypes.c_ulong()
    if wait_result == 0x102:  # WAIT_TIMEOUT
        ctypes.windll.kernel32.TerminateProcess(sei.hProcess, 1)
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return {"exit_code": -2, "error": f"UAC 等待超时（{_WAIT_UAC_TIMEOUT_MS}ms）"}
    elif wait_result == 0xFFFFFFFF:  # WAIT_FAILED
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return {"exit_code": -3, "error": "WaitForSingleObject 失败"}
    else:
        ctypes.windll.kernel32.GetExitCodeProcess(
            sei.hProcess, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return {"exit_code": int(exit_code.value), "error": ""}
