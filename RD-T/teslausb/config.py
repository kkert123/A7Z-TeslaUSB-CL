"""
TeslaUSB Neo Web - 全局配置常量
"""

import os

# ===== 密钥管理 =====
def _load_secret_key() -> str:
    """
    加载 Flask SECRET_KEY，优先级：
    1. /data/.secret_key 文件（持久化存储，最安全）
    2. 环境变量 TESLAUSB_SECRET_KEY（仅初始化时使用，不长期依赖）
    3. 自动生成并保存（首次运行）

    注意：环境变量优先级故意低于文件，原因：
    - /proc/<pid>/environ 可能暴露环境变量内容
    - 文件可以设置 600 权限，更安全
    - 文件跨重启持久，不依赖环境变量注入
    """
    import secrets as _secrets

    key_file = "/data/.secret_key"

    # 1. 优先读取密钥文件
    try:
        if os.path.exists(key_file):
            with open(key_file, "r") as f:
                key = f.read().strip()
                if key:
                    return key
    except Exception:
        pass

    # 2. 文件不存在时，检查环境变量（用于首次初始化）
    env_key = os.environ.get("TESLAUSB_SECRET_KEY", "").strip()
    key = env_key if env_key else _secrets.token_hex(32)

    # 3. 将密钥写入文件持久化（写入成功后不再依赖环境变量）
    try:
        os.makedirs(os.path.dirname(key_file) or "/data", exist_ok=True)
        with open(key_file, "w") as f:
            f.write(key)
        os.chmod(key_file, 0o600)  # 仅所有者可读写
    except Exception:
        pass  # 写失败也没关系，本次运行用生成的 key

    return key


# ===== 路径配置 =====
TESLAUSB_TOML = "/data/teslausb.toml"
WIFI_STATUS_FILE = "/tmp/teslausb_wifi_status.json"
LOG_FILE = "/data/logs/teslausb.log"

# ===== 分区挂载点 =====
PARTITIONS = {
    "cam": "/media/cnlvan/cam",
    "music": "/media/cnlvan/music",
    "lightshow": "/media/cnlvan/lightshow",
    "boombox": "/media/cnlvan/boombox",
    "data": "/data",
}

# ===== Web 服务配置 =====
WEB_HOST = "0.0.0.0"
WEB_PORT = 5000
SECRET_KEY = _load_secret_key()

# ===== 鉴权配置（AUTH_ENABLED=False 时跳过鉴权）=====
AUTH_ENABLED = False          # 预留，当前关闭
AUTH_USERNAME = "admin"
AUTH_PASSWORD = "teslausb"   # 启用时请修改

# ===== systemd 服务名 =====
TESLAUSB_SERVICE = "teslausb"

# ===== WiFi 接口 =====
WIFI_INTERFACE = "wlan0"
