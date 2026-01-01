# HLS_Stream_Interactive.py - Minyami 格式支持

## 更新说明

脚本现已支持解析 minyami 风格的输入格式。

## 新增功能

### 1. Minyami 命令格式解析

脚本现在可以从以下格式的输入中提取信息：

```
节目id:351452-0070-001
节目名称:わんちゃんわんわんねこにゃんにゃん ＷＡＮＮＹＡＮ　５ｔｈ　ＡＮＮＩＶＥＲＳＡＲＹ　ＯＮＥＭＡＮ　ＬＩＶＥ
下载命令:
minyami -d "https://example.com/stream.m3u8" --threads 8 --headers "Cookie: key1=val1;key2=val2" -o "output.ts"
```

### 2. 自动提取信息

- **URL 提取**：从 `minyami -d "..."` 中提取 URL
- **Cookie 提取**：从 `--headers "Cookie: ..."` 中提取 Cookie
- **节目名称提取**：从 `节目名称:` 行提取名称，用作建议的文件名

### 3. 智能文件名建议

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
[信息] 检测到节目名称: わんちゃんわんわんねこにゃんにゃん ＷＡＮＮＹＡＮ　５ｔｈ　ＡＮＮＩＶＥＲＳＡＲＹ　ＯＮＥＭＡＮ　ＬＩＶＥ
[信息] 从 minyami 命令中提取到 URL: https://example.com/stream.m3u8
[信息] 从 minyami 命令中提取到 Cookie
[信息] 检测到视频链接: https://example.com/stream.m3u8
[信息] 使用提供的Cookie进行请求。
[信息] 成功下载M3U8内容。
```

下载时的文件名建议：
```
请输入完整的保存路径和文件名 (默认为当前目录下的 わんちゃんわんわんねこにゃんにゃん_ＷＡＮＮＹＡＮ_５ｔｈ_ＡＮＮＩＶＥＲＳＡＲＹ_ＯＮＥＭＡＮ_ＬＩＶＥ_1080p.ts):
```
