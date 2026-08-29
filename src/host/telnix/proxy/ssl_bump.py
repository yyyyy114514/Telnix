"""SSL bump: root certificate management + dynamic certificate issuance.

Uses the cryptography library to generate a self-signed root certificate and
dynamically issue leaf certificates for each domain (signed by the root).
Certificate caching avoids repeated issuance. Install/check/remove of the root
certificate uses certutil (installation requires UAC elevation).

Cross-platform notes:
- Windows: uses certutil + ShellExecuteEx with elevation to install into the Root trust store.
- macOS: uses `security add-trusted-cert` to install into the keychain (requires user authorization).
- Linux: no automatic installation (different distros use different certificate stores);
  the user is prompted to install manually.
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
    """Strip the trailing port from host, correctly handling IPv6 literals."""
    if host.startswith("["):
        idx = host.find("]")
        if idx != -1:
            return host[1:idx]
        return host
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


def _is_valid_host(host: str) -> bool:
    """Validate host legitimacy (path traversal protection: reject / \\ .. etc.)."""
    if not host or len(host) > 253:
        return False
    # 拒绝包含路径分隔符或 .. 段（防止写入 cert_dir 之外）
    if "/" in host or "\\" in host or ".." in host:
        return False
    return bool(_HOST_RE.match(host))


class SSLBumpManager:
    """SSL bump: root certificate management + dynamic certificate issuance."""

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
        """Get the per-host lock (lazily created, with LRU eviction)."""
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
        """Generate or load the root certificate and key."""
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
        """Get the certificate for the specified domain (cached), returns (cert_path, key_path).

        Performance: per-host lock — same host is mutually exclusive to avoid duplicate issuance,
        different hosts' keygen runs fully in parallel (the original global lock serialized all
        domains, with each keygen taking 50-150ms).

        Security: validates host legitimacy to prevent path traversal writes outside cert_dir.
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
        """Issue a leaf certificate for host signed by the root certificate.

        Performance: leaf certificates use ECDSA P-256 (keygen ~1-5ms),
        rather than RSA 2048 (keygen ~50-150ms).
        The root certificate remains RSA (already installed); when issuing leaf
        certificates the RSA root key is used for signing (cross-algorithm signing is valid).
        When the browser loads 30 new domains, total keygen time drops from 3s to 150ms.
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
        """Clear all cached leaf certificate files (called when the root cert is regenerated).

        Old leaf certificates were signed by the old root's private key and do not
        match the new root's public key. If not cleared, get_cert will keep returning
        old leaf certificates; the client, upon receiving them, cannot verify the
        signature with the new root in the trust store, sending a certificate_unknown
        alert (46) and causing TLS handshake failures for all hosts.

        This method is called in _ensure_root_cert before regenerating the root
        certificate (at startup, no concurrency).
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

    def get_leaf_cert_count(self) -> int:
        """Get the count of cached leaf certificates (in-memory cache size)."""
        return len(self._cert_cache)

    def _needs_resign(self, cert_path: str) -> bool:
        """Check whether the leaf certificate on disk needs to be re-issued.

        Cases requiring re-issuance:
        1. The leaf certificate signature cannot be verified with the current root
           public key (the root cert has been regenerated, old leaf cert is stale).
        2. The certificate file does not contain the full chain (old format only
           contains the leaf, missing the root certificate chain).

        Returns True if re-issuance is required, False to continue using the cache.
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
        """Check whether the root certificate is installed in the system trust store (exact match by thumbprint)."""
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
        """Verify the root certificate is in the Local Machine or Current User Root store by SHA1 thumbprint."""
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
        """Compute the SHA1 thumbprint of the root certificate (uppercase hex, no separator)."""
        return self._root_cert.fingerprint(hashes.SHA1()).hex().upper()

    def install_root_cert(self) -> dict:
        """Install the root certificate into the system trust store."""
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
                        "error": error or f"certutil exit code {exit_code}"}
            # 关键：主动复核（按指纹），避免 certutil exit 0 但实际未安装
            time.sleep(0.5)
            if not self._verify_root_cert_in_store_by_thumbprint():
                return {"installed": False, "exit_code": 0,
                        "error": "certutil returned success but no certificate with the matching thumbprint was found in the trust store"}
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
                        "error": f"macOS certificate installation failed: {e}"}
        return {
            "installed": False,
            "exit_code": -1,
            "hint": ("On Linux the root certificate must be installed manually. Debian/Ubuntu: "
                     f"sudo cp '{self.root_cert_path}' /usr/local/share/ca-certificates/telnix_root.crt && "
                     f"sudo update-ca-certificates. "
                     f"RHEL/CentOS: sudo trust anchor '{self.root_cert_path}'"),
        }

    def regenerate_root_cert(self) -> None:
        """Regenerate the root certificate and key (dangerous operation)."""
        # 清空所有旧叶证书缓存
        self._purge_leaf_certs()
        # 清除内存中的根证书
        self._root_key = None
        self._root_cert = None
        # 删除旧的根证书文件
        try:
            if os.path.exists(self.root_cert_path):
                os.remove(self.root_cert_path)
            if os.path.exists(self.root_key_path):
                os.remove(self.root_key_path)
        except OSError:
            pass
        # 删除 DER 格式的副本
        for ext in ("", ".der"):
            path = self.root_cert_path + ext
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        # 重新生成根证书
        self._ensure_root_cert()

    def remove_root_cert(self) -> dict:
        """Remove the root certificate from the system trust store."""
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
                        "error": f"macOS certificate removal failed: {e}"}
        return {
            "removed": False,
            "exit_code": -1,
            "hint": "On Linux the root certificate must be removed manually (delete /usr/local/share/ca-certificates/telnix_root.crt then run update-ca-certificates)",
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
    """Run exe with administrator privileges and wait for completion; returns a dict.

    Returns:
      - exit_code: process exit code (-1 launch failed, -2 timeout, -3 wait failed)
      - error: error description (empty on success)
    Windows-only.
    """
    if not IS_WINDOWS:
        return {"exit_code": -1, "error": "non-Windows platform"}
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
            return {"exit_code": -1, "error": "user cancelled UAC elevation"}
        return {"exit_code": -1, "error": f"ShellExecuteExW failed (GetLastError={err_code})"}
    if not sei.hProcess:
        return {"exit_code": 0, "error": ""}
    wait_result = ctypes.windll.kernel32.WaitForSingleObject(
        sei.hProcess, _WAIT_UAC_TIMEOUT_MS)
    exit_code = ctypes.c_ulong()
    if wait_result == 0x102:  # WAIT_TIMEOUT
        ctypes.windll.kernel32.TerminateProcess(sei.hProcess, 1)
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return {"exit_code": -2, "error": f"UAC wait timeout ({_WAIT_UAC_TIMEOUT_MS}ms)"}
    elif wait_result == 0xFFFFFFFF:  # WAIT_FAILED
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return {"exit_code": -3, "error": "WaitForSingleObject failed"}
    else:
        ctypes.windll.kernel32.GetExitCodeProcess(
            sei.hProcess, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return {"exit_code": int(exit_code.value), "error": ""}
