"""SSL bump：根证书管理 + 动态证书签发。

用 cryptography 库生成自签根证书，并为每个域名动态签发叶证书（用根证书签）。
证书缓存避免重复签发。安装/检查/移除根证书走 certutil（安装需 UAC）。
"""

import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT_SUBJECT_CN = "OpenNet Root CA"
ROOT_ORG = "OpenNet"


class SSLBumpManager:
    """SSL bump：根证书管理 + 动态证书签发。"""

    def __init__(self, cert_dir: str):
        self.cert_dir = cert_dir
        self.root_key_path = os.path.join(cert_dir, "opennet_root.key")
        self.root_cert_path = os.path.join(cert_dir, "opennet_root.crt")
        self._cert_cache: dict[str, tuple[str, str]] = {}  # host -> (cert_path, key_path)
        self._root_key = None
        self._root_cert = None
        self._lock = threading.Lock()
        self._ensure_root_cert()

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

        性能优化：双检锁 — 锁内查缓存、锁内签发，避免并发首请求重复 RSA keygen（50-100ms）
        + 避免多线程同时写同一对文件导致损坏。
        """
        # 去掉端口
        if ":" in host:
            host = host.split(":")[0]
        # 第一次无锁检查（命中则零开销）
        cached = self._cert_cache.get(host)
        if cached is not None:
            return cached
        # 双检锁：锁内再查一次 + 锁内签发
        with self._lock:
            cached = self._cert_cache.get(host)
            if cached is not None:
                return cached
            cert_path = os.path.join(self.cert_dir, f"{host}.crt")
            key_path = os.path.join(self.cert_dir, f"{host}.key")
            # 锁内签发（保证只有一个线程做 RSA keygen + 写文件）
            if not (os.path.exists(cert_path) and os.path.exists(key_path)):
                self._sign_host_cert(host, cert_path, key_path)
            self._cert_cache[host] = (cert_path, key_path)
            return cert_path, key_path

    def _sign_host_cert(self, host: str, cert_path: str, key_path: str):
        """用根证书为 host 签发叶证书。"""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
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
                    key_encipherment=True, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False),
                critical=True,
            )
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

    # ---------- Windows 证书库 ----------

    def is_root_cert_installed(self) -> bool:
        """检查根证书是否已安装到 Windows 信任库（当前用户或本机）。"""
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

    def root_thumbprint(self) -> str:
        """计算根证书 SHA1 指纹（大写十六进制，无分隔）。"""
        return self._root_cert.fingerprint(hashes.SHA1()).hex().upper()

    def install_root_cert(self) -> dict:
        """安装根证书到本机 Root 信任库（需 UAC，会弹窗）。"""
        # certutil -addstore Root 默认安装到本机，需要管理员权限
        code = _run_elevated("certutil.exe",
                             f'-addstore Root "{self.root_cert_path}"')
        return {"installed": code == 0, "exit_code": code}

    def remove_root_cert(self) -> dict:
        """从本机 Root 信任库移除根证书（需 UAC）。"""
        code = _run_elevated("certutil.exe",
                             f'-delstore Root {self.root_thumbprint()}')
        return {"removed": code == 0, "exit_code": code}


# ---------- 提权运行 certutil ----------

import ctypes
from ctypes import wintypes

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
WAIT_INFINITE = 0xFFFFFFFF


class _SHELLEXECUTEINFO(ctypes.Structure):
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
    """以管理员权限运行 exe 并等待完成，返回退出码。用户拒绝 UAC 返回 -1。"""
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
