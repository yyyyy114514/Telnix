"""TLS certificate info parser module.

Obtains the peer certificate DER during HTTPS forwarding and parses it into
structured info stored in the flow. Uses the cryptography library (already a
project dependency).

Parsed fields:
- subject: subject (CN/O/OU/C etc.)
- issuer: issuer
- not_before: validity start time
- not_after: expiration time
- san: Subject Alternative Names (domain list)
- serial_number: serial number
- fingerprint_sha256: SHA256 fingerprint
"""
from __future__ import annotations

import datetime
from typing import Optional


def get_cert_info(cert_der: bytes) -> Optional[dict]:
    """Parse a DER-encoded certificate and return structured info.

    Parameters:
        cert_der: DER-encoded certificate bytes (obtained from sock.getpeercert(binary_form=True))

    Returns:
        dict or None (returns None on parse failure)
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
        """Format an X509 Name into the 'CN=xxx, O=yyy' form."""
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
