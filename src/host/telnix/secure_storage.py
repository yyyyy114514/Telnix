"""敏感信息加密存储工具。

使用 Fernet 对称加密保护 settings.json 中的敏感凭据（API keys, secrets 等）。
首次使用时自动生成密钥，后续使用同一密钥进行加密/解密。
密钥文件存储在 data_dir/.secret_key，仅所有者可读写。
"""
import os
import base64
import threading

# 延迟导入避免循环依赖
_FERNET = None
_LOCK = threading.Lock()


def _get_fernet():
    """获取 Fernet 实例（线程安全，单例）。"""
    global _FERNET
    if _FERNET is None:
        with _LOCK:
            if _FERNET is None:
                from cryptography.fernet import Fernet
                # 获取或创建密钥
                key_path = _get_key_path()
                if os.path.exists(key_path):
                    try:
                        with open(key_path, "rb") as f:
                            key = f.read()
                    except Exception:  # noqa: BLE001
                        key = Fernet.generate_key()
                else:
                    key = Fernet.generate_key()
                    # 确保目录存在
                    os.makedirs(os.path.dirname(key_path), exist_ok=True)
                    try:
                        with open(key_path, "wb") as f:
                            f.write(key)
                        os.chmod(key_path, 0o600)
                    except Exception:  # noqa: BLE001
                        pass
                _FERNET = Fernet(key)
    return _FERNET


def _get_key_path() -> str:
    """获取加密密钥文件路径。"""
    # 使用硬编码路径避免循环导入
    home = os.path.expanduser("~")
    data_dir = os.path.join(home, ".telnix")
    return os.path.join(data_dir, ".secret_key")


def encrypt(plaintext: str) -> str:
    """加密明文字符串，返回 base64 编码的密文（可存储在 JSON 中）。"""
    if not plaintext:
        return ""
    try:
        f = _get_fernet()
        ciphertext = f.encrypt(plaintext.encode("utf-8"))
        return base64.b64encode(ciphertext).decode("ascii")
    except Exception:  # noqa: BLE001
        # 加密失败时返回原值（兼容旧数据）
        return plaintext


def decrypt(ciphertext: str) -> str:
    """解密 base64 编码的密文，返回明文。"""
    if not ciphertext:
        return ""
    try:
        f = _get_fernet()
        decoded = base64.b64decode(ciphertext.encode("ascii"))
        return f.decrypt(decoded).decode("utf-8")
    except Exception:  # noqa: BLE001
        # 解密失败时返回原值（可能是旧明文数据）
        return ciphertext


def is_encrypted(value: str) -> bool:
    """判断值是否为加密格式（Base64 编码的 Fernet 密文）。"""
    if not value:
        return False
    try:
        decoded = base64.b64decode(value.encode("ascii"))
        # Fernet 密文长度至少 40 字节（token 版本(1) + IV(16) + HMAC(32) + 至少1字节内容）
        return len(decoded) >= 40
    except Exception:  # noqa: BLE001
        return False
