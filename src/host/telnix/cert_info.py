"""TLS 证书信息解析模块。

在 HTTPS 转发时获取对端证书 DER，解析为结构化信息存入 flow。
用 cryptography 库解析（已是项目依赖）。

解析字段：
- subject: 主题（CN/O/OU/C 等）
- issuer: 颁发者
- not_before: 生效时间
- not_after: 过期时间
- san: Subject Alternative Names（域名列表）
- serial_number: 序列号
- fingerprint_sha256: SHA256 指纹
"""
from __future__ import annotations

import datetime
from typing import Optional


def get_cert_info(cert_der: bytes) -> Optional[dict]:
    """解析 DER 编码的证书，返回结构化信息。

    参数：
        cert_der: DER 编码的证书字节（从 sock.getpeercert(binary_form=True) 获取）

    返回：
        dict 或 None（解析失败返回 None）
        {
            "subject": "CN=example.com, O=Org",
            "issuer": "CN=DigiCert, O=DigiCert Inc",
            "not_before": "2024-01-01T00:00:00",
            "not_after": "2025-01-01T00:00:00",
            "san": ["example.com", "www.example.com"],
            "serial_number": "0x1234567890",
            "fingerprint_sha256": "AB:CD:...",
            "is_expired": False,
            "days_remaining": 30,
        }
    """
    if not cert_der:
        return None
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509.oid import NameOID, ExtensionOID
    except ImportError:
        return None

    try:
        cert = x509.load_der_x509_certificate(cert_der)
    except Exception:  # noqa: BLE001
        return None

    def _format_name(name) -> str:
        """格式化 X509 Name 为 'CN=xxx, O=yyy' 形式。"""
        try:
            parts = []
            for attr in name:
                oid = attr.oid
                try:
                    friendly = oid._name
                except Exception:  # noqa: BLE001
                    friendly = str(oid)
                parts.append(f"{friendly}={attr.value}")
            return ", ".join(parts) if parts else ""
        except Exception:  # noqa: BLE001
            return str(name)

    # 主题/颁发者
    subject_str = _format_name(cert.subject)
    issuer_str = _format_name(cert.issuer)

    # 有效期（cryptography 42+ 推荐 _utc 变体，旧版回退到 naïve datetime）
    try:
        not_before = cert.not_valid_before_utc
        not_after = cert.not_valid_after_utc
    except AttributeError:
        not_before = cert.not_valid_before
        not_after = cert.not_valid_after

    # SAN（Subject Alternative Names）
    san_list = []
    try:
        san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        san = san_ext.value
        san_list = san.get_values_for_type(x509.DNSName)
    except Exception:  # noqa: BLE001
        pass

    # 指纹
    try:
        fingerprint = cert.fingerprint(hashes.SHA256())
        fingerprint_hex = ":".join(f"{b:02X}" for b in fingerprint)
    except Exception:  # noqa: BLE001
        fingerprint_hex = ""

    # 序列号
    try:
        serial = format(cert.serial_number, "x")
        if len(serial) % 2:
            serial = "0" + serial
        serial = "0x" + serial.upper()
    except Exception:  # noqa: BLE001
        serial = ""

    # 过期检查
    # not_after 可能是 offset-aware（_utc 变体）或 offset-naive（旧版），分别处理
    if not_after.tzinfo is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
    else:
        now = datetime.datetime.utcnow()
    is_expired = not_after < now
    days_remaining = (not_after - now).days if not is_expired else 0

    def _fmt_dt(dt: datetime.datetime) -> str:
        try:
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:  # noqa: BLE001
            return str(dt)

    return {
        "subject": subject_str,
        "issuer": issuer_str,
        "not_before": _fmt_dt(not_before),
        "not_after": _fmt_dt(not_after),
        "san": san_list,
        "serial_number": serial,
        "fingerprint_sha256": fingerprint_hex,
        "is_expired": is_expired,
        "days_remaining": days_remaining,
    }
