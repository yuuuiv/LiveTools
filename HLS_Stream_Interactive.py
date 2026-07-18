import re
import os
import subprocess
import sys
import argparse
import shlex
import urllib.request
import urllib.parse
import time
import hashlib
import shutil # 用于处理临时目录和文件移动
import asyncio # 用于异步下载
import tempfile
import xml.etree.ElementTree as ET

# 页面自动收割模块（可选依赖）
try:
    from page_harvester import (
        build_harvest_argument_parser,
        run_harvest_and_push,
        harvest,
        HarvestResult,
        PageConfig,
        DrmAuthToken,
        ContentKey,
        extract_page_config,
        request_drm_auth_token,
        HttpSession,
    )
    _HARVEST_AVAILABLE = True
except ImportError:
    _HARVEST_AVAILABLE = False

# --- 阿里云直播鉴权函数 (A 类鉴权) ---

def md5sum(src):
    """计算字符串的 MD5 哈希值"""
    m = hashlib.md5()
    m.update(src)
    return m.hexdigest()

def a_auth(uri, key, exp):
    """
    生成阿里云视频直播 A 类鉴权的 URL。
    
    参数:
        uri: 原始 RTMP 推流地址 (例如: rtmp://push.domain.com/app/stream)
        key: 阿里云后台配置的鉴权主 KEY
        exp: 过期时间的 UNIX 时间戳 (秒)
    
    返回:
        带 auth_key 参数的完整推流 URL
    """
    p = re.compile(r"^(rtmp://)?([^/?]+)(/[^?]*)?(\?.*)?$")
    if not p:
        return None
    m = p.match(uri)
    scheme, host, path, args = m.groups()
    if not scheme: scheme = "rtmp://"
    if not path: path = "/"
    if not args: args = ""
    
    rand = "0"      # "0" by default, other value is ok
    uid = "0"       # "0" by default, other value is ok
    sstring = "%s-%s-%s-%s-%s" % (path, exp, rand, uid, key)
    hashvalue = md5sum(sstring.encode('utf-8'))
    auth_key = "%s-%s-%s-%s" % (exp, rand, uid, hashvalue)
    
    if args:
        return "%s%s%s%s&auth_key=%s" % (scheme, host, path, args, auth_key)
    else:
        return "%s%s%s%s?auth_key=%s" % (scheme, host, path, args, auth_key)

# --- 核心数据结构 ---

class VideoStream:
    """定义一个类来存储解析出的视频流信息"""
    def __init__(self, resolution, bandwidth, url, headers=None,
                 manifest_type="hls", video_index=None, codecs=None,
                 drm_info=None, relay_manifest_source=None):
        self.resolution = resolution
        self.bandwidth = bandwidth
        self.url = url
        self.headers = headers or {}
        self.manifest_type = manifest_type
        self.video_index = video_index
        self.codecs = codecs or "N/A"
        self.drm_info = drm_info or empty_drm_info()
        self.relay_manifest_source = relay_manifest_source
    
    def __str__(self):
        return f"分辨率: {self.resolution} | 码率: {self.bandwidth} | URL: {safe_source_label(self.url)[:60]}..."


DASH_DRM_SYSTEMS = {
    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "Google Widevine",
    "9a04f079-9840-4286-ab92-e65be0885f95": "Microsoft PlayReady",
    "94ce86fb-07ff-4f43-adb8-93d2fa968ca2": "Apple FairPlay",
    "e2719d58-a985-b3c9-781a-b030af78d30e": "Clear Key",
}

DRM_KEY_ENV_DEFAULT = "LIVETOOLS_DRM_KEY"
DRM_KEY_PAIR_PATTERN = re.compile(
    r'^(?P<kid>[0-9a-fA-F-]{32,36}):(?P<key>[0-9a-fA-F]{32})$')
DRM_BARE_KEY_PATTERN = re.compile(r'^[0-9a-fA-F]{32}$')
HTTP_HEADER_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
HEADER_SPLIT_PATTERN = re.compile(
    r'\r?\n|(?=\b(?:Cookie|Referer|Referrer|Authorization|User-Agent|Origin|'
    r'Accept(?:-[A-Za-z-]+)?|Range|Sec-[A-Za-z-]+):)',
    re.I,
)
HEADER_CANONICAL_NAMES = {
    'cookie': 'Cookie',
    'referer': 'Referer',
    'referrer': 'Referer',
    'authorization': 'Authorization',
    'user-agent': 'User-Agent',
    'origin': 'Origin',
    'accept': 'Accept',
    'accept-language': 'Accept-Language',
    'accept-encoding': 'Accept-Encoding',
    'range': 'Range',
}
DEFAULT_DOWNLOAD_CONCURRENCY = 8
ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def empty_drm_info():
    return {
        "protected": False,
        "schemes": [],
        "systems": [],
        "key_ids": [],
    }


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def sanitize_filename(value, fallback="output"):
    cleaned = re.sub(r'[<>:"/|?*\\]', '_', value or '')
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '_', cleaned).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned or fallback


def cleanup_temp_dir(temp_dir):
    try:
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir)
            print(f"\n[清理] 已移除临时目录: {temp_dir}")
    except Exception as e:
        print(f"[警告] 无法自动清理临时目录，请手动删除: {temp_dir} ({e})")


def normalize_header_name(name):
    lowered = name.strip().lower()
    return HEADER_CANONICAL_NAMES.get(lowered, '-'.join(
        part.capitalize() for part in lowered.split('-')))


def xml_local_name(name):
    """Return an XML tag/attribute local name without its namespace."""
    return name.rsplit('}', 1)[-1].rsplit(':', 1)[-1]


def detect_mpd_drm(mpd_content):
    """Identify standard DASH ContentProtection declarations without decoding PSSH data."""
    root = ET.fromstring(mpd_content)
    schemes = set()
    systems = set()
    key_ids = set()

    for element in root.iter():
        if xml_local_name(element.tag) != 'ContentProtection':
            continue

        scheme_uri = (element.attrib.get('schemeIdUri') or '').strip().lower()
        value = (element.attrib.get('value') or '').strip().lower()

        if scheme_uri == 'urn:mpeg:dash:mp4protection:2011' and value:
            schemes.add(value)

        uuid_match = re.fullmatch(r'urn:uuid:([0-9a-f-]{36})', scheme_uri)
        if uuid_match:
            system_id = uuid_match.group(1)
            systems.add(DASH_DRM_SYSTEMS.get(system_id, f"未知 DRM ({system_id})"))

        for attribute_name, attribute_value in element.attrib.items():
            if xml_local_name(attribute_name).lower() == 'default_kid' and attribute_value:
                key_ids.add(attribute_value.upper())

    return {
        "protected": bool(schemes or systems or key_ids),
        "schemes": sorted(schemes),
        "systems": sorted(systems),
        "key_ids": sorted(key_ids),
    }


def drm_scheme_description(scheme):
    descriptions = {
        "cenc": "MPEG-CENC AES-CTR",
        "cbcs": "MPEG-CBCS AES-CBC pattern",
        "cbc1": "MPEG-CENC AES-CBC",
        "cens": "MPEG-CENC AES-CTR pattern",
    }
    return descriptions.get(scheme.lower(), scheme)


def format_drm_summary(drm_info):
    if not drm_info.get('protected'):
        return "未检测到 DASH ContentProtection"

    parts = []
    if drm_info.get('schemes'):
        parts.append("加密方案: " + ', '.join(
            drm_scheme_description(value) for value in drm_info['schemes']))
    if drm_info.get('systems'):
        parts.append("DRM 系统: " + ', '.join(drm_info['systems']))
    if drm_info.get('key_ids'):
        parts.append("默认 KID: " + ', '.join(drm_info['key_ids']))
    return "；".join(parts)


def normalize_drm_kid(value):
    """Normalize a DASH KID to the form used by N_m3u8DL-RE key files."""
    normalized = (value or '').replace('-', '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{32}', normalized):
        raise ValueError("DRM KID 必须是 16 字节十六进制值")
    return normalized


def normalize_drm_key_material(value, required_kids):
    """Validate key text without returning or logging any unredacted input."""
    required = [normalize_drm_kid(kid) for kid in required_kids]
    entries = [entry.strip() for entry in re.split(r'[\r\n,;]+', value or '')
               if entry.strip()]
    if not entries:
        raise ValueError("没有读取到 DRM 密钥")

    pairs = {}
    bare_keys = []
    for entry in entries:
        match = DRM_KEY_PAIR_PATTERN.fullmatch(entry)
        if match:
            kid = normalize_drm_kid(match.group('kid'))
            pairs[kid] = match.group('key').lower()
        elif DRM_BARE_KEY_PATTERN.fullmatch(entry):
            bare_keys.append(entry.lower())
        else:
            raise ValueError("DRM 密钥格式应为 KID:KEY，二者均为 16 字节十六进制值")

    if bare_keys:
        if len(required) != 1 or len(bare_keys) != 1 or pairs:
            raise ValueError("省略 KID 的密钥格式只适用于清单中恰好一个 KID")
        pairs[required[0]] = bare_keys[0]

    missing = [kid for kid in required if kid not in pairs]
    if missing:
        raise ValueError("密钥材料未覆盖 MPD 中的全部 KID")
    return [f"{kid}:{pairs[kid]}" for kid in required]


def load_drm_key_lines(key_file, key_env, required_kids):
    """Read key material from a private file or environment variable."""
    if key_file:
        with open(key_file, 'r', encoding='utf-8-sig') as private_file:
            material = private_file.read()
    else:
        material = os.environ.get(key_env or DRM_KEY_ENV_DEFAULT, '')
    return normalize_drm_key_material(material, required_kids)


def kid_guid_byte_order_variant(kid):
    normalized = normalize_drm_kid(kid)
    raw = bytes.fromhex(normalized)
    variant = raw[3::-1] + raw[5:3:-1] + raw[7:5:-1] + raw[8:]
    return variant.hex()


def expand_key_lines_for_downloader(key_lines):
    expanded = []
    seen = set()
    for line in key_lines:
        kid, key = line.split(':', 1)
        for candidate in (normalize_drm_kid(kid), kid_guid_byte_order_variant(kid)):
            entry = f"{candidate}:{key.lower()}"
            if entry not in seen:
                seen.add(entry)
                expanded.append(entry)
    return expanded


def resolve_executable(explicit_path, names):
    """Resolve a configured executable, PATH entry, or common local filename."""
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        for name in names:
            candidates.extend([
                name,
                os.path.join(script_dir, name),
                os.path.join(script_dir, '.tools', name),
            ])

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return os.path.abspath(resolved)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    display_name = names[0] if names else explicit_path
    raise FileNotFoundError(f"未找到依赖程序: {display_name}")


def resolve_optional_executable(explicit_path, names):
    try:
        return resolve_executable(explicit_path, names)
    except FileNotFoundError:
        return ""


def validate_rtmp_url(value):
    """Keep the pipe-mux environment string structurally safe and predictable."""
    value = value or ''
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {'rtmp', 'rtmps'} or not parsed.netloc:
        raise ValueError("推流地址必须是完整的 RTMP/RTMPS URL")
    if any(character in value for character in ['"', '\r', '\n']):
        raise ValueError("推流地址包含不受支持的字符")
    return value


def build_drm_relay_command(stream, headers, key_file_path, downloader_path,
                            ffmpeg_path, work_dir, live_take_count=3,
                            decryption_engine="FFMPEG",
                            decryption_binary_path=None):
    """Build the live decrypt command without placing key bytes on argv."""
    manifest_input = stream.relay_manifest_source or stream.url
    decryption_engine = (decryption_engine or "FFMPEG").upper()
    decryption_binary_path = decryption_binary_path or ffmpeg_path
    command = [
        downloader_path,
        manifest_input,
        '--key-text-file', key_file_path,
        '--decryption-engine', decryption_engine,
        '--decryption-binary-path', decryption_binary_path,
        '--ffmpeg-binary-path', ffmpeg_path,
        '--mp4-real-time-decryption',
        '--live-real-time-merge',
        '--live-pipe-mux',
        '--live-keep-segments', 'false',
        '--live-take-count', str(max(1, int(live_take_count))),
        '--save-dir', work_dir,
        '--save-name', 'relay',
        '--log-level', 'OFF',
        '--no-log',
        '--no-ansi-color',
        '--disable-update-check',
    ]
    for name, value in (headers or {}).items():
        if value:
            command.extend(['-H', f'{name}: {value}'])

    if manifest_input != stream.url and urllib.parse.urlsplit(stream.url).scheme in {'http', 'https'}:
        command.extend(['--base-url', stream.url.rsplit('/', 1)[0] + '/'])

    if stream.resolution and stream.resolution != 'N/A':
        command.extend(['-sv', f'res={stream.resolution}:for=best'])
    else:
        command.extend(['-sv', 'best'])
    command.extend(['-sa', 'best'])
    return command


def clean_relay_output_line(line):
    return ANSI_ESCAPE_PATTERN.sub('', line or '').strip()


def is_relay_progress_line(line):
    if not line:
        return True
    lowered = line.lower()
    if any(keyword in line for keyword in ('解密失败', '错误', '失败')):
        return False
    if any(keyword in lowered for keyword in ('exception', 'forbidden', 'failed')):
        return False
    return (
        '----------' in line or
        bool(re.search(r'\b(?:Vid|Aud)\b.*(?:Kbps|Mbps|CH|fps)', line)) or
        bool(re.search(r'\b\d+/\d+\b.*(?:KB|MB|GB|%|--:--:--)', line))
    )


def should_print_relay_line(line):
    if not line or is_relay_progress_line(line):
        return False
    lowered = line.lower()
    keywords = (
        '解密失败', '错误', '失败', 'forbidden', 'exception', 'failed',
        'decrypt', '403', '401', 'invalid', 'denied',
    )
    return any(keyword in lowered or keyword in line for keyword in keywords)


def run_relay_process(command, child_environment, log_path):
    process = subprocess.Popen(
        command,
        env=child_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print("[状态] 解密转推进程已启动；无错误时终端会保持静默，按 Ctrl+C 停止。")
    pending = ''
    printed = set()
    with open(log_path, 'w', encoding='utf-8', errors='replace') as log_file:
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                text = chunk.decode('utf-8', errors='replace')
                log_file.write(text)
                log_file.flush()
                pending += text
                parts = re.split(r'[\r\n]+', pending)
                pending = parts.pop() if parts else ''
                for raw_line in parts:
                    line = clean_relay_output_line(raw_line)
                    if should_print_relay_line(line) and line not in printed:
                        printed.add(line)
                        print(f"[N_m3u8DL] {line}")
            if pending:
                line = clean_relay_output_line(pending)
                if should_print_relay_line(line) and line not in printed:
                    print(f"[N_m3u8DL] {line}")
            return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise


def perform_drm_livestream(stream, rtmp_url, headers=None, drm_key_file=None,
                           drm_key_env=DRM_KEY_ENV_DEFAULT,
                           downloader_path=None, ffmpeg_path=None,
                           mp4decrypt_path=None, restart_count=0, restart_delay=5,
                           live_take_count=3):
    """Decrypt an authorized dynamic CENC MPD and relay it through FFmpeg."""
    if stream.manifest_type != 'dash':
        raise ValueError("实时 DRM 转推当前需要 MPEG-DASH MPD")
    if urllib.parse.urlsplit(stream.url).scheme not in {'http', 'https'}:
        raise ValueError("实时转推需要原始 HTTP(S) MPD URL，以便持续刷新清单")

    rtmp_url = validate_rtmp_url(rtmp_url)
    required_kids = stream.drm_info.get('key_ids') or []
    if not required_kids:
        raise ValueError("MPD 未声明 default_KID，无法校验密钥映射")

    downloader = resolve_executable(
        downloader_path, ['N_m3u8DL-RE.exe', 'N_m3u8DL-RE'])
    ffmpeg = resolve_executable(ffmpeg_path, ['ffmpeg.exe', 'ffmpeg'])
    mp4decrypt = resolve_optional_executable(
        mp4decrypt_path, ['mp4decrypt.exe', 'mp4decrypt'])
    decryption_engine = 'MP4DECRYPT' if mp4decrypt else 'FFMPEG'
    decryption_binary = mp4decrypt or ffmpeg
    key_lines = expand_key_lines_for_downloader(
        load_drm_key_lines(drm_key_file, drm_key_env, required_kids))

    private_dir = tempfile.mkdtemp(prefix='livetools-drm-')
    key_path = os.path.join(private_dir, 'keys.private.txt')
    relay_log_path = os.path.join(
        tempfile.gettempdir(), f"livetools_relay_{os.getpid()}.log")
    try:
        with open(key_path, 'w', encoding='ascii', newline='\n') as key_handle:
            key_handle.write('\n'.join(key_lines) + '\n')
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass

        command = build_drm_relay_command(
            stream, headers, key_path, downloader, ffmpeg, private_dir,
            live_take_count=live_take_count,
            decryption_engine=decryption_engine,
            decryption_binary_path=decryption_binary)
        child_environment = os.environ.copy()
        child_environment.pop(drm_key_env or DRM_KEY_ENV_DEFAULT, None)
        # N_m3u8DL-RE feeds decrypted audio/video into its FFmpeg named pipes.
        # A leading '-' makes this value a complete FFmpeg output argument set.
        child_environment['RE_LIVE_PIPE_OPTIONS'] = f'-f flv "{rtmp_url}"'

        attempts = max(0, int(restart_count)) + 1
        for attempt in range(attempts):
            if attempt:
                print(f"[重试] {restart_delay} 秒后重新建立解密转推链路 "
                      f"({attempt}/{attempts - 1})...")
                time.sleep(max(0, restart_delay))
            returncode = run_relay_process(
                command, child_environment, relay_log_path)
            if returncode == 0:
                try:
                    os.unlink(relay_log_path)
                except OSError:
                    pass
                return 0
            print(f"[警告] 解密转推进程退出，代码: {returncode}")
            print(f"[日志] 详情已保存: {relay_log_path}")
            if decryption_engine == 'FFMPEG':
                print("[提示] 当前使用 FFmpeg 解密。若 CENC 分片仍解密失败，"
                      "请把 mp4decrypt.exe 放到 .tools 或用 --mp4decrypt-binary 指定。")
        return returncode
    finally:
        shutil.rmtree(private_dir, ignore_errors=True)


def parse_mpd_string(input_string, manifest_url):
    """Parse video representations from an MPEG-DASH MPD."""
    root = ET.fromstring(input_string)
    if xml_local_name(root.tag) != 'MPD':
        raise ValueError("XML 根元素不是 MPD")

    drm_info = detect_mpd_drm(input_string)
    streams = []
    video_index = 0

    for adaptation in root.iter():
        if xml_local_name(adaptation.tag) != 'AdaptationSet':
            continue

        adaptation_mime = adaptation.attrib.get('mimeType', '')
        adaptation_type = adaptation.attrib.get('contentType', '')
        is_video_adaptation = (
            adaptation_type.lower() == 'video' or
            adaptation_mime.lower().startswith('video/')
        )

        for representation in adaptation:
            if xml_local_name(representation.tag) != 'Representation':
                continue

            mime_type = representation.attrib.get('mimeType', adaptation_mime)
            codecs = representation.attrib.get(
                'codecs', adaptation.attrib.get('codecs', 'N/A'))
            is_video = is_video_adaptation or mime_type.lower().startswith('video/')
            if not is_video:
                continue

            width = representation.attrib.get('width', adaptation.attrib.get('width'))
            height = representation.attrib.get('height', adaptation.attrib.get('height'))
            resolution = f"{width}x{height}" if width and height else "N/A"
            try:
                bandwidth_value = int(representation.attrib.get('bandwidth', '0') or 0)
            except ValueError:
                bandwidth_value = 0
            bandwidth = f"{bandwidth_value / 1000000:.2f} Mbps" if bandwidth_value else "N/A"

            streams.append(VideoStream(
                resolution,
                bandwidth,
                manifest_url,
                manifest_type="dash",
                video_index=video_index,
                codecs=codecs,
                drm_info=drm_info,
            ))
            video_index += 1

    if not streams:
        streams.append(VideoStream(
            "N/A", "N/A", manifest_url,
            manifest_type="dash",
            drm_info=drm_info,
        ))

    return streams

# --- M3U8 解析函数 ---

def parse_attribute_list(value):
    """Parse an HLS attribute list without splitting commas inside quotes."""
    values = {}
    current = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
        if char == ',' and not quoted:
            current.append('\n')
        else:
            current.append(char)
    for item in ''.join(current).splitlines():
        if '=' not in item:
            continue
        key, raw = item.split('=', 1)
        values[key.strip().upper()] = raw.strip().strip('"')
    return values


def parse_m3u8_string(input_string, base_url=None):
    """
    逐行解析 M3U8 字符串，提取视频流信息。
    如果提供 base_url，则将相对 URL 转换为绝对 URL。
    """
    streams = []
    lines = input_string.splitlines() 
    
    current_info_attributes = None
    saw_media_segment = False
    
    for line in lines:
        line = line.strip() 
        
        if not line:
            continue 
            
        # 1. 检查是否是配置行 (#EXT-X-STREAM-INF)
        if line.startswith('#EXT-X-STREAM-INF:'):
            current_info_attributes = parse_attribute_list(line.split(':', 1)[1])
            
        # 2. 检查是否是 URL 行 
        elif current_info_attributes is not None and not line.startswith('#'):
            url = line
            if base_url and not urllib.parse.urlparse(url).scheme:
                url = urllib.parse.urljoin(base_url, url)
            resolution = current_info_attributes.get('RESOLUTION', 'N/A')
            bandwidth_raw = parse_int(current_info_attributes.get('BANDWIDTH'), 0)
            average_bandwidth = parse_int(
                current_info_attributes.get('AVERAGE-BANDWIDTH'),
                bandwidth_raw,
            )
            bandwidth = f"{max(bandwidth_raw, average_bandwidth) / 1000000:.2f} Mbps"
            streams.append(VideoStream(resolution, bandwidth, url))

            current_info_attributes = None
        elif line.startswith('#EXTINF:') or line.startswith('#EXT-X-PART:'):
            saw_media_segment = True
        elif line.startswith('#'):
            continue

    # A media playlist has no STREAM-INF entries. Treat the source itself as
    # one selectable stream so the caller can still preview or push it.
    if not streams and saw_media_segment and base_url:
        streams.append(VideoStream("N/A", "N/A", base_url))
            
    return streams


def make_headers(cookie=None, headers=None):
    result = dict(headers or {})
    if cookie and 'Cookie' not in result:
        result['Cookie'] = cookie
    return result


def add_request_headers(request, headers=None):
    for name, value in (headers or {}).items():
        if value:
            request.add_header(name, value)


def ffmpeg_headers(headers=None):
    return ''.join(f'{name}: {value}\r\n' for name, value in (headers or {}).items() if value)


def parse_header_blob(value):
    """Parse common curl/minyami header text without logging credentials."""
    result = {}
    if not value:
        return result
    parts = HEADER_SPLIT_PATTERN.split(value)
    for part in parts:
        part = part.strip().strip('"\'')
        if ':' not in part:
            continue
        name, header_value = part.split(':', 1)
        name = name.strip()
        if HTTP_HEADER_PATTERN.fullmatch(name):
            result[normalize_header_name(name)] = header_value.strip()
    return result


def extract_command_fields(input_data):
    """Extract manifest URL and headers from minyami/N_m3u8DL style commands."""
    result = {'source': None, 'headers': {}}
    command_names = {'minyami', 'n_m3u8dl-re', 'n_m3u8dl-re.exe'}

    for line in input_data.splitlines():
        if not re.search(r'\b(?:minyami|N_m3u8DL-RE(?:\.exe)?)\b', line, re.I):
            continue
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError:
            continue

        command_index = None
        for index, token in enumerate(tokens):
            command = os.path.basename(token).lower()
            if command in command_names:
                command_index = index
                break
        if command_index is None:
            continue

        index = command_index + 1
        while index < len(tokens):
            token = tokens[index]
            lowered = token.lower()

            if lowered in {'-d', '--download', '--url', '--source'} and index + 1 < len(tokens):
                result['source'] = tokens[index + 1].strip('"\'')
                index += 2
                continue

            if lowered in {'--headers', '--header', '-h', '-H'.lower()} and index + 1 < len(tokens):
                result['headers'].update(parse_header_blob(tokens[index + 1]))
                index += 2
                continue

            if not token.startswith('-') and not result['source']:
                candidate = token.strip('"\'')
                parsed = urllib.parse.urlsplit(candidate)
                if parsed.scheme in {'http', 'https'} or os.path.isfile(candidate):
                    result['source'] = candidate
            index += 1

    return result

# --- FFmpeg 检查函数 ---

def check_ffmpeg():
    """ 检查 FFmpeg 是否安装 """
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("\n[错误] FFmpeg 未安装或未添加到系统 PATH 中。无法执行此操作。")
        print("请访问 https://ffmpeg.org/ 下载并安装 FFmpeg。")
        return False

# --- 辅助函数：简易进度条 ---
def display_progress_bar(prefix, current, total, bar_length=15):
    """显示简易文本进度条"""
    if total == 0:
        return f'{prefix} [N/A] 0/0 (0.0%)'
    percent = current / total
    # 确保进度条至少有一个尖角
    num_chars = int(round(percent * bar_length))
    arrow = '=' * num_chars
    spaces = ' ' * (bar_length - num_chars)
    
    # 进度条末尾加上百分比
    return f'{prefix} [{arrow + spaces}] {current}/{total} ({percent * 100:.1f}%)'

# --- 异步下载段函数（增加存在性检查和重试） ---
async def async_download_segment(session, ts_url, ts_local_path, cookie,
                                 headers=None, max_retries=3, semaphore=None):
    """
    异步下载单个分片，失败重试 max_retries 次，并在下载前检查本地是否存在。
    返回: (成功状态, 文件路径, 是否跳过)
    """
    
    # * 断点续传/存在性检查 *
    if os.path.exists(ts_local_path) and os.path.getsize(ts_local_path) > 0:
        return True, ts_local_path, True # 成功，已跳过
    
    async def run_sync_fetch():
        if semaphore:
            async with semaphore:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, sync_fetch)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, sync_fetch)

    def sync_fetch():
        req = urllib.request.Request(ts_url)
        add_request_headers(req, make_headers(cookie, headers))
        part_path = f"{ts_local_path}.part"
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                with open(part_path, 'wb') as out_file:
                    shutil.copyfileobj(response, out_file)
            if not os.path.exists(part_path) or os.path.getsize(part_path) == 0:
                raise OSError("下载到空分片")
            os.replace(part_path, ts_local_path)
        finally:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass

    # 如果文件不存在，则开始下载
    for attempt in range(max_retries):
        try:
            # 阻塞调用，但放入线程池中运行，不阻塞事件循环
            await run_sync_fetch()
            return True, ts_local_path, False # 成功，未跳过
        
        except Exception as e:
            if attempt < max_retries - 1:
                # print(f"\n[警告] 分片 {os.path.basename(ts_local_path)} 下载失败 (第 {attempt + 1} 次)，正在重试...")
                await asyncio.sleep(5) # 重试前等待 5 秒
            else:
                # print(f"\n[严重警告] 分片 {os.path.basename(ts_local_path)} 最终下载失败，跳过。错误: {e}")
                return False, ts_local_path, False # 最终失败，未跳过

    return False, ts_local_path, False

async def async_perform_download(stream, cookie=None, suggested_filename=None,
                                 headers=None,
                                 download_concurrency=DEFAULT_DOWNLOAD_CONCURRENCY):
    """
    三阶段下载与合并 (异步并发下载历史分片，FFmpeg 下载实时分片)
    """
    if not check_ffmpeg():
        return 1
    
    # --- 0. 初始化和路径设置 ---
    if suggested_filename:
        suggested_filename = sanitize_filename(suggested_filename)
        default_filename = f"{suggested_filename}_{stream.resolution}.ts"
    else:
        default_filename = f"HLS_Stream_FULL_{stream.resolution}_{stream.bandwidth.replace(' ', '_').replace('.', 'p')}.ts"
    default_filename = sanitize_filename(default_filename, fallback="HLS_Stream.ts")
    output_path = input(f"\n请输入完整的保存路径和文件名 (默认为当前目录下的 {default_filename}): ").strip()
    final_output_filename = output_path if output_path else default_filename
    
    # 确保使用绝对路径，避免路径问题
    final_output_filename = os.path.abspath(os.path.expanduser(final_output_filename))
    output_dir = os.path.dirname(final_output_filename)
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:
        print(f"[错误] 无法创建输出目录 {output_dir}: {e}")
        return 1
    
    temp_dir = tempfile.mkdtemp(
        prefix=f"temp_hls_download_{int(time.time())}_",
        dir=output_dir,
    )
    base_name = os.path.splitext(os.path.basename(final_output_filename))[0]
    history_output_file = os.path.join(temp_dir, f"{base_name}_0.ts")
    live_output_file = os.path.join(temp_dir, f"{base_name}_1.ts")
    
    print(f"\n[开始] 正在开始三阶段下载，最终文件：{final_output_filename}")
    print(f"[信息] 所有临时文件将存储在: {temp_dir}")
    
    download_success = True
    saved_output = False
    
    try:
        os.makedirs(temp_dir, exist_ok=True)
    except Exception as e:
        print(f"[错误] 无法创建临时目录 {temp_dir}: {e}")
        return 1

    # --- 1. 阶段 1/3: 准备工作 (M3U8 解析) ---
    print("\n--- 阶段 1/3: 准备工作 (解析流信息) ---")
    
    # ---------------------------------------------------------------------
    # 同步 M3U8 解析代码 (与上一个版本保持一致)
    # ---------------------------------------------------------------------
    top_level_url = stream.url 
    final_stream_url = top_level_url
    
    try:
        req = urllib.request.Request(top_level_url)
        add_request_headers(req, make_headers(cookie, headers))
        with urllib.request.urlopen(req) as response:
            top_m3u8_content = response.read().decode('utf-8')
        
        sub_streams = parse_m3u8_string(top_m3u8_content, base_url=top_level_url)
        user_bandwidth_raw = 0
        try:
            user_bandwidth_raw = int(float(stream.bandwidth.split()[0]) * 1000000)
        except (ValueError, IndexError):
            pass
        selected_sub_stream_url = None
        
        for s in sub_streams:
            s_bandwidth_raw = 0
            try:
                s_bandwidth_raw = int(float(s.bandwidth.split()[0]) * 1000000)
            except:
                pass
            
            resolution_match = stream.resolution == 'N/A' or s.resolution == stream.resolution
            bandwidth_match = user_bandwidth_raw == 0 or abs(s_bandwidth_raw - user_bandwidth_raw) < 10000
            
            if resolution_match and bandwidth_match:
                selected_sub_stream_url = s.url
                break
        
        if selected_sub_stream_url:
            final_stream_url = selected_sub_stream_url
            print(f"[信息] 成功找到子流 URL: {final_stream_url}")
        else:
            print(f"[警告] 未能找到匹配的子流 URL。假定用户选择的 URL 本身 ({top_level_url[:50]}...) 即为子流播放列表。")
            final_stream_url = top_level_url

        req = urllib.request.Request(final_stream_url)
        add_request_headers(req, make_headers(cookie, headers))
        with urllib.request.urlopen(req) as response:
            live_m3u8_content = response.read().decode('utf-8')
            
        # 不再假设供应商使用 index_N_N.ts 命名。按 HLS playlist 逐项解析，
        # 这样可以兼容相对路径、query token 和不同的分片命名方式。
        key_lines = [line for line in live_m3u8_content.splitlines() if line.startswith('#EXT-X-KEY:')]
        if any('METHOD=NONE' not in line.upper() for line in key_lines):
            print("[错误] 输入是加密 HLS（包含非 NONE 的 EXT-X-KEY）；工具不会尝试解密或绕过内容保护。")
            cleanup_temp_dir(temp_dir)
            return 1
        segment_urls = []
        for line in live_m3u8_content.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            segment_urls.append(urllib.parse.urljoin(final_stream_url, line))

        if not segment_urls:
            print("[错误] 子流 M3U8 中没有找到媒体分片。")
            cleanup_temp_dir(temp_dir)
            return 1

        print(f"[信息] 检测到历史分片数量: {len(segment_urls)}。")
        
    except Exception as e:
        print(f"[错误] 阶段 1 发生致命错误: {e}")
        cleanup_temp_dir(temp_dir)
        return 1
    
    # --- 2. 阶段 A: 异步并发下载历史分片 (0 到 N) ---
    
    print(f"\n--- 阶段 2/3: 异步并发下载历史分片 (共 {len(segment_urls)} 个) ---")
    total_segments = len(segment_urls)
    concurrency = max(1, int(download_concurrency or DEFAULT_DOWNLOAD_CONCURRENCY))
    semaphore = asyncio.Semaphore(concurrency)
    print(f"[信息] 将使用 asyncio 并发下载 {total_segments} 个历史分片 (并发 {concurrency}，重试 3 次，支持断点续传)。")
    
    # 2.1 准备下载任务列表
    tasks = []
    for i, ts_url in enumerate(segment_urls):
        ts_local_path = os.path.join(temp_dir, f"segment_{i}.ts")
        tasks.append(async_download_segment(
            None, ts_url, ts_local_path, cookie, headers=headers,
            max_retries=3, semaphore=semaphore))
    
    # 2.2 运行异步下载任务并监控进度
    results = []
    completed_count = 0
    downloaded_count = 0
    skipped_count = 0
    
    start_time = time.time()
    
    for f in asyncio.as_completed(tasks):
        # 接收结果: success, path, skipped
        success, path, skipped = await f
        
        results.append((success, path))
        completed_count += 1
        
        if success:
            if skipped:
                skipped_count += 1
            else:
                downloaded_count += 1
        
        # 实时打印进度
        time_elapsed = time.time() - start_time
        download_speed = (downloaded_count / time_elapsed) if time_elapsed > 0 and downloaded_count > 0 else 0
        
        # 历史分片进度条
        history_progress_text = display_progress_bar(
            f"历史分片 (D: {downloaded_count}, S: {skipped_count}, {download_speed:.1f} seg/s)", 
            completed_count, 
            total_segments, 
            bar_length=15
        )
        
        # FFmpeg 状态 (简化)
        ffmpeg_status_text = "实时下载 [FFmpeg]: 正在准备..."
        
        # 清除当前行并重新打印统一进度条
        print(f"\r{history_progress_text} | {ffmpeg_status_text}", end='', flush=True)

    # 下载完成后，打印最终进度
    history_progress_text = display_progress_bar(
        f"历史分片 (完成 D:{downloaded_count}, S:{skipped_count})", 
        total_segments, 
        total_segments, 
        bar_length=15
    )
    print(f"\r{history_progress_text}", end='\n', flush=True)
    
    # 2.3 历史分片合并
    history_segments_count = sum(1 for success, path in results if success)
    
    if history_segments_count == 0:
        print("[警告] 没有成功下载任何历史分片，跳过历史合并。")
        download_success = False
    else:
        file_list_path = os.path.join(temp_dir, "history_filelist.txt")
        
        with open(file_list_path, 'w', encoding='utf-8') as filelist_f:
            # 必须按索引顺序合并
            for i in range(total_segments):
                seg_name = f"segment_{i}.ts"
                seg_path = os.path.join(temp_dir, seg_name)
                if os.path.exists(seg_path):
                    # 在FFmpeg的concat文件中使用正斜杠（跨平台兼容）
                    # 或者直接使用文件名（因为FFmpeg会在同一目录下查找）
                    filelist_f.write(f"file '{seg_name}'\n")
        
        try:
            # 使用 FFmpeg concat 协议合并历史分片
            history_merge_command = [
                "ffmpeg", 
                "-f", "concat", 
                "-safe", "0", 
                "-i", file_list_path, 
                "-c", "copy",
                history_output_file
            ]
            
            subprocess.run(
                history_merge_command, check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, cwd=temp_dir)
            
            print(f"[成功] 历史分片合并为 {os.path.basename(history_output_file)} 完成。")
        except subprocess.CalledProcessError as e:
            print(f"[严重错误] 历史分片合并失败。中止。")
            print(e.stderr.decode())
            download_success = False
        except Exception as e:
            print(f"[严重错误] 历史分片合并时发生未知错误: {e}")
            download_success = False


    # --- 3. 阶段 B: FFmpeg 下载后续直播分片 ($N+1$ 到 End) ---

    if download_success:
        print("\n--- 阶段 3/3: 下载后续直播分片 (实时) ---")
        print("[信息] 使用 FFmpeg 实时下载 (内置重试机制: -reconnect, 间隔 5s)。")
        
        download_command_1 = ["ffmpeg"]
        
        request_header_text = ffmpeg_headers(make_headers(cookie, headers))
        if request_header_text:
            download_command_1.extend(["-headers", request_header_text])
            
        # 设置 FFmpeg 内置重试机制
        download_command_1.extend([
            "-live_start_index", "-1", 
            "-reconnect", "1",
            "-reconnect_streamed", "1", 
            "-reconnect_delay_max", "5", 
            "-i", final_stream_url, 
            "-c", "copy",
            live_output_file
        ])

        try:
            print("--- FFmpeg 实时下载开始 (按 Q 键停止下载) ---")
            subprocess.run(download_command_1)
            print("--- 实时下载命令执行完毕 ---")
        except Exception as e:
            print(f"[错误] 实时下载过程中发生错误: {e}")

    # --- 4. 最终合并 (Stage C) ---
    
    history_exists = os.path.exists(history_output_file) and os.path.getsize(history_output_file) > 0
    live_exists = os.path.exists(live_output_file) and os.path.getsize(live_output_file) > 0

    if history_exists or live_exists:
        # ... (与上个版本相同的最终合并逻辑) ...
        if history_exists and live_exists:
            print("\n--- 最终合并: 合并历史和实时部分 ---")
            
            final_file_list_path = os.path.join(temp_dir, "final_merge_filelist.txt")
            with open(final_file_list_path, 'w', encoding='utf-8') as f:
                f.write(f"file '{os.path.basename(history_output_file)}'\n")
                f.write(f"file '{os.path.basename(live_output_file)}'\n")
            
            final_merge_command = [
                "ffmpeg", 
                "-f", "concat", 
                "-safe", "0", 
                "-i", final_file_list_path, 
                "-c", "copy",
                final_output_filename
            ]
            
            try:
                subprocess.run(
                    final_merge_command, check=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, cwd=temp_dir)
                print(f"\n[成功] 所有部分已合并并保存到最终文件: {final_output_filename}")
                saved_output = True
            except subprocess.CalledProcessError as e:
                print(f"[严重错误] 最终合并失败。请检查FFmpeg输出。")
                print(e.stderr.decode())
        
        elif history_exists:
            print(f"\n[信息] 未检测到后续直播内容。直接将历史文件移动到: {final_output_filename}")
            try:
                shutil.move(history_output_file, final_output_filename)
                print(f"[成功] 文件保存到: {final_output_filename}")
                saved_output = True
            except Exception as e:
                print(f"[错误] 无法移动历史文件: {e}")
        
        elif live_exists:
            print(f"\n[信息] 历史下载失败，只将直播部分移动到: {final_output_filename}")
            try:
                shutil.move(live_output_file, final_output_filename)
                print(f"[成功] 文件保存到: {final_output_filename}")
                saved_output = True
            except Exception as e:
                print(f"[错误] 无法移动直播文件: {e}")

    # --- 5. 清理 ---
    cleanup_temp_dir(temp_dir)
        
    print("\n程序运行结束。")
    return 0 if saved_output else 1

def perform_download(stream, cookie=None, suggested_filename=None, headers=None):
    """
    同步调用 async_perform_download，作为程序的主要入口。
    """
    if stream.manifest_type == 'dash':
        print("\n[错误] 当前三阶段下载器只适用于 HLS；DASH 请使用推流功能。")
        return 1
    try:
        # 使用 asyncio.run 执行异步函数
        return asyncio.run(async_perform_download(
            stream, cookie, suggested_filename, headers))
    except KeyboardInterrupt:
        print("\n[中断] 用户手动停止下载。")
        return 130
    except Exception as e:
        print(f"\n[致命错误] 程序运行出错: {e}")
        return 1

# --- 辅助函数 (本地播放和推流) ---

def perform_playback(stream):
    """
    尝试使用本地播放器打开 URL。
    """
    url = stream.url
    player_command = []

    # 针对不同操作系统设置播放器路径
    if sys.platform.startswith('win'):
        # 尝试 PotPlayer
        potplayer_path = r'D:\PotPlayer\PotPlayerMini64.exe' 
        vlc_path = r'C:\Program Files\VideoLAN\VLC\vlc.exe'
        if os.path.exists(potplayer_path):
            player_command = [potplayer_path, url]
            print("\n[尝试] 尝试使用 PotPlayer 播放...")
        elif os.path.exists(vlc_path):
            player_command = [vlc_path, url]
            print("\n[尝试] 尝试使用 VLC 播放...")
        else:
            print("\n[警告] 未在常见路径中找到 PotPlayer 或 VLC。")
            print(f"请手动复制 URL 并粘贴到您本地的播放器中：\n{url}")
            return
    elif sys.platform.startswith('darwin'): # macOS
        player_command = ["/Applications/VLC.app/Contents/MacOS/VLC", url]
        print("\n[尝试] 尝试使用 VLC 播放...")
    elif sys.platform.startswith('linux'):
        player_command = ["vlc", url]
        print("\n[尝试] 尝试使用 VLC 播放...")
    else:
        print("\n[警告] 当前系统不支持自动调用本地播放器。")
        print(f"请手动复制 URL 并粘贴到您本地的播放器中：\n{url}")
        return

    try:
        # 使用 Popen 启动播放器，避免阻塞主程序
        subprocess.Popen(player_command)
        print("[信息] 播放器已在后台启动（或尝试启动）。请检查您的屏幕。")
    except FileNotFoundError:
        print(f"[错误] 播放器命令未找到。")
        print(f"请手动复制 URL 并粘贴到您本地的播放器中：\n{url}")
    except Exception as e:
        print(f"[错误] 启动播放器时发生错误: {e}")

def perform_livestream(stream, cookie=None, headers=None, stream_name=None, push_url=None,
                       drm_key_file=None, drm_key_env=DRM_KEY_ENV_DEFAULT,
                       downloader_path=None, ffmpeg_path=None,
                       mp4decrypt_path=None,
                       restart_count=0, restart_delay=5, live_take_count=3):
    """
    使用 FFmpeg 转推普通流，或编排有现成内容密钥的 CENC DASH 直播流。
    """
    if not stream.url:
        print("\n[错误] 推流需要原始 MPD/M3U8 URL 或可解析分片的本地清单路径。")
        return 1
    
    # 阿里云视频直播配置
    PUSH_DOMAIN = "push.neofantasy.online"  # 阿里云推流域名
    PLAY_DOMAIN = "play.neofantasy.online"  # 阿里云播放域名
    APP_NAME = "live"                       # 应用名称 (AppName)
    
    # StreamName 是流名称，不是源站账号凭据。可由配置/命令行传入。
    stream_key = (stream_name or '').strip()
    if not stream_key and push_url:
        stream_key = os.path.basename(urllib.parse.urlparse(push_url).path.rstrip('/'))
    if not stream_key:
        stream_key = input(f"请输入流名称 (StreamName, 例如: my_stream_key): ").strip()
    
    if not stream_key:
        print("[错误] 推流密钥不能为空，操作取消。")
        return 1
    
    # 未提供完整推流 URL 时沿用默认阿里云地址。启用推流鉴权的环境应通过
    # --push-url 或私有输入文件提供阿里云签名后的完整地址。
    rtmp_url = push_url or f"rtmp://{PUSH_DOMAIN}/{APP_NAME}/{stream_key}"
    
    compact_push_output = bool(stream.relay_manifest_source)
    if compact_push_output:
        print(f"\n[推流] {stream.manifest_type.upper()} {stream.resolution} -> {APP_NAME}/{stream_key}")
        print(f"[播放] https://{PLAY_DOMAIN}/{APP_NAME}/{stream_key}.m3u8")
    else:
        print(f"\n[推流] 正在将 {stream.manifest_type.upper()} 流 ({stream.resolution} @ {stream.bandwidth}) 推送到阿里云")
        print(f"[配置] 推流域名: {PUSH_DOMAIN}")
        print(f"[配置] 应用名称: {APP_NAME}")
        print(f"[配置] 流名称: {stream_key}")
        print(f"\n[注意] 推流开始后，您可以通过以下地址观看：")
        print(f"       RTMP 播放: rtmp://{PLAY_DOMAIN}/{APP_NAME}/{stream_key}")
        print(f"       HLS 播放:  https://{PLAY_DOMAIN}/{APP_NAME}/{stream_key}.m3u8")
        print(f"       FLV 播放:  https://{PLAY_DOMAIN}/{APP_NAME}/{stream_key}.flv")
        print("       播放鉴权已启用时，请通过 /get-stream 获取签名播放地址。")

    if stream.drm_info.get('protected'):
        if compact_push_output:
            print("[DRM] 使用已获取内容密钥启动解密转推。")
        else:
            print("\n[DRM] " + format_drm_summary(stream.drm_info))
            print("[流程] 启动动态 MPD 刷新、实时 CENC 解密与 FFmpeg 管道转推。")
        try:
            return perform_drm_livestream(
                stream,
                rtmp_url,
                make_headers(cookie, headers),
                drm_key_file=drm_key_file,
                drm_key_env=drm_key_env,
                downloader_path=downloader_path,
                ffmpeg_path=ffmpeg_path,
                mp4decrypt_path=mp4decrypt_path,
                restart_count=restart_count,
                restart_delay=restart_delay,
                live_take_count=live_take_count,
            )
        except KeyboardInterrupt:
            print("\n[中断] 用户手动停止解密转推。")
            return 130
        except (FileNotFoundError, OSError, ValueError) as error:
            print(f"[错误] 解密转推初始化失败: {error}")
            return 1

    if not check_ffmpeg(): return 1

    # 构建推流命令 (使用流复制，无需重新编码)
    livestream_command = [
        "ffmpeg",
        # 关键设置: 忽略输入流中的时间戳错误，对直播源尤其重要
        "-fflags", "+genpts",
    ]
    
    request_header_text = ffmpeg_headers(make_headers(cookie, headers))
    if request_header_text:
        livestream_command.extend(["-headers", request_header_text])
    
    livestream_command.extend(["-i", stream.url])

    # DASH demuxer 会把各个 Representation 暴露成独立视频流，按菜单选择映射。
    if stream.manifest_type == 'dash' and stream.video_index is not None:
        livestream_command.extend([
            "-map", f"0:v:{stream.video_index}",
            "-map", "0:a:0?",
        ])

    livestream_command.extend([
        # 直接复制音视频流，不重新编码（节省 CPU，保持质量，降低延迟）
        "-c", "copy",
        # 输出格式和目标地址
        "-f", "flv",
        rtmp_url
    ])

    try:
        print("\n--- FFmpeg 推流开始 (按 Ctrl+C 停止) ---")
        result = subprocess.run(livestream_command)
        print("--- 推流已停止 ---")
        return result.returncode
    except KeyboardInterrupt:
        print("\n[中断] 用户手动停止推流。")
        return 130
    except Exception as e:
        print(f"[错误] 推流过程中发生错误: {e}")
        return 1

# --- 用户交互逻辑 ---

def view_or_download_manifest(stream, cookie=None, headers=None):
    """
    查看或下载指定流的 M3U8/MPD 清单内容。
    """
    manifest_name = "MPD" if stream.manifest_type == 'dash' else "M3U8"
    print(f"\n--- 查看/下载 {manifest_name} 清单 ---")
    print(f"[1] 查看 {manifest_name} 内容")
    print(f"[2] 下载 {manifest_name} 文件")
    print("----------------------------")

    while True:
        choice = input("请选择 (1-2): ").strip()
        if choice == '1':
            try:
                content = load_manifest(stream.url, make_headers(cookie, headers))
                print(f"\n--- {manifest_name} 内容 ---")
                print(content)
                print("--- 内容结束 ---")
            except Exception as e:
                print(f"[错误] 读取 {manifest_name} 内容失败: {e}")
            break
        elif choice == '2':
            parsed_path = urllib.parse.urlsplit(stream.url).path
            extension = '.mpd' if stream.manifest_type == 'dash' else '.m3u8'
            base_name = os.path.splitext(os.path.basename(parsed_path))[0] or 'manifest'
            filename = f"{base_name}{extension}"
            try:
                content = load_manifest(stream.url, make_headers(cookie, headers))
                with open(filename, 'w', encoding='utf-8') as manifest_file:
                    manifest_file.write(content)
                print(f"[成功] {manifest_name} 文件已保存为: {filename}")
            except Exception as e:
                print(f"[错误] 下载 {manifest_name} 文件失败: {e}")
            break
        else:
            print("[警告] 输入无效，请重新输入 1 或 2。")

def handle_user_choice(streams, cookie=None, suggested_filename=None, headers=None,
                       requested_quality=None, requested_operation=None,
                       stream_name=None, push_url=None, drm_key_file=None,
                       drm_key_env=DRM_KEY_ENV_DEFAULT, downloader_path=None,
                       ffmpeg_path=None, mp4decrypt_path=None,
                       restart_count=0, restart_delay=5,
                       live_take_count=3):
    """
    处理用户的视频流选择和操作选择。
    """
    if not streams:
        print("\n[错误] 未找到任何视频流信息。")
        return 1

    try:
        # 尝试按分辨率高度排序
        streams.sort(key=lambda x: int(x.resolution.split('x')[1]) if 'x' in x.resolution else 0, reverse=True)
    except:
        pass 

    selected_stream = None
    quality = (requested_quality or '').strip().lower()
    if quality in {'best', '最高', '最高画质'}:
        selected_stream = streams[0]
    elif quality:
        if quality.isdigit():
            stream_index = int(quality) - 1
            if 0 <= stream_index < len(streams):
                selected_stream = streams[stream_index]
        if selected_stream is None:
            for candidate in streams:
                height = candidate.resolution.split('x')[-1] if 'x' in candidate.resolution else ''
                if candidate.resolution.lower() == quality or f"{height}p" == quality:
                    selected_stream = candidate
                    break
        if selected_stream is None:
            print(f"[警告] 未找到指定清晰度 {requested_quality}，请手动选择。")

    if selected_stream is None:
        print("\n--- 可用的视频流列表 (按分辨率排序) ---")
        for i, stream in enumerate(streams):
            display_url = safe_source_label(stream.url)
            print(f"[{i + 1}] {stream.resolution.ljust(10)} | {stream.bandwidth.rjust(10)} | URL: {display_url[:70]}...")
        print("---------------------------------------------------------------------------------------------------")

    while selected_stream is None:
        try:
            choice = input(f"请输入要操作的视频流编号 (1-{len(streams)}): ")
            stream_index = int(choice) - 1
            if 0 <= stream_index < len(streams):
                selected_stream = streams[stream_index]
            else:
                print("[警告] 输入无效，请重新输入正确的编号。")
        except ValueError:
            print("[警告] 输入无效，请输入数字。")

    operation_aliases = {
        'download': '1', '下载': '1',
        'play': '2', '播放': '2',
        'push': '3', '推流': '3',
        'manifest': '4', '清单': '4',
        'exit': '5', '退出': '5',
    }
    operation = operation_aliases.get(
        (requested_operation or '').strip().lower(),
        (requested_operation or '').strip(),
    )
    automatic_choice = bool(requested_quality and operation)
    if not automatic_choice:
        print(f"\n[选择] {selected_stream.resolution}，码率：{selected_stream.bandwidth}")

    while True:
        if not operation:
            print("\n--- 请选择要进行的操作 ---")
            print("[1] 下载 (需要 FFmpeg)")
            print("[2] 本地播放 (PotPlayer/VLC)")
            print("[3] 推流直播 (需要 FFmpeg)")
            print("[4] 查看/下载清单")
            print("[5] 退出")
            print("----------------------------")
            operation = input("请输入操作编号 (1-5): ")
        if operation == '1':
            result = perform_download(
                selected_stream, cookie, suggested_filename, headers)
            return result if isinstance(result, int) else 0
        elif operation == '2':
            perform_playback(selected_stream)
            return 0
        elif operation == '3':
            result = perform_livestream(
                selected_stream, cookie, headers,
                stream_name=stream_name, push_url=push_url,
                drm_key_file=drm_key_file, drm_key_env=drm_key_env,
                downloader_path=downloader_path, ffmpeg_path=ffmpeg_path,
                mp4decrypt_path=mp4decrypt_path,
                restart_count=restart_count, restart_delay=restart_delay,
                live_take_count=live_take_count)
            return result if isinstance(result, int) else 0
        elif operation == '4':
            view_or_download_manifest(selected_stream, cookie, headers)
            return 0
        elif operation == '5':
            print("操作取消，程序退出。")
            return 0
        else:
            print("[警告] 输入无效，请重新输入正确的操作编号。")
            operation = ''

# --- 输入与主执行逻辑 ---

def safe_source_label(source):
    """Hide URL query credentials in console output."""
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme in {'http', 'https'}:
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    return source


def labeled_value(input_data, labels):
    label_pattern = '|'.join(re.escape(label) for label in labels)
    match = re.search(rf'^(?:{label_pattern})[:：]\s*(.+)$', input_data, re.I | re.M)
    return match.group(1).strip() if match else None


def parse_input_description(input_data):
    """Extract source, headers and optional non-secret workflow settings."""
    result = {
        'source': None,
        'headers': {},
        'program_name': labeled_value(input_data, ['节目名称']),
        'operation': labeled_value(input_data, ['操作']),
        'quality': labeled_value(input_data, ['清晰度', '画质']),
        'stream_name': labeled_value(input_data, ['流名称', 'StreamName']),
        'push_url': labeled_value(input_data, ['推流地址', 'Push URL']),
        'drm_key_file': labeled_value(input_data, ['DRM密钥文件', '密钥文件']),
        'drm_key_env': labeled_value(
            input_data, ['DRM密钥环境变量', '密钥环境变量']),
        'downloader_path': labeled_value(
            input_data, ['N_m3u8DL-RE路径', '下载器路径']),
        'ffmpeg_path': labeled_value(input_data, ['FFmpeg路径']),
        'mp4decrypt_path': labeled_value(
            input_data, ['mp4decrypt路径', 'MP4Decrypt路径']),
    }

    command_fields = extract_command_fields(input_data)
    if command_fields['source']:
        result['source'] = command_fields['source']
    result['headers'].update(command_fields['headers'])

    for line in input_data.splitlines():
        stripped = line.strip()
        source_match = re.match(
            r'^(?:视频链接|MPD链接|清单链接|MPD文件|清单文件)[:：]\s*(.+)$',
            stripped, re.I)
        if source_match:
            result['source'] = source_match.group(1).strip().strip('"\'')
        elif re.match(r'^(Cookie|Referer|Authorization|User-Agent):', stripped, re.I):
            result['headers'].update(parse_header_blob(stripped))

    # A single URL/path is also accepted, which makes --input unnecessary for
    # manifests that do not need request headers.
    candidate = input_data.strip().strip('"\'')
    if not result['source'] and '\n' not in candidate and '\r' not in candidate:
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.scheme in {'http', 'https'} or os.path.isfile(candidate):
            result['source'] = candidate

    return result


def load_manifest(source, headers):
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme in {'http', 'https'}:
        request = urllib.request.Request(source)
        add_request_headers(request, headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            charset = response.headers.get_content_charset() or 'utf-8'
            return response.read().decode(charset)

    if os.path.isfile(source):
        with open(source, 'r', encoding='utf-8-sig') as manifest_file:
            return manifest_file.read()

    raise FileNotFoundError(f"清单不存在或不是受支持的 HTTP(S) URL: {source}")


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="解析并转推 HLS/MPEG-DASH；支持使用已有内容密钥实时处理 CENC MPD")
    parser.add_argument('--input', help="输入配置文本；建议把私有配置命名为 stream_input.txt")
    parser.add_argument('--source', help="M3U8/MPD URL 或本地清单路径")
    parser.add_argument('--manifest-content-file', help=argparse.SUPPRESS)
    parser.add_argument('--header', action='append', default=[],
                        help="源站请求头，可重复使用，例如 --header \"Referer: https://example.com/\"")
    parser.add_argument('--operation', choices=['download', 'play', 'push', 'manifest', 'inspect'],
                        help="跳过操作菜单")
    parser.add_argument('--quality', help="best、菜单编号、1920x1080 或 1080p")
    parser.add_argument('--stream-name', help="RTMP StreamName")
    parser.add_argument('--push-url', help="完整 RTMP(S) 推流地址，可包含平台签名")
    parser.add_argument('--drm-key-file', help="私有 KID:KEY 文本文件；不要提交到 Git")
    parser.add_argument('--drm-key-env', default=DRM_KEY_ENV_DEFAULT,
                        help=f"保存 KID:KEY 的环境变量名（默认 {DRM_KEY_ENV_DEFAULT}）")
    parser.add_argument('--n-m3u8dl-re', dest='downloader_path',
                        help="N_m3u8DL-RE 可执行文件路径")
    parser.add_argument('--ffmpeg-binary', dest='ffmpeg_path',
                        help="FFmpeg 可执行文件路径")
    parser.add_argument('--mp4decrypt-binary', dest='mp4decrypt_path',
                        help="mp4decrypt 可执行文件路径；存在时优先用于 CENC 解密")
    parser.add_argument('--relay-restarts', type=int, default=0,
                        help="转推进程异常退出后的重启次数")
    parser.add_argument('--relay-restart-delay', type=float, default=5,
                        help="转推重启等待秒数（默认 5）")
    parser.add_argument('--live-take-count', type=int, default=3,
                        help="首次获取的直播分片数（默认 3，越小启动越快）")

    # --- 页面自动收割参数（新） ---
    harvest_group = parser.add_argument_group("页面自动收割 — 从直播页面到推流全自动")
    harvest_group.add_argument(
        '--page-url',
        help="直播页面 URL。自动发现 MPD、完成 DRM 鉴权、获取内容密钥并推流。")
    harvest_group.add_argument(
        '--auth-cookie',
        dest='auth_cookie',
        help="登录后 Cookie（用于页面访问与 DRM 鉴权）。"
             " 也接受 Cookie: name=value 格式。")
    harvest_group.add_argument(
        '--wvd',
        dest='wvd_path',
        help="Widevine .wvd 设备文件路径（用于 license 交换）。")
    harvest_group.add_argument(
        '--channel-id',
        help="频道 ID（覆盖从页面提取的 drm_asset_id）。")
    harvest_group.add_argument(
        '--browser',
        action='store_true',
        dest='use_browser',
        help="Playwright 浏览器辅助模式（需要登录/验证码时人工操作）。")
    harvest_group.add_argument(
        '--browser-headless',
        action='store_true',
        dest='browser_headless',
        help="浏览器无头模式（仅已登录会话）。")

    return parser


def main(argv=None):
    args = build_argument_parser().parse_args(argv)

    # --- 页面自动收割管线（新） ---
    if args.page_url:
        if not _HARVEST_AVAILABLE:
            print("[错误] page_harvester 模块未找到。请确保 page_harvester.py 在同一目录。")
            return 1
        return run_harvest_and_push(args)

    internal_relay = bool(args.manifest_content_file)
    if internal_relay:
        print("LiveTools 推流子进程")
    else:
        print("=========================================================")
        print("LiveTools HLS / MPEG-DASH 视频流解析工具")
        print("=========================================================")
        print("支持 M3U8、MPD URL、本地清单、配置文本和 minyami 文本。")
        print("DASH ContentProtection 会被识别；已有内容密钥可通过私有文件或环境变量传入。")
        print("全新: 使用 --page-url 从直播页面自动发现 MPD 并获取内容密钥。")
        print("---------------------------------------------------------")

    try:
        if args.input:
            with open(args.input, 'r', encoding='utf-8-sig') as input_file:
                input_data = input_file.read()
        elif args.source:
            input_data = f"视频链接: {args.source}"
        else:
            print("请粘贴输入；结束时按 Ctrl+D (Linux/macOS) 或 Ctrl+Z 后回车 (Windows)。")
            input_data = sys.stdin.read()
    except Exception as error:
        print(f"[错误] 读取输入失败: {error}")
        return 1

    if not input_data.strip():
        print("\n[退出] 未接收到任何输入，程序退出。")
        return 0

    description = parse_input_description(input_data)
    headers = description['headers']
    for header_blob in args.header:
        headers.update(parse_header_blob(header_blob))
    cookie = headers.get('Cookie')

    if description['program_name']:
        print(f"[信息] 检测到节目名称: {description['program_name']}")
    if headers and not internal_relay:
        print(f"[信息] 使用请求头: {', '.join(headers.keys())}")

    manifest_source = args.source or description['source']
    hls_start = input_data.find('#EXTM3U')
    mpd_start_match = re.search(r'<(?:[\w.-]+:)?MPD\b', input_data)

    try:
        if hls_start != -1:
            manifest_content = input_data[hls_start:]
            manifest_type = 'hls'
        elif mpd_start_match and not manifest_source:
            manifest_content = input_data[mpd_start_match.start():]
            manifest_type = 'dash'
            manifest_source = ''
        elif manifest_source:
            if args.manifest_content_file:
                print("[信息] 使用已缓存清单解析")
                manifest_content = load_manifest(
                    args.manifest_content_file, make_headers(cookie, headers))
            else:
                print(f"[信息] 正在读取清单: {safe_source_label(manifest_source)}")
                manifest_content = load_manifest(
                    manifest_source, make_headers(cookie, headers))
            stripped_content = manifest_content.lstrip('\ufeff\r\n\t ')
            if stripped_content.startswith('#EXTM3U'):
                manifest_type = 'hls'
            else:
                root = ET.fromstring(manifest_content)
                if xml_local_name(root.tag) != 'MPD':
                    raise ValueError("输入既不是 M3U8，也不是 MPEG-DASH MPD")
                manifest_type = 'dash'
            print(f"[信息] 成功读取 {manifest_type.upper()} 清单。")
        else:
            print("[错误] 未找到 M3U8/MPD 内容、视频链接或本地清单路径。")
            return 1
    except Exception as error:
        print(f"[错误] 读取或解析清单失败: {error}")
        return 1

    try:
        if manifest_type == 'dash':
            streams = parse_mpd_string(manifest_content, manifest_source)
            if args.manifest_content_file:
                for stream in streams:
                    stream.relay_manifest_source = args.manifest_content_file
            if not internal_relay:
                print("[DRM] " + format_drm_summary(streams[0].drm_info))
        else:
            streams = parse_m3u8_string(manifest_content, manifest_source)
    except (ET.ParseError, ValueError) as error:
        print(f"[错误] 清单格式无效: {error}")
        return 1

    operation = args.operation or description['operation']
    quality = args.quality or description['quality']
    stream_name = args.stream_name or description['stream_name']
    if not operation and (stream_name or '').strip():
        operation = 'push'
    push_url = args.push_url or description['push_url']
    drm_key_file = args.drm_key_file or description['drm_key_file']
    drm_key_env = description['drm_key_env'] or args.drm_key_env
    downloader_path = args.downloader_path or description['downloader_path']
    ffmpeg_path = args.ffmpeg_path or description['ffmpeg_path']
    mp4decrypt_path = args.mp4decrypt_path or description['mp4decrypt_path']

    if operation == 'inspect':
        for index, stream in enumerate(streams, 1):
            print(f"[{index}] {stream.resolution} | {stream.bandwidth} | {stream.codecs}")
        return 0

    result = handle_user_choice(
        streams,
        cookie,
        description['program_name'],
        headers,
        requested_quality=quality,
        requested_operation=operation,
        stream_name=stream_name,
        push_url=push_url,
        drm_key_file=drm_key_file,
        drm_key_env=drm_key_env,
        downloader_path=downloader_path,
        ffmpeg_path=ffmpeg_path,
        mp4decrypt_path=mp4decrypt_path,
        restart_count=args.relay_restarts,
        restart_delay=args.relay_restart_delay,
        live_take_count=args.live_take_count,
    )
    print("\n程序运行结束。")
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
