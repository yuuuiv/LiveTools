import re
import os
import subprocess
import sys

# 定义一个类来存储解析出的视频流信息
class VideoStream:
    def __init__(self, resolution, bandwidth, url):
        self.resolution = resolution
        self.bandwidth = bandwidth
        self.url = url
    
    def __str__(self):
        return f"分辨率: {self.resolution} | 码率: {self.bandwidth} | URL: {self.url[:60]}..."

def parse_m3u8_string(input_string):
    """
    逐行解析 M3U8 字符串，提取视频流信息。
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

def handle_user_choice(streams):
    """
    处理用户的视频流选择和操作选择。
    """
    if not streams:
        print("\n[错误] 未找到任何视频流信息。")
        return

    # --- 1. 展示并选择视频流 ---
    print("\n--- 可用的视频流列表 (按分辨率排序) ---")
    try:
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
    print("[4] 退出")
    print("----------------------------")

    while True:
        operation = input("请输入操作编号 (1-4): ")
        if operation == '1':
            perform_download(selected_stream)
            break
        elif operation == '2':
            perform_playback(selected_stream)
            break
        elif operation == '3':
            # 调用新增的推流函数
            perform_livestream(selected_stream)
            break
        elif operation == '4':
            print("操作取消，程序退出。")
            break
        else:
            print("[警告] 输入无效，请重新输入正确的操作编号。")

def check_ffmpeg():
    """ 检查 FFmpeg 是否安装 """
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("\n[错误] FFmpeg 未安装或未添加到系统 PATH 中。无法执行此操作。")
        print("请访问 https://ffmpeg.org/ 下载并安装 FFmpeg。")
        return False

def perform_download(stream):
    if not check_ffmpeg(): return
    
    filename = f"HLS_Stream_{stream.resolution}_{stream.bandwidth.replace(' ', '_').replace('.', 'p')}.mp4"
    print(f"\n[开始] 正在开始下载到文件：{filename}")
    
    download_command = [
        "ffmpeg",
        "-i", stream.url,
        "-c", "copy",
        filename
    ]
    
    try:
        print("--- FFmpeg 输出 (按 Q 键停止下载) ---")
        subprocess.run(download_command)
        print("--- 下载命令执行完毕 ---")
    except Exception as e:
        print(f"[错误] 下载过程中发生错误: {e}")

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

def perform_livestream(stream):
    """
    使用 FFmpeg 将 HLS 流推送到 RTMP 服务器。
    """
    if not check_ffmpeg(): return
    
    # 您的服务器 IP 地址和配置的应用名称
    SERVER_IP = "165.22.106.165"
    APP_NAME = "live"
    
    # 提示用户输入推流密钥
    stream_key = input(f"请输入推流密钥 (例如: my_stream_key): ").strip()
    # 本次密钥为livestream
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
    ]

    try:
        print("--- FFmpeg 推流开始 (按 Ctrl+C 停止) ---")
        subprocess.run(livestream_command)
        print("--- 推流已停止 ---")
    except Exception as e:
        print(f"[错误] 推流过程中发生错误: {e}")


# --- 主执行逻辑 ---
if __name__ == "__main__":
    
    print("=========================================================")
    print("HLS M3U8 视频流解析工具")
    print("=========================================================")
    print("提示: 请粘贴完整的 M3U8 播放列表内容，然后按回车键。")
    print("对于多行内容，请在粘贴后按 Ctrl+D (Linux/macOS) 或 Ctrl+Z (Windows) 确认输入结束。")
    print("---------------------------------------------------------")

    try:
        input_data = sys.stdin.read()
    except Exception as e:
        print(f"[错误] 读取输入时发生错误: {e}")
        sys.exit(1)
        
    if not input_data.strip():
        print("\n[退出] 未接收到任何输入，程序退出。")
        sys.exit(0)

    m3u8_content_start = input_data.find("#EXTM3U")
    if m3u8_content_start == -1:
        print("\n[错误] 输入字符串中未找到 #EXTM3U 标记，无法解析为 M3U8 格式。")
    else:
        m3u8_content = input_data[m3u8_content_start:]
        
        streams = parse_m3u8_string(m3u8_content)
        handle_user_choice(streams)
        
    print("\n程序运行结束。")