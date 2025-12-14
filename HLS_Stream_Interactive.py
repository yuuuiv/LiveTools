import re
import os
import subprocess
import sys
import urllib.request
import urllib.parse
import time
import shutil # 用于处理临时目录和文件移动
import concurrent.futures # 新增导入
import asyncio # 用于异步下载
import threading

# --- 核心数据结构 ---

class VideoStream:
    """定义一个类来存储解析出的视频流信息"""
    def __init__(self, resolution, bandwidth, url):
        self.resolution = resolution
        self.bandwidth = bandwidth
        self.url = url
    
    def __str__(self):
        return f"分辨率: {self.resolution} | 码率: {self.bandwidth} | URL: {self.url[:60]}..."

# --- M3U8 解析函数 ---

def parse_m3u8_string(input_string, base_url=None):
    """
    逐行解析 M3U8 字符串，提取视频流信息。
    如果提供 base_url，则将相对 URL 转换为绝对 URL。
    """
    streams = []
    lines = input_string.splitlines() 
    
    stream_info_pattern = re.compile(r'^#EXT-X-STREAM-INF:(.+)')
    resolution_pattern = re.compile(r'RESOLUTION=([\d]+x[\d]+)')
    bandwidth_pattern = re.compile(r'BANDWIDTH=([\d]+)')
    
    current_info_attributes = None
    
    for line in lines:
        line = line.strip() 
        
        if not line:
            continue 
            
        # 1. 检查是否是配置行 (#EXT-X-STREAM-INF)
        info_match = stream_info_pattern.match(line)
        if info_match:
            current_info_attributes = info_match.group(1)
            
        # 2. 检查是否是 URL 行 
        elif current_info_attributes is not None and not line.startswith('#'):
            url = line
            if base_url and not url.startswith('http'):
                url = urllib.parse.urljoin(base_url, url)
            
            resolution = "N/A"
            bandwidth_raw = 0
            
            resolution_match = resolution_pattern.search(current_info_attributes)
            if resolution_match:
                resolution = resolution_match.group(1)
                
            bandwidth_match = bandwidth_pattern.search(current_info_attributes)
            if bandwidth_match:
                bandwidth_raw = int(bandwidth_match.group(1))
            
            bandwidth = f"{bandwidth_raw / 1000000:.2f} Mbps" 
            
            streams.append(VideoStream(resolution, bandwidth, url))
            
            current_info_attributes = None
            
        elif line.startswith('#'):
            current_info_attributes = None
            
    return streams

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
async def async_download_segment(session, ts_url, ts_local_path, cookie, max_retries=3):
    """
    异步下载单个分片，失败重试 max_retries 次，并在下载前检查本地是否存在。
    返回: (成功状态, 文件路径, 是否跳过)
    """
    
    # * 断点续传/存在性检查 *
    if os.path.exists(ts_local_path) and os.path.getsize(ts_local_path) > 0:
        return True, ts_local_path, True # 成功，已跳过
    
    # 如果文件不存在，则开始下载
    for attempt in range(max_retries):
        try:
            # 使用 urllib.request 进行同步下载的包装器 (在线程池中运行)
            def sync_fetch():
                req = urllib.request.Request(ts_url)
                if cookie:
                    req.add_header('Cookie', cookie)
                
                with urllib.request.urlopen(req, timeout=10) as response: 
                    # 写入文件
                    with open(ts_local_path, 'wb') as out_file:
                        out_file.write(response.read())
            
            # 阻塞调用，但放入线程池中运行，不阻塞事件循环
            await asyncio.get_event_loop().run_in_executor(None, sync_fetch) 
            return True, ts_local_path, False # 成功，未跳过
        
        except Exception as e:
            if attempt < max_retries - 1:
                # print(f"\n[警告] 分片 {os.path.basename(ts_local_path)} 下载失败 (第 {attempt + 1} 次)，正在重试...")
                await asyncio.sleep(5) # 重试前等待 5 秒
            else:
                # print(f"\n[严重警告] 分片 {os.path.basename(ts_local_path)} 最终下载失败，跳过。错误: {e}")
                return False, ts_local_path, False # 最终失败，未跳过

    return False, ts_local_path, False

async def async_perform_download(stream, cookie=None):
    """
    三阶段下载与合并 (异步并发下载历史分片，FFmpeg 下载实时分片)
    """
    if not check_ffmpeg(): return
    
    # --- 0. 初始化和路径设置 ---
    default_filename = f"HLS_Stream_FULL_{stream.resolution}_{stream.bandwidth.replace(' ', '_').replace('.', 'p')}.ts"
    output_path = input(f"\n请输入完整的保存路径和文件名 (默认为当前目录下的 {default_filename}): ").strip()
    final_output_filename = output_path if output_path else default_filename
    
    temp_dir = os.path.join(os.path.dirname(final_output_filename) or os.getcwd(), f"temp_hls_download_{int(time.time())}")
    base_name = os.path.splitext(os.path.basename(final_output_filename))[0]
    history_output_file = os.path.join(temp_dir, f"{base_name}_0.ts")
    live_output_file = os.path.join(temp_dir, f"{base_name}_1.ts")
    
    print(f"\n[开始] 正在开始三阶段下载，最终文件：{final_output_filename}")
    print(f"[信息] 所有临时文件将存储在: {temp_dir}")
    
    download_success = True
    
    try:
        os.makedirs(temp_dir, exist_ok=True)
    except Exception as e:
        print(f"[错误] 无法创建临时目录 {temp_dir}: {e}")
        return

    # --- 1. 阶段 1/3: 准备工作 (M3U8 解析) ---
    print("\n--- 阶段 1/3: 准备工作 (解析流信息) ---")
    
    # ---------------------------------------------------------------------
    # 同步 M3U8 解析代码 (与上一个版本保持一致)
    # ---------------------------------------------------------------------
    top_level_url = stream.url 
    final_stream_url = top_level_url
    
    try:
        req = urllib.request.Request(top_level_url)
        if cookie:
            req.add_header('Cookie', cookie)
        with urllib.request.urlopen(req) as response:
            top_m3u8_content = response.read().decode('utf-8')
        
        sub_streams = parse_m3u8_string(top_m3u8_content, base_url=top_level_url)
        user_bandwidth_raw = int(float(stream.bandwidth.split()[0]) * 1000000) 
        selected_sub_stream_url = None
        
        for s in sub_streams:
            s_bandwidth_raw = 0
            try:
                s_bandwidth_raw = int(float(s.bandwidth.split()[0]) * 1000000)
            except:
                pass
            
            resolution_match = (s.resolution == stream.resolution)
            bandwidth_match = abs(s_bandwidth_raw - user_bandwidth_raw) < 10000 
            
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
        if cookie:
            req.add_header('Cookie', cookie)
        with urllib.request.urlopen(req) as response:
            live_m3u8_content = response.read().decode('utf-8')
            
        ts_url_pattern = re.compile(r'index_(\d)_(\d+)\.ts(\?m=\d+)')
        last_index = -1
        last_segment_url = None
        
        for line in live_m3u8_content.splitlines():
            line = line.strip()
            ts_match = ts_url_pattern.search(line)
            if ts_match:
                current_index = int(ts_match.group(2))
                if current_index > last_index:
                    last_index = current_index
                    last_segment_url = line 
        
        if last_index == -1:
            print("[错误] 未能在子流 M3U8 中找到可识别的分片 URL 模式。下载中止。")
            return
            
        print(f"[信息] 检测到最新的分片索引 N 为: {last_index}。")
            
        base_prefix_match = re.search(r'(.*/index_\d+)\.m3u8', final_stream_url)
        
        if not base_prefix_match:
            final_stream_dir = final_stream_url.rsplit('/', 1)[0]
            index_match = re.search(r'(index_\d+)', last_segment_url)
            if index_match:
                 base_prefix = f"{final_stream_dir}/{index_match.group(1)}"
            else:
                print("[严重错误] 无法从 URL 构造分片基础前缀。下载中止。")
                return
        else:
             base_prefix = base_prefix_match.group(1) 
        
        url_suffix_match = re.search(r'(\.ts\?m=\d+)', last_segment_url)
        url_suffix = url_suffix_match.group(1) if url_suffix_match else ".ts"
        
    except Exception as e:
        print(f"[错误] 阶段 1 发生致命错误: {e}")
        return
    
    # --- 2. 阶段 A: 异步并发下载历史分片 (0 到 N) ---
    
    print(f"\n--- 阶段 2/3: 异步并发下载历史分片 (索引 0 到 {last_index}) ---")
    total_segments = last_index + 1
    print(f"[信息] 将使用 asyncio 并发下载 {total_segments} 个历史分片 (重试 3 次，支持断点续传)。")
    
    # 2.1 准备下载任务列表
    tasks = []
    for i in range(total_segments):
        ts_url = f"{base_prefix}_{i}{url_suffix}"
        ts_local_path = os.path.join(temp_dir, f"segment_{i}.ts")
        tasks.append(async_download_segment(None, ts_url, ts_local_path, cookie, max_retries=3))
    
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
                    filelist_f.write(f"file '{seg_name}'\n")

        print(f"\n[信息] 历史分片下载完成。开始合并 {history_segments_count} 个分片到 {os.path.basename(history_output_file)}")
        
        merge_command_0 = [
            "ffmpeg", "-f", "concat", "-safe", "0", "-i", file_list_path, 
            "-c", "copy", history_output_file
        ]
        
        try:
            original_cwd = os.getcwd()
            os.chdir(temp_dir)
            subprocess.run(merge_command_0, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            os.chdir(original_cwd) 
            print(f"[成功] 历史分片合并为 {os.path.basename(history_output_file)} 完成。")
        except subprocess.CalledProcessError as e:
            print(f"[严重错误] 历史分片合并失败。中止。")
            print(e.stderr.decode())
            download_success = False


    # --- 3. 阶段 B: FFmpeg 下载后续直播分片 ($N+1$ 到 End) ---

    if download_success:
        print(f"\n--- 阶段 3/3: 下载后续直播分片 ({last_index + 1} 到 End) ---")
        print("[信息] 使用 FFmpeg 实时下载 (内置重试机制: -reconnect, 间隔 5s)。")
        
        download_command_1 = ["ffmpeg"]
        
        if cookie:
            download_command_1.extend(["-headers", f"Cookie: {cookie}"])
            
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
                original_cwd = os.getcwd()
                os.chdir(temp_dir)
                subprocess.run(final_merge_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                os.chdir(original_cwd)
                print(f"\n[成功] 所有部分已合并并保存到最终文件: {final_output_filename}")
            except subprocess.CalledProcessError as e:
                print(f"[严重错误] 最终合并失败。请检查FFmpeg输出。")
                print(e.stderr.decode())
        
        elif history_exists:
            print(f"\n[信息] 未检测到后续直播内容。直接将历史文件移动到: {final_output_filename}")
            try:
                shutil.move(history_output_file, final_output_filename)
                print(f"[成功] 文件保存到: {final_output_filename}")
            except Exception as e:
                print(f"[错误] 无法移动历史文件: {e}")
        
        elif live_exists:
            print(f"\n[信息] 历史下载失败，只将直播部分移动到: {final_output_filename}")
            try:
                shutil.move(live_output_file, final_output_filename)
                print(f"[成功] 文件保存到: {final_output_filename}")
            except Exception as e:
                print(f"[错误] 无法移动直播文件: {e}")

    # --- 5. 清理 ---
    try:
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir)
            print(f"\n[清理] 已移除临时目录: {temp_dir}")
    except Exception as e:
        print(f"[警告] 无法自动清理临时目录，请手动删除: {temp_dir} ({e})")
        
    print("\n程序运行结束。")

def perform_download(stream, cookie=None):
    """
    同步调用 async_perform_download，作为程序的主要入口。
    """
    try:
        # 使用 asyncio.run 运行异步主函数
        asyncio.run(async_perform_download(stream, cookie))
    except KeyboardInterrupt:
        print("\n[中断] 用户手动停止下载。")
    except Exception as e:
        print(f"\n[致命错误] 程序运行出错: {e}")

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

def perform_livestream(stream, cookie=None):
    """
    使用 FFmpeg 将 HLS 流推送到 RTMP 服务器。
    """
    if not check_ffmpeg(): return
    
    # 您的服务器 IP 地址和配置的应用名称
    SERVER_IP = "165.22.106.165"
    APP_NAME = "live"
    
    # 提示用户输入推流密钥
    stream_key = input(f"请输入推流密钥 (例如: my_stream_key): ").strip()
    
    if not stream_key:
        print("[错误] 推流密钥不能为空，操作取消。")
        return
        
    rtmp_url = f"rtmp://{SERVER_IP}:1935/{APP_NAME}/{stream_key}"

    print(f"\n[推流] 正在将 HLS 流 ({stream.resolution} @ {stream.bandwidth}) 推送到 {rtmp_url}")
    print(f"[注意] 推流开始后，您可以在浏览器中访问以下 HLS 地址观看：")
    print(f"       http://{SERVER_IP}/hls/{stream_key}.m3u8")

    # 构建推流命令 (使用兼容性更高的转码命令)
    livestream_command = [
        "ffmpeg",
        # 关键设置: 忽略输入流中的时间戳错误，对直播源尤其重要
        "-fflags", "+genpts",
    ]
    
    if cookie:
        livestream_command.extend(["-headers", f"Cookie: {cookie}"])
    
    # 尝试使用 -i 自动处理流选择
    livestream_command.extend([
        # 输入源
        "-i", stream.url,
        # 视频编码参数 (H.264 快速编码)
        "-c:v", "libx264", 
        "-preset", "veryfast", 
        "-b:v", "4000k",        # 目标码率 4 Mbps
        "-maxrate", "5000k", 
        "-bufsize", "7000k", 
        "-pix_fmt", "yuv420p",
        # 音频编码参数
        "-c:a", "aac", 
        "-b:a", "128k", 
        # 输出格式和目标地址
        "-f", "flv", 
        rtmp_url
    ])

    try:
        print("--- FFmpeg 推流开始 (按 Ctrl+C 停止) ---")
        subprocess.run(livestream_command)
        print("--- 推流已停止 ---")
    except Exception as e:
        print(f"[错误] 推流过程中发生错误: {e}")

# --- 用户交互逻辑 ---

def view_or_download_m3u8(stream, cookie=None):
    """
    查看或下载指定流的 M3U8 列表内容。
    """
    print("\n--- 查看/下载 M3U8 列表 ---")
    print("[1] 查看 M3U8 内容")
    print("[2] 下载 M3U8 文件")
    print("----------------------------")

    while True:
        choice = input("请选择 (1-2): ").strip()
        if choice == '1':
            # 查看 M3U8 内容
            try:
                req = urllib.request.Request(stream.url)
                if cookie:
                    req.add_header('Cookie', cookie)
                with urllib.request.urlopen(req) as response:
                    content = response.read().decode('utf-8')
                print("\n--- M3U8 内容 ---")
                print(content)
                print("--- 内容结束 ---")
            except Exception as e:
                print(f"[错误] 下载 M3U8 内容失败: {e}")
            break
        elif choice == '2':
            # 下载 M3U8 文件
            filename = f"{os.path.splitext(os.path.basename(stream.url))[0]}.m3u8"
            try:
                req = urllib.request.Request(stream.url)
                if cookie:
                    req.add_header('Cookie', cookie)
                with urllib.request.urlopen(req) as response:
                    with open(filename, 'wb') as f:
                        f.write(response.read())
                print(f"[成功] M3U8 文件已保存为: {filename}")
            except Exception as e:
                print(f"[错误] 下载 M3U8 文件失败: {e}")
            break
        else:
            print("[警告] 输入无效，请重新输入 1 或 2。")

def handle_user_choice(streams, cookie=None):
    """
    处理用户的视频流选择和操作选择。
    """
    if not streams:
        print("\n[错误] 未找到任何视频流信息。")
        return

    # --- 1. 展示并选择视频流 ---
    print("\n--- 可用的视频流列表 (按分辨率排序) ---")
    try:
        # 尝试按分辨率高度排序
        streams.sort(key=lambda x: int(x.resolution.split('x')[1]) if 'x' in x.resolution else 0, reverse=True)
    except:
        pass 
    
    for i, stream in enumerate(streams):
        print(f"[{i + 1}] {stream.resolution.ljust(10)} | {stream.bandwidth.rjust(10)} | URL: {stream.url[:70]}...")
    print("---------------------------------------------------------------------------------------------------")
    
    while True:
        try:
            choice = input(f"请输入要操作的视频流编号 (1-{len(streams)}): ")
            stream_index = int(choice) - 1
            if 0 <= stream_index < len(streams):
                selected_stream = streams[stream_index]
                break
            else:
                print("[警告] 输入无效，请重新输入正确的编号。")
        except ValueError:
            print("[警告] 输入无效，请输入数字。")

    print(f"\n[选择] 您选择了：{selected_stream.resolution}，码率：{selected_stream.bandwidth}")
    
    # --- 2. 选择操作 ---
    print("\n--- 请选择要进行的操作 ---")
    print("[1] 下载 (需要 FFmpeg)")
    print("[2] 本地播放 (PotPlayer/VLC)")
    print("[3] 推流直播 (需要 FFmpeg)")
    print("[4] 查看/下载 M3U8 列表")
    print("[5] 退出")
    print("----------------------------")

    while True:
        operation = input("请输入操作编号 (1-5): ")
        if operation == '1':
            perform_download(selected_stream, cookie)
            break
        elif operation == '2':
            perform_playback(selected_stream)
            break
        elif operation == '3':
            perform_livestream(selected_stream, cookie)
            break
        elif operation == '4':
            view_or_download_m3u8(selected_stream, cookie)
            break
        elif operation == '5':
            print("操作取消，程序退出。")
            break
        else:
            print("[警告] 输入无效，请重新输入正确的操作编号。")

# --- 主执行逻辑 ---
if __name__ == "__main__":
    
    print("=========================================================")
    print("HLS M3U8 视频流解析工具")
    print("=========================================================")
    print("提示: 请粘贴完整的 M3U8 播放列表内容，或包含视频链接的文本。")
    print("---------------------------------------------------------")

    try:
        # 默认从标准输入读取所有数据
        input_data = sys.stdin.read()
    except Exception as e:
        print(f"[错误] 读取输入时发生错误: {e}")
        sys.exit(1)
        
    if not input_data.strip():
        print("\n[退出] 未接收到任何输入，程序退出。")
        sys.exit(0)

    m3u8_content_start = input_data.find("#EXTM3U")
    base_url = None
    cookie = None
    
    if m3u8_content_start != -1:
        # 情况 1: 用户直接粘贴了 M3U8 内容
        m3u8_content = input_data[m3u8_content_start:]
    else:
        # 情况 2: 用户粘贴了包含链接和 Cookie 的文本
        lines = input_data.splitlines()
        url = None
        for line in lines:
            line = line.strip()
            if line.startswith("视频链接:"):
                url = line.split(":", 1)[1].strip()
            elif line.startswith("Cookie:"):
                cookie = line.split(":", 1)[1].strip()
        
        if url:
            print(f"[信息] 检测到视频链接: {url}")
            if cookie:
                print("[信息] 使用提供的Cookie进行请求。")
            
            base_url = url 
            
            # 下载M3U8内容
            req = urllib.request.Request(url)
            if cookie:
                req.add_header('Cookie', cookie)
            
            try:
                with urllib.request.urlopen(req, timeout=15) as response: # 增加超时设置
                    m3u8_content = response.read().decode('utf-8')
                print("[信息] 成功下载M3U8内容。")
            except Exception as e:
                print(f"[错误] 下载M3U8内容失败: {e}")
                sys.exit(1)
        else:
            print("\n[错误] 输入字符串中未找到 #EXTM3U 标记或视频链接，无法解析。")
            sys.exit(1)
    
    streams = parse_m3u8_string(m3u8_content, base_url)
    handle_user_choice(streams, cookie)
        
    print("\n程序运行结束。")
