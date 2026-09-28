# stream_home

FastAPI service that pulls an RTSP feed (video + audio), repackages it to HLS with ffmpeg, and serves it on port 5001.

```sh
docker compose up --build
```

1. Open http://localhost:5001/publish, click **Start camera + mic**, allow access.
2. Open http://localhost:5001 to watch (click unmute for audio).

The browser sends camera/mic over a WebSocket; the app pushes it to the bundled RTSP server (`rtsp://localhost:8554/cam`, also usable from VLC), and the viewer reads it back as HLS.

Other sources:

```sh
# Test pattern + tone
RTSP_URL="rtsp://mediamtx:8554/test" docker compose --profile demo up --build

# IP camera
RTSP_URL="rtsp://user:pass@192.168.1.50:554/stream1" docker compose up --build
```

Publishing from another device on the LAN requires HTTPS (browsers only allow camera access on `localhost` or HTTPS). Set `PUBLISH_TOKEN` and open `/publish?token=...` to restrict who can go live.

| Env | Default | Notes |
|---|---|---|
| `RTSP_URL` | `rtsp://mediamtx:8554/cam` | Source stream |
| `PUBLISH_TOKEN` | *(empty)* | Required token for `/publish` when set |
| `VIDEO_CODEC` | `libx264` | Set `copy` if the source is already H.264 (much lower CPU) |
| `SEGMENT_SECONDS` | `2` | HLS segment length (latency ≈ 2–3× this) |
