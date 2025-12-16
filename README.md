# LiveTools

A collection of tools for live streaming, including HLS conversion, interactive streaming, and player interfaces.

## Websites

- [Live Site 1](https://live.neofantasy.online/)
- [Live Site 2](https://live.livetrial.me/)

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

## Files

- `HLS_convert.bat`: Batch script for HLS conversion
- `HLS_Stream_Interactive.py`: Python script for interactive streaming
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