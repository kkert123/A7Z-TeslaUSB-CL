"""
TeslaUSB Neo Web - 位置检测模块

从 TeslaMate 页面解析车辆位置，结合 WiFi SSID 验证，提供准确的位置状态判断。

Features:
    - TeslaMate HTML 解析
    - TeslaMate API 认证 (自定义服务)
    - Token 持久化缓存（减少登录推送）
    - WiFi SSID 辅助验证
    - 位置变化回调机制
    - 容错与降级策略
"""

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class LocationState(Enum):
    """位置状态枚举"""
    HOME = "home"
    AWAY = "away"
    UNKNOWN = "unknown"


@dataclass
class LocationInfo:
    """位置信息数据类"""
    state: LocationState
    raw_location: str  # TeslaMate 返回的原始位置文本
    wifi_connected: str
    needs_wifi_switch: bool
    confidence: float  # 置信度 (0-1)
    location_source: str = field(default="")  # 位置来源: teslamate/wifi/unknown
    
    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            "state": self.state.value,
            "raw_location": self.raw_location,
            "wifi_connected": self.wifi_connected,
            "needs_wifi_switch": self.needs_wifi_switch,
            "confidence": self.confidence,
            "location_source": self.location_source,
        }


class LocationDetectionError(Exception):
    """位置检测异常基类"""
    pass


class TeslaMateConnectionError(LocationDetectionError):
    """TeslaMate 连接失败"""
    pass


class WifiDetectionError(LocationDetectionError):
    """WiFi 检测失败"""
    pass


class TeslaMateCustomAPI:
    """
    TeslaMate 自定义服务 API 封装
    
    支持通过 /login 获取 token，然后访问 /msg 获取车辆位置数据。
    Token 支持持久化缓存，减少频繁登录导致的微信推送。
    """
    
    # Token 默认有效期（秒），用于本地缓存判断
    TOKEN_CACHE_DURATION = 3600 * 23  # 23 小时，避免 JWT 24h 过期
    
    def __init__(self, base_url: str, password: str, token_cache_path: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        
        # Token 缓存路径
        if token_cache_path:
            self._token_cache_path = Path(token_cache_path)
        else:
            # 默认保存到 /opt/teslausb-web/data/
            self._token_cache_path = Path("/opt/teslausb-web/data/.teslamate_token.json")
        
        # 尝试从缓存加载 token
        self._load_token_from_cache()
    
    def _get_cache_key(self) -> str:
        """生成缓存 key（基于 URL 和密码哈希）"""
        import hashlib
        key_data = f"{self.base_url}:{self.password}"
        return hashlib.sha256(key_data.encode()).hexdigest()[:16]
    
    def _load_token_from_cache(self) -> bool:
        """
        从本地缓存加载 token
        
        Returns:
            成功加载返回 True
        """
        try:
            if not self._token_cache_path.exists():
                return False
            
            with open(self._token_cache_path, "r") as f:
                cache = json.load(f)
            
            cache_key = self._get_cache_key()
            if cache_key not in cache:
                return False
            
            entry = cache[cache_key]
            expiry = entry.get('expiry', 0)
            
            # 检查 token 是否过期
            if time.time() < expiry:
                self._token = entry.get('token')
                self._token_expiry = expiry
                logger.debug(f"从缓存加载 TeslaMate token，有效期至 {time.ctime(expiry)}")
                return True
            else:
                logger.debug("缓存的 token 已过期")
                return False
                
        except Exception as e:
            logger.debug(f"加载 token 缓存失败: {e}")
            return False
    
    def _save_token_to_cache(self) -> bool:
        """
        保存 token 到本地缓存
        
        Returns:
            成功保存返回 True
        """
        try:
            # 确保目录存在
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 保存原 umask，设置新 umask（只允许 owner 读写）
            original_umask = os.umask(0o077)
            
            cache = {}
            if self._token_cache_path.exists():
                with open(self._token_cache_path, "r") as f:
                    cache = json.load(f)
            
            cache_key = self._get_cache_key()
            cache[cache_key] = {
                "token": self._token,
                "expiry": self._token_expiry,
                "created": time.time(),
            }
            
            try:
                with open(self._token_cache_path, "w") as f:
                    json.dump(cache, f, indent=2)
                
                # 设置文件权限为 600（只有 owner 可读写）
                os.chmod(self._token_cache_path, 0o600)
            finally:
                # 恢复原始 umask
                os.umask(original_umask)
            logger.debug("TeslaMate token 已缓存")
            return True
            
        except Exception as e:
            logger.warning(f"保存 token 缓存失败: {e}")
            return False
    
    def _clear_token_cache(self) -> None:
        """清除当前 token 缓存"""
        try:
            if not self._token_cache_path.exists():
                return
            
            with open(self._token_cache_path, "r") as f:
                cache = json.load(f)
            
            cache_key = self._get_cache_key()
            if cache_key in cache:
                del cache[cache_key]
                with open(self._token_cache_path, "w") as f:
                    json.dump(cache, f, indent=2)
                logger.debug("已清除 token 缓存")
        except Exception as e:
            logger.debug(f"清除 token 缓存失败: {e}")
    
    def login(self, force: bool = False) -> bool:
        """
        登录获取 JWT token
        
        如果缓存中有未过期的 token，会直接使用缓存，除非指定 force=True。
        
        Args:
            force: 强制重新登录，忽略缓存
            
        Returns:
            成功返回 True，失败返回 False
        """
        # 检查缓存的 token 是否仍有效
        if not force and self._token and time.time() < self._token_expiry:
            logger.debug("使用缓存的 token（未过期）")
            return True
        
        try:
            url = f"{self.base_url}/login"
            data = {
                "password": self.password,
                "publicIp": "127.0.0.1"  # 兼容自定义服务
            }
            
            resp = requests.post(url, json=data, timeout=10)
            if resp.status_code == 200:
                result = resp.json()
                self._token = result.get("token")
                if self._token:
                    # 设置 token 过期时间（23 小时后）
                    self._token_expiry = time.time() + self.TOKEN_CACHE_DURATION
                    # 保存到缓存
                    self._save_token_to_cache()
                    if force:
                        logger.info("TeslaMate 重新登录成功")
                    else:
                        logger.info("TeslaMate 登录成功（token 已缓存）")
                    return True
            else:
                logger.warning(f"登录失败: {resp.status_code} - {resp.text[:200]}")
                
        except Exception as e:
            logger.error(f"登录请求失败: {e}")
        
        return False
    
    def get_token(self, force_refresh: bool = False) -> Optional[str]:
        """
        获取有效的 token
        
        Args:
            force_refresh: 强制刷新 token
            
        Returns:
            有效的 token 字符串，失败返回 None
        """
        if force_refresh or not self._token or time.time() >= self._token_expiry:
            if not self.login(force=force_refresh):
                return None
        return self._token
    
    def get_vehicle_data(self) -> Optional[dict]:
        """
        获取车辆实时数据（包含位置）
        
        使用 /msg 端点获取 raw_data，其中包含 ui_current_address。
        自动处理 token 过期和刷新。
        
        Returns:
            raw_data 字典，失败返回 None
        """
        # 获取有效 token（优先使用缓存）
        token = self.get_token()
        if not token:
            return None
        
        try:
            url = f"{self.base_url}/msg"
            headers = {"Authorization": f"Bearer {token}"}
            
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # 返回 raw_data，其中包含位置信息
                return data.get("raw_data")
            elif resp.status_code == 401:
                # Token 过期，清除缓存并重新登录
                logger.warning("Token 无效，清除缓存并重新登录...")
                self._token = None
                self._clear_token_cache()
                # 递归重试一次
                token = self.get_token(force_refresh=True)
                if token:
                    headers = {"Authorization": f"Bearer {token}"}
                    resp = requests.get(url, headers=headers, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        return data.get("raw_data")
            else:
                logger.warning(f"获取车辆数据失败: {resp.status_code}")
                
        except Exception as e:
            logger.error(f"获取车辆数据失败: {e}")
        
        return None
    
    def get_location(self) -> Optional[str]:
        """
        获取车辆当前位置地址
        
        从 /msg 返回的 raw_data 中提取 ui_current_address
        
        Returns:
            位置地址字符串（如"乐清市象阳高园村综合办公楼东南"），失败返回 None
        """
        raw_data = self.get_vehicle_data()
        if not raw_data:
            return None
        
        # ui_current_address 是主要的位置字段
        address = raw_data.get("ui_current_address")
        if address and isinstance(address, str) and address.strip():
            return address.strip()
        
        # 备选：检查 location 字段（通常是 JSON 格式坐标）
        location = raw_data.get("location")
        if location and isinstance(location, dict):
            # 如果有坐标但没有地址，返回坐标
            lat = location.get("latitude")
            lng = location.get("longitude")
            if lat and lng:
                return f"GPS:{lat},{lng}"
        
        return None
    
    def get_states(self) -> Optional[dict]:
        """
        获取服务配置状态（兼容性保留）
        
        Returns:
            状态字典，失败返回 None
        """
        token = self.get_token()
        if not token:
            return None
        
        try:
            url = f"{self.base_url}/states"
            headers = {"Authorization": f"Bearer {token}"}
            
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 401:
                # Token 过期，清除缓存并刷新
                self._token = None
                self._clear_token_cache()
                token = self.get_token(force_refresh=True)
                if token:
                    headers = {"Authorization": f"Bearer {token}"}
                    resp = requests.get(url, headers=headers, timeout=10)
                    if resp.status_code == 200:
                        return resp.json()
            else:
                logger.warning(f"获取状态失败: {resp.status_code}")
                
        except Exception as e:
            logger.error(f"获取状态失败: {e}")
        
        return None
    
    def get_location_from_states(self, states: dict) -> Optional[str]:
        """
        从 states 数据中解析位置信息（兼容性保留）
        
        注意：此服务的 states 端点不包含位置信息，
        请使用 get_location() 方法获取位置。
        
        Returns:
            位置字符串，未找到返回 None
        """
        # states 端点不包含位置信息
        return None


class LocationDetector:
    """
    位置检测器 - 基于 TeslaMate API（主） + WiFi 验证（辅）
    
    优先使用 TeslaMate API 获取实时位置，WiFi SSID 作为辅助验证。
    
    Args:
        teslamate_url: TeslaMate 页面地址
        home_location: 家的位置标识（用于关键词匹配）
        home_wifi_ssids: 家庭 WiFi SSID 列表（辅助验证）
        hotspot_ssids: 热点 SSID 列表（可选）
        wifi_interface: WiFi 接口名（默认 wlan0）
        poll_interval: 轮询间隔（秒，默认 60）
        auth_password: TeslaMate 认证密码（用于自定义服务的 /login）
        use_custom_api: 是否使用自定义 TeslaMate API（默认 True）
        token_cache_path: Token 缓存文件路径（默认 /opt/teslausb-web/data/.teslamate_token.json）
    """
    
    DEFAULT_TIMEOUT = 10  # TeslaMate 请求超时
    WIFI_RETRY_COUNT = 3  # WiFi 检测重试次数
    
    def __init__(
        self,
        teslamate_url: str = "http://100.64.0.11:7777/",
        home_location: str = "家",
        home_wifi_ssids: Optional[list[str]] = None,
        hotspot_ssids: Optional[list[str]] = None,
        wifi_interface: str = "wlan0",
        poll_interval: int = 60,
        auth_password: Optional[str] = None,
        use_custom_api: bool = True,
        token_cache_path: Optional[str] = None,
    ):
        self.teslamate_url = teslamate_url.rstrip("/")
        self.home_location = home_location
        self.home_wifi_ssids = set(home_wifi_ssids or [])
        self.hotspot_ssids = set(hotspot_ssids or [])
        self.wifi_interface = wifi_interface
        self.poll_interval = poll_interval
        self.auth_password = auth_password
        self.use_custom_api = use_custom_api
        self.token_cache_path = token_cache_path
        
        self._session: Optional[requests.Session] = None
        self._custom_api: Optional[TeslaMateCustomAPI] = None
        self._last_location: Optional[LocationInfo] = None
        self._on_location_change: Optional[Callable[[LocationInfo, LocationInfo], None]] = None
        self._on_wifi_switch_needed: Optional[Callable[[str, str], None]] = None
        
        # 初始化自定义 API（如果启用）
        if self.use_custom_api and self.auth_password:
            self._custom_api = TeslaMateCustomAPI(
                self.teslamate_url, 
                self.auth_password,
                token_cache_path=self.token_cache_path
            )
    
    def set_callbacks(
        self,
        on_location_change: Optional[Callable[[LocationInfo, LocationInfo], None]] = None,
        on_wifi_switch_needed: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """
        设置回调函数
        
        Args:
            on_location_change: 位置变化回调 (新位置, 旧位置)
            on_wifi_switch_needed: 需要切换 WiFi 回调 (目标 SSID, 当前 SSID)
        """
        self._on_location_change = on_location_change
        self._on_wifi_switch_needed = on_wifi_switch_needed
    
    def _get_session(self) -> requests.Session:
        """获取带认证的 session（传统 HTML 解析方式）"""
        if self._session is None:
            self._session = requests.Session()
            
            # 如果需要认证（传统方式）
            if self.auth_password and not self.use_custom_api:
                logger.debug("尝试 TeslaMate 传统认证...")
                try:
                    # TeslaMate 使用表单登录
                    login_url = f"{self.teslamate_url}/signin"
                    auth_url = f"{self.teslamate_url}/api/authenticate"
                    
                    # 首先获取登录页面，提取 CSRF token（如果有）
                    login_page = self._session.get(
                        login_url,
                        timeout=self.DEFAULT_TIMEOUT,
                    )
                    
                    # 尝试表单登录
                    response = self._session.post(
                        login_url,
                        data={
                            "_csrf_token": "",
                            "password": self.auth_password,
                        },
                        timeout=self.DEFAULT_TIMEOUT,
                        allow_redirects=True,
                    )
                    
                    # 检查登录是否成功（页面重定向到主页或没有登录表单）
                    if response.status_code == 200:
                        if self._is_authenticated(response.text):
                            logger.info("TeslaMate 表单认证成功")
                        else:
                            # 尝试 JSON API 认证
                            response = self._session.post(
                                auth_url,
                                json={"password": self.auth_password},
                                timeout=self.DEFAULT_TIMEOUT,
                            )
                            if response.status_code in [200, 201, 204]:
                                logger.info("TeslaMate API 认证成功")
                            else:
                                logger.warning(f"TeslaMate 认证失败: {response.status_code}")
                    else:
                        logger.warning(f"TeslaMate 认证请求失败: {response.status_code}")
                        
                except Exception as e:
                    logger.warning(f"TeslaMate 认证请求失败: {e}")
        
        return self._session
    
    def _is_authenticated(self, response_text: str) -> bool:
        """检查响应是否表示已认证（没有登录表单，而非简单关键词匹配）。
        
        仅检测登录表单/页面特征，避免把页面中正常的 CSS/JS 含 "password" 
        字样误判为未认证。
        """
        # 明确的登录表单特征（不是泛关键词）
        login_form_indicators = [
            '<form action="/signin"',
            '<form action="/login"',
            'name="password"',
            'id="password"',
            'placeholder="密码"',
            'placeholder="Password"',
        ]
        for indicator in login_form_indicators:
            if indicator.lower() in response_text.lower():
                return False
        return True
    
    def fetch_location_from_teslamate(self) -> str:
        """
        从 TeslaMate 获取位置信息
        
        策略顺序：
        1. Custom API（/login + /msg）
        2. 无认证直连（页面可能不需要密码）
        3. 表单认证 + HTML 解析
        4. 全部失败 → 返回 "unknown"，WiFi 接管
        
        Returns:
            位置文本或 "unknown"
        """
        # ── 策略 1: Custom API ──
        if self.use_custom_api and self._custom_api:
            try:
                location = self._custom_api.get_location()
                if location:
                    logger.info(f"TeslaMate API 位置: '{location}'")
                    return location
                logger.debug("TeslaMate API 未返回位置")
            except Exception as e:
                logger.info(f"TeslaMate API 不可用: {e}，尝试其他方式")
        
        # ── 策略 2: 无认证直连（很多 TeslaMate 实例不需要密码） ──
        try:
            session = requests.Session()
            response = session.get(
                self.teslamate_url,
                timeout=self.DEFAULT_TIMEOUT,
                headers={"Accept": "text/html"},
                allow_redirects=True,
            )
            response.raise_for_status()
            html_text = response.text
            
            # 如果页面不需要登录，直接解析位置
            if self._is_authenticated(html_text):
                location = self._parse_location_html(html_text)
                if location:
                    logger.info(f"TeslaMate 无认证直连成功: '{location}'")
                    return location
            
            # 需要登录 → 尝试认证
            logger.info("TeslaMate 需要登录认证，尝试认证...")
        except requests.RequestException as e:
            logger.info(f"TeslaMate 直连失败: {e}，尝试认证...")
        
        # ── 策略 3: 表单认证 + HTML 解析 ──
        try:
            session = self._get_session()
            if session is None:
                logger.warning("无法创建 TeslaMate 认证 session")
                return "unknown"
            
            response = session.get(
                self.teslamate_url,
                timeout=self.DEFAULT_TIMEOUT,
                headers={"Accept": "text/html"},
            )
            response.raise_for_status()
            html_text = response.text
            
            if self._is_authenticated(html_text):
                location = self._parse_location_html(html_text)
                if location:
                    logger.info(f"TeslaMate 认证后位置: '{location}'")
                    return location
                logger.warning("TeslaMate 认证成功但未找到位置信息")
                return "unknown"
            
            # 如果有密码，再试一次重认证
            if self.auth_password:
                self._session = None
                session = self._get_session()
                response = session.get(
                    self.teslamate_url,
                    timeout=self.DEFAULT_TIMEOUT,
                    headers={"Accept": "text/html"},
                )
                html_text = response.text
                if self._is_authenticated(html_text):
                    location = self._parse_location_html(html_text)
                    if location:
                        logger.info(f"TeslaMate 重认证后位置: '{location}'")
                        return location
            
            logger.warning("TeslaMate 认证失败，将使用 WiFi 定位")
        except requests.RequestException as e:
            logger.warning(f"TeslaMate 认证请求失败: {e}")
        except Exception as e:
            logger.error(f"TeslaMate 认证异常: {e}")
        
        return "unknown"
    
    def _parse_location_html(self, html_text: str) -> Optional[str]:
        """从 TeslaMate HTML 页面解析位置文本"""
        # 正则匹配 "停放位置：xxx"
        match = re.search(r"停放位置[：:]\s*([^<\n,]+)", html_text, re.U)
        if match:
            return match.group(1).strip()
        
        # BeautifulSoup 解析
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            for elem in soup.find_all(string=re.compile("停放位置")):
                parent = elem.parent
                if parent:
                    text = parent.get_text(strip=True)
                    m = re.search(r"停放位置[：:]\s*(.+)", text)
                    if m:
                        return m.group(1).strip()[:50]
        except Exception:
            pass
        
        return None
    
    def get_current_wifi(self) -> str:
        """
        获取当前连接的 WiFi SSID
        
        Returns:
            SSID 名称，未连接返回空字符串
            
        Raises:
            WifiDetectionError: WiFi 检测失败
        """
        for attempt in range(self.WIFI_RETRY_COUNT):
            try:
                # 方法 1: 使用 nmcli
                result = subprocess.run(
                    ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        parts = line.split(":")
                        if len(parts) >= 2 and parts[0] == "yes":
                            ssid = parts[1].strip()
                            if ssid:
                                logger.debug(f"当前 WiFi (nmcli): '{ssid}'")
                                return ssid
                
                # 方法 2: 使用 iw
                result = subprocess.run(
                    ["iw", "dev", self.wifi_interface, "link"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                
                if result.returncode == 0:
                    match = re.search(r"SSID:\s*(.+)", result.stdout)
                    if match:
                        ssid = match.group(1).strip()
                        logger.debug(f"当前 WiFi (iw): '{ssid}'")
                        return ssid
                
                # 方法 3: 使用 wpa_cli
                result = subprocess.run(
                    ["wpa_cli", "-i", self.wifi_interface, "status"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                
                if result.returncode == 0:
                    match = re.search(r"^ssid=(.+)$", result.stdout, re.MULTILINE)
                    if match:
                        ssid = match.group(1).strip()
                        logger.debug(f"当前 WiFi (wpa_cli): '{ssid}'")
                        return ssid
                
                if attempt < self.WIFI_RETRY_COUNT - 1:
                    logger.debug(f"WiFi 检测重试 {attempt + 1}/{self.WIFI_RETRY_COUNT}")
                    import time
                    time.sleep(0.5)
                    
            except subprocess.TimeoutExpired:
                logger.warning(f"WiFi 检测超时 (尝试 {attempt + 1})")
            except Exception as e:
                logger.warning(f"WiFi 检测异常 (尝试 {attempt + 1}): {e}")
        
        raise WifiDetectionError(f"无法检测当前 WiFi（已重试 {self.WIFI_RETRY_COUNT} 次）")
    
    def check_location(self) -> LocationInfo:
        """
        检查当前位置状态
        
        优先使用 TeslaMate API 获取位置，WiFi SSID 作为辅助验证。
        TeslaMate 为主，WiFi 为辅。
        
        Returns:
            LocationInfo 位置信息对象
        """
        # 获取 TeslaMate 位置（主检测）
        try:
            raw_location = self.fetch_location_from_teslamate()
            teslamate_available = raw_location != "unknown"
            location_source = "teslamate" if teslamate_available else "unknown"
        except TeslaMateConnectionError:
            raw_location = "unknown"
            teslamate_available = False
            location_source = "unknown"
            logger.warning("TeslaMate 不可用，降级到 WiFi 检测")
        
        # 获取当前 WiFi（辅助验证）
        try:
            current_wifi = self.get_current_wifi()
        except WifiDetectionError:
            current_wifi = ""
            logger.warning("WiFi 检测失败，位置判断可能不准确")
        
        # 判断位置状态（TeslaMate 为主）
        is_home = False
        confidence = 0.5
        
        # 优先判断：TeslaMate 位置包含家关键词
        if teslamate_available:
            # 从配置的 home_location 中拆分多关键词（逗号分隔）；保留硬编码作为兜底
            config_keywords = [kw.strip() for kw in self.home_location.split(",") if kw.strip()]
            home_keywords = config_keywords + ["家", "乐清", "象阳", "高园村"]
            # 去重保序
            seen = set()
            home_keywords = [kw for kw in home_keywords if not (kw in seen or seen.add(kw))]
            if any(kw in raw_location for kw in home_keywords if kw):
                is_home = True
                confidence = 0.9
                logger.info(f"TeslaMate 判定在家: '{raw_location}'")
            else:
                # TeslaMate 显示外出
                is_home = False
                confidence = 0.9
                logger.info(f"TeslaMate 判定外出: '{raw_location}'")
        
        # WiFi 辅助验证
        if current_wifi in self.home_wifi_ssids:
            if is_home:
                confidence = 1.0  # TeslaMate + WiFi 都确认在家
                location_source = "teslamate+wifi"
            elif not teslamate_available:
                # TeslaMate 不可用，使用 WiFi 判断
                is_home = True
                confidence = 0.7
                location_source = "wifi"
                logger.info("WiFi 确认在家 (TeslaMate 不可用)")
            else:
                # TeslaMate 显示外出但连了家里 WiFi（可能是 TeslaMate 延迟）
                logger.info("WiFi 确认在家，但 TeslaMate 位置不匹配（可能延迟）")
        elif current_wifi in self.hotspot_ssids:
            if not is_home:
                confidence = 0.8  # 使用热点，大概率外出
                if not teslamate_available:
                    location_source = "wifi"
        
        # 确定状态
        if is_home:
            state = LocationState.HOME
        elif raw_location != "unknown" or current_wifi:
            state = LocationState.AWAY
        else:
            state = LocationState.UNKNOWN
            confidence = 0.0
            location_source = "unknown"
        
        # 判断是否需要切换 WiFi
        needs_switch = False
        if state == LocationState.HOME and current_wifi not in self.home_wifi_ssids:
            if self.home_wifi_ssids:  # 有配置家庭 WiFi
                needs_switch = True
                logger.info(f"在家但连接了非家庭 WiFi '{current_wifi}'，建议切换")
        elif state == LocationState.AWAY and current_wifi in self.home_wifi_ssids:
            if self.hotspot_ssids:  # 有配置热点
                needs_switch = True
                logger.info(f"外出但连接了家庭 WiFi '{current_wifi}'，建议切换到热点")
        
        location_info = LocationInfo(
            state=state,
            raw_location=raw_location,
            wifi_connected=current_wifi,
            needs_wifi_switch=needs_switch,
            confidence=confidence,
            location_source=location_source,
        )
        
        # 触发位置变化回调
        if self._last_location and self._last_location.state != state:
            logger.info(f"位置变化: {self._last_location.state.value} -> {state.value}")
            if self._on_location_change:
                try:
                    self._on_location_change(location_info, self._last_location)
                except Exception as e:
                    logger.error(f"位置变化回调异常: {e}", exc_info=True)
        
        # 触发 WiFi 切换回调
        if needs_switch and self._on_wifi_switch_needed:
            target_ssid = (
                list(self.home_wifi_ssids)[0] if state == LocationState.HOME
                else list(self.hotspot_ssids)[0] if self.hotspot_ssids
                else ""
            )
            if target_ssid:
                try:
                    self._on_wifi_switch_needed(target_ssid, current_wifi)
                except Exception as e:
                    logger.error(f"WiFi 切换回调异常: {e}", exc_info=True)
        
        self._last_location = location_info
        return location_info
    
    def get_recommended_wifi(self) -> Optional[str]:
        """
        获取推荐的 WiFi SSID
        
        根据当前位置返回应该连接的 WiFi。
        
        Returns:
            推荐的 SSID，如无推荐返回 None
        """
        if not self._last_location:
            return None
        
        if self._last_location.state == LocationState.HOME:
            return list(self.home_wifi_ssids)[0] if self.home_wifi_ssids else None
        elif self._last_location.state == LocationState.AWAY:
            return list(self.hotspot_ssids)[0] if self.hotspot_ssids else None
        
        return None


# 单例实例（便于全局使用）
_location_detector: Optional["LocationDetector"] = None


def get_location_detector() -> "LocationDetector":
    """获取全局位置检测器实例"""
    global _location_detector
    if _location_detector is None:
        _location_detector = LocationDetector()
    return _location_detector


def init_location_detector(config: dict) -> "LocationDetector":
    """
    使用配置初始化位置检测器
    
    Args:
        config: 配置字典，包含 teslamate_url, home_location, home_wifi_ssids 等
        
    Returns:
        LocationDetector 实例
    """
    global _location_detector
    _location_detector = LocationDetector(
        teslamate_url=config.get("teslamate_url", "http://100.64.0.11:7777/"),
        home_location=config.get("home_location", "家"),
        home_wifi_ssids=config.get("home_wifi_ssids", []),
        hotspot_ssids=config.get("hotspot_ssids", []),
        wifi_interface=config.get("wifi_interface", "wlan0"),
        poll_interval=config.get("location_poll_interval", 60),
        auth_password=config.get("teslamate_password"),
        token_cache_path=config.get("token_cache_path"),
    )
    return _location_detector


def clear_token_cache(cache_path: str = "/opt/teslausb-web/data/.teslamate_token.json") -> bool:
    """
    清除 TeslaMate token 缓存
    
    用于手动清除缓存或调试。
    
    Args:
        cache_path: token 缓存文件路径
        
    Returns:
        成功清除返回 True
    """
    try:
        path = Path(cache_path)
        if path.exists():
            path.unlink()
            logger.info(f"Token 缓存已清除: {cache_path}")
            return True
        return False
    except Exception as e:
        logger.error(f"清除 token 缓存失败: {e}")
        return False
