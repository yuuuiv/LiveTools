# LiveTools

A collection of tools for live streaming, including HLS conversion, interactive streaming, and player interfaces.

## Websites

Deployed live streaming player interfaces are available for testing and production use.

## Features

- HLS conversion from TS files to M3U8 streams
- Interactive streaming scripts
- Web-based players for live streams
- Command-line tools for FFmpeg integration

## Usage

### Command Line Example (Better for Local Videos)

Use FFmpeg to convert and stream local videos:

```
ffmpeg -re -i "TargetVideo.ts" -c:v libx264 -preset veryfast -b:v 3000k -maxrate 3500k -bufsize 5000k -pix_fmt yuv420p -g 150 -keyint_min 150 -sc_threshold 0 -c:a aac -b:a 128k -ar 44100 -ac 2 -strict experimental -f flv rtmp://{IP:Port}/live/{stream name}
```

### Program Examples (Better for Streamings)

- Self-downloading capabilities
- Self-watching interfaces
- Push forward streaming
- M3U8 playlist illustration
- CDN Services integration
- HLS master/media playlist parsing with relative URL resolution
- Legal request header passthrough for Cookie, Referer, Authorization and User-Agent
- Refuses encrypted HLS playlists instead of attempting decryption
- Parses MPEG-DASH MPD representations and identifies standard DASH DRM declarations
- Relays authorized CENC DASH live streams when a content key is supplied explicitly
- Keeps content keys out of the wrapper/download-manager command line and log files

## MPEG-DASH / MPD

检查本地 MPD 的格式、清晰度和 DRM：

```powershell
python .\HLS_Stream_Interactive.py --source "C:\path\index.mpd" --operation inspect
```

本地 MPD 适合检查；要转推带相对分片地址的直播 MPD，请使用原始 MPD URL，
否则 FFmpeg 无法依据原始站点解析分片。

转推普通 MPD：

```powershell
python .\HLS_Stream_Interactive.py `
  --source "https://media.example.com/index.mpd" `
  --header "Referer: https://media.example.com/" `
  --operation push `
  --quality best `
  --stream-name 3696505
```

未传 `--push-url` 时，工具会自动推送到
`rtmp://push.neofantasy.online/live/<stream-name>`，并使用默认的阿里云 A 类
鉴权主 key `neofantasyonline` 生成短时 `auth_key`。如需切换环境，可通过
`LIVETOOLS_PUSH_AUTH_KEY`、`LIVETOOLS_PUSH_DOMAIN` 和
`LIVETOOLS_PUSH_AUTH_TTL_SECONDS` 覆盖默认值；传入 `--push-url` 时则直接使用
该完整地址。

如果每次都需要相同的源站请求头，可将 `stream_input.example.txt` 复制为
`stream_input.txt`，填入私有信息后运行：

```powershell
python .\HLS_Stream_Interactive.py --input .\stream_input.txt
```

`stream_input.txt` 和 `*.private.txt` 已被 Git 忽略。不要提交 Cookie、Bearer
Token、内容密钥或带签名参数的推流地址。MPD 中出现 `ContentProtection` 时，
工具会显示加密方案、DRM 系统与默认 KID。

### CENC DASH 实时解密转推

这条链路用于已经取得内容密钥的动态 MPD：

1. `N_m3u8DL-RE` 持续刷新 MPD、下载选定的视频与音频分片。
2. FFmpeg 解密 CENC 分片。
3. `N_m3u8DL-RE --live-pipe-mux` 通过命名管道把解密后的音视频交给 FFmpeg。
4. FFmpeg 以流复制方式输出 RTMP/RTMPS，默认不保留明文分片。

依赖：

- `N_m3u8DL-RE`（放入 PATH，或使用 `--n-m3u8dl-re` 指定）
- FFmpeg（放入 PATH，或使用 `--ffmpeg-binary` 指定）

脚本也会自动查找 `LiveTools/.tools/N_m3u8DL-RE.exe`；`.tools` 已被 Git 忽略。

PowerShell 示例。环境变量内容可写成 `KID:KEY`；当 MPD 只有一个 KID 时，
也可只写 32 位十六进制 KEY：

```powershell
$env:LIVETOOLS_DRM_KEY = '<KID>:<KEY>'

python .\HLS_Stream_Interactive.py `
  --source 'https://media.example.com/path/index.mpd' `
  --header 'Cookie: CloudFront-Policy=...; CloudFront-Signature=...' `
  --operation push `
  --quality 1080p `
  --stream-name 3696505 `
  --n-m3u8dl-re 'C:\Tools\N_m3u8DL-RE.exe' `
  --ffmpeg-binary 'D:\ffmpeg\bin\ffmpeg.exe' `
  --relay-restarts 3

Remove-Item Env:LIVETOOLS_DRM_KEY
```

也可以创建 `keys.private.txt`，每行写一个 `KID:KEY`，然后使用
`--drm-key-file .\keys.private.txt`。`*.private.txt` 已在 `.gitignore` 中。
包装器会校验密钥是否覆盖 MPD 声明的全部 KID，再生成短生命周期的临时密钥
文件；子进程环境中会移除原始密钥变量，并关闭 `N_m3u8DL-RE` 文件日志。
解密器仍会在短生命周期的 FFmpeg 子进程参数中接收原始 KEY，这是
`N_m3u8DL-RE` 当前 FFmpeg 解密后端的工作方式；请在受控主机上运行。

直播转推必须使用原始 HTTP(S) MPD URL。下载到本地的 `index.mpd` 适合检查
KID、清晰度和编码，但此类清单通常只有相对分片地址，且不会继续刷新。

常用参数：

- `--push-url`: 完整 RTMP/RTMPS 目标地址（可含推流鉴权参数）
- `--live-take-count`: 首次抓取分片数，默认 3
- `--relay-restarts`: 异常退出后的重启次数
- `--relay-restart-delay`: 每次重启前的等待秒数

## 页面自动收割 — 从直播页到推流全自动

🚀 **全新功能**：给定直播页面 URL，自动完成 MPD 发现 → DRM 鉴权 →
Widevine 许可证交换 → 一键推流。

### 工作原理（协议逆向层级）

```
页面 HTML (Edge/Chrome UA)
  │  extract_page_config()
  ├── app_id          ← app.application_id
  ├── channels_dash   ← [MPD URL, ...]
  ├── drm_asset_id    ← drmEncryptKey.assetId
  └── presto_license  ← drmEncryptKey.prestoLicense
        │
        ▼ request_drm_auth_token()
  DRM 鉴权接口: /api/stream-vp/{app_id}/get_auth_token_drm?channel_id=...
        │
        ├── auth_token
        ├── sessionId
        ├── user_id
        └── merchantId
              │
              ▼ exchange_widevine_license()
  Widevine License Server (prestoLicense)
        │  PSSH (from MPD) + auth_token
        │
        └── KID:KEY 对 → 直通现有 CENC 解密转推管线
```

每层按请求边界隔离，字段名保留原始大小写，不跨层推断。

### 快速开始

```powershell
# 直接 HTTP 模式（页面内嵌配置，无需浏览器）
python .\HLS_Stream_Interactive.py `
  --page-url 'https://example.com/live/12345' `
  --auth-cookie 'session=eyJ...' `
  --wvd 'device.wvd' `
  --stream-name mystream `
  --operation push
```

```powershell
# 浏览器辅助模式（需要登录/验证码时）
python .\HLS_Stream_Interactive.py `
  --page-url 'https://example.com/live/12345' `
  --wvd 'device.wvd' `
  --browser `
  --stream-name mystream
```

### 新增参数

| 参数 | 说明 |
|------|------|
| `--page-url` | 直播页面 URL |
| `--auth-cookie` | 登录后 Cookie 字符串 |
| `--wvd` | Widevine .wvd 设备文件路径 |
| `--channel-id` | 覆盖页面提取的 drm_asset_id |
| `--browser` | 启用 Playwright 浏览器辅助 |
| `--browser-headless` | 浏览器无头模式 |

### 依赖

```powershell
pip install requests          # HTTP 会话（推荐）
pip install pywidevine        # Widevine 许可证交换
pip install playwright        # 浏览器辅助模式（可选）
playwright install chromium
```

### 仅收割（不推流）

省略 `--operation push` 和 `--stream-name`，工具将只执行收割并输出
MPD URL 与 KID:KEY 对，供后续手动使用：

```powershell
python .\HLS_Stream_Interactive.py `
  --page-url 'https://example.com/live/12345' `
  --auth-cookie 'session=...' `
  --wvd 'device.wvd'
```

### DRM 鉴权响应结构验证

收割过程中会自动输出 DRM 鉴权接口的响应字段名与值长度
（不输出实际值，保护隐私）：

```
[DRM Auth] 请求: https://.../get_auth_token_drm?channel_id=...
  auth_token 长度:  196
  sessionId 长度:   36
  user_id 长度:     24
  merchantId 长度:  8
  响应顶层结构: data:{auth_token:str(196), sessionId:str(36), ...}, code:num(0), msg:str(2)
```

## Files

- `HLS_convert.bat`: Batch script for HLS conversion
- `HLS_Stream_Interactive.py`: Python script for interactive streaming
- `page_harvester.py`: **新** 页面自动收割模块
- `index.html`: Main player interface
- `player.html`: Additional player

## TODO

- Implement stronger encryption
- Add responsive adaptation
- Develop a GUI interface

## Requirements

- FFmpeg for command-line operations
- Python for interactive scripts
- Modern web browser for players

## 更新说明

脚本现已支持解析 minyami 风格的输入格式。

## 新增功能

### 1. Minyami 命令格式解析

脚本现在可以从以下格式的输入中提取信息：

```
节目id:AAAAAA-BBBB-CCC
节目名称:NAME
下载命令:
minyami -d "https://example.com/stream.m3u8" --threads 8 --headers "Cookie: key1=val1;key2=val2" -o "output.ts"
```

### 2. 标准 HLS 请求头

除 `Cookie:` 外，也可以提供 `Referer:`、`Authorization:` 和
`User-Agent:`。解析器不再依赖 `index_N_N.ts` 的分片命名，而是直接读取
playlist 中的分片 URL，并兼容 master playlist 的相对地址与 query 参数。

仅对你有权访问和转播的普通未加密 HLS 使用这些功能。遇到带有非 `NONE`
`#EXT-X-KEY` 的 playlist，工具会停止并提示，不会尝试解密或绕过 DRM。

### 3. 自动提取信息

- **URL 提取**：从 `minyami -d "..."` 中提取 URL
- **Cookie 提取**：从 `--headers "Cookie: ..."` 中提取 Cookie
- **节目名称提取**：从 `节目名称:` 行提取名称，用作建议的文件名

### 4. 智能文件名建议

当检测到节目名称时，下载时会自动建议使用清理后的节目名称作为文件名：
- 非法字符（`<>:"/\|?*`）会被替换为下划线
- 格式：`{节目名称}_{分辨率}.ts`

### 4. 向后兼容

原有的输入格式仍然支持：

```
视频链接: https://example.com/stream.m3u8
Cookie: key1=val1;key2=val2
```

## 使用方法

### 方式 1：通过管道输入

```bash
cat test_minyami_input.txt | python HLS_Stream_Interactive.py
```

或

```bash
python HLS_Stream_Interactive.py < test_minyami_input.txt
```

### 方式 2：直接运行并粘贴

```bash
python HLS_Stream_Interactive.py
```

然后粘贴包含 minyami 命令的文本，按 Ctrl+Z (Windows) 或 Ctrl+D (Linux/Mac) 结束输入。

## 正则表达式说明

脚本使用以下正则表达式进行解析：

1. **节目名称**：`r'节目名称[:：]\s*(.+)'`
   - 支持中英文冒号
   - 提取冒号后的所有内容

2. **Minyami URL**：`r'minyami\s+-d\s+["\']([^"\'\n]+)["\']'`
   - 匹配 `-d` 参数后的 URL
   - 支持单引号和双引号

3. **Minyami Cookie**：`r'--headers\s+["\']Cookie:\s*([^"\'\n]+)["\']'`
   - 从 `--headers` 参数中提取 Cookie
   - 支持单引号和双引号

## 错误处理

- 如果 minyami 格式解析失败，会自动回退到原有的格式解析
- 缺少某些字段（如节目名称或 Cookie）不会导致程序崩溃
- 所有提取到的信息都会在控制台中显示

## 示例输出

```
[信息] 检测到节目名称: NAME
[信息] 从 minyami 命令中提取到 URL: https://example.com/stream.m3u8
[信息] 从 minyami 命令中提取到 Cookie
[信息] 检测到视频链接: https://example.com/stream.m3u8
[信息] 使用提供的Cookie进行请求。
[信息] 成功下载M3U8内容。
```

下载时的文件名建议：
```
请输入完整的保存路径和文件名 (默认为当前目录下的 NAME_1080p.ts):
```
