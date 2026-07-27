"""WebRTC sidecar for Lucy Live. Runs in C4D's Python subprocess."""

import argparse
import asyncio
import base64
import contextlib
import ctypes
import functools
import json
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from fractions import Fraction
from pathlib import Path


MODEL = "decart/lucy-2-5/realtime"
ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
WINDOWS_SHARING_ERRORS = {5, 32, 33}
HANDSHAKE_TIMEOUT_SECONDS = 90
MEDIA_TIMEOUT_SECONDS = 60
MEDIA_STALL_TIMEOUT_SECONDS = 20
READY_FALLBACK_SECONDS = 1.0
VIDEO_FPS = 30
H264_MIN_BITRATE = 500_000
H264_DEFAULT_BITRATE = 6_000_000
H264_MAX_BITRATE = 12_000_000
VP8_MIN_BITRATE = 250_000
VP8_DEFAULT_BITRATE = 5_000_000
VP8_MAX_BITRATE = 10_000_000
RECORDING_TIME_BASE = 90000
RECORDING_CRF = 0
RECORDING_MAX_THREADS = 8
CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 720
CANVAS_SIZE = (CANVAS_WIDTH, CANVAS_HEIGHT)
TELEMETRY_INTERVAL_SECONDS = 0.25
SOURCE_FPS_WINDOW_SECONDS = 2.0
SOURCE_FPS_STALE_SECONDS = 1.0
COMPOSITION_SUFFIX = (
    "Keep the source composition fixed; apply only the requested change."
)
DEFAULT_ICE_SERVERS = (
    {"urls": "stun:stun.l.google.com:19302"},
)
REFERENCE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
REFERENCE_MAX_BYTES = 16 * 1024 * 1024
REFERENCE_INLINE_MAX_BYTES = 512 * 1024
REFERENCE_URL_REFRESH_SECONDS = 45 * 60
REFERENCE_RETRY_SECONDS = 5.0
CONTROL_GATE_MAX_SECONDS = 10.0
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))


def is_sharing_error(error):
    return os.name == "nt" and getattr(error, "winerror", None) in WINDOWS_SHARING_ERRORS


def replace_with_retry(source, target, attempts=5, delay=0.01):
    """Replace a small state file while tolerating brief Windows reader locks."""
    for attempt in range(attempts):
        try:
            os.replace(str(source), str(target))
            return
        except OSError as exc:
            if not is_sharing_error(exc) or attempt + 1 == attempts:
                raise
            time.sleep(delay)


def atomic_json(path, data):
    tmp = path.with_name(".%s.%d.%d.tmp" %
                         (path.name, os.getpid(), time.time_ns()))
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        replace_with_retry(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def update_telemetry(state, values):
    """Merge one telemetry section without erasing another producer."""
    path = Path(state) / "telemetry.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except (OSError, TypeError, ValueError):
        current = {}
    current.update(dict(values or {}))
    atomic_json(path, current)
    return current


def log_line(message):
    try:
        print(message, flush=True)
    except (OSError, UnicodeError):
        pass


def report(state, message, stage):
    """Persist and log a non-secret worker lifecycle checkpoint."""
    atomic_json(state / "status.json", {
        "message": message,
        "stage": stage,
        "updated": time.time(),
        "worker_pid": os.getpid(),
    })
    log_line("[%s] %s" % (stage, message))


def safe_future_epoch_gate(value, wall_now=None):
    """Accept only a near-future epoch gate so bad control cannot stall media."""
    current = time.time() if wall_now is None else float(wall_now)
    try:
        deadline = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if (not math.isfinite(deadline) or deadline <= current or
            deadline - current > CONTROL_GATE_MAX_SECONDS):
        return 0.0
    return deadline


def lanczos(Image):
    resampling = getattr(Image, "Resampling", Image)
    return getattr(resampling, "LANCZOS", getattr(Image, "BICUBIC", 3))


def _image_size(image):
    size = getattr(image, "size", None)
    if isinstance(size, (tuple, list)) and len(size) >= 2:
        return max(1, int(size[0])), max(1, int(size[1]))
    width = getattr(image, "width", None)
    height = getattr(image, "height", None)
    if width is not None and height is not None:
        return max(1, int(width)), max(1, int(height))
    return CANVAS_SIZE


def _resize_image(image, size, Image):
    try:
        return image.resize(size, lanczos(Image))
    except TypeError:
        return image.resize(size)


def _close_image(image):
    close = getattr(image, "close", None)
    if callable(close):
        close()


def normalized_active_rect(control):
    """Return one validated [x, y, width, height] rectangle."""
    raw = control.get("active_rect") if isinstance(control, dict) else None
    if not isinstance(raw, (tuple, list)) or len(raw) != 4:
        return 0.0, 0.0, 1.0, 1.0
    try:
        x, y, width, height = (float(value) for value in raw)
    except (TypeError, ValueError):
        return 0.0, 0.0, 1.0, 1.0
    if not all(value == value for value in (x, y, width, height)):
        return 0.0, 0.0, 1.0, 1.0
    x = min(1.0, max(0.0, x))
    y = min(1.0, max(0.0, y))
    width = min(1.0 - x, max(0.0, width))
    height = min(1.0 - y, max(0.0, height))
    if width <= 0.0 or height <= 0.0:
        return 0.0, 0.0, 1.0, 1.0
    return x, y, width, height


def active_rect_pixels(control, image_size):
    """Scale the normalized canvas rectangle to any actual frame size."""
    image_width, image_height = (
        max(1, int(image_size[0])), max(1, int(image_size[1])))
    x, y, width, height = normalized_active_rect(control)
    left = min(image_width - 1, max(0, int(round(x * image_width))))
    top = min(image_height - 1, max(0, int(round(y * image_height))))
    right = min(
        image_width,
        max(left + 1, int(round((x + width) * image_width))))
    bottom = min(
        image_height,
        max(top + 1, int(round((y + height) * image_height))))
    return left, top, right, bottom


def _resize_inside(image, target_size, Image):
    target_width, target_height = target_size
    image_width, image_height = _image_size(image)
    scale = min(
        float(target_width) / image_width,
        float(target_height) / image_height,
    )
    size = (
        max(1, min(target_width, int(round(image_width * scale)))),
        max(1, min(target_height, int(round(image_height * scale)))),
    )
    return _resize_image(image, size, Image)


def _resize_cover(image, target_size, Image):
    target_width, target_height = target_size
    image_width, image_height = _image_size(image)
    scale = max(
        float(target_width) / image_width,
        float(target_height) / image_height,
    )
    size = (
        max(target_width, int(round(image_width * scale))),
        max(target_height, int(round(image_height * scale))),
    )
    resized = _resize_image(image, size, Image)
    resized_width, resized_height = _image_size(resized)
    left = (resized_width - target_width) // 2
    top = (resized_height - target_height) // 2
    crop = getattr(resized, "crop", None)
    result = (
        crop((left, top, left + target_width, top + target_height))
        if callable(crop)
        else _resize_image(resized, target_size, Image))
    _close_image(resized)
    return result


def prepare_source_canvas(image, control, Image, ImageFilter=None):
    """Pad a source into 1280x720 with derived edges and no stretching."""
    source = image.convert("RGB")
    background = _resize_cover(source, CANVAS_SIZE, Image)
    filter_image = getattr(background, "filter", None)
    if ImageFilter is not None and callable(filter_image):
        blurred = filter_image(ImageFilter.GaussianBlur(radius=14.0))
        _close_image(background)
        background = blurred
    left, top, right, bottom = active_rect_pixels(control, CANVAS_SIZE)
    target_size = (right - left, bottom - top)
    foreground = _resize_inside(source, target_size, Image)
    foreground_width, foreground_height = _image_size(foreground)
    paste_x = left + (target_size[0] - foreground_width) // 2
    paste_y = top + (target_size[1] - foreground_height) // 2
    paste = getattr(background, "paste", None)
    if callable(paste):
        paste(foreground, (paste_x, paste_y))
    _close_image(foreground)
    _close_image(source)
    return background


def crop_remote_frame(image, control):
    """Crop a remote frame using its actual dimensions, never canvas pixels."""
    return image.crop(active_rect_pixels(control, image.size))


def fit_recording_frame(image, target_size):
    """Keep the first REC dimensions while preserving later frame aspect."""
    if image.size == target_size:
        return image
    # The stretched copy is only an edge-fill background. The sharp foreground
    # is resized proportionally and replaces its central area.
    background = image.resize(target_size)
    scale = min(
        float(target_size[0]) / max(1, image.width),
        float(target_size[1]) / max(1, image.height),
    )
    foreground = image.resize((
        max(1, int(round(image.width * scale))),
        max(1, int(round(image.height * scale))),
    ))
    background.paste(
        foreground,
        ((target_size[0] - foreground.width) // 2,
         (target_size[1] - foreground.height) // 2),
    )
    foreground.close()
    return background


class HighQualityMp4Recorder:
    """Encode native remote frames as a high-quality, real-time MP4."""

    def __init__(self, path, av, started_at=None, frame_rate=VIDEO_FPS):
        self.path = Path(path)
        self.av = av
        self.started_at = (
            time.monotonic() if started_at is None else float(started_at))
        self.frame_rate = max(1, int(frame_rate))
        self.container = None
        self.stream = None
        self.width = 0
        self.height = 0
        self.last_pts = -1
        self.last_image = None
        self.encoded = 0

    def _open(self, image):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.width = max(2, int(image.width) // 2 * 2)
        self.height = max(2, int(image.height) // 2 * 2)
        self.container = self.av.open(
            str(self.path), "w", options={"movflags": "+faststart"})
        self.stream = self.container.add_stream(
            "libx264", rate=self.frame_rate)
        self.stream.width = self.width
        self.stream.height = self.height
        self.stream.pix_fmt = "yuv420p"
        self.stream.time_base = Fraction(1, RECORDING_TIME_BASE)
        self.stream.codec_context.time_base = Fraction(
            1, RECORDING_TIME_BASE)
        self.stream.codec_context.max_b_frames = 0
        self.stream.codec_context.gop_size = self.frame_rate * 2
        self.stream.codec_context.thread_count = min(
            RECORDING_MAX_THREADS, max(1, os.cpu_count() or 1))
        self.stream.options = {
            "crf": str(RECORDING_CRF),
            "preset": "veryfast",
            "tune": "zerolatency",
        }

    def _pts(self, received_at):
        return max(
            self.last_pts + 1,
            int(round(
                max(0.0, float(received_at) - self.started_at)
                * RECORDING_TIME_BASE)),
        )

    def _encode_image(self, image, received_at):
        if self.container is None:
            self._open(image)
        fitted = fit_recording_frame(
            image, (self.width, self.height))
        frame = self.av.VideoFrame.from_image(fitted)
        if fitted is not image:
            fitted.close()
        frame = frame.reformat(
            width=self.width, height=self.height, format="yuv420p")
        frame.pts = self._pts(received_at)
        frame.time_base = Fraction(1, RECORDING_TIME_BASE)
        self.last_pts = frame.pts
        for packet in self.stream.encode(frame):
            self.container.mux(packet)
        self.encoded += 1

    def write_image(self, image, received_at=None):
        timestamp = (
            time.monotonic() if received_at is None else float(received_at))
        self._encode_image(image, timestamp)
        previous = self.last_image
        self.last_image = image.copy()
        if previous is not None:
            previous.close()

    def finish(self, stopped_at=None):
        if self.container is None or self.last_image is None:
            raise RuntimeError("No frames were received for the recording")
        timestamp = (
            time.monotonic() if stopped_at is None else float(stopped_at))
        final_pts = int(round(
            max(0.0, timestamp - self.started_at)
            * RECORDING_TIME_BASE))
        if final_pts > self.last_pts:
            self._encode_image(self.last_image, timestamp)
        for packet in self.stream.encode(None):
            self.container.mux(packet)
        self.container.close()
        self.container = None
        self.stream = None
        self.last_image.close()
        self.last_image = None
        return {
            "encoded": self.encoded,
            "duration": max(0.0, timestamp - self.started_at),
            "width": self.width,
            "height": self.height,
        }

    def abort(self):
        if self.container is not None:
            try:
                self.container.close()
            except Exception:
                pass
        self.container = None
        self.stream = None
        if self.last_image is not None:
            self.last_image.close()
            self.last_image = None
        try:
            self.path.unlink()
        except OSError:
            pass


class BackgroundMp4Recorder(threading.Thread):
    """Own PyAV on one thread and never backpressure the WebRTC consumer."""

    def __init__(self, state, recording_id, work_path, destination, av,
                 started_at=None, queue_size=4, frame_rate=VIDEO_FPS):
        super().__init__(
            name="AI Render MP4 Recorder", daemon=True)
        self.state = Path(state)
        self.recording_id = str(recording_id)
        self.work_path = Path(work_path)
        self.destination = Path(destination)
        self.av = av
        self.frame_rate = max(1, int(frame_rate))
        self.started_at = (
            time.monotonic() if started_at is None else float(started_at))
        self.capture_started_at = None
        self.frames = queue.Queue(maxsize=max(1, int(queue_size)))
        self.finish_event = threading.Event()
        self.cancel_event = threading.Event()
        self.stopped_at = self.started_at
        self.received = 0
        self.dropped = 0

    def _status(self, state, message="", **extra):
        payload = {
            "recording_id": self.recording_id,
            "state": state,
            "message": str(message or ""),
            "destination": str(self.destination),
            "received": self.received,
            "dropped": self.dropped,
            "updated": time.time(),
        }
        payload.update(extra)
        atomic_json(self.state / "recording_status.json", payload)

    @staticmethod
    def _close_image(image):
        try:
            image.close()
        except (AttributeError, OSError):
            pass

    def submit_image(self, image, received_at=None):
        if self.finish_event.is_set() or self.cancel_event.is_set():
            return False
        timestamp = (
            time.monotonic() if received_at is None else float(received_at))
        if self.capture_started_at is None:
            self.capture_started_at = timestamp
        timeline_timestamp = (
            self.started_at +
            max(0.0, timestamp - self.capture_started_at))
        item = (image, timeline_timestamp)
        self.received += 1
        try:
            self.frames.put_nowait(item)
            return True
        except queue.Full:
            try:
                stale, _received_at = self.frames.get_nowait()
                self._close_image(stale)
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.frames.put_nowait(item)
                return True
            except queue.Full:
                self._close_image(image)
                self.dropped += 1
                return False

    def request_finish(self, stopped_at=None):
        timestamp = (
            time.monotonic() if stopped_at is None else float(stopped_at))
        self.stopped_at = (
            self.started_at
            if self.capture_started_at is None else
            self.started_at +
            max(0.0, timestamp - self.capture_started_at))
        self.finish_event.set()

    def request_cancel(self):
        self.cancel_event.set()

    def _validate(self):
        source = self.av.open(str(self.work_path))
        try:
            streams = list(source.streams.video)
            if not streams:
                raise RuntimeError("Recorded MP4 has no video stream")
            try:
                next(source.decode(streams[0]))
            except StopIteration as exc:
                raise RuntimeError(
                    "Recorded MP4 has no decodable frames") from exc
        finally:
            source.close()

    def _drain_images(self):
        while True:
            try:
                image, _received_at = self.frames.get_nowait()
            except queue.Empty:
                return
            self._close_image(image)

    def run(self):
        recorder = HighQualityMp4Recorder(
            self.work_path, self.av, started_at=self.started_at,
            frame_rate=self.frame_rate)
        try:
            self._status("recording")
            while True:
                if self.cancel_event.is_set():
                    raise RuntimeError("Recording cancelled")
                try:
                    image, received_at = self.frames.get(timeout=0.02)
                except queue.Empty:
                    if self.finish_event.is_set():
                        break
                    continue
                try:
                    recorder.write_image(image, received_at)
                finally:
                    self._close_image(image)
                if self.finish_event.is_set() and self.frames.empty():
                    break

            self._status("finalizing")
            summary = recorder.finish(stopped_at=self.stopped_at)
            self._validate()
            replace_with_retry(self.work_path, self.destination)
            self._status("saved", **summary)
        except Exception as exc:
            recorder.abort()
            try:
                self.work_path.unlink()
            except OSError:
                pass
            state = (
                "cancelled"
                if self.cancel_event.is_set() else "error")
            try:
                self._status(state, str(exc), encoded=recorder.encoded)
            except Exception:
                pass
        finally:
            self._drain_images()


class RecordingCommandController:
    """Translate idempotent control.json state into one recorder thread."""

    def __init__(self, state, av):
        self.state = Path(state)
        self.av = av
        self.active = None
        self.capture_enabled = False
        self.finishing = []
        self.started_ids = set()

    @staticmethod
    def _same_directory(first, second):
        return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(
            os.path.abspath(str(second)))

    def _error(self, recording_id, destination, message):
        atomic_json(self.state / "recording_status.json", {
            "recording_id": str(recording_id or ""),
            "state": "error",
            "message": str(message),
            "destination": str(destination or ""),
            "received": 0,
            "encoded": 0,
            "dropped": 0,
            "updated": time.time(),
        })

    def _cleanup_finished(self):
        if self.active is not None and not self.active.is_alive():
            self.active = None
            self.capture_enabled = False
        self.finishing = [
            recorder for recorder in self.finishing
            if recorder.is_alive()
        ]

    def sync(self, control, now=None, wall_now=None):
        self._cleanup_finished()
        current = control if isinstance(control, dict) else {}
        requested = bool(current.get("recording", False))
        capture_requested = bool(
            current.get("recording_capture", True))
        wall_timestamp = (
            time.time() if wall_now is None else float(wall_now))
        capture_gate = safe_future_epoch_gate(
            current.get("recording_capture_after", 0.0),
            wall_timestamp)
        capture_enabled = bool(
            capture_requested and capture_gate <= 0.0)
        recording_id = str(current.get("recording_id") or "")
        raw_work_path = str(
            current.get("recording_work_path") or "").strip()
        raw_destination = str(
            current.get("recording_final_path") or "").strip()
        timestamp = time.monotonic() if now is None else float(now)

        if requested:
            if self.active is not None:
                if self.active.recording_id != recording_id:
                    return False
                self.capture_enabled = capture_enabled
                return True
            if not recording_id or recording_id in self.started_ids:
                return False
            if not raw_work_path or not raw_destination:
                self.started_ids.add(recording_id)
                self._error(
                    recording_id, raw_destination,
                    "Recording paths are missing")
                return False
            work_path = Path(raw_work_path)
            destination = Path(raw_destination)
            if (work_path == destination or
                    not self._same_directory(
                        work_path.parent, destination.parent)):
                self.started_ids.add(recording_id)
                self._error(
                    recording_id, destination,
                    "Recording work file must be beside its destination")
                return False
            recorder = BackgroundMp4Recorder(
                state=self.state,
                recording_id=recording_id,
                work_path=work_path,
                destination=destination,
                av=self.av,
                started_at=timestamp,
            )
            self.started_ids.add(recording_id)
            self.active = recorder
            self.capture_enabled = capture_enabled
            recorder.start()
            return True

        if (self.active is not None and
                (not recording_id or
                 self.active.recording_id == recording_id)):
            recorder = self.active
            self.active = None
            self.capture_enabled = False
            recorder.request_finish(stopped_at=timestamp)
            self.finishing.append(recorder)
            return True
        if recording_id and recording_id not in self.started_ids:
            self.started_ids.add(recording_id)
            self._error(
                recording_id, raw_destination,
                "Recording stopped before the encoder started")
            return True
        return False

    def submit_image(self, image, received_at=None):
        self._cleanup_finished()
        if self.active is None or not self.capture_enabled:
            return False
        return self.active.submit_image(image, received_at)

    def wait_for_idle(self, timeout=2.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            self._cleanup_finished()
            if self.active is None and not self.finishing:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            for recorder in tuple(self.finishing):
                recorder.join(timeout=min(0.05, remaining))

    def shutdown(self, timeout=2.0, stopped_at=None):
        timestamp = (
            time.monotonic() if stopped_at is None else float(stopped_at))
        recorders = list(self.finishing)
        self.finishing = []
        if self.active is not None:
            self.active.request_finish(stopped_at=timestamp)
            recorders.append(self.active)
            self.active = None
            self.capture_enabled = False
        deadline = time.monotonic() + max(0.0, float(timeout))
        for recorder in recorders:
            recorder.join(timeout=max(0.0, deadline - time.monotonic()))
        unfinished = [
            recorder for recorder in recorders if recorder.is_alive()]
        for recorder in unfinished:
            recorder.request_cancel()
        for recorder in unfinished:
            recorder.join(timeout=0.5)
        return all(not recorder.is_alive() for recorder in recorders)


@functools.lru_cache(maxsize=1)
def _cached_reference_data_uri(path, modified_ns, size):
    """Encode one immutable file revision; prompt polling reuses the result."""
    del modified_ns, size
    reference = Path(path)
    mime_type = REFERENCE_MIME_TYPES.get(reference.suffix.lower())
    if mime_type is None:
        raise ValueError("Unsupported reference image format")
    encoded = base64.b64encode(reference.read_bytes()).decode("ascii")
    return "data:%s;base64,%s" % (mime_type, encoded)


def reference_image_data_uri(path):
    """Return a small API-compatible fallback data URI."""
    raw_path = str(path or "").strip()
    if not raw_path:
        return ""
    reference = Path(raw_path).expanduser()
    stat = reference.stat()
    if stat.st_size > REFERENCE_MAX_BYTES:
        raise ValueError("Reference image exceeds 16 MB")
    if stat.st_size > REFERENCE_INLINE_MAX_BYTES:
        raise ValueError("Reference image is too large for inline transport")
    return _cached_reference_data_uri(
        str(reference.resolve()), stat.st_mtime_ns, stat.st_size)


def _base_prompt_payload(control):
    """Build Lucy settings that never contain local file data."""
    prompt = str(control.get("prompt") or "").strip()
    if bool(control.get("preserve_composition", True)):
        prompt = ("%s %s" % (prompt, COMPOSITION_SUFFIX)).strip()
    return {
        "prompt": prompt,
        "enable_prompt_expansion": bool(
            control.get("enable_prompt_expansion", False)),
    }


def prompt_payload(control):
    """Build a payload with a bounded inline fallback for pure callers."""
    payload = _base_prompt_payload(control)
    reference_path = str(control.get("reference_image_path") or "").strip()
    if reference_path:
        try:
            reference_url = reference_image_data_uri(reference_path)
        except (OSError, TypeError, ValueError):
            reference_url = ""
        if reference_url:
            payload["reference_image_url"] = reference_url
    return payload


async def prepare_prompt_payload(control, fal_client, reference_cache):
    """Upload a local reference before it can block realtime signaling."""
    payload = _base_prompt_payload(control)
    raw_path = str(control.get("reference_image_path") or "").strip()
    if not raw_path:
        return payload

    reference = Path(raw_path).expanduser().resolve()
    mime_type = REFERENCE_MIME_TYPES.get(reference.suffix.lower())
    if mime_type is None:
        raise ValueError("Unsupported reference image format")
    stat = reference.stat()
    if stat.st_size > REFERENCE_MAX_BYTES:
        raise ValueError("Reference image exceeds 16 MB")
    revision = (str(reference), stat.st_mtime_ns, stat.st_size)
    now = time.monotonic()
    cached_url = reference_cache.get("url")
    cached_at = float(reference_cache.get("uploaded_at") or 0.0)
    if (reference_cache.get("revision") == revision and cached_url and
            now - cached_at < REFERENCE_URL_REFRESH_SECONDS):
        payload["reference_image_url"] = cached_url
        return payload

    try:
        lifecycle = fal_client.StorageSettings(expires_in="1h")
        reference_url = await fal_client.async_client.upload_file(
            str(reference), lifecycle=lifecycle)
    except Exception as exc:
        # A tiny file can safely use fal's documented data-URI fallback. A
        # multi-megabyte message must never enter the signaling WebSocket.
        if stat.st_size > REFERENCE_INLINE_MAX_BYTES:
            raise RuntimeError(
                "Could not upload the reference: %s" % exc) from exc
        reference_url = await asyncio.to_thread(
            reference_image_data_uri, reference)

    reference_cache.clear()
    reference_cache.update({
        "revision": revision,
        "url": reference_url,
        "uploaded_at": now,
    })
    payload["reference_image_url"] = reference_url
    return payload


def prompt_update_payload(previous, current):
    """Make removing a reference explicit without reconnecting WebRTC."""
    outgoing = dict(current)
    if (previous and "reference_image_url" in previous and
            "reference_image_url" not in current):
        outgoing["reference_image_url"] = None
    return outgoing


def prompt_payload_after_reference_error(control, previous=None):
    """Keep rendering with the last good reference after an upload error."""
    payload = _base_prompt_payload(control)
    previous_url = (
        previous.get("reference_image_url") if previous else None)
    if previous_url:
        payload["reference_image_url"] = previous_url
    return payload


def measured_source_fps(timestamps, now):
    """Measure distinct successfully decoded source frames, not track ticks."""
    while timestamps and now - timestamps[0] > SOURCE_FPS_WINDOW_SECONDS:
        timestamps.popleft()
    if (len(timestamps) < 2 or
            now - timestamps[-1] > SOURCE_FPS_STALE_SECONDS):
        return 0.0
    elapsed = timestamps[-1] - timestamps[0]
    return 0.0 if elapsed <= 0.0 else (len(timestamps) - 1) / elapsed


def _stat_number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


class TransportTelemetrySampler:
    """Convert cumulative aiortc counters into compact interval metrics."""

    def __init__(self):
        self.last_at = None
        self.last_outbound_bytes = 0
        self.last_inbound_bytes = 0
        self.last_outbound_frames = 0
        self.last_inbound_frames = 0

    @staticmethod
    def _size(value):
        try:
            width, height = value
            return max(0, int(width)), max(0, int(height))
        except (TypeError, ValueError):
            return 0, 0

    def sample(self, report, now, outbound_frames=0, inbound_frames=0,
               outbound_size=CANVAS_SIZE, inbound_size=(0, 0), codec=""):
        values = (
            report.values()
            if callable(getattr(report, "values", None))
            else report or ())
        outbound_bytes = 0
        inbound_bytes = 0
        inbound_loss = 0
        inbound_jitter = 0.0
        remote_loss = 0
        remote_jitter = 0.0
        remote_fraction_lost_raw = 0.0
        remote_rtt = 0.0

        for stat in values:
            kind = str(getattr(stat, "kind", "") or "").lower()
            stat_type = str(getattr(stat, "type", "") or "").lower()
            if kind and kind != "video":
                continue
            if stat_type == "outbound-rtp":
                outbound_bytes += max(
                    0, int(_stat_number(getattr(stat, "bytesSent", 0))))
            elif stat_type == "inbound-rtp":
                inbound_loss += max(
                    0, int(_stat_number(getattr(stat, "packetsLost", 0))))
                inbound_jitter = max(
                    inbound_jitter,
                    _stat_number(getattr(stat, "jitter", 0)))
            elif stat_type == "remote-inbound-rtp":
                remote_loss += max(
                    0, int(_stat_number(getattr(stat, "packetsLost", 0))))
                remote_jitter = max(
                    remote_jitter,
                    _stat_number(getattr(stat, "jitter", 0)))
                remote_fraction_lost_raw = max(
                    remote_fraction_lost_raw,
                    _stat_number(getattr(stat, "fractionLost", 0)))
                remote_rtt = max(
                    remote_rtt,
                    _stat_number(getattr(stat, "roundTripTime", 0)))
            elif stat_type == "transport":
                inbound_bytes += max(
                    0, int(_stat_number(getattr(stat, "bytesReceived", 0))))

        current = float(now)
        outbound_frames = max(0, int(outbound_frames))
        inbound_frames = max(0, int(inbound_frames))
        elapsed = (
            max(0.0, current - self.last_at)
            if self.last_at is not None else 0.0)

        def rate(current_value, previous_value, scale):
            if elapsed <= 0.0 or current_value < previous_value:
                return 0.0
            return (current_value - previous_value) * scale / elapsed

        outbound_kbps = rate(
            outbound_bytes, self.last_outbound_bytes, 8.0 / 1000.0)
        inbound_kbps = rate(
            inbound_bytes, self.last_inbound_bytes, 8.0 / 1000.0)
        outbound_fps = rate(
            outbound_frames, self.last_outbound_frames, 1.0)
        inbound_fps = rate(
            inbound_frames, self.last_inbound_frames, 1.0)
        self.last_at = current
        self.last_outbound_bytes = outbound_bytes
        self.last_inbound_bytes = inbound_bytes
        self.last_outbound_frames = outbound_frames
        self.last_inbound_frames = inbound_frames

        outbound_width, outbound_height = self._size(outbound_size)
        inbound_width, inbound_height = self._size(inbound_size)
        codec_name = str(codec or "unknown")
        # The pinned aiortc exposes the RTCP 8-bit fixed fraction (0..255),
        # not the browser-style normalized value suggested by the field name.
        remote_fraction_lost = min(
            1.0, max(0.0, remote_fraction_lost_raw / 256.0))
        return {
            "codec": codec_name,
            "outbound": {
                "codec": codec_name,
                "bitrate_kbps": round(outbound_kbps, 1),
                "fps": round(outbound_fps, 2),
                "width": outbound_width,
                "height": outbound_height,
            },
            "inbound": {
                "codec": codec_name,
                "bitrate_kbps": round(inbound_kbps, 1),
                "fps": round(inbound_fps, 2),
                "width": inbound_width,
                "height": inbound_height,
                "packets_lost": inbound_loss,
                # aiortc reports video jitter in the 90 kHz RTP clock.
                "jitter_ms": round(inbound_jitter / 90.0, 3),
            },
            "remote_inbound": {
                "packets_lost": remote_loss,
                "jitter_ms": round(remote_jitter / 90.0, 3),
                "fraction_lost": round(remote_fraction_lost, 6),
                "rtt_ms": round(remote_rtt * 1000.0, 3),
            },
        }


async def collect_transport_telemetry(
        pc, state, sampler, source_track, output_metrics, now=None):
    """Collect one non-blocking WebRTC stats sample and publish it."""
    report = await pc.getStats()
    description = (
        getattr(pc, "remoteDescription", None) or
        getattr(pc, "localDescription", None))
    codec = video_codec_from_sdp(getattr(description, "sdp", ""))
    metrics = output_metrics if isinstance(output_metrics, dict) else {}
    sample = sampler.sample(
        report,
        time.monotonic() if now is None else now,
        outbound_frames=getattr(source_track, "frames_emitted", 0),
        inbound_frames=metrics.get("frames", 0),
        outbound_size=getattr(
            source_track, "last_frame_size", CANVAS_SIZE),
        inbound_size=metrics.get("size", (0, 0)),
        codec=codec,
    )
    sample["sampled_at"] = time.time()
    update_telemetry(state, {"webrtc": sample})
    return sample


async def monitor_transport_telemetry(
        pc, state, source_track, output_metrics, interval=1.0):
    """Periodically publish media quality without affecting the stream."""
    sampler = TransportTelemetrySampler()
    while True:
        await asyncio.sleep(interval)
        try:
            await collect_transport_telemetry(
                pc, state, sampler, source_track, output_metrics)
        except Exception:
            # Diagnostics must never tear down a paid realtime session.
            continue


def error_message(error):
    """Flatten TaskGroup exception trees into a useful status message."""
    children = getattr(error, "exceptions", None)
    if not children:
        return str(error)
    messages = []
    for child in children:
        message = error_message(child)
        if message and message not in messages:
            messages.append(message)
    return "; ".join(messages) or str(error)


def realtime_connection_kwargs():
    """Keep every WebRTC control message; media frames do not use this socket."""
    return {"path": ""}


def safe_signal_summary(message):
    """Return only a signaling type; never log SDP, candidates, or TURN data."""
    if not isinstance(message, dict):
        return "non-object"
    kind = str(message.get("type", "")).strip().lower()
    if kind:
        return kind
    if any(key in message for key in
           ("iceServers", "ice_servers", "iceservers")):
        return "iceservers"
    return "unknown"


def select_ice_servers(raw_servers):
    """Return valid relay servers or the fallback used by fal's playground."""
    if isinstance(raw_servers, (list, tuple)):
        servers = [
            value for value in raw_servers
            if isinstance(value, dict) and value.get("urls")
        ]
        if servers:
            return servers
    return [dict(value) for value in DEFAULT_ICE_SERVERS]


def ice_servers_from_message(message):
    """Read all field spellings currently emitted by the fal relay."""
    for key in ("iceServers", "ice_servers", "iceservers"):
        if key in message:
            return True, message[key]
    return False, None


class FalSignalingController:
    """Order fal signaling so WebRTC setup matches the browser playground."""

    def __init__(self, setup_peer, apply_answer, add_candidate, schedule_task,
                 ready_delay=READY_FALLBACK_SECONDS):
        self.setup_peer = setup_peer
        self.apply_answer = apply_answer
        self.add_candidate = add_candidate
        self.schedule_task = schedule_task
        self.ready_delay = ready_delay
        self.ready_task = None
        self.ready_seen = False
        self.remote_description_set = False
        self.pending_candidates = []

    async def _setup_after_ready(self):
        await asyncio.sleep(self.ready_delay)
        await self.setup_peer(None)

    async def handle(self, message):
        if not isinstance(message, dict):
            return False

        kind = str(message.get("type", "")).lower()
        has_servers, raw_servers = ice_servers_from_message(message)
        if kind == "answer":
            sdp = message.get("sdp")
            if not sdp:
                raise RuntimeError("Server sent an empty WebRTC answer")
            await self.apply_answer(sdp)
            self.remote_description_set = True
            pending, self.pending_candidates = self.pending_candidates, []
            for candidate in pending:
                await self.add_candidate(candidate)
            return True

        if kind == "icecandidate":
            candidate = message.get("candidate")
            if candidate is not None and not isinstance(candidate, dict):
                raise RuntimeError("Server sent an invalid ICE candidate")
            if self.remote_description_set:
                await self.add_candidate(candidate)
            else:
                self.pending_candidates.append(candidate)
            return True

        if kind == "ready":
            self.ready_seen = True
            if has_servers:
                if self.ready_task is not None and not self.ready_task.done():
                    self.ready_task.cancel()
                self.ready_task = None
                await self.setup_peer(raw_servers)
            elif self.ready_task is None:
                self.ready_task = self.schedule_task(
                    self._setup_after_ready())
            return True

        if kind in ("iceservers", "ice_servers") or (
                not kind and has_servers):
            if self.ready_task is not None and not self.ready_task.done():
                self.ready_task.cancel()
            self.ready_task = None
            await self.setup_peer(raw_servers)
            return True

        return False


def local_ice_candidate_messages(sdp):
    """Translate gathered aiortc SDP candidates to fal's browser messages."""
    media_sections = []
    current = None
    for raw_line in (sdp or "").replace("\r", "").split("\n"):
        line = raw_line.strip()
        if line.startswith("m="):
            current = {
                "index": len(media_sections),
                "mid": None,
                "candidates": [],
            }
            media_sections.append(current)
        elif current is not None and line.startswith("a=mid:"):
            current["mid"] = line[len("a=mid:"):]
        elif current is not None and line.startswith("a=candidate:"):
            current["candidates"].append(line[len("a="):])

    messages = []
    for section in media_sections:
        mid = section["mid"]
        if mid is None:
            mid = str(section["index"])
        for candidate in section["candidates"]:
            messages.append({
                "type": "icecandidate",
                "candidate": {
                    "candidate": candidate,
                    "sdpMid": mid,
                    "sdpMLineIndex": section["index"],
                },
            })
    return messages


def advertise_trickle_ice(sdp):
    """Add the RFC 8838 trickle token missing from aiortc raw offers."""
    newline = "\r\n" if "\r\n" in (sdp or "") else "\n"
    trailing_newline = bool(sdp) and (
        sdp.endswith("\r\n") or sdp.endswith("\n"))
    lines = (sdp or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    prefix = []
    sections = []
    current = None
    for line in lines:
        if line.startswith("m="):
            current = [line]
            sections.append(current)
        elif current is None:
            prefix.append(line)
        else:
            current.append(line)

    if any(line.startswith("a=ice-options:") and
           "trickle" in line.split(":", 1)[1].split()
           for line in prefix):
        return sdp

    for section in sections:
        if any(line.startswith("a=ice-options:") and
               "trickle" in line.split(":", 1)[1].split()
               for line in section):
            continue
        insert_at = 1
        for index, line in enumerate(section):
            if line.startswith("a=ice-pwd:"):
                insert_at = index + 1
                break
            if line.startswith("a=ice-ufrag:"):
                insert_at = index + 1
        section.insert(insert_at, "a=ice-options:trickle")

    result = newline.join(prefix + [line for section in sections for line in section])
    return result + newline if trailing_newline else result


async def send_offer_with_trickle(pc, socket, offer, on_offer_sent=None):
    """Send the offer promptly, then relay candidates gathered by aiortc."""
    local_description_task = asyncio.create_task(
        pc.setLocalDescription(offer))
    try:
        # Browser setLocalDescription returns before ICE gathering completes,
        # while aiortc waits for gathering. fal's relay expects the browser
        # ordering: an early offer followed by separate candidate messages.
        await socket.send({
            "type": "offer",
            "sdp": advertise_trickle_ice(offer.sdp),
        })
        log_line("[signal] -> offer")
        if on_offer_sent is not None:
            on_offer_sent()
        await local_description_task
        description = pc.localDescription
        if description is None:
            raise RuntimeError("aiortc did not create a local description")
        candidate_messages = local_ice_candidate_messages(description.sdp)
        for message in candidate_messages:
            await socket.send(message)
        await socket.send({
            "type": "icecandidate",
            "candidate": None,
        })
        log_line("[signal] -> icecandidate x%d" % len(candidate_messages))
    except BaseException:
        if not local_description_task.done():
            local_description_task.cancel()
        with contextlib.suppress(BaseException):
            await local_description_task
        raise


async def start_offer_with_trickle(schedule_task, pc, socket, offer,
                                   local_ready=None, on_offer_sent=None):
    """Publish an offer now and keep ICE gathering in a supervised task."""
    offer_sent = asyncio.Event()
    local_ready = local_ready or asyncio.Event()

    def mark_offer_sent():
        offer_sent.set()
        if on_offer_sent is not None:
            on_offer_sent()

    async def publish():
        await send_offer_with_trickle(
            pc, socket, offer, on_offer_sent=mark_offer_sent)
        local_ready.set()

    schedule_task(publish())
    await offer_sent.wait()
    return local_ready


async def receive_signal(socket, timeout=HANDSHAKE_TIMEOUT_SECONDS):
    """Receive a signaling message, failing clearly when startup stalls."""
    try:
        if timeout is None:
            return await socket.recv()
        return await asyncio.wait_for(socket.recv(), timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError(
            "The service did not start WebRTC within %g s; check model access, "
            "account balance, and network connectivity" % timeout) from exc


def remaining_handshake_timeout(deadline, now=None):
    """Return time left on one absolute signaling deadline."""
    current = time.monotonic() if now is None else now
    remaining = deadline - current
    if remaining <= 0:
        raise RuntimeError(
            "The service did not start WebRTC within %g s; check model access, "
            "account balance, and network connectivity" %
            HANDSHAKE_TIMEOUT_SECONDS)
    return remaining


async def wait_for_first_frame(event, timeout=MEDIA_TIMEOUT_SECONDS):
    """Fail the session when signaling succeeds but media never arrives."""
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError(
            "WebRTC connected, but no video stream arrived within %g s; "
            "check the firewall or VPN" % timeout) from exc


async def monitor_media(event, last_frame_time,
                        first_timeout=MEDIA_TIMEOUT_SECONDS,
                        stall_timeout=MEDIA_STALL_TIMEOUT_SECONDS,
                        poll_interval=1.0):
    """Guard both initial delivery and later media freezes."""
    await wait_for_first_frame(event, timeout=first_timeout)
    while True:
        await asyncio.sleep(poll_interval)
        last_frame = last_frame_time()
        if (last_frame is None or
                time.monotonic() - last_frame > stall_timeout):
            raise RuntimeError(
                "The WebRTC video stream stalled for more than %g s; "
                "the session was closed, so press Start again" % stall_timeout)


def _video_codec_name(codec):
    return str(getattr(codec, "mimeType", "") or "").strip().lower()


def video_codec_from_sdp(sdp):
    """Return the first primary codec in the negotiated video m-line."""
    payloads = []
    names = {}
    in_video = False
    found_video = False
    for raw_line in str(sdp or "").replace("\r", "").split("\n"):
        line = raw_line.strip()
        if line.startswith("m="):
            if found_video:
                break
            in_video = line.startswith("m=video ")
            if in_video:
                found_video = True
                fields = line.split()
                payloads = fields[3:]
            continue
        if not in_video or not line.startswith("a=rtpmap:"):
            continue
        try:
            payload, encoding = line[len("a=rtpmap:"):].split(None, 1)
        except ValueError:
            continue
        names[payload] = encoding.split("/", 1)[0]
    for payload in payloads:
        codec = str(names.get(payload) or "").strip()
        if codec.lower() not in ("", "rtx", "red", "ulpfec"):
            return codec.upper()
    return "unknown"


def h264_first_video_codecs(codecs):
    """Prefer H.264 while retaining every advertised fallback codec."""
    values = list(codecs or ())
    h264 = [
        codec for codec in values
        if _video_codec_name(codec) == "video/h264"
    ]
    vp8 = [
        codec for codec in values
        if _video_codec_name(codec) == "video/vp8"
    ]
    remaining = [
        codec for codec in values
        if codec not in h264 and codec not in vp8
    ]
    return h264 + vp8 + remaining if h264 else values


def apply_video_bitrate_policy():
    """Raise aiortc's encoder bounds for a detailed 720p30 source."""
    try:
        from aiortc.codecs import h264, vpx
    except (ImportError, AttributeError):
        return False
    h264.MIN_BITRATE = H264_MIN_BITRATE
    h264.DEFAULT_BITRATE = H264_DEFAULT_BITRATE
    h264.MAX_BITRATE = H264_MAX_BITRATE
    vpx.MIN_BITRATE = VP8_MIN_BITRATE
    vpx.DEFAULT_BITRATE = VP8_DEFAULT_BITRATE
    vpx.MAX_BITRATE = VP8_MAX_BITRATE
    return True


def add_quality_video_track(pc, track, rtp_sender_type):
    """Add one track and apply an H.264-first offer with a safe fallback."""
    apply_video_bitrate_policy()
    sender = pc.addTrack(track)
    try:
        capabilities = rtp_sender_type.getCapabilities("video")
        preferences = h264_first_video_codecs(
            getattr(capabilities, "codecs", ()))
        for transceiver in pc.getTransceivers():
            if getattr(transceiver, "sender", None) is sender:
                transceiver.setCodecPreferences(preferences)
                break
    except (AttributeError, TypeError, ValueError):
        # Older aiortc builds retain their valid default VP8-first offer.
        pass
    return sender


class ViewportTrack:
    """Factory wrapper delayed until optional packages have been imported."""

    @staticmethod
    def build(state, av, VideoStreamTrack, Image, ImageFilter=None):
        class Track(VideoStreamTrack):
            def __init__(self):
                super().__init__()
                self.source_image = None
                self.last_image = None
                self.last_signature = None
                self.last_active_rect = None
                self.last_control = {}
                self.source_timestamps = deque()
                self.unique_source_frames = 0
                self.last_telemetry_at = 0.0
                self.source_revision = 0
                self.source_sequence_loaded = False
                self.source_sequence_revision = 0
                self.source_sequence_fps = float(VIDEO_FPS)
                self.source_sequence_paths = ()
                self.source_sequence_tick = 0
                self.source_sequence_index = -1
                self.source_sequence_image = None
                self.source_sequence_active_rect = (
                    0.0, 0.0, 1.0, 1.0)
                self.source_sequence_state = "idle"
                self.source_sequence_last_at = 0.0
                self.last_sequence_telemetry = None
                self.frames_emitted = 0
                self.last_frame_size = CANVAS_SIZE

            def _load_sequence(self, control, requested_revision):
                self.source_sequence_loaded = True
                self.source_sequence_revision = requested_revision
                self.source_sequence_paths = ()
                self.source_sequence_tick = 0
                self.source_sequence_index = -1
                self.source_sequence_state = "error"
                raw_path = str(
                    control.get("source_sequence_manifest") or "").strip()
                if not raw_path:
                    return
                manifest_path = Path(raw_path)
                if not manifest_path.is_absolute():
                    manifest_path = state / manifest_path
                try:
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8"))
                    if not isinstance(manifest, dict):
                        return
                    fps = float(manifest.get("fps"))
                    raw_frames = manifest.get("frames")
                    if (not (fps > 0.0 and fps <= 1000.0) or
                            not isinstance(raw_frames, list) or
                            not raw_frames):
                        return
                    root = manifest_path.parent.resolve()
                    paths = []
                    for raw_frame in raw_frames:
                        if not isinstance(raw_frame, str) or not raw_frame:
                            return
                        relative = Path(raw_frame)
                        if relative.is_absolute():
                            return
                        frame_path = (root / relative).resolve()
                        try:
                            frame_path.relative_to(root)
                        except ValueError:
                            return
                        if not frame_path.is_file():
                            return
                        paths.append(frame_path)
                except (OSError, TypeError, ValueError):
                    return
                self.source_sequence_fps = fps
                self.source_sequence_paths = tuple(paths)
                self.source_sequence_active_rect = normalized_active_rect({
                    "active_rect": manifest.get("active_rect"),
                })
                self.source_sequence_state = "playing"

            def _sequence_frame(self, control, now):
                try:
                    requested_revision = max(
                        0, int(control.get(
                            "source_sequence_revision", 0)))
                except (TypeError, ValueError):
                    requested_revision = 0
                if (not self.source_sequence_loaded or
                        requested_revision > self.source_sequence_revision):
                    self._load_sequence(control, requested_revision)
                if self.source_sequence_state == "error":
                    return self.source_sequence_image
                if not self.source_sequence_paths:
                    return self.source_sequence_image
                start_gate = safe_future_epoch_gate(
                    control.get("source_sequence_start_at", 0.0))
                warming = start_gate > 0.0
                raw_index = (
                    0 if warming else int(
                        self.source_sequence_tick *
                        self.source_sequence_fps / VIDEO_FPS))
                index = min(len(self.source_sequence_paths) - 1, raw_index)
                if index != self.source_sequence_index:
                    try:
                        with Image.open(
                                self.source_sequence_paths[index]) as source:
                            source_image = source.convert("RGB")
                        sequence_control = dict(control)
                        sequence_control["active_rect"] = (
                            self.source_sequence_active_rect)
                        prepared = prepare_source_canvas(
                            source_image, sequence_control, Image, ImageFilter)
                        _close_image(source_image)
                    except (OSError, ValueError):
                        self.source_sequence_state = "error"
                        return self.source_sequence_image
                    previous = self.source_sequence_image
                    self.source_sequence_image = prepared
                    self.source_sequence_index = index
                    self.source_sequence_last_at = now
                    if previous is not None:
                        _close_image(previous)
                self.source_sequence_state = (
                    "warming" if warming else
                    "ended"
                    if raw_index >= len(self.source_sequence_paths)
                    else "playing")
                if not warming:
                    self.source_sequence_tick += 1
                return self.source_sequence_image

            def _write_sequence_telemetry(self, now):
                sequence_telemetry = (
                    self.source_sequence_state,
                    len(self.source_sequence_paths),
                    self.source_sequence_revision,
                )
                if (sequence_telemetry == self.last_sequence_telemetry and
                        now - self.last_telemetry_at <
                        TELEMETRY_INTERVAL_SECONDS):
                    return
                try:
                    update_telemetry(state, {
                        "source_fps": (
                            round(self.source_sequence_fps, 3)
                            if self.source_sequence_paths else 0.0),
                        "unique_source_frames": self.unique_source_frames,
                        "source_revision": self.source_revision,
                        "last_source_at": self.source_sequence_last_at,
                        "track_fps": VIDEO_FPS,
                        "source_sequence_state":
                            self.source_sequence_state,
                        "source_sequence_index":
                            self.source_sequence_index,
                        "source_sequence_total":
                            len(self.source_sequence_paths),
                        "source_sequence_revision":
                            self.source_sequence_revision,
                        "updated": time.time(),
                    })
                except OSError:
                    pass
                self.last_sequence_telemetry = sequence_telemetry
                self.last_telemetry_at = now

            async def recv(self):
                pts, time_base = await self.next_timestamp()
                now = time.monotonic()
                control = read_control(state)
                if control:
                    self.last_control = control
                else:
                    control = self.last_control
                if str(control.get("source_mode") or "").lower() == "sequence":
                    image = self._sequence_frame(control, now) or Image.new(
                        "RGB", CANVAS_SIZE, (18, 20, 23))
                    self._write_sequence_telemetry(now)
                    frame = av.VideoFrame.from_image(image)
                    frame.pts = pts
                    frame.time_base = time_base
                    self.frames_emitted += 1
                    self.last_frame_size = (
                        getattr(frame, "width", CANVAS_WIDTH),
                        getattr(frame, "height", CANVAS_HEIGHT),
                    )
                    return frame
                path = Path(control.get("input_path", state / "viewport.jpg"))
                active_rect = normalized_active_rect(control)
                try:
                    requested_revision = max(
                        0, int(control.get("source_revision", 0)))
                except (TypeError, ValueError):
                    requested_revision = 0
                revision_advanced = (
                    requested_revision > self.source_revision)
                source_changed = False
                source_available = False
                try:
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    source_available = True
                    signature_changed = signature != self.last_signature
                    if (revision_advanced or
                            (self.source_revision == 0 and
                             signature_changed)):
                        with Image.open(path) as source:
                            self.source_image = source.convert("RGB")
                        self.last_signature = signature
                        source_changed = True
                        self.source_timestamps.append(now)
                        self.unique_source_frames += 1
                except (OSError, ValueError):
                    pass
                if (self.source_image is not None and
                        (source_changed or
                         active_rect != self.last_active_rect)):
                    previous = self.last_image
                    self.last_image = prepare_source_canvas(
                        self.source_image, control, Image, ImageFilter)
                    self.last_active_rect = active_rect
                    if previous is not None:
                        _close_image(previous)
                source_fps = measured_source_fps(
                    self.source_timestamps, now)
                revision_changed = bool(
                    source_available and self.source_image is not None and
                    source_changed and revision_advanced)
                if revision_changed:
                    self.source_revision = requested_revision
                if (revision_changed or
                        now - self.last_telemetry_at >=
                        TELEMETRY_INTERVAL_SECONDS):
                    try:
                        update_telemetry(state, {
                            "source_fps": round(source_fps, 3),
                            "unique_source_frames": self.unique_source_frames,
                            "source_revision": self.source_revision,
                            "last_source_at": (
                                self.source_timestamps[-1]
                                if self.source_timestamps else 0.0),
                            "track_fps": VIDEO_FPS,
                            "updated": time.time(),
                        })
                    except OSError:
                        pass
                    self.last_telemetry_at = now
                image = self.last_image or Image.new(
                    "RGB", CANVAS_SIZE, (18, 20, 23))
                frame = av.VideoFrame.from_image(image)
                frame.pts = pts
                frame.time_base = time_base
                self.frames_emitted += 1
                self.last_frame_size = (
                    getattr(frame, "width", CANVAS_WIDTH),
                    getattr(frame, "height", CANVAS_HEIGHT),
                )
                return frame

            def stop(self):
                owned = (
                    self.source_image,
                    self.last_image,
                    self.source_sequence_image,
                )
                self.source_image = None
                self.last_image = None
                self.source_sequence_image = None
                closed = set()
                for image in owned:
                    if image is None or id(image) in closed:
                        continue
                    closed.add(id(image))
                    _close_image(image)
                parent_stop = getattr(super(), "stop", None)
                if callable(parent_stop):
                    parent_stop()
        return Track()


def read_control(state):
    try:
        value = json.loads((state / "control.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def process_is_alive(pid):
    """Check the owning Cinema 4D process without adding a psutil dependency."""
    if not pid:
        return True
    if os.name == "nt":
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


async def main(state, parent_pid=None):
    try:
        import av
        import fal_client
        from PIL import Image, ImageFilter
        from aiortc import (
            RTCPeerConnection,
            RTCIceServer,
            RTCRtpSender,
            RTCSessionDescription,
        )
        from aiortc.rtcconfiguration import RTCConfiguration
        from aiortc.sdp import candidate_from_sdp
    except ImportError as exc:
        report(state, "Missing dependency %s — click Install deps" % exc.name,
               "imports")
        return 2

    report(state, "Authorizing…", "auth")
    pc = None
    peer_setup_lock = asyncio.Lock()
    local_description_ready = asyncio.Event()
    last_prompt_payload = None
    reference_transport_cache = {}
    media_watch_started = False
    first_frame_received = asyncio.Event()
    last_output_at = None
    last_output_control = {}
    output_media_metrics = {
        "frames": 0,
        "size": (0, 0),
    }
    recording_controller = RecordingCommandController(state, av)

    async def consume_output(track):
        nonlocal last_output_at, last_output_control
        while True:
            frame = await track.recv()
            received_at = time.monotonic()
            output_media_metrics["frames"] += 1
            output_media_metrics["size"] = (
                getattr(frame, "width", 0),
                getattr(frame, "height", 0),
            )
            raw_image = frame.to_image().convert("RGB")
            control = read_control(state)
            if control:
                last_output_control = control
            else:
                control = last_output_control
            image = crop_remote_frame(raw_image, control)
            raw_image.close()
            target = Path(control.get(
                "output_path", state / "lucy_output.jpg"))
            tmp = target.with_suffix(".tmp.jpg")
            published = False
            try:
                image.save(tmp, "JPEG", quality=92)
                try:
                    replace_with_retry(tmp, target, attempts=4, delay=0.003)
                    published = True
                except OSError as exc:
                    # The C4D preview can hold the previous JPEG for a few ms.
                    # Dropping one frame is preferable to closing a paid session.
                    if not is_sharing_error(exc):
                        raise
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                if not recording_controller.submit_image(
                        image, received_at):
                    try:
                        image.close()
                    except OSError:
                        pass
            if published and not first_frame_received.is_set():
                last_output_at = time.monotonic()
                first_frame_received.set()
                report(state, "Receiving", "receiving")
            elif published:
                last_output_at = time.monotonic()

    try:
        control = read_control(state)
        if str(control.get("reference_image_path") or "").strip():
            report(
                state,
                "Preparing reference…",
                "reference",
            )
        try:
            last_prompt_payload = await prepare_prompt_payload(
                control, fal_client, reference_transport_cache)
        except Exception as exc:
            last_prompt_payload = prompt_payload_after_reference_error(
                control)
            report(
                state,
                "Reference upload failed; starting prompt-only: %s" % exc,
                "reference-warning",
            )

        # MODEL already contains the endpoint path. Without path="", the Python
        # client would append its default and connect to .../realtime/realtime.
        async with fal_client.realtime_async(
                MODEL, **realtime_connection_kwargs()) as ws:
            async with asyncio.TaskGroup() as tasks:
                await ws.send(last_prompt_payload)

                async def watch_prompt():
                    nonlocal last_prompt_payload
                    while True:
                        await asyncio.sleep(0.25)
                        control = read_control(state)
                        try:
                            payload = await prepare_prompt_payload(
                                control,
                                fal_client,
                                reference_transport_cache,
                            )
                        except Exception as exc:
                            payload = prompt_payload_after_reference_error(
                                control, last_prompt_payload)
                            if payload != last_prompt_payload:
                                await ws.send(prompt_update_payload(
                                    last_prompt_payload, payload))
                                last_prompt_payload = payload
                            report(
                                state,
                                ("Reference upload failed; current render "
                                 "continues: %s") % exc,
                                "reference-warning",
                            )
                            await asyncio.sleep(REFERENCE_RETRY_SECONDS)
                            continue
                        if payload != last_prompt_payload:
                            await ws.send(prompt_update_payload(
                                last_prompt_payload, payload))
                            last_prompt_payload = payload

                async def watch_parent():
                    while True:
                        await asyncio.sleep(1.0)
                        if not process_is_alive(parent_pid):
                            raise RuntimeError(
                                "Cinema 4D exited; the realtime session was closed")

                async def watch_recording():
                    while True:
                        recording_controller.sync(read_control(state))
                        await asyncio.sleep(0.05)

                tasks.create_task(watch_prompt())
                tasks.create_task(watch_parent())
                tasks.create_task(watch_recording())

                async def setup_peer(raw_servers):
                    nonlocal pc
                    async with peer_setup_lock:
                        if pc is not None:
                            return
                        report(state, "WebRTC: preparing offer…", "offer")
                        servers = [
                            RTCIceServer(
                                urls=value.get("urls"),
                                username=value.get("username"),
                                credential=value.get("credential"),
                            )
                            for value in select_ice_servers(raw_servers)
                        ]
                        pc = RTCPeerConnection(
                            RTCConfiguration(iceServers=servers))
                        source_track = ViewportTrack.build(
                            state, av,
                            __import__("aiortc").VideoStreamTrack,
                            Image, ImageFilter)
                        add_quality_video_track(
                            pc,
                            source_track,
                            RTCRtpSender,
                        )
                        tasks.create_task(monitor_transport_telemetry(
                            pc, state, source_track, output_media_metrics))

                        @pc.on("track")
                        def on_track(track):
                            if track.kind == "video":
                                tasks.create_task(consume_output(track))

                        # aiortc gathers local ICE candidates into the SDP
                        # offer; relay them explicitly after sending the raw
                        # offer so fal sees the same ordering as its browser.
                        offer = await pc.createOffer()
                        await start_offer_with_trickle(
                            tasks.create_task,
                            pc,
                            ws,
                            offer,
                            local_ready=local_description_ready,
                            on_offer_sent=lambda: report(
                                state,
                                "WebRTC: offer sent…",
                                "offer-sent",
                            ),
                        )

                async def apply_answer(sdp):
                    nonlocal media_watch_started
                    if pc is None:
                        raise RuntimeError(
                            "Server sent a WebRTC answer before the offer")
                    await local_description_ready.wait()
                    await pc.setRemoteDescription(
                        RTCSessionDescription(sdp, "answer"))
                    report(
                        state,
                        "WebRTC: connecting media stream…",
                        "connecting",
                    )
                    if not media_watch_started:
                        tasks.create_task(monitor_media(
                            first_frame_received, lambda: last_output_at))
                        media_watch_started = True

                async def add_remote_candidate(data):
                    if pc is None:
                        raise RuntimeError(
                            "Server sent an ICE candidate before the offer")
                    if not data or not data.get("candidate"):
                        await pc.addIceCandidate(None)
                        return
                    value = data["candidate"]
                    candidate = candidate_from_sdp(
                        value.replace("candidate:", "", 1))
                    candidate.sdpMid = data.get("sdpMid")
                    candidate.sdpMLineIndex = data.get("sdpMLineIndex")
                    await pc.addIceCandidate(candidate)

                signaling = FalSignalingController(
                    setup_peer=setup_peer,
                    apply_answer=apply_answer,
                    add_candidate=add_remote_candidate,
                    schedule_task=tasks.create_task,
                )
                handshake_deadline = (
                    time.monotonic() + HANDSHAKE_TIMEOUT_SECONDS)
                report(state, "Waiting for the WebRTC session…", "signaling")
                while True:
                    message = await receive_signal(
                        ws, None if signaling.remote_description_set
                        else remaining_handshake_timeout(handshake_deadline))
                    if message is None:
                        raise RuntimeError(
                            "The service closed the realtime connection")
                    log_line("[signal] <- %s" % safe_signal_summary(message))
                    if await signaling.handle(message):
                        continue
                    kind = (str(message.get("type", "")).lower()
                            if isinstance(message, dict) else "")
                    if kind == "ice-restart":
                        raise RuntimeError(
                            "The server requested an ICE restart; press Start again")
                    elif kind == "error":
                        raise RuntimeError(str(message.get("error", message)))
                    elif kind in ("prompt_ack", "set_image_ack"):
                        if not message.get("success", True):
                            raise RuntimeError(str(message.get(
                                "error", "The server rejected the session parameters")))
                        if first_frame_received.is_set():
                            report(
                                state,
                                "Receiving",
                                "receiving",
                            )

    except Exception as exc:
        report(state, "AI error: %s" % error_message(exc), "error")
        return 1
    finally:
        await asyncio.to_thread(recording_controller.shutdown, 5.0)
        if pc:
            await pc.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main(args.state, args.parent_pid)))
