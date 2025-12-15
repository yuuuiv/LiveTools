@echo off
chcp 65001 >nul
title 视频转码和重命名脚本
echo ========================================
echo        视频转码和重命名脚本
echo ========================================

REM 检查ffmpeg是否可用
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo 错误: 未找到ffmpeg，请确保ffmpeg已添加到系统PATH中
    pause
    exit /b 1
)

REM 获取用户输入的文件名
echo.
echo 请输入要处理的视频文件名（包括扩展名）:
echo 示例: [251119] [edited Rehearsal] 蓮ノ空5th 大阪Day1.ts
echo.
set /p input_file="文件名: "

REM 检查输入文件是否存在
if not exist "%input_file%" (
    echo.
    echo 错误: 输入文件 "%input_file%" 不存在
    pause
    exit /b 1
)

REM 获取用户自定义的输出文件名
echo.
echo 请输入输出文件名（不含扩展名）:
echo 示例: MyCustomOutput
echo.
set /p output_name="输出文件名: "

REM 如果用户未输入，使用默认的输入文件名
if "%output_name%"=="" (
    for %%F in ("%input_file%") do (
        set "output_name=%%~nF"
    )
)

REM 创建输出目录
if not exist "VOD_Output" (
    mkdir "VOD_Output"
    echo 已创建输出目录: VOD_Output
)

echo.
echo 输入文件: %input_file%
echo 输出名称: %output_name%
echo 输出目录: VOD_Output
echo.

echo 步骤1: 开始视频转码...
echo ========================================
echo 正在将视频转换为HLS格式...
echo.

REM 执行ffmpeg转码命令
ffmpeg -i "%input_file%" -c:v libx264 -crf 23 -profile:v high -level 4.0 -pix_fmt yuv420p -c:a aac -b:a 128k -ac 2 -hls_time 10 -hls_list_size 0 -hls_flags delete_segments -hls_segment_filename "VOD_Output/%output_name%_%%d.ts" -f hls "VOD_Output/%output_name%.m3u8"

if %errorlevel% neq 0 (
    echo.
    echo 错误: 视频转码失败
    pause
    exit /b 1
)

echo.
echo 步骤1完成: 视频转码成功!
echo.

:show_tree
echo ========================================
echo 输出目录结构:
echo ========================================
echo VOD_Output/
if exist "VOD_Output" (
    REM 显示目录树结构
    for /f "delims=" %%F in ('dir /b /a-d "VOD_Output" ^| findstr /v /i "\.m3u8$"') do (
        echo ├── %%F
    )
    for /f "delims=" %%F in ('dir /b /a-d "VOD_Output" ^| findstr /i "\.m3u8$"') do (
        echo └── %%F
    )
)

echo.
echo ========================================
echo 所有任务已完成!
echo.
echo 输出文件:
echo   - VOD_Output\%output_name%.m3u8
echo   - VOD_Output\%output_name%_1.ts
echo   - VOD_Output\%output_name%_2.ts
echo   - ... (其他.ts片段)
echo ========================================
echo.

REM 显示实际的.ts文件列表
echo 实际的.ts文件列表:
dir /b "VOD_Output\*.ts" 2>nul || echo (无.ts文件)
echo.

REM 显示.m3u8文件内容（前几行）
echo .m3u8文件内容预览:
if exist "VOD_Output\%output_name%.m3u8" (
    echo.
    type "VOD_Output\%output_name%.m3u8" | findstr /n "." | findstr "^[1-5]:"
    echo ...
)

echo.
pause