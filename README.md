# LiveTools

https://live.neofantasy.online/

https://live.livetrial.me/

## Command Line Example (better for local videos)
ffmpeg -re -i "TargetVideo.ts" -c:v libx264 -preset veryfast -b:v 3000k -maxrate 3500k -bufsize 5000k -pix_fmt yuv420p -g 150 -keyint_min 150 -sc_threshold 0 -c:a aac -b:a 128k -ar 44100 -ac 2 -strict experimental -f flv rtmp://{IP:Port}/live/{stream name}

## Program Example (better for streamings)
- self-downloading
- self-watching
- push forward
- M3U8 illustration

## TODO
- CDN Services
- Stronger Encryption
- Responsive adaptation
- GUI
