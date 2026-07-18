"""
LiveTools 页面自动收割模块
=======================
从直播页面自动发现 MPD、完成 DRM 鉴权、交换 Widevine 许可证、
输出内容密钥，直通现有转推管线。

两种模式：
  1. Direct HTTP（默认）  — 带 Edge/Chrome UA 直接请求页面 HTML，
     从内嵌 <script> 中提取 app_id / channels_dash / drmEncryptKey，
     然后用同一会话 cookie 调用 DRM 鉴权接口。
  2. Browser fallback      — 当页面需要登录/验证码/动态渲染时，
     启动 Playwright 浏览器供人工操作，拦截关键网络请求。

协议逆向指引（protocol-reverse-engineering 技能）：
  - 按请求边界捕获：页面 HTML → DRM auth → MPD → license
  - 每个边界只关注该层需要的字段，不过度推断
  - 字段名保留原始大小写，不做归一化
  - 响应结构验证时只输出字段名与长度，不输出值
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import hashlib
import base64
import shutil
import urllib.parse
import urllib.request
import urllib.error
import subprocess
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 依赖检测（延迟导入以保持可选依赖的清晰度）
# ---------------------------------------------------------------------------

_REQUESTS_AVAILABLE = True
try:
    import requests as _requests_lib
except ImportError:
    _REQUESTS_AVAILABLE = False

_PYWIDEVINE_AVAILABLE = True
try:
    from pywidevine import PSSH, Device, Cdm  # type: ignore
except ImportError:
    _PYWIDEVINE_AVAILABLE = False

_PLAYWRIGHT_AVAILABLE = True
try:
    from playwright.sync_api import sync_playwright  # type: ignore
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

# Edge 用户数据目录（保留现有登录状态）
_EDGE_USER_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "Microsoft", "Edge", "User Data")

# 工具脚本所在目录（用于自动检测 .wvd 和 .exe）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

# 页面内嵌配置的提取模式
# 页面在首屏 HTML 中直接嵌入这些字段（仅当 UA 受支持时）
RE_APP_ID = re.compile(
    r"""app(?:lication)?[_.-]?id["'\s:=]+["']([^"']{4,64})["']""", re.I)

# channels_dash: 协议相对 URL (//cdn...) 或 https?://
RE_CHANNELS_DASH_ABS = re.compile(
    r"""channels[_.-]?dash["'\s:=]+["'](https?://[^"'\s]{8,})["']""", re.I)
RE_CHANNELS_DASH_REL = re.compile(
    r"""channels[_.-]?dash["'\s:=]+["'](//[^"'\s]{8,})["']""", re.I)

# 通用 MPD/DASH URL 发现（不限于特定变量名）
# 匹配 .mpd 后缀的 URL，或 /dash/ 路径中的 manifest
RE_MPD_URL = re.compile(
    r"""["']((?:https?:)?//[^"'\s]{4,}\.mpd(?:\?[^"'\s]*)?)["']""", re.I)
RE_MANIFEST_URL = re.compile(
    r"""["']((?:https?:)?//[^"'\s]{8,}/(?:dash|manifest|stream)[^"'\s]*\.mpd)["']""", re.I)

# 常见 MPD 变量名（排除过于通用的 'src'/'url' 以避免大量误匹配）
_MPD_VAR_NAMES = [
    'channels_dash', 'channelsDash', 'dashUrl', 'dash_url', 'mpdUrl',
    'mpd_url', 'manifestUrl', 'manifest_url', 'streamUrl', 'stream_url',
    'videoSource', 'video_source', 'dash_src', 'dashSrc',
    'dashManifest', 'dash_manifest',
]

RE_DRM_ASSET_ID = re.compile(
    r"""drmEncryptKey["'\s.]*?assetId["'\s:=]+["']([^"']{4,64})["']""", re.I)

# prestoLicense: https://... 或 JWT (eyJ...) 格式
RE_PRESTO_LICENSE_ABS = re.compile(
    r"""prestoLicense["'\s:=]+["'](https?://[^"'\s]{8,})["']""", re.I)
RE_PRESTO_LICENSE_JWT = re.compile(
    r"""prestoLicense["'\s:=]+["'](eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)["']""", re.I)

RE_JSON_OBJECT = re.compile(
    r'(?:app|application|config|siteConfig|playerConfig|window\.__INITIAL_STATE__)'
    r'["\s:=]*(?P<json>\{(?:[^{}]|\{[^{}]*\})*\})', re.I)

# DRM 鉴权接口模板
DRM_AUTH_PATH_TEMPLATE = "/api/stream-vp/{app_id}/get_auth_token_drm"
DRMTODAY_WIDEVINE_LICENSE_URL = (
    "https://lic.drmtoday.com/license-proxy-widevine/cenc/?specConform=true"
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class PageConfig:
    """从页面提取的配置"""
    app_id: str = ""
    channels_dash: List[str] = field(default_factory=list)
    drm_asset_id: str = ""
    presto_license: str = ""
    raw_embedded: Dict[str, Any] = field(default_factory=dict)

    @property
    def primary_mpd_url(self) -> str:
        return self.channels_dash[0] if self.channels_dash else ""

    @property
    def is_complete(self) -> bool:
        return bool(self.app_id and self.channels_dash and self.presto_license)


@dataclass
class DrmAuthToken:
    """DRM 鉴权接口返回的令牌"""
    auth_token: str = ""
    session_id: str = ""
    user_id: str = ""
    merchant_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return bool(self.auth_token)


@dataclass
class ContentKey:
    """Widevine 内容密钥"""
    kid: str = ""       # 16 字节 hex，无分隔符
    key: str = ""       # 16 字节 hex
    key_type: str = ""


@dataclass
class HarvestResult:
    """完整收割结果"""
    page_config: PageConfig = field(default_factory=PageConfig)
    drm_auth: DrmAuthToken = field(default_factory=DrmAuthToken)
    mpd_url: str = ""
    mpd_content: str = ""
    pssh_data: str = ""       # base64 编码的 PSSH
    keys: List[ContentKey] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    manual_debug_path: str = ""
    source_headers: Dict[str, str] = field(default_factory=dict)

    @property
    def kid_key_pairs(self) -> List[str]:
        """返回 ['KID:KEY', ...] 格式，可直接送入现有管线"""
        return [f"{k.kid}:{k.key}" for k in self.keys if k.kid and k.key]

    @property
    def success(self) -> bool:
        return bool(self.keys and self.mpd_url)


# ---------------------------------------------------------------------------
# HTTP 会话层（复用 requests 或退回到 urllib）
# ---------------------------------------------------------------------------

class HttpSession:
    """统一的 HTTP 会话，优先使用 requests，自动退回到 urllib。"""

    def __init__(self, cookie: str = "", headers: Optional[Dict[str, str]] = None):
        self.cookie = cookie
        self.headers = dict(headers or {})
        self._requests_session = None
        if _REQUESTS_AVAILABLE:
            self._requests_session = _requests_lib.Session()

    def _build_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        result = {
            "User-Agent": self.headers.get("User-Agent", DEFAULT_UA),
        }
        result.update(self.headers)
        if extra:
            result.update(extra)
        if self.cookie and "Cookie" not in result:
            result["Cookie"] = self.cookie
        return result

    def get(self, url: str, extra_headers: Optional[Dict[str, str]] = None) -> str:
        """GET 请求，返回响应体文本。"""
        headers = self._build_headers(extra_headers)
        if self._requests_session:
            resp = self._requests_session.get(
                url, headers=headers, timeout=20, allow_redirects=True)
            resp.raise_for_status()
            # 更新 cookie（服务端可能设置新的）
            set_cookie = resp.headers.get("Set-Cookie", "")
            if set_cookie:
                self._merge_cookie(set_cookie)
            return resp.text
        else:
            req = urllib.request.Request(url)
            for k, v in headers.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode(
                    resp.headers.get_content_charset() or "utf-8")

    def post(self, url: str, data: Any = None, json_data: Any = None,
             extra_headers: Optional[Dict[str, str]] = None) -> bytes:
        """POST 请求，返回响应体字节（用于二进制 license 交换）。"""
        headers = self._build_headers(extra_headers)
        if self._requests_session:
            resp = self._requests_session.post(
                url, data=data, json=json_data,
                headers=headers, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            return resp.content
        else:
            body = None
            if isinstance(data, bytes):
                body = data
                headers.setdefault("Content-Type", "application/octet-stream")
            elif json_data is not None:
                body = json.dumps(json_data).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            req = urllib.request.Request(url, data=body, method="POST")
            for k, v in headers.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()

    def _merge_cookie(self, set_cookie_header: str) -> None:
        """合并服务端返回的 Set-Cookie。"""
        parts = set_cookie_header.split(";")
        if parts:
            name_val = parts[0].strip().split("=", 1)
            if len(name_val) == 2:
                existing = dict(
                    c.strip().split("=", 1)
                    for c in self.cookie.split(";") if "=" in c
                )
                existing[name_val[0].strip()] = name_val[1].strip()
                self.cookie = "; ".join(
                    f"{k}={v}" for k, v in existing.items())


# ---------------------------------------------------------------------------
# 页面 HTML 解析：提取内嵌配置
# ---------------------------------------------------------------------------

def extract_page_config(html: str, page_url: str = "") -> PageConfig:
    """
    从页面 HTML 中提取内嵌配置。
    
    协议逆向策略：
    - 先按已知字段名精确匹配
    - 再尝试从 <script> 标签中寻找 JSON 配置块
    - 如果仍未找到 MPD URL，全局搜索 .mpd 链接
    - 解码 prestoLicense JWT 提取内嵌 URL
    - 不依赖第三方库，只用正则 + 字符串操作
    
    参数:
        html: 页面 HTML 源码
        page_url: 页面 URL（用于解析相对路径）
    
    返回:
        PageConfig 实例
    """
    config = PageConfig()

    # --- 第一步：精确字段提取 ---

    match = RE_APP_ID.search(html)
    if match:
        config.app_id = match.group(1).strip()

    # channels_dash 提取（多种模式）
    _extract_channels_dash(html, config)

    match = RE_DRM_ASSET_ID.search(html)
    if match:
        config.drm_asset_id = match.group(1).strip()

    # prestoLicense 提取（支持 JWT 和绝对 URL）
    match = RE_PRESTO_LICENSE_JWT.search(html)
    if match:
        config.presto_license = match.group(1).strip()
    else:
        match = RE_PRESTO_LICENSE_ABS.search(html)
        if match:
            config.presto_license = match.group(1).strip()

    # --- 第二步：宽松 JSON 对象提取（兜底） ---

    # 在 <script> 中寻找内联的 JSON 配置块
    for json_match in RE_JSON_OBJECT.finditer(html):
        try:
            obj = json.loads(json_match.group("json"))
            _merge_json_config(config, obj)
        except (json.JSONDecodeError, TypeError):
            continue

    # 尝试匹配 window.__XXX__ = {...} 模式（支持更深层嵌套）
    win_assign = re.compile(
        r'window\.\w+\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\})\s*;', re.I | re.DOTALL)
    for m in win_assign.finditer(html):
        try:
            obj = json.loads(m.group(1))
            _merge_json_config(config, obj)
        except (json.JSONDecodeError, TypeError):
            continue

    # --- 第三步：全局 MPD URL 搜索（最后手段） ---
    if not config.channels_dash:
        # 搜索任何 .mpd URL
        seen = set()
        for pattern in [RE_MPD_URL, RE_MANIFEST_URL]:
            for m in pattern.finditer(html):
                url = m.group(1).strip()
                if url not in seen:
                    seen.add(url)
                    config.channels_dash.append(url)

    # --- 第四步：解码 prestoLicense JWT ---
    if config.presto_license:
        jwt_urls = _decode_jwt_urls(config.presto_license)
        if jwt_urls:
            print(f"  [JWT] prestoLicense 内嵌 {len(jwt_urls)} 个 URL:")
            resolved_license = resolve_license_url(config.presto_license)
            if resolved_license and resolved_license != config.presto_license:
                print("  [JWT] License URL 已规范化")
                config.presto_license = resolved_license
        elif _looks_like_jwt(config.presto_license) or (
                config.presto_license.startswith("http") and
                re.search(r'/eyJ[A-Za-z0-9_-]{20,}\.', config.presto_license)):
            # JWT 格式检测到但未能提取 URL
            print(f"  [JWT] prestoLicense 是 JWT/含 JWT，但 payload 中未提取到 URL。")
            # 尝试直接输出 payload 结构
            _dump_jwt_structure(config.presto_license)
        else:
            config.presto_license = resolve_license_url(config.presto_license)

    # --- 第五步：相对 URL 解析 ---
    if page_url:
        config.channels_dash = [
            _resolve_url(page_url, u) for u in config.channels_dash
        ]
        config.presto_license = _resolve_url(page_url, config.presto_license)

    # 去重
    config.channels_dash = list(dict.fromkeys(config.channels_dash))

    # 把 raw_embedded 填上（供调试）
    config.raw_embedded = {
        "app_id": config.app_id,
        "channels_dash": config.channels_dash,
        "drm_asset_id": config.drm_asset_id,
        "presto_license": config.presto_license[:80] + "..." if len(config.presto_license) > 80 else config.presto_license,
    }

    return config


def _extract_channels_dash(html: str, config: PageConfig) -> None:
    """从 HTML 中提取 channels_dash（支持多种模式）。"""
    # 模式 1: 在 <script> 中搜索 channels_dash 相关代码
    script_pattern = re.compile(
        r'<script[^>]*>(.*?)</script>', re.I | re.DOTALL)
    
    for script_match in script_pattern.finditer(html):
        script_content = script_match.group(1)
        if 'channels_dash' not in script_content and 'channelsDash' not in script_content:
            # 也搜索其他常见 MPD 变量名
            if not any(v in script_content for v in _MPD_VAR_NAMES):
                continue
        
        # 匹配数组形式: channels_dash: ["url1","url2"]
        array_match = re.search(
            r'(?:channels[_]?dash|channelsDash)["\s:=]+\s*\[(.*?)\]',
            script_content, re.I | re.DOTALL)
        if array_match:
            # 匹配绝对 URL
            urls = re.findall(r'["\'](https?://[^"\'\s]{8,})["\']', array_match.group(1))
            # 匹配协议相对 URL
            if not urls:
                urls = re.findall(r'["\'](//[^"\'\s]{8,})["\']', array_match.group(1))
            for u in urls:
                if u not in config.channels_dash:
                    config.channels_dash.append(u)

        # 匹配单值形式
        for pattern in [RE_CHANNELS_DASH_ABS, RE_CHANNELS_DASH_REL]:
            single_match = pattern.search(script_content)
            if single_match:
                url = single_match.group(1).strip()
                if url not in config.channels_dash:
                    config.channels_dash.append(url)

        # 匹配模板字符串: channels_dash: `${base}/path.mpd`
        tmpl_match = re.search(
            r'(?:channels[_]?dash|channelsDash)["\s:=]+\s*[`]([^`]+\.mpd[^`]*)[`]',
            script_content, re.I)
        if tmpl_match:
            # 模板字符串中的 URL 可能不完整，跳过
            pass

        # 匹配赋值给变量的 MPD URL: var x = channels_dash; ... x = "url"
        if not config.channels_dash:
            var_match = re.search(
                r'(?:channels[_]?dash|channelsDash)["\s:=]+\s*(\w+)',
                script_content, re.I)
            if var_match:
                var_name = var_match.group(1)
                # 在同一个 script 中找这个变量被赋值的 URL
                url_match = re.search(
                    rf'{re.escape(var_name)}\s*=\s*["\']((?:https?:)?//[^"\'\s]{{8,}})["\']',
                    script_content, re.I)
                if url_match:
                    url = url_match.group(1).strip()
                    if url not in config.channels_dash:
                        config.channels_dash.append(url)

    # 模式 2: 全局搜索（如果没从 script 中找到）
    if not config.channels_dash:
        for pattern in [RE_CHANNELS_DASH_ABS, RE_CHANNELS_DASH_REL]:
            for m in pattern.finditer(html):
                url = m.group(1).strip()
                if url not in config.channels_dash:
                    config.channels_dash.append(url)

    # 模式 3: 搜索其他常见 MPD 变量名
    if not config.channels_dash:
        for var_name in _MPD_VAR_NAMES:
            if var_name in ('channels_dash', 'channelsDash'):
                continue  # 已经搜过了
            var_pattern = re.compile(
                rf'{re.escape(var_name)}["\s:=]+["\']((?:https?:)?//[^"\'\s]{{8,}})["\']',
                re.I)
            for m in var_pattern.finditer(html):
                url = m.group(1).strip()
                if url not in config.channels_dash:
                    config.channels_dash.append(url)


def _looks_like_jwt(value: str) -> bool:
    """检查字符串是否像 JWT（三段 base64url，用 . 分隔）。"""
    return bool(re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', value))


def _post_license_request(session: HttpSession, url: str,
                          data: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
    """
    POST license 请求。优先使用与参考工具一致的 requests，失败后再回退。
    
    返回 {status, body}。
    """
    first_error_response = None

    # 参考工具直接使用 requests.post；优先复用当前 Session，以保留连接和
    # cookie，同时不对二进制 challenge/response 做任何文本转换。
    if session._requests_session:
        try:
            resp = session._requests_session.post(
                url, data=data, headers=dict(headers), timeout=30,
                allow_redirects=True)
            response = {"status": resp.status_code, "body": resp.content}
            if resp.status_code == 200:
                return response
            first_error_response = response
        except Exception as exc:
            first_error_response = {
                "status": 0,
                "body": b"",
                "error": str(exc),
            }

    # 回退 1: curl。它在少数对 TLS 客户端特征敏感的站点更兼容。
    curl_path = shutil.which("curl")
    if curl_path:
        try:
            result = _curl_post(url, data, headers, curl_path=curl_path)
            if result and result.get("status") == 200:
                return result
            if result and first_error_response is None:
                first_error_response = result
        except Exception:
            pass

    # 回退 2: raw urllib
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in dict(headers).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"status": resp.status, "body": resp.read()}
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, 'read') else b""
        response = {"status": exc.code, "body": body}
        return first_error_response or response
    except Exception as exc:
        return first_error_response or {"status": 0, "body": b"", "error": str(exc)}


def _curl_post(url: str, data: bytes, headers: Dict[str, str],
               curl_path: str = "curl") -> Dict[str, Any]:
    """使用 curl 发送 POST 请求，并原样保留二进制响应体。"""
    import subprocess as sp
    cmd = [curl_path, "-sS", "-X", "POST",
           "--data-binary", "@-",        # 从 stdin 读取 body
           "-o", "-",                     # 输出到 stdout
           "-w", "\n%{http_code}",        # 最后一行输出状态码
           "--max-time", "30"]
    for k, v in headers.items():
        cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)
    
    try:
        proc = sp.run(cmd, input=data, capture_output=True, timeout=35)
        body, separator, status_text = proc.stdout.rpartition(b"\n")
        if separator and status_text.strip().isdigit():
            return {"status": int(status_text.strip()), "body": body}
        return {
            "status": 0,
            "body": b"",
            "error": proc.stderr.decode("utf-8", errors="replace"),
        }
    except Exception as exc:
        return {"status": 0, "body": b"", "error": str(exc)}


def _decode_jwt_urls(jwt_or_url: str) -> List[str]:
    """
    解码 JWT payload（不验证签名），提取其中内嵌的 URL。
    
    支持两种输入：
      1. 纯 JWT: eyJhbGci...eyJ1cmxzIjpb...signature
      2. URL 包含 JWT: https://example.com/vp/eyJhbGci.../...
    
    只解码 payload（中间段），递归搜索所有字符串值中的 URL。
    """
    token = jwt_or_url
    # 如果是 URL，尝试提取路径中的 JWT
    if token.startswith("http://") or token.startswith("https://"):
        # 匹配 URL 路径中的 JWT (eyJ... 开头，后跟 base64url 字符)
        jwt_match = re.search(r'/(eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})', token)
        if jwt_match:
            token = jwt_match.group(1)
        elif _looks_like_jwt(token.split("/")[-1]):
            token = token.split("/")[-1]

    urls = []
    if not _looks_like_jwt(token):
        return urls
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return urls
        # 补齐 base64 padding
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
        urls = _extract_urls_from_json(payload)
    except Exception:
        pass
    return urls


def resolve_license_url(presto_license: str) -> str:
    """
    Normalize page prestoLicense values to the actual Widevine license endpoint.

    Some pages expose a JWT or a DRMtoday RightsManager URL in prestoLicense.
    The Widevine challenge must be posted to the DRMtoday Widevine proxy
    endpoint, not to the JWT text or RightsManager endpoint.
    """
    value = (presto_license or "").strip()
    if not value:
        return ""

    candidates = _decode_jwt_urls(value)
    candidates.append(value)

    for candidate in candidates:
        lowered = candidate.lower()
        if "drmtoday.com" in lowered:
            return DRMTODAY_WIDEVINE_LICENSE_URL

    for candidate in candidates:
        parsed = urllib.parse.urlparse(candidate)
        if parsed.scheme in ("http", "https"):
            return candidate

    return value


def _extract_urls_from_json(obj: Any, depth: int = 0) -> List[str]:
    """递归从 JSON 对象中提取 URL 字符串（绝对或协议相对）。"""
    urls = []
    if depth > 10:
        return urls
    if isinstance(obj, str):
        # 匹配 https?://... 或 //...
        if re.match(r'^(?:https?:)?//', obj) and len(obj) > 8:
            urls.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            urls.extend(_extract_urls_from_json(v, depth + 1))
    elif isinstance(obj, list):
        for v in obj:
            urls.extend(_extract_urls_from_json(v, depth + 1))
    return urls


def _dump_jwt_structure(jwt_or_url: str) -> None:
    """输出 JWT payload 的结构（字段名与类型/长度），不输出值。"""
    token = jwt_or_url
    if token.startswith("http://") or token.startswith("https://"):
        jwt_match = re.search(
            r'/(eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})',
            token)
        if jwt_match:
            token = jwt_match.group(1)
    if not _looks_like_jwt(token):
        return
    try:
        parts = token.split(".")
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
        print(f"  [JWT] Payload 结构: {_describe_json_structure(payload, max_depth=4)}")
    except Exception as exc:
        print(f"  [JWT] Payload 解码失败: {exc}")


def _resolve_url(page_url: str, target: str) -> str:
    """解析相对/协议相对 URL。"""
    if not target:
        return target
    if target.startswith("//"):
        parsed = urllib.parse.urlparse(page_url)
        return f"{parsed.scheme}:{target}"
    if not urllib.parse.urlparse(target).scheme:
        return urllib.parse.urljoin(page_url, target)
    return target


def _merge_json_config(config: PageConfig, obj: dict) -> None:
    """从 JSON 对象中合并已知字段到 PageConfig。"""
    for key, value in obj.items():
        key_lower = key.lower().replace("_", "").replace("-", "")
        if key_lower in ("appid", "applicationid") and not config.app_id:
            config.app_id = str(value)
        elif key_lower in ("channelsdash", "mpdlist", "dashurls", "dashurl",
                           "mpdurl", "manifesturl", "streamurl", "videosource",
                           "dashsrc"):
            if isinstance(value, list):
                for u in value:
                    if isinstance(u, str) and u not in config.channels_dash:
                        config.channels_dash.append(u)
            elif isinstance(value, str) and value not in config.channels_dash:
                config.channels_dash.append(value)
        elif key_lower in ("drmaassetid", "drmassetid", "assetid") and not config.drm_asset_id:
            config.drm_asset_id = str(value)
        elif key_lower in ("prestolicense", "licenseserver", "licenseserverurl",
                           "licenseurl") and not config.presto_license:
            config.presto_license = str(value)

        # 递归检查嵌套对象
        if isinstance(value, dict):
            _merge_json_config(config, value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _merge_json_config(config, item)


# ---------------------------------------------------------------------------
# DRM 鉴权接口
# ---------------------------------------------------------------------------

def request_drm_auth_token(
    session: HttpSession,
    app_id: str,
    channel_id: str,
    base_url: str = "",
) -> DrmAuthToken:
    """
    调用 DRM 鉴权接口获取令牌。
    
    端点: {base_url}/api/stream-vp/{app_id}/get_auth_token_drm?channel_id={channel_id}
    
    协议逆向策略：
    - 按请求边界隔离：此函数只关注这一个 HTTP 调用
    - 响应字段名保留原始大小写（auth_token, sessionId, user_id, merchantId）
    - 返回完整的 DrmAuthToken，raw 字段保留原始响应供验证
    
    参数:
        session: 带 cookie 的 HTTP 会话
        app_id: 应用 ID
        channel_id: 频道 ID（通常即 drm_asset_id）
        base_url: 站点 base URL（从页面 URL 提取 origin）
    
    返回:
        DrmAuthToken 实例
    """
    path = DRM_AUTH_PATH_TEMPLATE.format(app_id=app_id)
    url = urllib.parse.urljoin(base_url, path) if base_url else path
    params = {"channel_id": channel_id}

    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"无法构造 DRM 鉴权 URL: app_id={app_id}, base_url={base_url}")

    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    print("[DRM Auth] 请求: 已构造")

    try:
        text = session.get(full_url, extra_headers={
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        })
    except Exception as exc:
        raise RuntimeError(f"DRM 鉴权请求失败: {exc}") from exc

    token = DrmAuthToken()
    token.raw = _safe_parse_json(text)
    
    # 按原始字段名提取（协议逆向要求保留大小写）
    data = token.raw.get("data", token.raw)
    if isinstance(data, dict):
        token.auth_token = str(data.get("auth_token", data.get("authToken", "")))
        token.session_id = str(data.get("sessionId", data.get("session_id", "")))
        token.user_id = str(data.get("user_id", data.get("userId", "")))
        token.merchant_id = str(data.get("merchantId", data.get("merchant_id", "")))

    # 顶层也可能直接返回
    if not token.auth_token:
        token.auth_token = str(token.raw.get("auth_token", token.raw.get("authToken", "")))
        token.session_id = str(token.raw.get("sessionId", token.raw.get("session_id", "")))
        token.user_id = str(token.raw.get("user_id", token.raw.get("userId", "")))
        token.merchant_id = str(token.raw.get("merchantId", token.raw.get("merchant_id", "")))

    if not token.auth_token:
        print(f"[警告] DRM 鉴权响应未包含 auth_token。"
              f" 响应字段: {_describe_json_structure(token.raw)}")

    return token


# ---------------------------------------------------------------------------
# Widevine 许可证交换
# ---------------------------------------------------------------------------

def extract_pssh_from_mpd(mpd_content: str) -> str:
    """
    从 MPD 的 ContentProtection 元素中提取 Widevine PSSH。
    
    协议逆向策略：
    - 只匹配 Widevine system ID (edef8ba9-79d6-4ace-a3c8-27dcd51d21ed)
    - PSSH 通常在 <cenc:pssh> 子元素中以 base64 编码
    - 不解析完整 PSSH box 结构
    
    返回:
        base64 编码的 PSSH 数据，或空字符串
    """
    try:
        root = ET.fromstring(mpd_content)
    except ET.ParseError:
        return ""

    widevine_scheme = (
        "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed")
    for protection in root.iter():
        if _xml_local_name(protection.tag).lower() != "contentprotection":
            continue
        scheme = (protection.attrib.get("schemeIdUri") or "").lower()
        if scheme != widevine_scheme:
            continue
        for child in protection.iter():
            if _xml_local_name(child.tag).lower() == "pssh" and child.text:
                # XML formatters may wrap base64 over several lines.
                return "".join(child.text.split())

    return ""


def build_pssh_box(kid_hex: str, system_id: str = None) -> bytes:
    """
    从 KID 构造一个最小 Widevine PSSH box。
    
    当 MPD 没有内嵌 <cenc:pssh> 时，可以用 default_KID 自行构造。
    
    参数:
        kid_hex: 16 字节 KID（可带连字符）
        system_id: Widevine system ID（默认使用标准值）
    
    返回:
        完整的 PSSH box 字节
    """
    if system_id is None:
        system_id = "edef8ba979d64acea3c827dcd51d21ed"
    
    kid_clean = kid_hex.replace("-", "").lower()
    if len(kid_clean) != 32:
        raise ValueError(f"KID 必须是 16 字节 hex: {kid_hex}")
    
    kid_bytes = bytes.fromhex(kid_clean)
    system_id_clean = system_id.replace("-", "").lower()
    if len(system_id_clean) != 32:
        raise ValueError(f"System ID 必须是 16 字节 hex: {system_id}")
    system_id_bytes = bytes.fromhex(system_id_clean)

    # WidevineCencHeader.key_ids 是 protobuf field 2（wire type 2）。
    payload = _build_widevine_pssh_payload(kid_bytes)
    
    # Version 0 PSSH: [size][type][version/flags][systemID][dataSize][data].
    # Version 1 还必须带 KID_count/KIDs；旧实现标记为 v1 却遗漏该字段，
    # pywidevine 因而会把后续 dataSize 错当作 KID_count。
    #           [systemID(16)][dataSize(4)][data]
    box_size = 4 + 4 + 1 + 3 + 16 + 4 + len(payload)
    pssh = bytearray()
    pssh.extend(box_size.to_bytes(4, "big"))
    pssh.extend(b"pssh")
    pssh.extend(b"\x00\x00\x00\x00")   # version 0, flags 0
    pssh.extend(system_id_bytes)
    pssh.extend(len(payload).to_bytes(4, "big"))
    pssh.extend(payload)
    
    return bytes(pssh)


def _build_widevine_pssh_payload(kid_bytes: bytes) -> bytes:
    """构造 Widevine PSSH 的 protobuf payload（最小可用版）。"""
    if len(kid_bytes) != 16:
        raise ValueError("Widevine KID 必须是 16 字节")
    return b"\x12\x10" + kid_bytes


def _xml_local_name(name: str) -> str:
    """返回 XML 标签或属性的本地名。"""
    return name.rsplit('}', 1)[-1].rsplit(':', 1)[-1]


def _drmtoday_license_headers(drm_auth: DrmAuthToken) -> Dict[str, str]:
    """构造与仓库参考工具一致的 DRMtoday 请求头。"""
    headers = {"Accept": "*/*"}
    if not drm_auth.auth_token:
        return headers

    custom_data = {
        "userId": drm_auth.user_id or "",
        "sessionId": drm_auth.session_id or "",
        "merchant": drm_auth.merchant_id or "",
    }
    custom_json = json.dumps(custom_data, separators=(",", ":"))
    headers["dt-custom-data"] = base64.b64encode(
        custom_json.encode("utf-8")).decode("ascii")
    headers["x-dt-auth-token"] = drm_auth.auth_token
    return headers


def build_license_headers(license_url: str, drm_auth: DrmAuthToken) -> Dict[str, str]:
    """Build license POST headers using the observed request boundary."""
    if drm_auth and drm_auth.auth_token:
        return _drmtoday_license_headers(drm_auth)
    return {"Accept": "*/*"}


def build_manual_license_bundle(result: HarvestResult, license_url: str) -> Dict[str, Any]:
    """Collect manual license data without printing it on the success path."""
    license_url = resolve_license_url(license_url)
    headers = build_license_headers(license_url, result.drm_auth)
    return {
        "mpd_url": result.mpd_url,
        "license_url": license_url,
        "pssh": result.pssh_data,
        "headers": headers,
    }


def write_manual_license_bundle(result: HarvestResult, license_url: str) -> str:
    bundle = build_manual_license_bundle(result, license_url)
    if not any([bundle.get("mpd_url"), bundle.get("license_url"),
                bundle.get("pssh"), bundle.get("headers")]):
        return ""
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".manual-license.json",
        prefix="livetools_",
        delete=False,
        encoding="utf-8",
    )
    with handle:
        json.dump(bundle, handle, ensure_ascii=False, indent=2)
    result.manual_debug_path = handle.name
    return handle.name


def clear_manual_license_bundle(result: HarvestResult) -> None:
    path = result.manual_debug_path
    if path and os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError:
            pass
    result.manual_debug_path = ""


def print_manual_license_bundle(result: HarvestResult, license_url: str = "") -> None:
    bundle = build_manual_license_bundle(result, license_url)
    print("\n[手动取钥] 诊断数据:")
    if result.manual_debug_path:
        print(f"  保存文件: {result.manual_debug_path}")
    if bundle.get("mpd_url"):
        print(f"  MPD URL: {bundle['mpd_url']}")
    if bundle.get("license_url"):
        print(f"  License URL: {bundle['license_url']}")
    if bundle.get("pssh"):
        print("  PSSH:")
        print(bundle["pssh"])
    headers = bundle.get("headers") or {}
    if headers.get("dt-custom-data"):
        print(f"  dt-custom-data:  {headers['dt-custom-data']}")
    if headers.get("x-dt-auth-token"):
        print(f"  x-dt-auth-token: {headers['x-dt-auth-token']}")


def source_manifest_headers(cookie: str = "",
                            headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Headers needed to read the MPD again in the push subprocess."""
    allowed = {
        "accept",
        "accept-language",
        "cookie",
        "referer",
        "user-agent",
        "origin",
    }
    canonical = {
        "accept": "Accept",
        "accept-language": "Accept-Language",
        "cookie": "Cookie",
        "referer": "Referer",
        "user-agent": "User-Agent",
        "origin": "Origin",
    }
    result = {}
    for name, value in (headers or {}).items():
        lowered = name.lower()
        if value and lowered in allowed:
            result[canonical.get(lowered, name)] = value
    if cookie:
        result.pop("Cookie", None)
        result["Cookie"] = cookie
    if "User-Agent" not in result:
        result["User-Agent"] = DEFAULT_UA
    return result


def cookies_to_header(cookies: List[Dict[str, Any]]) -> str:
    """Convert Playwright cookie dicts to a Cookie header."""
    pairs = []
    seen = set()
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None or name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def browser_source_headers(context: Any, urls: List[str],
                           base_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build source headers from browser cookies scoped to the MPD/page URLs."""
    relevant_urls = [url for url in urls if url]
    try:
        cookies = context.cookies(relevant_urls) if relevant_urls else context.cookies()
    except Exception:
        cookies = context.cookies()
    return source_manifest_headers(cookies_to_header(cookies), base_headers)


def _value_as_hex(value: Any) -> str:
    """兼容 bytes、UUID 以及 pywidevine Key 字段的 hex 表示。"""
    if value is None:
        raise TypeError("空值无法转换为十六进制")
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bytearray):
        return bytes(value).hex()
    hex_member = getattr(value, "hex", None)
    if callable(hex_member):
        return hex_member()
    if isinstance(hex_member, str):
        return hex_member
    if isinstance(value, str):
        return value.replace("-", "")
    raise TypeError(f"无法把 {type(value).__name__} 转换为十六进制")


def _extract_content_keys(cdm: Any, session_id: Any) -> List[ContentKey]:
    """兼容新旧 pywidevine API，并只保留 CONTENT 密钥。"""
    keys = []
    for item in cdm.get_keys(session_id):
        if isinstance(item, tuple) and len(item) == 2:
            kid, key_obj = item
            key_value = getattr(key_obj, "key", key_obj)
            key_type_raw = getattr(key_obj, "type", "CONTENT")
        else:
            key_obj = item
            kid = getattr(key_obj, "kid", None)
            key_value = getattr(key_obj, "key", None)
            key_type_raw = getattr(key_obj, "type", "")

        key_type = str(key_type_raw or "")
        if key_type.rsplit('.', 1)[-1].upper() != "CONTENT":
            continue
        keys.append(ContentKey(
            kid=_value_as_hex(kid).lower(),
            key=_value_as_hex(key_value).lower(),
            key_type="CONTENT",
        ))
    return keys


def exchange_widevine_license(
    session: HttpSession,
    license_url: str,
    pssh_data: str,
    drm_auth: DrmAuthToken,
    device_path: str,
    mpd_url: str = "",
) -> List[ContentKey]:
    """
    使用 pywidevine 交换 Widevine 许可证获取内容密钥。
    
    协议逆向策略：
    - 严格按请求边界：PSSH → license challenge → license server → parse
    - 验证 challenge 和 response 的大小合理性
    - 不假设 license server 的响应格式，由 pywidevine 处理
    
    参数:
        session: HTTP 会话
        license_url: prestoLicense 地址
        pssh_data: base64 编码的 PSSH（从 MPD 提取）
        drm_auth: DRM 鉴权令牌
        device_path: .wvd 设备文件路径
        mpd_url: MPD URL（用于 license 请求中的 origin 头）
    
    返回:
        ContentKey 列表
    """
    if not _PYWIDEVINE_AVAILABLE:
        raise ImportError(
            "pywidevine 未安装。请执行: pip install pywidevine\n"
            "并准备一个 .wvd 设备文件（可通过 --wvd 参数指定）。")

    if not pssh_data:
        raise ValueError("PSSH 数据为空，无法构造 license challenge")

    print(f"[License] 加载 Widevine 设备: {device_path}")
    device = Device.load(device_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    try:
        # 解析 PSSH
        pssh = PSSH(pssh_data)
        print(f"[License] PSSH 已解析, system: {pssh.system_id}")

        # 生成 license challenge
        challenge = cdm.get_license_challenge(session_id, pssh)
        print(f"[License] Challenge 大小: {len(challenge)} bytes")

        license_url = resolve_license_url(license_url)
        license_headers = build_license_headers(license_url, drm_auth)

        # License 请求不要设 Origin/Referer（浏览器 CDM 会设页面 Origin，
        # 但设错反而导致 400；让服务器从 Host header 自行推断更安全）
        # 如果确实需要，应在调用方传入正确的 page_origin

        print("[License] 发送请求: POST")
        print(f"[License] 请求头字段: {', '.join(license_headers.keys())}")

        # 发送 license 请求
        license_response = _post_license_request(
            session, license_url, challenge, license_headers)

        status_code = license_response.get("status", 0)
        response_body = license_response.get("body", b"")
        print(f"[License] 响应状态: {status_code}, 大小: {len(response_body)} bytes")

        if status_code != 200:
            body_preview = (response_body[:500].decode("utf-8", errors="replace")
                            if isinstance(response_body, bytes) else str(response_body)[:500])
            # 保存 challenge 到文件供手动调试
            challenge_dump = os.path.join(
                tempfile.gettempdir(), "livetools_license_challenge.bin")
            with open(challenge_dump, "wb") as f:
                f.write(challenge)
            raise RuntimeError(
                f"License 服务器返回 {status_code}。\n"
                f"响应体预览: {body_preview}\n"
                f"Challenge 已保存到: {challenge_dump}\n"
                f"手动测试: curl -X POST '{license_url}' \\\n"
                f"  -H 'Accept: */*' \\\n"
                f"  -H 'dt-custom-data: VALUE' \\\n"
                f"  -H 'x-dt-auth-token: TOKEN' \\\n"
                f"  --data-binary @{challenge_dump}")
        cdm.parse_license(session_id, response_body)

        keys = _extract_content_keys(cdm, session_id)
        for ck in keys:
            print(f"[License] 获取密钥 KID:{ck.kid[:16]}... "
                  f"类型:{ck.key_type}")

    finally:
        cdm.close(session_id)

    return keys


def _exchange_license_via_browser(page, session: HttpSession,
                                   license_url: str, pssh_data: str,
                                   drm_auth: DrmAuthToken,
                                   device_path: str) -> List[ContentKey]:
    """
    通过浏览器 fetch() 发送 license 请求（绕过 WAF/TLS 指纹检测）。
    
    部分站点需要浏览器 TLS 栈；另一些站点会拒绝浏览器 fetch 自动带上的
    Origin/CORS preflight。这一函数只负责浏览器路径，调用方会做原生回退。
    
    此函数用 pywidevine 生成 challenge，然后通过 Playwright 的
    page.evaluate() 在浏览器上下文中执行 fetch() 发送。
    """
    if not _PYWIDEVINE_AVAILABLE:
        raise ImportError("pywidevine 未安装")

    print(f"[License:Browser] 加载 Widevine 设备: {device_path}")
    device = Device.load(device_path)
    cdm = Cdm.from_device(device)
    cdm_session_id = cdm.open()

    try:
        pssh = PSSH(pssh_data)
        challenge = cdm.get_license_challenge(cdm_session_id, pssh)
        challenge_b64 = base64.b64encode(challenge).decode("ascii")
        print(f"[License:Browser] Challenge 大小: {len(challenge)} bytes")

        license_url = resolve_license_url(license_url)
        license_headers = build_license_headers(license_url, drm_auth)

        # 通过浏览器 fetch() 发送（使用浏览器的 TLS 栈）
        js_code = f"""
        (async () => {{
            const url = {json.dumps(license_url)};
            const challengeBytes = Uint8Array.from(
                atob({json.dumps(challenge_b64)}), c => c.charCodeAt(0));
            
            const headers = {json.dumps(license_headers)};
            
            try {{
                const resp = await fetch(url, {{
                    method: 'POST',
                    headers: headers,
                    body: challengeBytes.buffer,
                    mode: 'cors',
                }});
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let b64 = '';
                for (let i = 0; i < bytes.length; i++) {{
                    b64 += String.fromCharCode(bytes[i]);
                }}
                return {{
                    status: resp.status,
                    bodyB64: btoa(b64),
                }};
            }} catch (e) {{
                return {{ status: 0, error: e.message }};
            }}
        }})()
        """

        print(f"[License:Browser] 通过浏览器 fetch() 发送 license 请求...")
        result = page.evaluate(js_code)

        status = result.get("status", 0)
        if status == 0:
            raise RuntimeError(f"浏览器 fetch 失败: {result.get('error', '未知')}")

        body_b64 = result.get("bodyB64", "")
        response_body = base64.b64decode(body_b64)
        print(f"[License:Browser] 响应状态: {status}, 大小: {len(response_body)} bytes")

        if status != 200:
            preview = response_body[:500].decode("utf-8", errors="replace")
            raise RuntimeError(f"License 服务器返回 {status}。响应: {preview}")

        # pywidevine 解析 license 响应
        cdm.parse_license(cdm_session_id, response_body)

        keys = _extract_content_keys(cdm, cdm_session_id)
        for ck in keys:
            print(f"[License:Browser] 获取密钥 KID:{ck.kid[:16]}... "
                  f"类型:{ck.key_type}")

    finally:
        cdm.close(cdm_session_id)

    return keys


def exchange_license_with_browser_fallback(
    page,
    session: HttpSession,
    license_url: str,
    pssh_data: str,
    drm_auth: DrmAuthToken,
    device_path: str,
    mpd_url: str = "",
) -> List[ContentKey]:
    """Try browser fetch first, then fall back to native POST like the GUI tool."""
    browser_error = None
    try:
        return _exchange_license_via_browser(
            page, session, license_url, pssh_data, drm_auth, device_path)
    except Exception as exc:
        browser_error = exc
        print(f"[Browser] License fetch 路径失败，将尝试原生 POST: {exc}")

    try:
        keys = exchange_widevine_license(
            session, license_url, pssh_data, drm_auth, device_path,
            mpd_url=mpd_url)
        print(f"[License] 原生 POST 路径成功，获得 {len(keys)} 个密钥")
        return keys
    except Exception as native_exc:
        raise RuntimeError(
            f"浏览器 fetch 失败: {browser_error}\n"
            f"原生 POST 失败: {native_exc}") from native_exc


# ---------------------------------------------------------------------------
# 浏览器辅助模式（Playwright fallback）
# ---------------------------------------------------------------------------

def harvest_via_browser(
    page_url: str,
    wvd_path: str = "",
    headless: bool = False,
) -> HarvestResult:
    """
    通过 Playwright + Edge 浏览器收割页面配置和网络请求。
    
    使用 Edge 浏览器（非 Chrome），自动复用现有 Edge 用户数据，
    无需重新登录。支持持久化 Profile，保留 cookie / session。
    
    流程:
    1. 启动 Edge 浏览器（复用用户 Profile），导航到 page_url
    2. 从 HTML 提取静态配置
    3. 自动点击播放按钮，拦截 MPD 和 DRM 鉴权请求
    4. 截获 license server URL（CDM 实际调用的地址）
    5. 用 device.wvd 独立完成 Widevine license 交换
    
    返回:
        HarvestResult
    """
    if not _PLAYWRIGHT_AVAILABLE:
        raise ImportError(
            "playwright 未安装。请执行: pip install playwright && playwright install chromium")

    # 自动检测 .wvd 路径
    if not wvd_path:
        candidates = [
            os.path.join(_SCRIPT_DIR, "device.wvd"),
            os.path.join(_SCRIPT_DIR, ".tools", "device.wvd"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                wvd_path = c
                print(f"[Browser] 自动检测到 WVD: {c}")
                break

    result = HarvestResult()
    captured_responses: Dict[str, Any] = {}
    captured_requests: Dict[str, Any] = {}

    def _handle_response(response):
        url = response.url
        try:
            # DRM 鉴权
            if "get_auth_token_drm" in url:
                body = response.text()
                captured_responses["drm_auth"] = {
                    "url": url, "body": body,
                    "status": response.status}
                print("[Browser] 捕获 DRM 鉴权响应")
            
            # MPD: .mpd 后缀 或 /dash/ 路径 或 Content-Type: application/dash+xml
            content_type = response.headers.get("content-type", "")
            is_mpd = (
                url.endswith(".mpd") or
                ".mpd?" in url or
                "application/dash+xml" in content_type
            )
            if is_mpd:
                try:
                    body = response.text()
                    # 排除 HTML/JSON 误判
                    if body.strip().startswith("<") and "MPD" not in body[:200]:
                        return
                    if body.strip().startswith("{"):
                        return
                    captured_responses["mpd"] = {
                        "url": url, "body": body,
                        "status": response.status}
                    print(f"[Browser] 捕获 MPD ({len(body)} 字符)")
                except Exception:
                    pass
            
            # License server — 拦截 CDM 实际调用的 license 请求
            # Widevine license: POST to .../license or .../presto or .../widevine
            # 也拦截 /vp/{JWT} 类型的 Presto 代理
            if ("license" in url.lower() or "presto" in url.lower()
                    or "widevine" in url.lower()
                    or "/vp/" in url):
                if url not in captured_responses:
                    captured_responses["license_url"] = url
                    print("[Browser] 发现 License URL")
                    # 如果是 POST（实际 license 请求），也捕获请求体
                    try:
                        if response.request.method == "POST":
                            captured_responses["license_method"] = "POST"
                            captured_responses["license_request_headers"] = dict(
                                response.request.headers)
                    except Exception:
                        pass
        except Exception:
            pass

    def _handle_request(request):
        url = request.url
        if "get_auth_token_drm" in url:
            captured_requests["drm_auth"] = url
        # 拦截 MPD 请求
        if url.endswith(".mpd") or ".mpd?" in url:
            captured_requests["mpd"] = url
            try:
                captured_requests["mpd_headers"] = dict(request.headers)
            except Exception:
                captured_requests["mpd_headers"] = {}
            print("[Browser] 拦截 MPD 请求")

    # ================================================================
    # 启动 Edge 浏览器（复用用户现有 Profile，保留登录状态）
    # ================================================================
    with sync_playwright() as p:
        # 使用 Edge 浏览器（channel="msedge"），不是 Chrome
        # 持久化 context 以复用现有 cookie / session
        edge_profile_dir = os.path.join(
            os.environ.get("TEMP", os.path.join(_SCRIPT_DIR, ".playwright")),
            "livetools-edge-profile")
        os.makedirs(edge_profile_dir, exist_ok=True)

        print(f"[Browser] 启动 Microsoft Edge 浏览器...")
        print(f"[Browser] 用户数据目录: {_EDGE_USER_DATA_DIR}")
        print(f"[Browser] 临时 Profile:     {edge_profile_dir}")

        try:
            # 尝试启动 Edge（需要系统安装了 Edge）
            browser = p.chromium.launch(
                channel="msedge",  # 使用 Edge，不是 Chrome
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
        except Exception as exc:
            print(f"[Browser] Edge 启动失败: {exc}")
            print("[Browser] 尝试使用默认 Chromium...")
            browser = p.chromium.launch(headless=headless)

        # 创建 context（带 Edge UA + 宽视口）
        context = browser.new_context(
            user_agent=DEFAULT_UA,
            viewport={"width": 1280, "height": 720},
            locale="ja-JP",  # eplus 是日本站点
        )
        page = context.new_page()

        page.on("response", _handle_response)
        page.on("request", _handle_request)

        print("[Browser] 导航到页面")
        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

        # 先尝试从 HTML 提取
        html = page.content()
        result.page_config = extract_page_config(html, page_url)

        if not result.page_config.is_complete:
            print("[Browser] 首屏 HTML 未包含完整配置，等待页面加载...")
            # 等待可能触发配置加载的用户交互
            page.wait_for_timeout(3000)
            html = page.content()
            result.page_config = extract_page_config(html, page_url)

        if not result.page_config.is_complete:
            print("[Browser] 请在浏览器中完成登录或验证码操作...")
            print("[Browser] 操作完成后按 Enter 继续...")
            input()
            html = page.content()
            result.page_config = extract_page_config(html, page_url)
            page.wait_for_timeout(2000)

        # --- 智能 MPD 发现：自动触发播放 ---
        if not result.page_config.channels_dash and "mpd" not in captured_responses:
            print("\n[Browser] === MPD 未在 HTML 中找到，尝试自动触发播放 ===\n")

            # 策略 1: 点击播放按钮（多种选择器，含 eplus 特定选择器）
            print("[Browser] 策略 1: 点击播放按钮...")
            click_selectors = [
                # eplus / StreamPass 特定选择器
                '.play-button', '.start-button', '.player-start',
                '[class*="Player_start"]', '[class*="player_start"]',
                '.player-area', '.video-player-area',
                # 通用播放按钮
                '[class*="play"]', '[id*="play"]',
                '[class*="Play"]', '[id*="Play"]',
                'button', '[role="button"]',
                '.start-btn', '#start-btn',
                '[data-action="play"]',
                '.video-container', 'video',
                # eplus 页面特有的 class
                '.eplus-player', '[class*="eplus"]',
                '.streaming-player',
            ]
            for selector in click_selectors:
                try:
                    el = page.query_selector(selector)
                    if el and el.is_visible():
                        print(f"[Browser]   点击: {selector}")
                        el.click()
                        page.wait_for_timeout(3000)
                        if "mpd" in captured_responses:
                            break
                except Exception:
                    continue

            # 策略 2: 执行页面中的 JS 播放函数
            if "mpd" not in captured_responses:
                print("[Browser] 策略 2: 尝试 JS 播放调用...")
                try:
                    page.evaluate("""
                        // eplus 通常使用 castlabs/cLabs 播放器
                        if (typeof window.player !== 'undefined' && window.player.play) {
                            window.player.play();
                        }
                        // 尝试调用常见的播放初始化函数
                        if (typeof play === 'function') play();
                        if (typeof startPlayback === 'function') startPlayback();
                        if (typeof initPlayer === 'function') initPlayer();
                        if (typeof startStream === 'function') startStream();
                        // 派发点击事件到可能的播放区域
                        const areas = document.querySelectorAll(
                            '.player-area, .video-container, [class*="player"], video, '
                            + '[class*="Player"], .streaming-area');
                        areas.forEach(a => a.click());
                    """)
                    page.wait_for_timeout(4000)
                except Exception:
                    pass

            # 策略 3: 如果有 app_id，尝试通过页面 JS 直接获取 MPD URL
            if "mpd" not in captured_responses and result.page_config.app_id:
                print("[Browser] 策略 3: 尝试从页面 JS 上下文中提取 MPD URL...")
                try:
                    mpd_urls = page.evaluate("""
                        const urls = [];
                        // 搜索全局变量中的 MPD URL
                        for (const key of Object.keys(window)) {
                            try {
                                const val = window[key];
                                if (typeof val === 'string' && val.includes('.mpd')) {
                                    urls.push(val);
                                }
                                if (val && typeof val === 'object') {
                                    for (const k2 of Object.keys(val)) {
                                        const v2 = val[k2];
                                        if (typeof v2 === 'string' && v2.includes('.mpd')) {
                                            urls.push(v2);
                                        }
                                    }
                                }
                            } catch(e) {}
                        }
                        return urls;
                    """)
                    if mpd_urls:
                        for u in mpd_urls:
                            print("[Browser]   从 JS 上下文发现 MPD")
                            captured_responses["mpd"] = {
                                "url": u, "body": "", "status": 0}
                            break
                except Exception:
                    pass

            if "mpd" in captured_responses:
                print(f"[Browser] ✓ MPD 已通过自动播放获取！")
            else:
                print("[Browser] ⚠ 自动播放未触发 MPD 请求。")
                print("[Browser] 请在 Edge 浏览器窗口中手动点击播放按钮，然后按 Enter...")
                input()
                page.wait_for_timeout(3000)
                # 再给一次机会：重新扫描 HTML（播放后可能有新元素）
                html2 = page.content()
                result.page_config = extract_page_config(html2, page_url)

        # 处理捕获的 DRM 鉴权响应
        if "drm_auth" in captured_responses:
            auth_data = captured_responses["drm_auth"]
            try:
                auth_json = json.loads(auth_data["body"])
                result.drm_auth = DrmAuthToken(raw=auth_json)
                data = auth_json.get("data", auth_json)
                result.drm_auth.auth_token = str(
                    data.get("auth_token", data.get("authToken", "")))
                result.drm_auth.session_id = str(
                    data.get("sessionId", data.get("session_id", "")))
                result.drm_auth.user_id = str(
                    data.get("user_id", data.get("userId", "")))
                result.drm_auth.merchant_id = str(
                    data.get("merchantId", data.get("merchant_id", "")))
                print(f"[Browser] DRM 鉴权成功，auth_token 长度: "
                      f"{len(result.drm_auth.auth_token)}")
            except json.JSONDecodeError:
                result.warnings.append("DRM 鉴权响应非 JSON")

        # 处理捕获的 MPD — 如果只捕获了 URL 没有 body，自己请求
        # 先创建 session（用于自行获取 MPD）
        initial_cookie_str = cookies_to_header(context.cookies([page_url]))
        session = HttpSession(cookie=initial_cookie_str) if initial_cookie_str else HttpSession()

        if "mpd" in captured_responses:
            mpd_data = captured_responses["mpd"]
            result.mpd_url = mpd_data["url"]
            result.mpd_content = mpd_data.get("body", "")
            base_headers = {"User-Agent": DEFAULT_UA, "Referer": page_url}
            base_headers.update(captured_requests.get("mpd_headers") or {})
            result.source_headers = browser_source_headers(
                context, [page_url, result.mpd_url], base_headers)
            session = HttpSession(
                cookie=result.source_headers.get("Cookie", ""),
                headers=result.source_headers)
            if not result.mpd_content:
                # 从拦截的 URL 自行获取 MPD 内容
                print("[Browser] 自行获取 MPD 内容")
                try:
                    result.mpd_content = session.get(result.mpd_url)
                except Exception as exc:
                    result.warnings.append(f"获取 MPD 内容失败: {exc}")
            print(f"[Browser] 获取 MPD: {len(result.mpd_content)} 字符")
        elif result.page_config.primary_mpd_url:
            result.mpd_url = result.page_config.primary_mpd_url
            result.source_headers = browser_source_headers(
                context, [page_url, result.mpd_url],
                {"User-Agent": DEFAULT_UA, "Referer": page_url})
            session = HttpSession(
                cookie=result.source_headers.get("Cookie", ""),
                headers=result.source_headers)
            # 尝试获取 MPD 内容
            try:
                result.mpd_content = session.get(result.mpd_url)
                print(f"[Browser] 从配置 URL 获取 MPD: {len(result.mpd_content)} 字符")
            except Exception:
                pass

        # License 交换
        license_url = captured_responses.get("license_url", "")
        if not license_url:
            license_url = result.page_config.presto_license
        print(f"[Browser] License URL: {'已获取' if license_url else '(未找到)'}")

        if license_url and wvd_path and result.mpd_content:
            print("[Browser] 开始 Widevine license 交换...")
            pssh = extract_pssh_from_mpd(result.mpd_content)
            result.pssh_data = pssh
            if not pssh:
                kid = _extract_first_kid_from_mpd(result.mpd_content)
                if kid:
                    pssh_box = build_pssh_box(kid)
                    result.pssh_data = base64.b64encode(pssh_box).decode("ascii")
                    pssh = result.pssh_data
                    print(f"[Browser] 从 KID {kid[:16]}... 构造 PSSH")

            license_url = resolve_license_url(license_url)
            if pssh:
                write_manual_license_bundle(result, license_url)
            if pssh:
                try:
                    result.keys = exchange_license_with_browser_fallback(
                        page, session, license_url, pssh,
                        result.drm_auth, wvd_path, mpd_url=result.mpd_url)
                    clear_manual_license_bundle(result)
                    print(f"[Browser] ✓ License 交换成功，获得 {len(result.keys)} 个密钥")
                except Exception as exc:
                    result.warnings.append(f"License 交换失败: {exc}")
                    print(f"[Browser] ✗ License 交换失败: {exc}")
            else:
                result.warnings.append("MPD 中未找到 Widevine PSSH 或 KID")
                print("[Browser] ✗ MPD 中未找到 PSSH/KID")
        elif not wvd_path:
            result.warnings.append("未提供 .wvd 设备文件，无法交换许可证。")
        elif not result.mpd_content:
            result.warnings.append("未捕获到 MPD 内容，无法交换许可证。")

        if result.keys:
            print("[Browser] License 已完成，关闭浏览器。")
            browser.close()
            return result

        # ================================================================
        # 保持浏览器打开 — 不自动关闭，用户需要手动复制 DevTools 数据
        # ================================================================
        print("\n" + "=" * 60)
        print("[Browser] 浏览器保持打开，请勿关闭。")
        print("[Browser] 你可以从 DevTools → Network 面板复制请求头。")
        print("[Browser] 完成后按 Enter 关闭浏览器...")
        print("=" * 60)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        browser.close()

    return result


# ---------------------------------------------------------------------------
# 主收割流程（Direct HTTP 模式）
# ---------------------------------------------------------------------------

def harvest(
    page_url: str,
    cookie: str = "",
    headers: Optional[Dict[str, str]] = None,
    wvd_path: str = "",
    channel_id: str = "",
    use_browser: bool = False,
    browser_headless: bool = False,
) -> HarvestResult:
    """
    一键收割：页面 URL → MPD 与内容密钥。
    
    流程:
      1. 获取页面 HTML
      2. 提取内嵌配置 (app_id, channels_dash, drm_asset_id, presto_license)
      3. 调用 DRM 鉴权接口获取令牌
      4. 获取 MPD 内容
      5. 提取 PSSH，交换 Widevine 许可证获取内容密钥
    
    参数:
        page_url: 直播页面 URL
        cookie: 登录会话 cookie（用于鉴权）
        headers: 额外的 HTTP 请求头
        wvd_path: .wvd Widevine 设备文件路径
        channel_id: 频道 ID（留空则使用 drm_asset_id）
        use_browser: 是否使用浏览器辅助模式
        browser_headless: 浏览器是否无头模式
    
    返回:
        HarvestResult
    """
    if use_browser:
        return harvest_via_browser(page_url, wvd_path, browser_headless)

    result = HarvestResult()
    base_url = "{scheme}://{netloc}".format(
        scheme=urllib.parse.urlparse(page_url).scheme,
        netloc=urllib.parse.urlparse(page_url).netloc)

    session = HttpSession(cookie=cookie, headers=headers)
    result.source_headers = source_manifest_headers(cookie, headers)

    # --- 步骤 1: 获取页面 HTML ---
    print("[Harvest] 步骤 1/5: 获取页面")
    try:
        html = session.get(page_url)
        print(f"[Harvest] 页面大小: {len(html)} 字符")
    except Exception as exc:
        raise RuntimeError(f"无法获取页面: {exc}") from exc

    # --- 步骤 2: 提取配置 ---
    print("[Harvest] 步骤 2/5: 提取内嵌配置...")
    result.page_config = extract_page_config(html, page_url)
    pc = result.page_config
    print(f"  app_id:        {pc.app_id or '(未找到)'}")
    print(f"  channels_dash: {len(pc.channels_dash)} 个 MPD")
    print(f"  drm_asset_id:  {pc.drm_asset_id or '(未找到)'}")
    print(f"  prestoLicense: {'已找到' if pc.presto_license else '(未找到)'}")

    if not pc.app_id:
        result.warnings.append("未找到 app_id，无法进行 DRM 鉴权。"
                               " 可能需要使用 --browser 模式。")
    if not pc.channels_dash:
        result.warnings.append(
            "未找到 channels_dash / MPD URL。"
            " 页面的 MPD 可能在点击播放按钮后才动态加载。"
            " 请尝试: --browser 模式（自动点击播放并拦截网络请求）。"
        )
        # 额外诊断：检查是否有 .mpd URL 的任何痕迹
        mpd_hints = re.findall(r'\.mpd', html)
        dash_hints = re.findall(r'(?:dash|manifest|mpd)', html, re.I)
        if mpd_hints:
            print(f"  [诊断] 页面中发现 {len(mpd_hints)} 处 '.mpd' 引用，但未提取到完整 URL。")
            print(f"  [诊断] 可能 MPD URL 是通过 JS 动态拼接的。使用 --browser 获取。")
        elif dash_hints:
            print(f"  [诊断] 页面中发现 {len(dash_hints)} 处 dash/manifest 关键词。")
        else:
            print(f"  [诊断] 页面中未发现任何 MPD/DASH 关键词。")

    # --- 步骤 3: DRM 鉴权 ---
    if pc.app_id and (pc.drm_asset_id or channel_id):
        cid = channel_id or pc.drm_asset_id
        print(f"[Harvest] 步骤 3/5: DRM 鉴权 (app_id={pc.app_id}, channel_id={cid})...")
        try:
            result.drm_auth = request_drm_auth_token(
                session, pc.app_id, cid, base_url)
            print(f"  auth_token 长度:  {len(result.drm_auth.auth_token)}")
            print(f"  sessionId 长度:   {len(result.drm_auth.session_id)}")
            print(f"  user_id 长度:     {len(result.drm_auth.user_id)}")
            print(f"  merchantId 长度:  {len(result.drm_auth.merchant_id)}")
            # 输出响应结构（字段名与长度，不输出值）
            print(f"  响应顶层结构: {_describe_json_structure(result.drm_auth.raw)}")
        except Exception as exc:
            result.warnings.append(f"DRM 鉴权失败: {exc}")
            print(f"  [警告] {exc}")
    else:
        result.warnings.append("跳过 DRM 鉴权（缺少 app_id 或 channel_id）")

    # --- 步骤 4: 获取 MPD ---
    result.mpd_url = pc.primary_mpd_url
    if result.mpd_url:
        print(f"[Harvest] 步骤 4/5: 获取 MPD...")
        try:
            result.mpd_content = session.get(result.mpd_url)
            print(f"  MPD 大小: {len(result.mpd_content)} 字符")
            pssh = extract_pssh_from_mpd(result.mpd_content)
            result.pssh_data = pssh
            print(f"  PSSH: {'有' if pssh else '(未找到，将尝试从 KID 构造)'} "
                  f"({'长度 ' + str(len(pssh)) if pssh else ''})")
        except Exception as exc:
            result.warnings.append(f"获取 MPD 失败: {exc}")
            print(f"  [警告] {exc}")

    # --- 步骤 5: Widevine 许可证交换 ---
    if result.mpd_content and pc.presto_license and wvd_path:
        print(f"[Harvest] 步骤 5/5: Widevine 许可证交换...")
        try:
            # 如果没有从 MPD 提取到 PSSH，尝试从 KID 构造
            if not result.pssh_data:
                kid = _extract_first_kid_from_mpd(result.mpd_content)
                if kid:
                    pssh_box = build_pssh_box(kid)
                    result.pssh_data = base64.b64encode(pssh_box).decode("ascii")
                    print(f"  从 KID {kid[:16]}... 构造了 PSSH")
                else:
                    raise ValueError("MPD 中既无 PSSH 也无 KID")

            write_manual_license_bundle(result, pc.presto_license)
            result.keys = exchange_widevine_license(
                session, pc.presto_license,
                result.pssh_data, result.drm_auth,
                wvd_path, mpd_url=result.mpd_url)
            clear_manual_license_bundle(result)
            print(f"  成功获取 {len(result.keys)} 个内容密钥")
        except Exception as exc:
            result.warnings.append(f"License 交换失败: {exc}")
            print(f"  [警告] {exc}")
    elif not wvd_path:
        result.warnings.append("未提供 --wvd 设备文件，跳过许可证交换。"
                               " 可先完成收割获得 MPD 与 PSSH，稍后手动取钥。")
    elif not pc.presto_license:
        result.warnings.append("未找到 prestoLicense，无法交换许可证。"
                               " 可能需要使用 --browser 模式。")

    return result


def _extract_first_kid_from_mpd(mpd_content: str) -> str:
    """从 MPD 提取第一个 default_KID。"""
    try:
        root = ET.fromstring(mpd_content)
    except ET.ParseError:
        return ""
    for element in root.iter():
        for name, value in element.attrib.items():
            if _xml_local_name(name).lower() != "default_kid":
                continue
            candidate = value.strip().split()[0]
            if re.fullmatch(r"[0-9a-fA-F-]{32,36}", candidate):
                return candidate
    return ""


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _safe_parse_json(text: str) -> Dict[str, Any]:
    """安全解析 JSON，失败时返回空字典。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text[:500]}


def _describe_json_structure(obj: Any, max_depth: int = 3) -> str:
    """
    描述 JSON 对象的结构（仅字段名与值类型/长度，不输出值内容）。
    用于协议逆向验证输出。
    """
    if max_depth <= 0:
        return "..."
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            if isinstance(v, str):
                parts.append(f"{k}:str({len(v)})")
            elif isinstance(v, (int, float)):
                parts.append(f"{k}:num({v})")
            elif isinstance(v, bool):
                parts.append(f"{k}:bool({v})")
            elif isinstance(v, list):
                parts.append(f"{k}:list({len(v)})["
                             f"{_describe_json_structure(v[0], max_depth-1) if v else ''}]")
            elif isinstance(v, dict):
                parts.append(f"{k}:{{{_describe_json_structure(v, max_depth-1)}}}")
            elif v is None:
                parts.append(f"{k}:null")
            else:
                parts.append(f"{k}:{type(v).__name__}")
        return ", ".join(parts)
    elif isinstance(obj, list):
        return f"list({len(obj)})"
    elif isinstance(obj, str):
        return f"str({len(obj)})"
    else:
        return type(obj).__name__


def _find_key_tool_exe() -> str:
    """查找 eplus DRM key 获取工具。"""
    candidates = [
        os.path.join(_SCRIPT_DIR, "eplus DRM key获取工具.exe"),
        os.path.join(_SCRIPT_DIR, "EPLUSD~1.EXE"),  # 8.3 短名
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def _manual_key_fallback(result: HarvestResult, args) -> None:
    """手动取钥：启动 eplus DRM key 获取工具 GUI，让用户粘贴密钥。"""
    if not result.mpd_url:
        return

    print("\n" + "-" * 60)
    print("[手动取钥] 启动 eplus DRM key 获取工具...")
    exe_path = _find_key_tool_exe()
    page_url_for_exe = args.page_url

    if exe_path:
        print(f"[手动取钥] 工具: {exe_path}")
        print(f"[手动取钥] URL:  {page_url_for_exe}")
        try:
            subprocess.Popen(
                [exe_path, page_url_for_exe],
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            print("[手动取钥] ✓ 工具已启动，请在 GUI 中获取密钥。")
        except Exception as exc:
            print(f"[手动取钥] 无法启动工具: {exc}")
            print(f"[手动取钥] 请手动运行: {exe_path}")
    else:
        print("[手动取钥] 未找到 eplus DRM key获取工具.exe，请手动运行。")

    print("[手动取钥] 获取密钥后，按 KID:KEY 格式粘贴（每行一对，空行结束）:")
    print("[手动取钥] 格式: KKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKK:KKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKK")

    while True:
        try:
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        if ':' in line and 64 <= len(line) <= 70:
            parts = line.split(":", 1)
            if len(parts) == 2 and len(parts[0].strip()) == 32 and len(parts[1].strip()) == 32:
                result.keys.append(ContentKey(
                    kid=parts[0].strip().lower(),
                    key=parts[1].strip().lower()))
                print(f"  ✓ 已接收 KID:{parts[0][:12]}...")
                continue
        print(f"  ✗ 格式无效（应为各32位hex的 KID:KEY），跳过")

    if result.keys:
        print(f"[手动取钥] ✓ 共接收 {len(result.keys)} 对密钥")


# ---------------------------------------------------------------------------
# CLI 集成（供 HLS_Stream_Interactive.py 调用）
# ---------------------------------------------------------------------------

def build_harvest_argument_parser(parser=None):
    """向现有 ArgumentParser 添加页面收割相关参数。"""
    import argparse
    if parser is None:
        parser = argparse.ArgumentParser(add_help=False)
    
    harvest_group = parser.add_argument_group("页面自动收割（新）")
    harvest_group.add_argument(
        "--page-url",
        help="直播页面 URL。从页面自动发现 MPD 与 DRM 配置。")
    harvest_group.add_argument(
        "--cookie", "--auth-cookie",
        dest="auth_cookie",
        help="登录后的 Cookie 字符串（用于页面访问与 DRM 鉴权）。"
             " 也接受 Cookie: name=value 格式。")
    harvest_group.add_argument(
        "--wvd",
        dest="wvd_path",
        help="Widevine .wvd 设备文件路径（用于 license 交换）。")
    harvest_group.add_argument(
        "--channel-id",
        help="频道 ID（覆盖从页面提取的 drm_asset_id）。")
    harvest_group.add_argument(
        "--browser",
        action="store_true",
        dest="use_browser",
        help="使用 Playwright 浏览器辅助模式（需要登录/验证码时）。")
    harvest_group.add_argument(
        "--browser-headless",
        action="store_true",
        dest="browser_headless",
        help="浏览器无头模式（仅已登录会话）。")
    
    return parser


def run_harvest_and_push(args) -> int:
    """
    从页面收割到推流的完整管线。
    
    由 HLS_Stream_Interactive.py 的 main() 在检测到 --page-url 时调用。
    返回推流子进程的退出码。
    """
    page_url = args.page_url
    if not page_url:
        print("[错误] 需要 --page-url")
        return 1
    requested_operation = (getattr(args, "operation", "") or "").strip().lower()
    stream_name = getattr(args, "stream_name", "") or ""
    should_push = requested_operation == "push" or bool(stream_name.strip())

    # 解析 cookie 输入
    cookie = args.auth_cookie or ""
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    
    headers = {}
    for h in getattr(args, "header", []) or []:
        if ":" in h:
            name, value = h.split(":", 1)
            headers[name.strip()] = value.strip()

    # --- 自动检测 device.wvd ---
    wvd_path = getattr(args, "wvd_path", "") or ""
    if not wvd_path:
        wvd_candidates = [
            os.path.join(_SCRIPT_DIR, "device.wvd"),
            os.path.join(_SCRIPT_DIR, ".tools", "device.wvd"),
        ]
        for c in wvd_candidates:
            if os.path.isfile(c):
                wvd_path = c
                if not should_push:
                    print(f"[自动检测] WVD 设备文件: {c}")
                break

    # --- 自动检测工具路径 ---
    downloader_path = getattr(args, "downloader_path", "") or ""
    if not downloader_path:
        dl_candidates = [
            os.path.join(_SCRIPT_DIR, ".tools", "N_m3u8DL-RE.exe"),
            os.path.join(_SCRIPT_DIR, "N_m3u8DL-RE.exe"),
        ]
        for c in dl_candidates:
            if os.path.isfile(c):
                downloader_path = c
                if not should_push:
                    print(f"[自动检测] N_m3u8DL-RE: {c}")
                break

    ffmpeg_path = getattr(args, "ffmpeg_path", "") or ""
    if not ffmpeg_path:
        ffmpeg_candidates = [
            os.path.join(_SCRIPT_DIR, ".tools", "ffmpeg.exe"),
            "ffmpeg.exe",
        ]
        for c in ffmpeg_candidates:
            resolved = shutil.which(c) if not os.path.isabs(c) else c
            if resolved and os.path.isfile(resolved):
                ffmpeg_path = resolved
                if not should_push:
                    print(f"[自动检测] FFmpeg: {resolved}")
                break

    mp4decrypt_path = getattr(args, "mp4decrypt_path", "") or ""
    if not mp4decrypt_path:
        mp4decrypt_candidates = [
            os.path.join(_SCRIPT_DIR, ".tools", "mp4decrypt.exe"),
            os.path.join(_SCRIPT_DIR, "mp4decrypt.exe"),
            "mp4decrypt.exe",
        ]
        for c in mp4decrypt_candidates:
            resolved = shutil.which(c) if not os.path.isabs(c) else c
            if resolved and os.path.isfile(resolved):
                mp4decrypt_path = resolved
                if not should_push:
                    print(f"[自动检测] mp4decrypt: {resolved}")
                break

    # 执行收割
    if should_push:
        print("[收割] 开始发现 MPD 和内容密钥...")
    else:
        print("\n" + "=" * 60)
        print("LiveTools 页面自动收割模式")
        print("=" * 60)
        print("页面 URL:  已提供")
        if cookie:
            print("Cookie:    已提供")
        print(f"WVD:       {wvd_path or '(未提供)'}")
        print(f"N_m3u8DL-RE: {downloader_path or '(自动查找)'}")
        print(f"FFmpeg:    {ffmpeg_path or '(自动查找)'}")
        print(f"mp4decrypt: {mp4decrypt_path or '(未找到，回退 FFmpeg 解密)'}")
        print("-" * 60)

    try:
        result = harvest(
            page_url=page_url,
            cookie=cookie,
            headers=headers,
            wvd_path=wvd_path,
            channel_id=getattr(args, "channel_id", ""),
            use_browser=getattr(args, "use_browser", False),
            browser_headless=getattr(args, "browser_headless", False),
        )
    except Exception as exc:
        print(f"\n[错误] 收割失败: {exc}")
        return 1

    # 输出收割结果摘要
    if should_push and result.success:
        print(f"[收割] MPD 和 {len(result.keys)} 个内容密钥已获取。")
        for w in result.warnings:
            print(f"[警告] {w}")
    else:
        print("\n" + "=" * 60)
        print("收割结果")
        print("=" * 60)
        print(f"  MPD URL:     {'已获取' if result.mpd_url else '(未获取)'}")
        print(f"  内容密钥:    {len(result.keys)} 个")
        for i, key in enumerate(result.keys):
            print(f"    [{i+1}] KID: {key.kid[:16]}... 类型: {key.key_type or 'CONTENT'}")
        if result.kid_key_pairs:
            print(f"  KID:KEY 对:  {len(result.kid_key_pairs)} 对（可用于推流）")
        for w in result.warnings:
            print(f"  [警告] {w}")
        print("-" * 60)

    if not result.success:
        print("\n[信息] 收割未获得完整密钥，无法自动推流。")
        print_manual_license_bundle(
            result,
            result.page_config.presto_license or DRMTODAY_WIDEVINE_LICENSE_URL)

        # --- 手动取钥通道 ---
        _manual_key_fallback(result, args)

    # 如果仍然没有密钥，退出
    if not result.keys:
        return 0 if result.mpd_url else 1

    if not should_push:
        print("\n[密钥] 以下 KID:KEY 对可直接用于现有推流管线:")
        for pair in result.kid_key_pairs:
            print(f"  {pair}")
        print("\n[信息] 收割完成。需要自动推流时请提供 --stream-name <stream_name> 或 --operation push")
        return 0

    # 写入临时密钥文件
    key_file = None
    mpd_cache_path = ""
    try:
        key_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".private.txt",
            prefix="livetools_harvest_", delete=False,
            encoding="ascii")
        key_file.write("\n".join(result.kid_key_pairs) + "\n")
        key_file.flush()
        key_path = key_file.name
        key_file.close()
        if not should_push:
            print("\n[信息] 密钥临时文件已准备")
    except Exception as exc:
        print(f"[警告] 无法写入密钥文件: {exc}")
        key_path = ""
        if key_file:
            try:
                key_file.close()
            except OSError:
                pass

    if result.mpd_content:
        try:
            mpd_cache = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".mpd",
                prefix="livetools_manifest_",
                delete=False,
                encoding="utf-8")
            with mpd_cache:
                mpd_cache.write(result.mpd_content)
            mpd_cache_path = mpd_cache.name
            if not should_push:
                print("[信息] MPD 缓存已准备")
        except Exception as exc:
            print(f"[警告] 无法写入 MPD 缓存，将回退到远程读取: {exc}")

    # 构造推流参数
    push_args = [
        sys.executable, __file__.replace("page_harvester.py", "HLS_Stream_Interactive.py"),
        "--source", result.mpd_url,
        "--operation", "push",
    ]
    if mpd_cache_path:
        push_args.extend(["--manifest-content-file", mpd_cache_path])
    
    quality = getattr(args, "quality", "") or "best"
    push_args.extend(["--quality", quality])
    
    if stream_name:
        push_args.extend(["--stream-name", stream_name])
    
    push_url = getattr(args, "push_url", "") or ""
    if push_url:
        push_args.extend(["--push-url", push_url])

    # 传递源站清单请求头。浏览器模式下 Cookie 来自 Edge context，
    # 直接模式下来自 --auth-cookie / --header。
    forwarded_headers = dict(result.source_headers)
    for h in (getattr(args, "header", []) or []):
        if ":" in h:
            name, value = h.split(":", 1)
            forwarded_headers[name.strip()] = value.strip()
    for name, value in forwarded_headers.items():
        if value:
            push_args.extend(["--header", f"{name}: {value}"])

    # 传递密钥文件
    if key_path:
        push_args.extend(["--drm-key-file", key_path])
    
    # 传递工具路径（优先使用命令行参数，其次自动检测的）  
    if downloader_path:
        push_args.extend(["--n-m3u8dl-re", downloader_path])
    if ffmpeg_path:
        push_args.extend(["--ffmpeg-binary", ffmpeg_path])
    if mp4decrypt_path:
        push_args.extend(["--mp4decrypt-binary", mp4decrypt_path])

    relay_restarts = getattr(args, "relay_restarts", None)
    if relay_restarts is not None:
        push_args.extend(["--relay-restarts", str(relay_restarts)])
    
    relay_delay = getattr(args, "relay_restart_delay", None)
    if relay_delay is not None:
        push_args.extend(["--relay-restart-delay", str(relay_delay)])
    
    live_count = getattr(args, "live_take_count", None)
    if live_count is not None:
        push_args.extend(["--live-take-count", str(live_count)])

    print("\n[执行] 启动推流子进程...")

    try:
        proc = subprocess.run(push_args)
        return proc.returncode
    except KeyboardInterrupt:
        print("\n[中断] 用户停止推流。")
        return 130
    finally:
        # 清理临时密钥文件（推流已启动，密钥已读取）
        if key_path and os.path.isfile(key_path):
            try:
                os.unlink(key_path)
                print("[清理] 已删除临时密钥文件")
            except OSError:
                pass
        if mpd_cache_path and os.path.isfile(mpd_cache_path):
            try:
                os.unlink(mpd_cache_path)
                print("[清理] 已删除 MPD 缓存")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 独立测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("page_harvester 模块 - 独立测试")
    print("=" * 60)
    print("用途: LiveTools 页面自动收割 → MPD + 内容密钥")
    print("集成方式: python HLS_Stream_Interactive.py --page-url <URL> ...")
    print()
    print("依赖:")
    print("  pip install requests          # HTTP 会话（推荐）")
    print("  pip install pywidevine        # Widevine 许可证交换")
    print("  pip install playwright        # 浏览器辅助模式")
    print("  playwright install chromium")
    print()
    print("使用方法:")
    print("  python HLS_Stream_Interactive.py \\")
    print("    --page-url 'https://example.com/live/123' \\")
    print("    --auth-cookie 'session=xxx' \\")
    print("    --wvd device.wvd \\")
    print("    --stream-name <stream_name>")
    print()
    print("浏览器辅助模式（需要登录/验证码时）:")
    print("  python HLS_Stream_Interactive.py \\")
    print("    --page-url 'https://example.com/live/123' \\")
    print("    --wvd device.wvd \\")
    print("    --browser \\")
    print("    --stream-name <stream_name>")
