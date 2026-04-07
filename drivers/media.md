# Media Driver

ENVIRONMENT: bash shell for audio/video/image processing.
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
  ffmpeg -i in.mp4 out.mp3         → convert formats
  ffmpeg -i in.mp4 -ss 00:01:00 -t 30 out.mp4  → extract clip
  ffmpeg -i in.mp4 -vn -acodec copy out.aac     → extract audio
  ffprobe -v quiet -print_format json -show_streams in.mp4  → media info
  yt-dlp URL -o out.mp4            → download video
  yt-dlp --extract-audio URL       → download audio only
  yt-dlp --list-formats URL        → show available formats
  convert in.png -resize 50% out.png  → resize image (ImageMagick)
  convert in.png -quality 85 out.jpg  → convert + compress

PATTERNS:
- Get duration: ffprobe -v error -show_entries format=duration -of default=nw=1 in.mp4
- Thumbnail: ffmpeg -i in.mp4 -ss 00:00:05 -frames:v 1 thumb.jpg
- Audio waveform: ffmpeg -i in.mp3 -filter_complex showwavespic -frames:v 1 wave.png
- Batch convert: for f in *.png; do convert "$f" "${f%.png}.jpg"; done
- Concatenate: ffmpeg -f concat -i filelist.txt -c copy out.mp4

PITFALLS:
- ffmpeg overwrites without asking: add -y flag to confirm, -n to skip
- yt-dlp rate limiting: add --sleep-interval 2
- ImageMagick convert conflicts with Windows convert: use magick on Windows
- Large files: check disk space with df -h before processing
- Codec issues: use -c:v libx264 -c:a aac for maximum compatibility

COMPLETION: Use <cmd type="task_complete">summary</cmd> when done.
Write results to /tmp/clive/.
