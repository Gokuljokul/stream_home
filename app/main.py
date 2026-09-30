import asyncio
import logging
import os
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

RTSP_URL = os.environ.get("RTSP_URL", "rtsp://mediamtx:8554/cam")
PUBLISH_URL = os.environ.get("PUBLISH_URL", "rtsp://mediamtx:8554/cam")
PUBLISH_TOKEN = os.environ.get("PUBLISH_TOKEN", "")
HLS_DIR = Path(os.environ.get("HLS_DIR", "/tmp/hls"))
VIDEO_CODEC = os.environ.get("VIDEO_CODEC", "libx264")  # "copy" to pass H.264 through untouched
SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", "2"))
# When CAMERA_DEVICE is set, the server captures its own camera/mic instead of accepting browser publishers.
CAMERA_DEVICE = os.environ.get("CAMERA_DEVICE", "")
CAMERA_FORMAT = os.environ.get("CAMERA_FORMAT", "mjpeg")
CAMERA_SIZE = os.environ.get("CAMERA_SIZE", "1280x720")
CAMERA_FPS = os.environ.get("CAMERA_FPS", "25")
AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", "")

log = logging.getLogger("stream")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SEGMENT_NAME = re.compile(r"^(index\.m3u8|seg_\d{5}\.ts)$")


def masked(url: str) -> str:
    parts = urlsplit(url)
    if parts.password:
        netloc = f"{parts.username}:***@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        return urlunsplit(parts._replace(netloc=netloc))
    return url


def ffmpeg_cmd() -> list[str]:
    video = ["-c:v", "copy"] if VIDEO_CODEC == "copy" else [
        "-c:v", VIDEO_CODEC, "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
        "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
    ]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", RTSP_URL,
        "-map", "0:v:0", "-map", "0:a:0?",
        *video,
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+independent_segments+omit_endlist",
        "-hls_segment_filename", str(HLS_DIR / "seg_%05d.ts"),
        str(HLS_DIR / "index.m3u8"),
    ]


def publish_cmd() -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "webm", "-i", "pipe:0",
        "-fps_mode", "passthrough",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
        "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
        "-af", "aresample=async=1000",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-f", "rtsp", "-rtsp_transport", "tcp", PUBLISH_URL,
    ]


def capture_cmd() -> list[str]:
    video_in = ["-thread_queue_size", "512", "-f", "v4l2"]
    if CAMERA_FORMAT:
        video_in += ["-input_format", CAMERA_FORMAT]
    video_in += ["-video_size", CAMERA_SIZE, "-framerate", CAMERA_FPS, "-i", CAMERA_DEVICE]
    audio_in, audio_out = [], ["-an"]
    if AUDIO_DEVICE:
        audio_in = ["-thread_queue_size", "512", "-f", "alsa", "-i", AUDIO_DEVICE]
        audio_out = ["-map", "1:a:0", "-af", "aresample=async=1000",
                     "-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        *video_in, *audio_in,
        "-map", "0:v:0",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
        "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
        *audio_out,
        "-f", "rtsp", "-rtsp_transport", "tcp", PUBLISH_URL,
    ]


def reset_hls_dir() -> None:
    shutil.rmtree(HLS_DIR, ignore_errors=True)
    HLS_DIR.mkdir(parents=True, exist_ok=True)


async def supervise(name: str, cmd: list[str], before=None) -> None:
    delay = 1
    while True:
        if before:
            before()
        log.info("Starting %s", name)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(*cmd)
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            proc.terminate()
            await proc.wait()
            raise
        if time.monotonic() - started > 10:
            delay = 1
        log.warning("%s exited with code %s, restarting in %ss", name, code, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 2 if name == "reader" else 10)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(
        supervise("reader", ffmpeg_cmd(), reset_hls_dir))]
    if CAMERA_DEVICE:
        log.info("Capturing %s (audio: %s) -> %s", CAMERA_DEVICE, AUDIO_DEVICE or "none", PUBLISH_URL)
        tasks.append(asyncio.create_task(supervise("capture", capture_cmd())))
    else:
        log.info("Reading %s", masked(RTSP_URL))
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="RTSP Live Stream", lifespan=lifespan)

PUBLISH_LINK = '<a href="/publish" style="position:fixed;top:8px;left:8px;color:#8cf">Go live with camera</a>'

INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Live Stream</title>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
  <style>body{margin:0;background:#111;display:flex;justify-content:center;align-items:center;height:100vh}
  video{max-width:100%;max-height:100%}</style>
</head>
<body>
  PUBLISH_LINK
  <video id="v" controls autoplay muted playsinline></video>
  <script>
    const v = document.getElementById('v'), src = '/hls/index.m3u8';
    if (v.canPlayType('application/vnd.apple.mpegurl')) { v.src = src; }
    else if (Hls.isSupported()) {
      const hls = new Hls({ liveSyncDurationCount: 2 });
      hls.loadSource(src); hls.attachMedia(v);
      hls.on(Hls.Events.ERROR, (_, d) => { if (d.fatal) setTimeout(() => hls.loadSource(src), 2000); });
    }
  </script>
</body>
</html>"""


PUBLISH_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Go Live</title>
  <style>body{margin:0;background:#111;color:#eee;font-family:sans-serif;text-align:center}
  video{max-width:90vw;max-height:75vh;margin-top:12px} button{font-size:18px;margin:12px;padding:8px 20px}</style>
</head>
<body>
  <div>Camera <select id="cam"></select> Mic <select id="mic"></select></div>
  <div><button id="start">Start camera + mic</button><button id="stop" disabled>Stop</button>
  <span id="status">idle</span> | <a href="/" style="color:#8cf">Viewer</a></div>
  <video id="preview" autoplay muted playsinline></video>
  <script>
    let ws, rec, stream;
    const $ = id => document.getElementById(id), status = t => $('status').textContent = t;
    async function listDevices() {
      const devs = await navigator.mediaDevices.enumerateDevices();
      for (const [sel, kind] of [[$('cam'), 'videoinput'], [$('mic'), 'audioinput']]) {
        const prev = sel.value;
        sel.innerHTML = '';
        devs.filter(d => d.kind === kind).forEach((d, i) =>
          sel.add(new Option(d.label || `${kind} ${i + 1}`, d.deviceId)));
        if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
      }
    }
    if (!navigator.mediaDevices) status('camera needs http://localhost or HTTPS');
    else { listDevices(); navigator.mediaDevices.ondevicechange = listDevices; }
    $('start').onclick = async () => {
      const pick = id => $(id).value ? { deviceId: { exact: $(id).value } } : {};
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { ...pick('cam'), width: 1280, height: 720, frameRate: 25 },
          audio: { ...pick('mic'), echoCancellation: true, noiseSuppression: true },
        });
      } catch (e) { status('camera/mic error: ' + e.message); return; }
      $('preview').srcObject = stream;
      await listDevices();
      $('cam').value = stream.getVideoTracks()[0].getSettings().deviceId || $('cam').value;
      $('mic').value = stream.getAudioTracks()[0]?.getSettings().deviceId || $('mic').value;
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ws/publish${location.search}`);
      ws.onopen = () => {
        const mimeType = ['video/webm;codecs=vp8,opus', 'video/webm'].find(t => MediaRecorder.isTypeSupported(t));
        rec = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 2500000 });
        rec.ondataavailable = e => { if (e.data.size && ws.readyState === 1) ws.send(e.data); };
        rec.start(250);
        status('LIVE'); $('start').disabled = true; $('stop').disabled = false;
      };
      ws.onclose = e => { stop(); status('disconnected' + (e.reason ? ': ' + e.reason : '')); };
    };
    function stop() {
      if (rec && rec.state !== 'inactive') rec.stop();
      if (stream) stream.getTracks().forEach(t => t.stop());
      if (ws && ws.readyState <= 1) ws.close();
      $('start').disabled = false; $('stop').disabled = true; status('idle');
    }
    $('stop').onclick = stop;
  </script>
</body>
</html>"""

publish_lock = asyncio.Lock()
current_publisher: WebSocket | None = None


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML.replace("PUBLISH_LINK", "" if CAMERA_DEVICE else PUBLISH_LINK)


@app.get("/publish", response_class=HTMLResponse)
async def publish_page() -> str:
    if CAMERA_DEVICE:
        raise HTTPException(404, "Browser publishing is disabled; streaming the server camera")
    return PUBLISH_HTML


@app.websocket("/ws/publish")
async def publish(ws: WebSocket, token: str = "") -> None:
    global current_publisher
    if CAMERA_DEVICE:
        await ws.accept()
        await ws.close(code=1008, reason="browser publishing disabled")
        return
    if PUBLISH_TOKEN and not secrets.compare_digest(token, PUBLISH_TOKEN):
        # Accept before closing so the browser receives the reason instead of a bare 403.
        await ws.accept()
        await ws.close(code=1008, reason="invalid token")
        return
    if current_publisher is not None:
        log.info("New publisher is replacing the current one")
        try:
            await current_publisher.close(code=1012, reason="replaced by a new publisher")
        except RuntimeError:
            pass
    async with publish_lock:
        await ws.accept()
        current_publisher = ws
        log.info("Publisher connected")
        proc = await asyncio.create_subprocess_exec(*publish_cmd(), stdin=asyncio.subprocess.PIPE)
        assert proc.stdin
        try:
            while True:
                proc.stdin.write(await ws.receive_bytes())
                await proc.stdin.drain()
        except (WebSocketDisconnect, BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        finally:
            if current_publisher is ws:
                current_publisher = None
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            log.info("Publisher disconnected")


@app.get("/hls/{name}")
async def hls(name: str) -> FileResponse:
    if not SEGMENT_NAME.match(name):
        raise HTTPException(404)
    path = HLS_DIR / name
    if not path.is_file():
        raise HTTPException(404, "Stream not ready")
    if name.endswith(".m3u8"):
        return FileResponse(path, media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-cache"})
    return FileResponse(path, media_type="video/mp2t")


@app.get("/health")
async def health() -> dict:
    return {
        "streaming": (HLS_DIR / "index.m3u8").is_file(),
        "source": "server-camera" if CAMERA_DEVICE else "rtsp-or-browser",
        "camera": CAMERA_DEVICE or None,
        "audio": AUDIO_DEVICE or None,
    }
