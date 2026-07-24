"""SSL bump：根证书管理 + 动态证书签发。

用 cryptography 库生成自签根证书，并为每个域名动态签发叶证书（用根证书签）。
证书缓存避免重复签发。安装/检查/移除根证书走 certutil（安装需 UAC）。

跨平台说明：
- Windows：用 certutil + ShellExecuteEx 提权安装到 Root 信任库。
- macOS：用 security add-trusted-cert 安装到钥匙串（需用户授权）。
- Linux：不自动安装（不同发行版证书库不同），提示用户手动安装。
"""

import os
import subprocess
import sys
import threading
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


class SSLBumpManager:
    """SSL bump：根证书管理 + 动态证书签发。"""

    def __init__(self, cert_dir: str):
        self.cert_dir = cert_dir
        self.root_key_path = os.path.join(cert_dir, "telnix_root.key")
        self.root_cert_path = os.path.join(cert_dir, "telnix_root.crt")
        self._cert_cache: dict[str, tuple[str, str]] = {}  # host -> (cert_path, key_path)
        self._root_key = None
        self._root_cert = None
        # 性能优化：每域名独立锁，不同 host 的首次签发可并行 keygen
        # （原全局锁会把多域名首次访问串行化，每个 keygen 50-150ms）
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_locks_lock = threading.Lock()
        self._ensure_root_cert()

    def _get_host_lock(self, host: str) -> threading.Lock:
        """获取指定 host 的独立锁（懒创建）。"""
        with self._host_locks_lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    # ---------- 根证书 ----------

    def _ensure_root_cert(self):
        """生成或加载根证书与密钥。"""
        if os.path.exists(self.root_cert_path) and os.path.exists(self.root_key_path):
            self._load_root()
            return
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
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        with open(self.root_key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(self.root_cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
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
        """
        # 去掉端口
        if ":" in host:
            host = host.split(":")[0]
        # 第一次无锁检查（命中则零开销）
        cached = self._cert_cache.get(host)
        if cached is not None:
            return cached
        # 每域名独立锁：锁内双检 + 签发
        with self._get_host_lock(host):
            cached = self._cert_cache.get(host)
            if cached is not None:
                return cached
            cert_path = os.path.join(self.cert_dir, f"{host}.crt")
            key_path = os.path.join(self.cert_dir, f"{host}.key")
            # 锁内签发（保证同 host 只有一个线程做 RSA keygen + 写文件）
            if not (os.path.exists(cert_path) and os.path.exists(key_path)):
                self._sign_host_cert(host, cert_path, key_path)
            self._cert_cache[host] = (cert_path, key_path)
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
            .sign(self._root_key, hashes.SHA256())
        )
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

    # ---------- 证书库（跨平台） ----------

    def is_root_cert_installed(self) -> bool:
        """检查根证书是否已安装到系统信任库。

        Windows：用 certutil 查 Root 信任库。
        macOS：用 security find-certificate 查钥匙串。
        Linux：检查 /etc/ssl/certs 是否有对应证书（简化判断）。
        """
        if IS_WINDOWS:
            for cmd in (["certutil", "-store", "Root"],
                        ["certutil", "-user", "-store", "Root"]):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True,
                                       timeout=10, encoding="utf-8", errors="ignore")
                except Exception:  # noqa: BLE001
                    continue
                if r.stdout and ROOT_SUBJECT_CN in r.stdout:
                    return True
            return False
        if IS_MACOS:
            # macOS：用 security find-certificate 查钥匙串
            try:
                r = subprocess.run(
                    ["security", "find-certificate", "-c", ROOT_SUBJECT_CN],
                    capture_output=True, text=True, timeout=10,
                )
                return r.returncode == 0
            except Exception:  # noqa: BLE001
                return False
        # Linux：检查 CA 捆绑目录（不精确，仅作存在性提示）
        # 真实环境需要 update-ca-certificates，这里返回 False 让用户手动安装
        return False

    def root_thumbprint(self) -> str:
        """计算根证书 SHA1 指纹（大写十六进制，无分隔）。"""
        return self._root_cert.fingerprint(hashes.SHA1()).hex().upper()

    def install_root_cert(self) -> dict:
        """安装根证书到系统信任库。

        Windows：certutil -addstore Root（需 UAC 提权）。
        macOS：security add-trusted-cert（需用户授权）。
        Linux：提示用户手动安装（不同发行版命令不同）。
        """
        if IS_WINDOWS:
            # certutil -addstore Root 默认安装到本机，需要管理员权限
            code = _run_elevated("certutil.exe",
                                 f'-addstore Root "{self.root_cert_path}"')
            return {"installed": code == 0, "exit_code": code}
        if IS_MACOS:
            # macOS：用 security 命令安装到系统钥匙串并标记为信任
            # 需要用户在弹窗中输入密码授权
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
        # Linux：不自动安装，提示用户手动操作
        return {
            "installed": False,
            "exit_code": -1,
            "hint": ("Linux 上需手动安装根证书。Debian/Ubuntu: "
                     f"sudo cp '{self.root_cert_path}' /usr/local/share/ca-certificates/telnix_root.crt && "
                     "sudo update-ca-certificates。"
                     "RHEL/CentOS: sudo trust anchor '{self.root_cert_path}'"),
        }

    def remove_root_cert(self) -> dict:
        """从系统信任库移除根证书。

        Windows：certutil -delstore Root（需 UAC）。
        macOS：security delete-certificate。
        Linux：提示用户手动移除。
        """
        if IS_WINDOWS:
            code = _run_elevated("certutil.exe",
                                 f'-delstore Root {self.root_thumbprint()}')
            return {"removed": code == 0, "exit_code": code}
        if IS_MACOS:
            # macOS：用 SHA1 哈希值删除
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
        # Linux：提示用户手动移除
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
    WAIT_INFINITE = 0xFFFFFFFF


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


def _run_elevated(exe: str, args: str) -> int:
    """以管理员权限运行 exe 并等待完成，返回退出码。用户拒绝 UAC 返回 -1。

    仅 Windows 调用（macOS/Linux 不会走到这里，install_root_cert 已分流）。
    """
    if not IS_WINDOWS:
        # 非 Windows 不应调用此函数，返回 -1 表示不支持
        return -1
    sei = _SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFO)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = args
    sei.nShow = SW_HIDE
    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok:
        return -1
    if sei.hProcess:
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, WAIT_INFINITE)
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(
            sei.hProcess, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return int(exit_code.value)
    return 0
