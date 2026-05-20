"""
TeslaUSB Neo Web - 配置管理模块

统一配置读取/写入，支持密码加密、配置验证、热重载。

Features:
    - JSON 配置文件管理
    - Fernet 对称加密（NAS 密码）
    - 配置验证与默认值
    - 热重载支持
    - 原子写入（防止配置损坏）
"""

import base64
import json
import logging
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# 默认配置常量
DEFAULT_CONFIG_PATH = "/data/teslausb-web.json"
DEFAULT_ENCRYPTION_KEY_PATH = "/data/.teslausb-key"

DEFAULT_HOME_GRACE_PERIOD = 1800  # 30分钟
DEFAULT_DELETE_DELAY = 1800  # 30分钟
DEFAULT_MAX_RETRIES = 10
DEFAULT_RETRY_INTERVAL = 30  # 30秒
DEFAULT_POLL_INTERVAL = 60  # 60秒


class ConfigError(Exception):
    """配置异常基类"""
    pass


class ConfigNotFoundError(ConfigError):
    """配置文件不存在"""
    pass


class ConfigValidationError(ConfigError):
    """配置验证失败"""
    pass


class EncryptionError(ConfigError):
    """加密/解密失败"""
    pass


@dataclass
class LocationConfig:
    """位置检测配置"""
    teslamate_url: str = "http://100.111.252.121:7777/"
    home_location: str = "家"
    home_wifi_ssids: list[str] = field(default_factory=list)
    hotspot_ssids: list[str] = field(default_factory=list)
    wifi_interface: str = "wlan0"
    poll_interval: int = DEFAULT_POLL_INTERVAL
    teslamate_password_encrypted: str = ""  # TeslaMate 登录密码（加密存储）


@dataclass
class UploadConfig:
    """上传配置"""
    home_grace_period_seconds: int = DEFAULT_HOME_GRACE_PERIOD
    delete_delay_seconds: int = DEFAULT_DELETE_DELAY
    auto_upload_at_home: bool = True
    require_confirmation_away: bool = True
    confirmation_timeout_hours: int = 24
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_interval_seconds: int = DEFAULT_RETRY_INTERVAL


@dataclass
class NasConfig:
    """NAS 配置"""
    type: str = "smb"  # smb, nfs, etc.
    host: str = ""
    share: str = "TeslaUSB"
    username: str = ""
    password_encrypted: str = ""  # 加密存储
    mount_point: str = "/mnt/nas"


@dataclass
class WechatConfig:
    """微信机器人配置"""
    sentry_bot_key: str = ""  # 哨兵事件推送
    status_bot_key: str = ""  # 状态通知


@dataclass
class WatchdogConfig:
    """看门狗配置"""
    enabled: bool = True
    timeout_seconds: int = 60


@dataclass
class SecurityConfig:
    """安全配置"""
    https_enabled: bool = False
    password_encryption_key: str = ""  # 自动生成的密钥


@dataclass
class TeslaUSBConfig:
    """主配置类"""
    version: str = "1.0"
    location: LocationConfig = field(default_factory=LocationConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    nas: NasConfig = field(default_factory=NasConfig)
    wechat: WechatConfig = field(default_factory=WechatConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


class ConfigEncryption:
    """
    配置加密器 - 使用 Fernet 对称加密
    
    密钥管理策略：
    1. 首次运行时自动生成密钥
    2. 密钥保存在 /data/.teslausb-key（权限 0600）
    3. 配置文件中的密码使用此密钥加密
    """
    
    def __init__(self, key_path: str = DEFAULT_ENCRYPTION_KEY_PATH):
        self.key_path = Path(key_path)
        self._cipher: Optional[Fernet] = None
    
    def _load_or_generate_key(self) -> bytes:
        """加载或生成加密密钥"""
        if self.key_path.exists():
            try:
                key_data = self.key_path.read_bytes()
                # 验证密钥格式
                Fernet(key_data)
                return key_data
            except Exception as e:
                logger.error(f"密钥文件损坏: {e}，将生成新密钥")
        
        # 生成新密钥
        key = Fernet.generate_key()
        
        # 保存密钥（设置权限 0600）
        try:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(key)
            os.chmod(self.key_path, 0o600)
            logger.info(f"已生成新加密密钥: {self.key_path}")
        except Exception as e:
            logger.error(f"保存密钥失败: {e}")
            raise EncryptionError(f"无法保存加密密钥: {e}")
        
        return key
    
    @property
    def cipher(self) -> Fernet:
        """获取加密器实例（懒加载）"""
        if self._cipher is None:
            key = self._load_or_generate_key()
            self._cipher = Fernet(key)
        return self._cipher
    
    def encrypt(self, plaintext: str) -> str:
        """
        加密明文，返回 base64 编码的密文
        
        Args:
            plaintext: 待加密的明文
            
        Returns:
            base64 编码的密文
        """
        try:
            encrypted = self.cipher.encrypt(plaintext.encode("utf-8"))
            return base64.b64encode(encrypted).decode("ascii")
        except Exception as e:
            logger.error(f"加密失败: {e}")
            raise EncryptionError(f"加密失败: {e}")
    
    def decrypt(self, ciphertext: str) -> str:
        """
        解密 base64 编码的密文
        
        Args:
            ciphertext: base64 编码的密文
            
        Returns:
            解密后的明文
        """
        try:
            encrypted = base64.b64decode(ciphertext.encode("ascii"))
            return self.cipher.decrypt(encrypted).decode("utf-8")
        except InvalidToken:
            logger.error("解密失败: 无效的密文或密钥不匹配")
            raise EncryptionError("解密失败: 无效的密文或密钥不匹配")
        except Exception as e:
            logger.error(f"解密失败: {e}")
            raise EncryptionError(f"解密失败: {e}")
    
    def get_key_fingerprint(self) -> str:
        """获取密钥指纹（用于显示）"""
        key = self._load_or_generate_key()
        import hashlib
        return hashlib.sha256(key).hexdigest()[:16]


class ConfigManager:
    """
    配置管理器
    
    提供配置的读取、写入、验证、加密功能。
    支持热重载（通过检查文件修改时间）。
    
    Args:
        config_path: 配置文件路径
        encryption_key_path: 加密密钥路径
    """
    
    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG_PATH,
        encryption_key_path: str = DEFAULT_ENCRYPTION_KEY_PATH,
    ):
        self.config_path = Path(config_path)
        self.encryption = ConfigEncryption(encryption_key_path)
        self._config: Optional[TeslaUSBConfig] = None
        self._last_modified: float = 0
        self._load_config()
    
    def _load_config(self) -> TeslaUSBConfig:
        """从文件加载配置"""
        if not self.config_path.exists():
            logger.info(f"配置文件不存在，使用默认配置: {self.config_path}")
            self._config = TeslaUSBConfig()
            # 保存默认配置
            self.save_config()
            return self._config
        
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            self._config = self._parse_config(data)
            self._last_modified = self.config_path.stat().st_mtime
            logger.info(f"配置加载成功: {self.config_path}")
            return self._config
        except json.JSONDecodeError as e:
            logger.error(f"配置文件 JSON 格式错误: {e}")
            raise ConfigError(f"配置文件格式错误: {e}")
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            raise ConfigError(f"加载配置失败: {e}")
    
    def _parse_config(self, data: dict) -> TeslaUSBConfig:
        """解析配置字典为配置对象"""
        try:
            return TeslaUSBConfig(
                version=data.get("version", "1.0"),
                location=LocationConfig(**data.get("location", {})),
                upload=UploadConfig(**data.get("upload", {})),
                nas=NasConfig(**data.get("nas", {})),
                wechat=WechatConfig(**data.get("wechat", {})),
                watchdog=WatchdogConfig(**data.get("watchdog", {})),
                security=SecurityConfig(**data.get("security", {})),
            )
        except TypeError as e:
            raise ConfigValidationError(f"配置字段错误: {e}")
    
    def _config_to_dict(self, config: TeslaUSBConfig) -> dict:
        """将配置对象转换为字典"""
        return {
            "version": config.version,
            "location": asdict(config.location),
            "upload": asdict(config.upload),
            "nas": asdict(config.nas),
            "wechat": asdict(config.wechat),
            "watchdog": asdict(config.watchdog),
            "security": asdict(config.security),
        }
    
    def reload_if_changed(self) -> bool:
        """
        如果配置文件有变化则重新加载
        
        Returns:
            是否重新加载
        """
        if not self.config_path.exists():
            return False
        
        current_mtime = self.config_path.stat().st_mtime
        if current_mtime > self._last_modified:
            logger.info("配置文件已修改，重新加载")
            self._load_config()
            return True
        return False
    
    def get_config(self) -> TeslaUSBConfig:
        """获取当前配置（自动检查更新）"""
        self.reload_if_changed()
        return self._config
    
    def save_config(self, config: Optional[TeslaUSBConfig] = None) -> None:
        """
        保存配置到文件（原子写入）
        
        Args:
            config: 要保存的配置，None 则保存当前配置
        """
        if config is None:
            config = self._config
        
        data = self._config_to_dict(config)
        
        # 原子写入：先写入临时文件，再重命名
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".json",
                dir=self.config_path.parent,
                delete=False,
            ) as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                temp_path = f.name
            
            # 设置权限（只有所有者读写）
            os.chmod(temp_path, 0o600)
            
            # 原子重命名
            shutil.move(temp_path, self.config_path)
            
            self._last_modified = self.config_path.stat().st_mtime
            logger.info(f"配置已保存: {self.config_path}")
            
        except Exception as e:
            # 清理临时文件
            if "temp_path" in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            logger.error(f"保存配置失败: {e}")
            raise ConfigError(f"保存配置失败: {e}")
    
    def encrypt_password(self, password: str) -> str:
        """加密密码"""
        return self.encryption.encrypt(password)
    
    def decrypt_password(self, encrypted: str) -> str:
        """解密密码"""
        if not encrypted:
            return ""
        return self.encryption.decrypt(encrypted)
    
    def update_nas_password(self, password: str) -> None:
        """
        更新 NAS 密码（自动加密）
        
        Args:
            password: 明文密码
        """
        config = self.get_config()
        if password:
            config.nas.password_encrypted = self.encrypt_password(password)
        else:
            config.nas.password_encrypted = ""
        self.save_config()
        logger.info("NAS 密码已更新")
    
    def get_nas_password(self) -> str:
        """获取 NAS 密码（自动解密）"""
        config = self.get_config()
        if config.nas.password_encrypted:
            return self.decrypt_password(config.nas.password_encrypted)
        return ""
    
    def update_teslamate_password(self, password: str) -> None:
        """
        更新 TeslaMate 密码（自动加密）
        
        Args:
            password: 明文密码
        """
        config = self.get_config()
        if password:
            config.location.teslamate_password_encrypted = self.encrypt_password(password)
        else:
            config.location.teslamate_password_encrypted = ""
        self.save_config()
        logger.info("TeslaMate 密码已更新")
    
    def get_teslamate_password(self) -> str:
        """获取 TeslaMate 密码（自动解密）"""
        config = self.get_config()
        if config.location.teslamate_password_encrypted:
            return self.decrypt_password(config.location.teslamate_password_encrypted)
        return ""
    
    def validate_config(self) -> list[str]:
        """
        验证配置有效性
        
        Returns:
            错误信息列表，空列表表示验证通过
        """
        errors = []
        config = self.get_config()
        
        # 验证 NAS 配置
        if config.nas.type not in ["smb", "nfs"]:
            errors.append(f"NAS 类型无效: {config.nas.type}")
        if not config.nas.host:
            errors.append("NAS 主机地址不能为空")
        if not config.nas.share:
            errors.append("NAS 共享名不能为空")
        
        # 验证上传配置
        if config.upload.max_retries < 0:
            errors.append("重试次数不能为负数")
        if config.upload.retry_interval_seconds < 1:
            errors.append("重试间隔至少 1 秒")
        
        # 验证位置配置
        if not config.location.teslamate_url:
            errors.append("TeslaMate URL 不能为空")
        if not config.location.home_wifi_ssids:
            errors.append("家庭 WiFi SSID 列表不能为空")
        
        return errors


# 单例实例
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """获取全局配置管理器实例"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


def init_config_manager(
    config_path: str = DEFAULT_CONFIG_PATH,
    encryption_key_path: str = DEFAULT_ENCRYPTION_KEY_PATH,
) -> ConfigManager:
    """初始化全局配置管理器"""
    global _config_manager
    _config_manager = ConfigManager(config_path, encryption_key_path)
    return _config_manager


# -----------------------------------------
# 配置兼容层 — 统一读取微信 Webhook Key
# -----------------------------------------

def get_wecom_keys() -> dict:
    """
    统一读取企业微信机器人配置，优先从 sentry.json 读取（最新配置），
    回退到 config_manager 的 WechatConfig。

    Returns:
        {
            "status_key": str,  # 状态通知机器人
            "sentry_key": str,  # 哨兵事件机器人
        }
    """
    import json
    import os

    SENTRY_JSON = "/opt/teslausb-web/config/sentry.json"

    status_key = ""
    sentry_key = ""

    # 优先从 sentry.json 读取
    if os.path.exists(SENTRY_JSON):
        try:
            with open(SENTRY_JSON, encoding="utf-8") as f:
                cfg = json.load(f)
            status_key = cfg.get("wecom_status_webhook_key") or cfg.get("wecom_webhook_key", "")
            sentry_key = cfg.get("wecom_sentry_webhook_key", "")
        except Exception:
            pass

    # 回退到 config_manager
    if not status_key or not sentry_key:
        try:
            mgr = get_config_manager()
            wechat = mgr.get_config().wechat
            if not status_key:
                status_key = wechat.status_bot_key
            if not sentry_key:
                sentry_key = wechat.sentry_bot_key
        except Exception:
            pass

    return {"status_key": status_key, "sentry_key": sentry_key}
