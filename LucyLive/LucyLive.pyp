"""Lucy Live — dockable Lucy 2.5 viewport preview for Cinema 4D 2024+.

While a session is running, at most one C4DThread calls RenderDocument and the
dialog publishes its completed bitmap. Editor move events keep only the latest
requested state. Network/WebRTC work remains isolated in a subprocess.
"""

import contextlib
import ctypes
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import c4d
from c4d import bitmaps, documents, gui, plugins, storage


PLUGIN_ID = 1069827  # Development ID; obtain a permanent ID before distribution.
PLUGIN_NAME = "AI Render"
MODEL_ID = "decart/lucy-2-5/realtime"
DEFAULT_PROMPT = "cinematic product render, studio lighting"
CONFIG_VERSION = 4
DEFAULT_INTERVAL_MS = 125
RESPONSIVE_INTERVAL_MS = 75
MIN_INTERVAL_MS = 50
MAX_INTERVAL_MS = 5000
UI_TIMER_MS = 33
CAPTURE_DUTY_CYCLE = 0.65
STATUS_POLL_SECONDS = 0.25
INTERACTIVE_QUIET_SECONDS = 0.2
# Explicit MOVE_START/CONTINUE events are authoritative. A short scheduling
# pause under render load must not be mistaken for MOVE_END.
INTERACTIVE_LOST_END_SECONDS = 1.0
# Geometry Only needs a private full-document clone on Cinema's main thread.
# Keep drag sampling responsive without forcing that expensive operation into
# every 50 ms UI slice.
INTERACTIVE_INTERVAL_MS = 125
IDLE_SAFETY_REFRESH_SECONDS = 2.0
AUTO_RESYNC_THRESHOLD = 0.26
AUTO_RESYNC_DELAY_SECONDS = 0.35
AUTO_RESYNC_COOLDOWN_SECONDS = 10.0
RECORDING_START_TIMEOUT_SECONDS = 5.0
RECORDING_FINALIZE_TIMEOUT_SECONDS = 30.0
RENDER_FRAMES_RECORD_TAIL_SECONDS = 2.0
RENDER_FRAMES_OUTPUT_TIMEOUT_SECONDS = 10.0
RENDER_FRAMES_REPLAY_TIMEOUT_SECONDS = 5.0
RENDER_FRAMES_WARMUP_SECONDS = 1.5
AUTO_PAUSE_SECONDS = 20.0
SEQUENCE_START_TIMEOUT_SECONDS = 100.0
RETIRED_SEQUENCE_GRACE_SECONDS = 2.0
DOCUMENT_RENDER_VIEW_ID = 10001
CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 720
DEFAULT_FILM_ASPECT = float(CANVAS_WIDTH) / float(CANVAS_HEIGHT)
DEFAULT_ACTIVE_RECT = (0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
# Internal compatibility names for pre-adaptive session state. They no longer
# select a UI mode or enter config/control payloads.
SOURCE_WIDTH = CANVAS_WIDTH
SOURCE_HEIGHT = CANVAS_HEIGHT
INPUT_RESOLUTION_SPEED = "adaptive"
INPUT_RESOLUTION_QUALITY = "adaptive"
REFERENCE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
REFERENCE_MIN_SIZE = 512
REFERENCE_MAX_BYTES = 16 * 1024 * 1024
REFERENCE_PREVIEW_MAX_SIZE = 1024
REFERENCE_PREVIEW_CACHE_LIMIT = 8
_DEFERRED_CAPTURE_OWNERS = []

ID_PREVIEW = 1001
ID_PROMPT = 1002
ID_START = 1003
ID_AUTO = 1004
ID_INTERVAL = 1005
ID_API_KEY = 1006
ID_SAVE_KEY = 1007
ID_STATUS = 1008
ID_INSTALL = 1009
ID_CAPTURE = 1010
ID_SETTINGS = 1011
ID_SETTINGS_STATUS = 1012
ID_SETTINGS_CLOSE = 1013
ID_RESET = 1014
ID_CLEAN_FEED = 1015
ID_VIEW_MODE = 1016
ID_METRICS = 1017
ID_LOW_LATENCY = 1018
ID_PROMPT_EXPANSION = 1019
ID_PRESERVE_COMPOSITION = 1020
ID_AUTO_RESYNC = 1021
ID_FOLLOW_VIEW = 1022
ID_REFERENCE_PREVIEW = 1023
ID_REFERENCE_LOAD = 1024
ID_REFERENCE_CLEAR = 1025
ID_RENDER_MODE = 1026
ID_RENDER_TIME = 1027
ID_SAVE_FRAME = 1028
ID_RECORD = 1029
ID_RENDER_FRAMES = 1030
ID_BRAND_LOGO = 1031
ID_WORKSPACE_SPLIT = 2015
ID_REFRESH_GROUP = 2016
ID_RENDER_INFO_GROUP = 2019

VIEW_AI = 0
VIEW_SOURCE = 1
VIEW_COMPARE = 2

RENDER_MODE_SPEED = 0
RENDER_MODE_QUALITY = 1

FOLLOW_ACTIVE_VIEW = 0
FOLLOW_RENDER_VIEW = 1


def _retain_deferred_capture_owner(owner):
    if owner not in _DEFERRED_CAPTURE_OWNERS:
        _DEFERRED_CAPTURE_OWNERS.append(owner)


def _discard_deferred_capture_owner(owner):
    try:
        _DEFERRED_CAPTURE_OWNERS.remove(owner)
    except ValueError:
        pass


def _finish_deferred_capture_cleanup(owner):
    """Release owner-level state after its capture has been joined."""
    owner.render_frames_cancel_requested = False
    owner.render_frames_phase = ""
    owner._restore_followed_views()
    owner._release_stopped_render_frames_resources(
        delete_directories=owner.proc is None)
    _discard_deferred_capture_owner(owner)


def _drain_deferred_capture_owners():
    """Retry SDK joins from Cinema's main thread without dropping ownership."""
    for owner in tuple(_DEFERRED_CAPTURE_OWNERS):
        try:
            if not owner._stop_capture_thread():
                continue
            _finish_deferred_capture_cleanup(owner)
        except Exception:
            continue
    return not _DEFERRED_CAPTURE_OWNERS

def _drain_capture_owner_for_shutdown(owner):
    """Yield the GIL until a cancelled Python C4DThread returns from Main."""
    while True:
        try:
            if owner._stop_capture_thread():
                _finish_deferred_capture_cleanup(owner)
                return True
        except Exception:
            pass
        # Unlike End(True) from this Python callback, sleep releases the GIL.
        time.sleep(0.005)


def _drain_all_capture_owners_for_shutdown():
    """Guarantee that no retained Python thread survives plugin unloading."""
    while _DEFERRED_CAPTURE_OWNERS:
        for owner in tuple(_DEFERRED_CAPTURE_OWNERS):
            _drain_capture_owner_for_shutdown(owner)
    return True


ROOT = Path(__file__).resolve().parent
VENDOR_READY = ROOT / "vendor" / ".lucy_live_ready"
BRAND_LOGO_PATH = ROOT / "assets" / "powered_by_vantage.png"
BRAND_LOGO_AREA_SIZE = (170, 18)
BRAND_LOGO_DRAW_SIZE = (165, 14)
WINDOWS_SHARING_ERRORS = {5, 32, 33}
CLEAN_FEED_PARAMETERS = (
    ("BASEDRAW_DISPLAYFILTER_GRID", False),
    ("BASEDRAW_DISPLAYFILTER_BASEGRID", False),
    ("BASEDRAW_DISPLAYFILTER_WORLDAXIS", False),
    ("BASEDRAW_DISPLAYFILTER_HORIZON", False),
    ("BASEDRAW_DISPLAYFILTER_HUD", False),
    ("BASEDRAW_DISPLAYFILTER_NULL", False),
    ("BASEDRAW_DISPLAYFILTER_FIELD", False),
    ("BASEDRAW_DISPLAYFILTER_CAMERA", False),
    ("BASEDRAW_DISPLAYFILTER_LIGHT", False),
    ("BASEDRAW_DISPLAYFILTER_JOINT", False),
    ("BASEDRAW_DISPLAYFILTER_DEFORMER", False),
    ("BASEDRAW_DISPLAYFILTER_OTHER", False),
    ("BASEDRAW_DISPLAYFILTER_OBJECTHANDLES", False),
    ("BASEDRAW_DISPLAYFILTER_MULTIAXIS", False),
    ("BASEDRAW_DISPLAYFILTER_HANDLES", False),
    ("BASEDRAW_DISPLAYFILTER_OBJECTHIGHLIGHTING", False),
    ("BASEDRAW_DISPLAYFILTER_HIGHLIGHTING", False),
    ("BASEDRAW_DISPLAYFILTER_GUIDELINES", False),
    ("BASEDRAW_DISPLAYFILTER_SDSCAGE", False),
    ("BASEDRAW_DISPLAYFILTER_NGONLINES", False),
    ("BASEDRAW_DATA_SHOWPATH", False),
    ("BASEDRAW_DISPLAYFILTER_ONION", False),
)
CLEAN_FEED_VIDEOPOST_PARAMETERS = (
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_GRID", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_BASEGRID", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_WORLDAXIS", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_HORIZON", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_HUD", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_NULL", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_FIELD", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_CAMERA", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_LIGHT", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_JOINT", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_DEFORMER", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_OTHER", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_OBJECTHANDLES", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_MULTIAXIS", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_HANDLES", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_GUIDELINES", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_SDSCAGE", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_NGONLINES", False),
    ("VP_PREVIEWHARDWARE_DATA_SHOWPATH", False),
    ("VP_PREVIEWHARDWARE_DISPLAYFILTER_ONION", False),
)
AI_VIDEOPOST_PARAMETERS = (
    # This flag belongs only to the detached Hardware Preview RenderData.
    # Never mirror the user's live BaseDraw Geometry Only toggle into it.
    ("VP_PREVIEWHARDWARE_ONLY_GEOMETRY", True),
)
AI_BASEDRAW_PARAMETERS = (
    # Hardware Preview also reads the Render View BaseDraw when RenderDocument
    # is called outside a viewport window. Apply this only to our private
    # document snapshot; the user's live BaseDraw must remain untouched.
    ("BASEDRAW_DATA_ONLY_GEOMETRY", True),
)
VIEWPORT_EFFECT_PARAMETERS = (
    ("BASEDRAW_DATA_HQ_VIEWPORT", "VP_PREVIEWHARDWARE_ENHANCEDOPENGL"),
    ("BASEDRAW_DATA_HQ_NOISES", "VP_PREVIEWHARDWARE_NOISE"),
    ("BASEDRAW_DATA_HQ_TRANSPARENCY", "VP_PREVIEWHARDWARE_TRANSPARENCY"),
    ("BASEDRAW_DATA_HQ_SHADOWS", "VP_PREVIEWHARDWARE_SHADOW"),
    ("BASEDRAW_DATA_SHADOW_MAP_SIZE", "VP_PREVIEWHARDWARE_SHADOW_MAP_SIZE"),
    ("BASEDRAW_DATA_SHADOW_PCF",
     "VP_PREVIEWHARDWARE_ALTERNATIVESHADOWMAPPING"),
    ("BASEDRAW_DATA_HQ_REFLECTIONS", "VP_PREVIEWHARDWARE_REFLECTIONS"),
    ("BASEDRAW_DATA_REFLECTIONS_ENV_OVERRIDE",
     "VP_PREVIEWHARDWARE_REFLECTIONS_ENV_OVERRIDE"),
    ("BASEDRAW_DATA_REFLECTIONS_ENV_ROTATION",
     "VP_PREVIEWHARDWARE_REFLECTIONS_ENV_ROTATION"),
    ("BASEDRAW_DATA_REFLECTIONS_SSR",
     "VP_PREVIEWHARDWARE_REFLECTIONS_SSR"),
    ("BASEDRAW_DATA_REFLECTIONS_SSR_ITERATIONS",
     "VP_PREVIEWHARDWARE_REFLECTIONS_SSR_ITERATIONS"),
    ("BASEDRAW_DATA_REFLECTIONS_SSR_GEOMETRY_THICKNESS",
     "VP_PREVIEWHARDWARE_REFLECTIONS_SSR_GEOMETRY_THICKNESS"),
    ("BASEDRAW_DATA_REFLECTIONS_SSR_HALF_RES",
     "VP_PREVIEWHARDWARE_REFLECTIONS_SSR_HALF_RES"),
    ("BASEDRAW_DATA_HQ_POST_EFFECTS", "VP_PREVIEWHARDWARE_POSTEFFECT"),
    ("BASEDRAW_DATA_HQ_MAGICBULLETLOOKS",
     "VP_PREVIEWHARDWARE_MAGICBULLETLOOKS"),
    ("BASEDRAW_DATA_HQ_SSAO", "VP_PREVIEWHARDWARE_SSAO"),
    ("BASEDRAW_DATA_HQ_TESSELLATION", "VP_PREVIEWHARDWARE_TESSELLATION"),
    ("BASEDRAW_DATA_HQ_DEPTHOFFIELD", "VP_PREVIEWHARDWARE_DEPTHOFFIELD"),
    ("BASEDRAW_DATA_DEPTHOFFIELD_ANTIALIASED",
     "VP_PREVIEWHARDWARE_DEPTHOFFIELD_ANTIALIASED"),
    ("BASEDRAW_DATA_IGNORE_HIDDEN_POLYGON_SELECTION",
     "VP_PREVIEWHARDWARE_IGNORE_HIDDEN_POLYGON_SELECTION"),
    ("BASEDRAW_DATA_EXPERIMENT_32BIT_DEPTH", "VP_PREVIEWHARDWARE_32BIT_DEPTH"),
)
RENDER_MUTATED_VIEWPORT_PARAMETERS = (
    # Reproduced with C4D 2026.2: Hardware Preview RenderDocument can mirror
    # detached VideoPost flags back into the live render BaseDraw. Preserve
    # both the effect it is known to change and our forced Geometry Only flag.
    "BASEDRAW_DATA_HQ_SSAO",
    "BASEDRAW_DATA_ONLY_GEOMETRY",
)


def _copy_viewport_effects(source_view, preview_post):
    """Copy effect settings from Lucy's selected viewport to Hardware Preview."""
    if source_view is None or preview_post is None:
        return False
    changed = False
    for source_name, target_name in VIEWPORT_EFFECT_PARAMETERS:
        source_id = getattr(c4d, source_name, None)
        target_id = getattr(c4d, target_name, None)
        if source_id is None or target_id is None:
            continue
        try:
            value = source_view[source_id]
            if value is None:
                continue
            preview_post[target_id] = value
            changed = True
        except (AttributeError, KeyError, ReferenceError, RuntimeError, TypeError):
            continue
    return changed


def _snapshot_render_mutated_viewport_state(source_view):
    """Capture live BaseDraw values which Hardware Preview mutates."""
    if source_view is None:
        return ()
    state = []
    for name in RENDER_MUTATED_VIEWPORT_PARAMETERS:
        parameter_id = getattr(c4d, name, None)
        if parameter_id is None:
            continue
        try:
            value = source_view[parameter_id]
        except (AttributeError, KeyError, ReferenceError, RuntimeError, TypeError):
            continue
        if value is not None:
            state.append((parameter_id, value))
    return tuple(state)


def _restore_render_mutated_viewport_state(source_view, state):
    """Undo Hardware Preview side effects on the live render BaseDraw."""
    if source_view is None or not state:
        return False
    try:
        entries = tuple(state)
    except TypeError:
        return False
    changed = False
    for entry in entries:
        try:
            parameter_id, value = entry
            if source_view[parameter_id] == value:
                continue
            source_view[parameter_id] = value
            changed = True
        except (AttributeError, KeyError, ReferenceError, RuntimeError,
                TypeError, ValueError):
            continue
    if changed:
        change_message = getattr(c4d, "MSG_CHANGE", None)
        message = getattr(source_view, "Message", None)
        if change_message is not None and callable(message):
            try:
                message(change_message)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        event_add = getattr(c4d, "EventAdd", None)
        if callable(event_add):
            try:
                event_add()
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
    return changed


@contextlib.contextmanager
def _preserve_render_mutated_viewport_state(source_view):
    state = _snapshot_render_mutated_viewport_state(source_view)
    try:
        yield
    finally:
        _restore_render_mutated_viewport_state(source_view, state)


def _state_root():
    base = Path(storage.GeGetC4DPath(c4d.C4D_PATH_PREFS)) / "lucy_live"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def _file_revision(path):
    """Return a stable local-file identity used for reference reloads."""
    try:
        resolved = Path(path).expanduser().resolve()
        stat = resolved.stat()
    except (OSError, TypeError, ValueError):
        return None
    return str(resolved), int(stat.st_mtime_ns), int(stat.st_size)


def _is_sharing_error(error):
    return os.name == "nt" and getattr(error, "winerror", None) in WINDOWS_SHARING_ERRORS


def _replace_with_retry(source, target, attempts=5, delay=0.01):
    """Replace a file while tolerating brief Windows reader locks."""
    for attempt in range(attempts):
        try:
            os.replace(str(source), str(target))
            return
        except OSError as exc:
            if not _is_sharing_error(exc) or attempt + 1 == attempts:
                raise
            time.sleep(delay)


def _unlink_with_retry(path, attempts=5, delay=0.01):
    """Remove a plugin-owned temporary file after brief Windows locks clear."""
    for attempt in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if not _is_sharing_error(exc) or attempt + 1 == attempts:
                return False
            time.sleep(delay)
    return False


def _create_reference_preview(source):
    """Create a C4D-readable PNG preview without replacing the source image."""
    source = Path(source)
    revision = _file_revision(source)
    if revision is None:
        return None, "Reference file is no longer available."

    vendor = ROOT / "vendor"
    vendor_path = str(vendor)
    if vendor.is_dir() and vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return (
            None,
            "Could not decode this WebP. Open Settings and install "
            "dependencies, then try again.",
        )

    identity = repr(revision).encode("utf-8", errors="replace")
    filename = "reference-%s.png" % hashlib.sha256(identity).hexdigest()[:20]
    cache_dir = _state_root() / "reference_previews"
    target = cache_dir / filename
    if target.is_file():
        try:
            with Image.open(str(target)) as cached:
                cached.verify()
            return target, ""
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass

    temporary = cache_dir / (
        ".%s-%d-%d.png" % (target.stem, os.getpid(), time.time_ns()))
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(str(source)) as image:
            if getattr(image, "is_animated", False):
                image.seek(0)
            frame = ImageOps.exif_transpose(image)
            try:
                frame.load()
                width, height = frame.size
                if width < REFERENCE_MIN_SIZE or height < REFERENCE_MIN_SIZE:
                    return (
                        None,
                        "Reference image must be at least %d x %d pixels."
                        % (REFERENCE_MIN_SIZE, REFERENCE_MIN_SIZE),
                    )
                if max(width, height) > REFERENCE_PREVIEW_MAX_SIZE:
                    frame.thumbnail(
                        (REFERENCE_PREVIEW_MAX_SIZE,
                         REFERENCE_PREVIEW_MAX_SIZE),
                        Image.Resampling.LANCZOS,
                    )
                has_alpha = (
                    frame.mode in ("LA", "RGBA", "PA")
                    or "transparency" in frame.info
                )
                converted = frame.convert("RGBA" if has_alpha else "RGB")
                try:
                    converted.save(str(temporary), format="PNG")
                finally:
                    converted.close()
            finally:
                if frame is not image:
                    frame.close()
        _replace_with_retry(temporary, target)
        try:
            cached_previews = sorted(
                cache_dir.glob("reference-*.png"),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            cached_previews = []
        for stale in cached_previews[REFERENCE_PREVIEW_CACHE_LIMIT:]:
            try:
                stale.unlink()
            except OSError:
                pass
        return target, ""
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        return None, "Could not decode this WebP image."


def _capsule_pointer(value):
    """Return a Cinema 4D void-pointer payload as an integer.

    EVMSG_ASYNCEDITORMOVE stores MOVE_START/CONTINUE/END in BFM_CORE_PAR1 as
    a PyCapsule. This is the conversion used by Maxon's Python example. Plain
    integers are accepted too for SDK wrappers and offline contract tests.
    """
    if isinstance(value, int):
        return value
    if value is None:
        return None
    try:
        getter = ctypes.pythonapi.PyCapsule_GetPointer
        getter.restype = ctypes.c_void_p
        getter.argtypes = [ctypes.py_object, ctypes.c_char_p]
        pointer = getter(value, None)
        return int(pointer or 0)
    except (AttributeError, TypeError, ValueError, ctypes.ArgumentError):
        return None


def _editor_move_phase(message):
    try:
        payload = message.GetVoid(c4d.BFM_CORE_PAR1)
        # MOVE_START is encoded as pointer value zero and some Python builds
        # expose that as None rather than as a zero-valued capsule.
        if payload is None:
            return getattr(c4d, "MOVE_START", 0)
        return _capsule_pointer(payload)
    except (AttributeError, TypeError, RuntimeError):
        return None


def _mouse_buttons_pressed():
    """Return True while any viewport navigation/drag mouse button is held."""
    get_input_state = getattr(gui, "GetInputState", None)
    device = getattr(c4d, "BFM_INPUT_MOUSE", None)
    value_id = getattr(c4d, "BFM_INPUT_VALUE", None)
    channels = tuple(
        value for value in (
            getattr(c4d, "BFM_INPUT_MOUSELEFT", None),
            getattr(c4d, "BFM_INPUT_MOUSERIGHT", None),
            getattr(c4d, "BFM_INPUT_MOUSEMIDDLE", None),
        ) if value is not None
    )
    if (not callable(get_input_state) or device is None or
            not channels or value_id is None):
        return False
    for channel in channels:
        state = c4d.BaseContainer()
        try:
            if not get_input_state(device, channel, state):
                continue
            get_value = getattr(state, "GetInt32", None)
            value = (get_value(value_id) if callable(get_value)
                     else state[value_id])
            if value:
                return True
        except (AttributeError, KeyError, TypeError, RuntimeError, ValueError):
            continue
    return False


def _left_mouse_pressed():
    """Compatibility alias retained for layouts/tests made before v3."""
    return _mouse_buttons_pressed()


def _vector_signature(value):
    """Convert a C4D vector-like value into stable rounded floats."""
    try:
        return tuple(round(float(getattr(value, name)), 6)
                     for name in ("x", "y", "z"))
    except (AttributeError, TypeError, ValueError):
        return None


def _matrix_signature(value):
    """Convert a C4D matrix into a value signature without object identity."""
    parts = []
    for name in ("v1", "v2", "v3", "off"):
        vector = _vector_signature(getattr(value, name, None))
        if vector is None:
            return None
        parts.extend(vector)
    return tuple(parts)


def _value_signature(value):
    """Flatten SDK view parameters into deterministic primitive values."""
    if isinstance(value, dict):
        # BaseDraw.GetViewParameter() returns a dictionary in C4D 2026. Keep
        # orthographic offset/scale changes in the scene signature too.
        keys = sorted(value, key=lambda key: (type(key).__name__, repr(key)))
        return tuple((key, _value_signature(value[key])) for key in keys)
    vector = _vector_signature(value)
    if vector is not None:
        return vector
    matrix = _matrix_signature(value)
    if matrix is not None:
        return matrix
    if isinstance(value, (tuple, list)):
        return tuple(_value_signature(item) for item in value)
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return None


def _semantic_scene_signature(signature):
    """Strip RenderDocument dirty counters but keep visible source state."""
    if signature is None:
        return None
    return tuple(
        item for item in signature
        if (isinstance(item, tuple) and len(item) == 2 and
            isinstance(item[0], str))
    )


def _positive_finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0.0 else None


def _effective_render_data(document):
    """Resolve the active Take's inherited RenderData, then the document RD."""
    if document is None:
        return None
    get_take_data = getattr(document, "GetTakeData", None)
    if callable(get_take_data):
        try:
            take_data = get_take_data()
            current_take = (
                take_data.GetCurrentTake()
                if take_data is not None and
                callable(getattr(take_data, "GetCurrentTake", None))
                else None)
            get_effective = getattr(
                current_take, "GetEffectiveRenderData", None)
            effective = (
                get_effective(take_data) if callable(get_effective) else None)
            if isinstance(effective, (tuple, list)):
                effective = effective[0] if effective else None
            if effective is not None:
                return effective
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
    get_active = getattr(document, "GetActiveRenderData", None)
    try:
        return get_active() if callable(get_active) else None
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None


def _render_film_aspect(document):
    """Read the effective square-pixel image aspect from Render Settings."""
    render_data = _effective_render_data(document)
    if render_data is None:
        return DEFAULT_FILM_ASPECT

    get_resolution = getattr(render_data, "GetResolution", None)
    if callable(get_resolution):
        try:
            resolution = get_resolution()
            if isinstance(resolution, (tuple, list)) and len(resolution) >= 4:
                film_aspect = _positive_finite(resolution[3])
                if film_aspect is not None:
                    return film_aspect
                width = _positive_finite(resolution[0])
                height = _positive_finite(resolution[1])
                pixel_aspect = _positive_finite(resolution[2]) or 1.0
                if width is not None and height is not None:
                    return width / height * pixel_aspect
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            pass

    film_id = getattr(c4d, "RDATA_FILMASPECT", None)
    if film_id is not None:
        try:
            film_aspect = _positive_finite(render_data[film_id])
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            film_aspect = None
        if film_aspect is not None:
            return film_aspect
    try:
        width = _positive_finite(render_data[c4d.RDATA_XRES])
        height = _positive_finite(render_data[c4d.RDATA_YRES])
        pixel_id = getattr(c4d, "RDATA_PIXELASPECT", None)
        pixel_aspect = (
            _positive_finite(render_data[pixel_id])
            if pixel_id is not None else None) or 1.0
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        width = height = None
        pixel_aspect = 1.0
    if width is not None and height is not None:
        return width / height * pixel_aspect
    return DEFAULT_FILM_ASPECT


def _nearest_even(value):
    """Round a positive size to its nearest usable even integer."""
    return max(2, int(float(value) / 2.0 + 0.5) * 2)


def _active_rect_for_aspect(aspect):
    """Return the largest centered even rectangle inside the 720p canvas."""
    aspect = _positive_finite(aspect) or DEFAULT_FILM_ASPECT
    if aspect >= DEFAULT_FILM_ASPECT:
        width = CANVAS_WIDTH
        height = min(CANVAS_HEIGHT, _nearest_even(CANVAS_WIDTH / aspect))
    else:
        height = CANVAS_HEIGHT
        width = min(CANVAS_WIDTH, _nearest_even(CANVAS_HEIGHT * aspect))
    x = (CANVAS_WIDTH - width) // 2
    y = (CANVAS_HEIGHT - height) // 2
    return x, y, width, height


def _normalized_active_rect(rect):
    x, y, width, height = rect
    return [
        float(x) / float(CANVAS_WIDTH),
        float(y) / float(CANVAS_HEIGHT),
        float(width) / float(CANVAS_WIDTH),
        float(height) / float(CANVAS_HEIGHT),
    ]


def _same_sdk_object(first, second):
    """Compare SDK wrappers without dereferencing an already-dead wrapper."""
    if first is second:
        return True
    if first is None or second is None:
        return False

    creator_id = getattr(c4d, "MAXON_CREATOR_ID", None)
    first_unique = getattr(first, "FindUniqueID", None)
    second_unique = getattr(second, "FindUniqueID", None)
    if (creator_id is not None and callable(first_unique) and
            callable(second_unique)):
        try:
            first_marker = first_unique(creator_id)
            second_marker = second_unique(creator_id)
            first_marker = (bytes(first_marker) if first_marker else None)
            second_marker = (bytes(second_marker) if second_marker else None)
            if first_marker is not None or second_marker is not None:
                return (first_marker is not None and
                        first_marker == second_marker)
        except (AttributeError, BufferError, ReferenceError, RuntimeError,
                TypeError, ValueError):
            pass

    first_guid = getattr(first, "GetGUID", None)
    second_guid = getattr(second, "GetGUID", None)
    if callable(first_guid) and callable(second_guid):
        try:
            first_value, second_value = int(first_guid()), int(second_guid())
            # Zero means "no marker/GUID" for an unmarked SDK node. It cannot
            # establish identity between two otherwise distinct wrappers.
            if first_value and second_value:
                return first_value == second_value
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError):
            pass
    # Maxon node equality compares data, not native identity. Never use == as
    # a fallback: two distinct untitled documents can contain identical data.
    return False


def _sdk_object_alive(value):
    """Best-effort liveness check for C4DAtom/BaseDocument wrappers."""
    if value is None:
        return False
    checker = getattr(value, "IsAlive", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _bitmap_fingerprint(bitmap, columns=16, rows=9):
    """Return a tiny normalized RGB fingerprint for settled-frame cut tests."""
    get_width = getattr(bitmap, "GetBw", None)
    get_height = getattr(bitmap, "GetBh", None)
    get_pixel = getattr(bitmap, "GetPixel", None)
    if not all(callable(value) for value in (get_width, get_height, get_pixel)):
        return None
    try:
        width, height = int(get_width()), int(get_height())
        if width <= 0 or height <= 0:
            return None
        result = []
        for row in range(rows):
            y = min(height - 1, int((row + 0.5) * height / rows))
            for column in range(columns):
                x = min(width - 1, int((column + 0.5) * width / columns))
                color = get_pixel(x, y)
                result.extend(float(color[index]) / 255.0 for index in range(3))
        return tuple(result)
    except (AttributeError, IndexError, TypeError, RuntimeError, ValueError):
        return None


def _fingerprint_difference(first, second):
    if not first or not second or len(first) != len(second):
        return None
    pixel_deltas = []
    for index in range(0, len(first), 3):
        pixel_deltas.append(sum(
            abs(first[index + channel] - second[index + channel])
            for channel in range(3)) / 3.0)
    mean_delta = sum(pixel_deltas) / len(pixel_deltas)
    changed_ratio = sum(value >= 0.12 for value in pixel_deltas) / float(
        len(pixel_deltas))
    # A reframed grey scene can have a modest global RGB delta while most
    # spatial samples changed. Preserve both signals in one conservative score.
    return max(mean_delta, changed_ratio * 0.55)


def _atomic_write_json(path, value):
    tmp = path.with_name(".%s.%d.%d.tmp" %
                         (path.name, os.getpid(), time.time_ns()))
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        _replace_with_retry(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


@contextlib.contextmanager
def _clean_render_view(document, enabled):
    """Temporarily remove editor overlays from the viewport renderer input."""
    if not enabled:
        yield
        return

    basedraw = None
    for getter_name in ("GetRenderBaseDraw", "GetActiveBaseDraw"):
        getter = getattr(document, getter_name, None)
        if callable(getter):
            try:
                basedraw = getter()
            except (AttributeError, RuntimeError):
                basedraw = None
        if basedraw is not None:
            break
    if basedraw is None:
        yield
        return

    saved = []
    for name, clean_value in CLEAN_FEED_PARAMETERS:
        parameter_id = getattr(c4d, name, None)
        if parameter_id is None:
            continue
        try:
            previous = basedraw[parameter_id]
            basedraw[parameter_id] = clean_value
        except (AttributeError, KeyError, TypeError, RuntimeError):
            continue
        saved.append((parameter_id, previous))

    change_message = getattr(c4d, "MSG_CHANGE", None)
    message = getattr(basedraw, "Message", None)
    try:
        if change_message is not None and callable(message):
            message(change_message)
        yield
    finally:
        for parameter_id, previous in reversed(saved):
            try:
                basedraw[parameter_id] = previous
            except (AttributeError, KeyError, TypeError, RuntimeError):
                pass
        if change_message is not None and callable(message):
            try:
                message(change_message)
            except (AttributeError, RuntimeError):
                pass


def _document_basedraws(document):
    """Return each distinct BaseDraw owned by a private document snapshot."""
    if document is None:
        return ()

    force_create = getattr(document, "ForceCreateBaseDraw", None)
    if callable(force_create):
        try:
            force_create()
        except (AttributeError, ReferenceError, RuntimeError):
            pass

    views = []

    def append_view(view):
        if view is None:
            return
        if any(_same_sdk_object(view, existing) for existing in views):
            return
        views.append(view)

    get_count = getattr(document, "GetBaseDrawCount", None)
    get_view = getattr(document, "GetBaseDraw", None)
    if callable(get_view):
        try:
            count = int(get_count()) if callable(get_count) else 0
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError):
            count = 0
        for index in range(max(0, count)):
            try:
                append_view(get_view(index))
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                continue

    # Older/fake environments can omit GetBaseDrawCount. These fallbacks also
    # cover a document whose first editor view was just materialized.
    for getter_name in ("GetRenderBaseDraw", "GetActiveBaseDraw"):
        getter = getattr(document, getter_name, None)
        if not callable(getter):
            continue
        try:
            append_view(getter())
        except (AttributeError, ReferenceError, RuntimeError):
            pass
    return tuple(views)


def _apply_ai_render_views(document, clean_feed=False):
    """Prepare only the private snapshot views used as AI render input."""
    parameters = list(AI_BASEDRAW_PARAMETERS)
    if clean_feed:
        parameters.extend(CLEAN_FEED_PARAMETERS)
    changed = False
    change_message = getattr(c4d, "MSG_CHANGE", None)

    for basedraw in _document_basedraws(document):
        view_changed = False
        for name, value in parameters:
            parameter_id = getattr(c4d, name, None)
            if parameter_id is None:
                continue
            try:
                basedraw[parameter_id] = value
                view_changed = True
                changed = True
            except (AttributeError, KeyError, ReferenceError, TypeError,
                    RuntimeError):
                pass
        message = getattr(basedraw, "Message", None)
        if view_changed and change_message is not None and callable(message):
            try:
                message(change_message)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
    return changed


def _apply_clean_render_view(document):
    """Compatibility wrapper for preparing a clean private AI snapshot."""
    return _apply_ai_render_views(document, clean_feed=True)


def _object_type_signature(node):
    """Return a stable-enough type marker for clone topology validation."""
    get_type = getattr(node, "GetType", None)
    if callable(get_type):
        try:
            return int(get_type())
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError):
            return None
    return type(node).__name__


def _document_object_entries(document, limit=100000):
    """Return ``(path, type, node)`` entries for one document hierarchy.

    ``None`` means the hierarchy cannot be inspected safely. An empty tuple is
    a valid, inspectable document without objects. Paths make a source and its
    full-document clone pairable without writing IDs into the user's scene.
    """
    get_first = getattr(document, "GetFirstObject", None)
    if not callable(get_first):
        return None
    try:
        first = get_first()
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None

    entries = []
    pending = [(first, ())]
    try:
        while pending:
            first_sibling, parent_path = pending.pop()
            siblings = []
            sibling = first_sibling
            sibling_index = 0
            while sibling is not None:
                path = parent_path + (sibling_index,)
                node_type = _object_type_signature(sibling)
                if node_type is None:
                    return None
                siblings.append((sibling, path, node_type))
                if len(entries) + len(siblings) > limit:
                    return None
                get_next = getattr(sibling, "GetNext", None)
                if not callable(get_next):
                    sibling = None
                else:
                    sibling = get_next()
                sibling_index += 1

            for node, path, node_type in siblings:
                entries.append((path, node_type, node))
            # LIFO in reverse preserves depth-first document order.
            for node, path, _node_type in reversed(siblings):
                get_down = getattr(node, "GetDown", None)
                child = get_down() if callable(get_down) else None
                if child is not None:
                    pending.append((child, path))
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None
    return tuple(entries)


def _object_topology_signature(entries):
    if entries is None:
        return None
    return tuple((path, node_type) for path, node_type, _node in entries)


def _dirty_counter(node, flag_name):
    """Return one SDK dirty counter, or ``None`` when it is unavailable."""
    flag = getattr(c4d, flag_name, None)
    getter = getattr(node, "GetDirty", None)
    if flag is None or not callable(getter):
        return None
    try:
        return int(getter(flag))
    except (AttributeError, ReferenceError, RuntimeError, TypeError,
            ValueError):
        return None


def _hdirty_counter(node, flag_name):
    """Return one hierarchical dirty checksum when the SDK supports it."""
    flag = getattr(c4d, flag_name, None)
    getter = getattr(node, "GetHDirty", None)
    if flag is None or not callable(getter):
        return None
    try:
        return int(getter(flag))
    except (AttributeError, ReferenceError, RuntimeError, TypeError,
            ValueError):
        return None


def _hierarchy_revision(document):
    """Use Cinema's dedicated object-hierarchy checksum."""
    return _hdirty_counter(
        document, "HDIRTYFLAGS_OBJECT_HIERARCHY")


def _document_non_matrix_revision(document):
    """Return broad object/tag/material revisions for a fast unchanged check.

    Some Cinema 4D releases advance ``HDIRTYFLAGS_OBJECT`` for matrix-only
    motion. Callers must confirm an object-only revision with per-node data
    counters before retiring an interactive snapshot.
    """
    values = tuple(
        _hdirty_counter(document, name)
        for name in (
            "HDIRTYFLAGS_OBJECT",
            "HDIRTYFLAGS_TAG",
            "HDIRTYFLAGS_MATERIAL",
            "HDIRTYFLAGS_SHADER",
        )
    )
    return values if any(value is not None for value in values) else None


def _non_matrix_dirty_signature(nodes):
    """Track edits which cannot be mirrored safely with ``SetMg``.

    Matrix dirtiness is deliberately excluded: that is the fast path. Data and
    description counters cover point edits and generator/deformer parameters.
    Component selection is deliberately excluded because it changes only an
    editor overlay hidden by Geometry Only; Selection tags and other
    render-relevant edits still advance data, description, or tag counters.
    """
    result = []
    supported = False
    for node in tuple(nodes or ()):
        values = tuple(
            _dirty_counter(node, name)
            for name in (
                "DIRTYFLAGS_DATA",
                "DIRTYFLAGS_DESCRIPTION",
            )
        )
        if any(value is not None for value in values):
            supported = True
        result.append(values)
    return tuple(result) if supported else None


def _copy_node_matrix(source, target, required=False):
    """Copy one native transform without mutating the live source wrapper."""
    get_matrix = getattr(source, "GetMg", None)
    set_matrix = getattr(target, "SetMg", None)
    if not callable(get_matrix):
        return not required
    if not callable(set_matrix):
        return False
    try:
        source_matrix = get_matrix()
        target_matrix = getattr(target, "GetMg", lambda: None)()
        source_signature = _matrix_signature(source_matrix)
        target_signature = _matrix_signature(target_matrix)
        if source_signature is None:
            return not required
        if source_signature != target_signature:
            set_matrix(source_matrix)
            message = getattr(target, "Message", None)
            update_message = getattr(c4d, "MSG_UPDATE", None)
            if update_message is not None and callable(message):
                message(update_message)
        return True
    except (AttributeError, ReferenceError, RuntimeError, TypeError,
            ValueError):
        return False


def _basedraw_projection_value(basedraw):
    if basedraw is None:
        return None
    getter = getattr(basedraw, "GetProjection", None)
    if callable(getter):
        try:
            return int(getter())
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError):
            return None
    parameter_id = getattr(c4d, "BASEDRAW_DATA_PROJECTION", None)
    if parameter_id is not None:
        try:
            return int(basedraw[parameter_id])
        except (AttributeError, KeyError, ReferenceError, RuntimeError,
                TypeError, ValueError):
            pass
    return None


def _copy_basedraw_state(source, target):
    """Copy the selected editor view/default camera into a private clone."""
    if source is None or target is None:
        return False
    # BaseDraw implementations differ between C4D releases. Copy every
    # supported matrix representation, but do not require optional APIs.
    for getter_name, setter_name in (
            ("GetMg", "SetMg"),
            ("GetBaseMatrix", "SetBaseMatrix")):
        getter = getattr(source, getter_name, None)
        setter = getattr(target, setter_name, None)
        if not callable(getter) or not callable(setter):
            continue
        try:
            setter(getter())
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError):
            return False

    get_projection = getattr(source, "GetProjection", None)
    set_projection = getattr(target, "SetProjection", None)
    if callable(get_projection):
        try:
            projection = get_projection()
            if callable(set_projection):
                set_projection(projection)
            else:
                projection_id = getattr(
                    c4d, "BASEDRAW_DATA_PROJECTION", None)
                if projection_id is not None:
                    target[projection_id] = projection
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError, KeyError):
            return False

    get_rotation = getattr(source, "GetPlanarRotation", None)
    set_rotation = getattr(target, "SetPlanarRotation", None)
    if callable(get_rotation) and callable(set_rotation):
        try:
            set_rotation(get_rotation())
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError):
            return False
    return True


def _copy_camera_parameters(source, target):
    """Copy the documented projection/lens controls of an editor camera."""
    if source is None or target is None:
        return source is None and target is None
    for name in (
            "CAMERA_PROJECTION",
            "CAMERAOBJECT_APERTURE",
            "CAMERA_FOCUS",
            "CAMERA_ZOOM"):
        parameter_id = getattr(c4d, name, None)
        if parameter_id is None:
            continue
        try:
            target[parameter_id] = source[parameter_id]
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError, KeyError):
            # Some editor cameras expose only a subset of these parameters.
            continue
    return True


class InteractiveSnapshot:
    """Explicit UI-owned cache for sequential interactive renders.

    A SourceCaptureThread may borrow ``document`` and ``render_data``. The
    dialog never synchronizes this state until that C4DThread has been joined.
    Retiring only removes the dialog's reusable reference; an in-flight thread
    keeps its own Python ownership until End(True).
    """

    def __init__(self, source_document, document, render_data, active_rect,
                 clean_feed, follow_view, view_index, scene_camera,
                 view_projection, topology_signature, source_nodes,
                 snapshot_nodes, non_matrix_signature,
                 hierarchy_revision, document_non_matrix_revision):
        self.source_document = source_document
        self.document = document
        self.render_data = render_data
        self.active_rect = tuple(active_rect)
        self.clean_feed = bool(clean_feed)
        self.follow_view = int(follow_view)
        self.view_index = view_index
        self.scene_camera = scene_camera
        self.view_projection = view_projection
        self.topology_signature = topology_signature
        self.source_nodes = tuple(source_nodes)
        self.snapshot_nodes = tuple(snapshot_nodes)
        self.non_matrix_signature = non_matrix_signature
        self.hierarchy_revision = hierarchy_revision
        self.document_non_matrix_revision = document_non_matrix_revision
        self.retired = False


def _exact_basedraw(document, getter_name):
    getter = getattr(document, getter_name, None)
    if not callable(getter):
        return None
    try:
        return getter()
    except (AttributeError, ReferenceError, RuntimeError):
        return None


def _source_basedraw(document, follow_mode=FOLLOW_RENDER_VIEW):
    """Return the view selected as Lucy's source, with a safe fallback."""
    names = (("GetActiveBaseDraw", "GetRenderBaseDraw")
             if follow_mode == FOLLOW_ACTIVE_VIEW else
             ("GetRenderBaseDraw", "GetActiveBaseDraw"))
    for getter_name in names:
        basedraw = _exact_basedraw(document, getter_name)
        if basedraw is not None:
            return basedraw
    return None


def _basedraw_scene_camera(document, basedraw):
    """Return only a document camera, never Cinema's transient editor camera."""
    if basedraw is None:
        return None
    scene_camera = getattr(basedraw, "GetSceneCamera", None)
    if callable(scene_camera):
        try:
            return scene_camera(document)
        except (AttributeError, ReferenceError, TypeError, RuntimeError):
            return None
    return None


def _basedraw_camera(document, basedraw):
    camera = _basedraw_scene_camera(document, basedraw)
    if camera is None:
        editor_camera = getattr(basedraw, "GetEditorCamera", None)
        if callable(editor_camera):
            try:
                camera = editor_camera()
            except (AttributeError, ReferenceError, RuntimeError):
                camera = None
    return camera


def _render_view_label(document, follow_mode=FOLLOW_RENDER_VIEW):
    """Describe the actual viewport/camera Hardware Preview will capture."""
    if (follow_mode == FOLLOW_ACTIVE_VIEW and
            _exact_basedraw(document, "GetActiveBaseDraw") is None):
        follow_mode = FOLLOW_RENDER_VIEW
    basedraw = _source_basedraw(document, follow_mode)

    camera = _basedraw_camera(document, basedraw)

    camera_name = "Default Camera"
    get_name = getattr(camera, "GetName", None)
    if callable(get_name):
        try:
            camera_name = get_name() or camera_name
        except (AttributeError, RuntimeError):
            pass
    view_name = ("ACTIVE VIEW" if follow_mode == FOLLOW_ACTIVE_VIEW
                 else "RENDER VIEW")
    return "SOURCE · %s · %s" % (view_name, camera_name)


def _application_roots():
    """Return candidate roots for the currently running Cinema 4D build."""
    getters = (
        getattr(storage, "GeGetStartupPath", None),
        getattr(storage, "GeGetStartupApplication", None),
        lambda: storage.GeGetC4DPath(c4d.C4D_PATH_APPLICATION),
    )
    roots = []
    for getter in getters:
        if not callable(getter):
            continue
        try:
            raw_path = getter()
        except (OSError, RuntimeError):
            continue
        if not raw_path:
            continue
        path = Path(str(raw_path))
        if path.is_file() or path.suffix.lower() in (".exe", ".app"):
            path = path.parent
        if path not in roots:
            roots.append(path)
    return roots


def _find_c4dpy():
    """Return the standalone interpreter shipped with this Cinema 4D build."""
    # The startup functions directly identify the installation of the running
    # Cinema 4D build. C4D_PATH_APPLICATION remains only as a legacy fallback.
    roots = _application_roots()

    for root in roots:
        candidates = (
            root / "c4dpy.exe",
            root / "c4dpy.app" / "Contents" / "MacOS" / "c4dpy",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def _find_worker_python():
    """Find the lightweight CPython runtime bundled with Cinema 4D.

    The WebRTC worker does not import c4d. Running it with this executable
    avoids starting a second headless Cinema 4D instance and loading every C4D
    plugin again. c4dpy remains a compatibility fallback for older layouts.
    """
    for root in _application_roots():
        candidates = (
            root / "resource" / "modules" / "python" / "libs" /
            "win64" / "python.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return _find_c4dpy()


class SourceCaptureThread(c4d.threading.C4DThread):
    """Render one source snapshot without blocking Cinema 4D's UI thread."""

    def __init__(self, document, render_data, bitmap, target, source_label,
                 interactive=False, clean_feed=False, isolated=False,
                 source_view=None, source_view_state=()):
        super().__init__()
        self.document = document
        self.render_data = render_data
        self.bitmap = bitmap
        self.target = Path(target)
        self.source_label = source_label
        self.interactive = bool(interactive)
        self.clean_feed = bool(clean_feed)
        self.isolated = bool(isolated)
        # These wrappers are never touched from Main(). The dialog restores
        # them only after End() has joined this render on Cinema's UI thread.
        self.source_view = source_view
        self.source_view_state = tuple(source_view_state or ())
        self.success = False
        self.cancelled = False
        self.error = ""
        self.started_at = 0.0
        self.duration_ms = 0.0
        self.finished_at = 0.0
        self.main_completed = False

    def Main(self):
        try:
            # Interactive matrices are synchronized into a reusable private
            # document between frames. Omitting NODOCUMENTCLONE makes
            # RenderDocument take its own immutable snapshot for each render.
            flags = c4d.RENDERFLAGS_EXTERNAL
            if self.isolated and not self.interactive:
                # _make_clean_snapshot already owns a private immutable clone.
                # Final/sequence captures do not mutate it while rendering.
                flags |= getattr(c4d, "RENDERFLAGS_NODOCUMENTCLONE", 0)
            # An interactive cache is intentionally reused between samples.
            # Never evaluate XPresso directly on that graph: Cinema must make
            # its own render clone for every in-drag frame.
            result = documents.RenderDocument(
                self.document,
                self.render_data.GetDataInstance(),
                self.bitmap,
                flags,
                self.Get(),
            )
            if self.TestBreak():
                self.cancelled = True
                return
            if result != c4d.RENDERRESULT_OK:
                self.error = "Viewport render error: %s" % result
                return
            self.success = True
        except Exception as exc:
            self.error = "Viewport capture exception: %s" % exc
        finally:
            self.main_completed = True
            # GeDialog.Timer can be starved while the native Move tool owns the
            # mouse. SpecialEventAdd is Cinema's supported thread-to-main-thread
            # bridge and lets CoreMessage harvest this completed frame safely.
            post_event = getattr(c4d, "SpecialEventAdd", None)
            if callable(post_event):
                try:
                    post_event(PLUGIN_ID)
                except (AttributeError, RuntimeError, TypeError):
                    pass


class BrandLogoArea(gui.GeUserArea):
    """Small UI-only attribution mark shown below the prompt."""

    def __init__(self, path=BRAND_LOGO_PATH):
        super().__init__()
        self.path = Path(path)
        self.bitmap = None
        bitmap = bitmaps.BaseBitmap()
        try:
            result, is_movie = bitmap.InitWith(str(self.path))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            result, is_movie = None, False
        if result == c4d.IMAGERESULT_OK and not is_movie:
            self.bitmap = bitmap

    def DrawMsg(self, x1, y1, x2, y2, msg):
        dirty_width = max(0, int(x2) - int(x1) + 1)
        dirty_height = max(0, int(y2) - int(y1) + 1)
        if dirty_width <= 0 or dirty_height <= 0:
            return
        try:
            area_width = max(0, int(self.GetWidth()))
            area_height = max(0, int(self.GetHeight()))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return
        if area_width <= 0 or area_height <= 0:
            return
        # Repaint one complete double-buffered surface. Partial offscreen
        # buffers can leave differently scaled retained tiles after a resize.
        self.OffScreenOn()
        try:
            rgb = self.GetColorRGB(c4d.COLOR_BG)
            background = c4d.Vector(
                float(rgb["r"]) / 255.0,
                float(rgb["g"]) / 255.0,
                float(rgb["b"]) / 255.0,
            )
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            background = c4d.Vector(0.12)
        self.DrawSetPen(background)
        self.DrawRectangle(0, 0, area_width - 1, area_height - 1)
        if self.bitmap is None:
            return

        try:
            width = int(self.bitmap.GetBw())
            height = int(self.bitmap.GetBh())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return
        if width <= 0 or height <= 0 or area_width <= 2 or area_height <= 2:
            return

        max_width = min(BRAND_LOGO_DRAW_SIZE[0], area_width - 2)
        max_height = min(BRAND_LOGO_DRAW_SIZE[1], area_height - 2)
        scale = min(max_width / float(width), max_height / float(height))
        draw_width = max(1, int(width * scale + 0.5))
        draw_height = max(1, int(height * scale + 0.5))
        draw_x = 1
        draw_y = max(1, (area_height - draw_height) // 2)
        mode = (
            getattr(c4d, "BMP_NORMALSCALED", 0) |
            getattr(c4d, "BMP_ALLOWALPHA", 0)
        )
        self.DrawBitmap(
            self.bitmap,
            draw_x, draw_y, draw_width, draw_height,
            0, 0, width, height, mode)

    def GetMinSize(self):
        return BRAND_LOGO_AREA_SIZE


class ReferenceArea(gui.GeUserArea):
    """Square local preview for Lucy's optional reference image."""

    def __init__(self):
        super().__init__()
        self.bitmap = None
        self.path = None
        self.preview_path = None
        self.caption = "No reference"
        self.error = ""

    def set_image(self, path):
        original_path = Path(path)
        preview_path = original_path
        self.error = ""
        bitmap = bitmaps.BaseBitmap()
        try:
            result, is_movie = bitmap.InitWith(str(preview_path))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            result, is_movie = None, False
        if result != c4d.IMAGERESULT_OK or is_movie:
            if original_path.suffix.lower() != ".webp":
                self.error = "Could not decode this reference image."
                return False
            preview_path, self.error = _create_reference_preview(original_path)
            if preview_path is None:
                return False
            bitmap = bitmaps.BaseBitmap()
            try:
                result, is_movie = bitmap.InitWith(str(preview_path))
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                result, is_movie = None, False
        if result != c4d.IMAGERESULT_OK or is_movie:
            self.error = "Could not load the generated WebP preview."
            return False
        try:
            if (bitmap.GetBw() < REFERENCE_MIN_SIZE or
                    bitmap.GetBh() < REFERENCE_MIN_SIZE):
                self.error = (
                    "Reference image must be at least %d x %d pixels."
                    % (REFERENCE_MIN_SIZE, REFERENCE_MIN_SIZE)
                )
                return False
        except (AttributeError, RuntimeError, TypeError, ValueError):
            self.error = "Could not read the reference image dimensions."
            return False
        self.bitmap = bitmap
        self.path = original_path
        self.preview_path = Path(preview_path)
        self.caption = self.path.name
        self.Redraw()
        return True

    def clear(self):
        self.bitmap = None
        self.path = None
        self.preview_path = None
        self.caption = "No reference"
        self.error = ""
        self.Redraw()

    def DrawMsg(self, x1, y1, x2, y2, msg):
        dirty_width = max(0, int(x2) - int(x1) + 1)
        dirty_height = max(0, int(y2) - int(y1) + 1)
        if dirty_width <= 0 or dirty_height <= 0:
            return
        try:
            area_width = max(0, int(self.GetWidth()))
            area_height = max(0, int(self.GetHeight()))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return
        if area_width <= 0 or area_height <= 0:
            return
        self.OffScreenOn()
        self.DrawSetPen(c4d.Vector(0.055, 0.06, 0.07))
        self.DrawRectangle(0, 0, area_width - 1, area_height - 1)
        if self.bitmap is None:
            self.DrawSetTextCol(c4d.Vector(0.58), c4d.COLOR_TRANS)
            width = self.DrawGetTextWidth(self.caption)
            self.DrawText(
                self.caption, max(6, (area_width - width) // 2),
                max(6, (area_height - self.DrawGetFontHeight()) // 2))
            return

        width, height = self.bitmap.GetBw(), self.bitmap.GetBh()
        if width <= 0 or height <= 0:
            return
        scale = min(area_width / float(width), area_height / float(height))
        draw_width = max(1, int(width * scale))
        draw_height = max(1, int(height * scale))
        draw_x = (area_width - draw_width) // 2
        draw_y = (area_height - draw_height) // 2
        self.DrawBitmap(
            self.bitmap, draw_x, draw_y, draw_width, draw_height,
            0, 0, width, height, c4d.BMP_NORMALSCALED)

    def GetMinSize(self):
        return 64, 64


class PreviewArea(gui.GeUserArea):
    def __init__(self):
        super().__init__()
        self.source_bitmap = None
        self.ai_bitmap = None
        self.view_mode = VIEW_AI
        self.source_label = "SOURCE · RENDER VIEW"
        self.caption = "Press Start"

    @staticmethod
    def _load_bitmap(path):
        bmp = bitmaps.BaseBitmap()
        result, _ = bmp.InitWith(str(path))
        if result == c4d.IMAGERESULT_OK:
            return bmp
        return None

    def set_source_image(self, path):
        bitmap = self._load_bitmap(path)
        if bitmap is None:
            return False
        self.source_bitmap = bitmap
        self.caption = ""
        self.Redraw()
        return True

    def set_ai_image(self, path):
        bitmap = self._load_bitmap(path)
        if bitmap is None:
            return False
        self.ai_bitmap = bitmap
        self.caption = ""
        self.Redraw()
        return True

    def set_image(self, path):
        """Compatibility alias for older layouts/tests."""
        return self.set_ai_image(path)

    def clear_ai(self):
        self.ai_bitmap = None
        self.Redraw()

    def set_source_label(self, value):
        self.source_label = str(value or "SOURCE · RENDER VIEW")

    def set_view_mode(self, mode):
        self.view_mode = mode if mode in (
            VIEW_AI, VIEW_SOURCE, VIEW_COMPARE) else VIEW_AI
        self.Redraw()

    def _draw_bitmap(self, bitmap, x1, y1, x2, y2, label):
        if bitmap is None:
            return False
        bw, bh = bitmap.GetBw(), bitmap.GetBh()
        if bw <= 0 or bh <= 0:
            return False
        aw, ah = max(1, x2 - x1 + 1), max(1, y2 - y1 + 1)
        scale = min(aw / float(bw), ah / float(bh))
        dw, dh = max(1, int(bw * scale)), max(1, int(bh * scale))
        dx, dy = x1 + (aw - dw) // 2, y1 + (ah - dh) // 2
        self.DrawBitmap(bitmap, dx, dy, dw, dh, 0, 0, bw, bh,
                        c4d.BMP_NORMALSCALED)
        if label:
            self.DrawSetTextCol(c4d.Vector(0.9), c4d.COLOR_TRANS)
            self.DrawText(label, dx + 8, dy + 8)
        return True

    def DrawMsg(self, x1, y1, x2, y2, msg):
        dirty_width = max(0, int(x2) - int(x1) + 1)
        dirty_height = max(0, int(y2) - int(y1) + 1)
        if dirty_width <= 0 or dirty_height <= 0:
            return
        try:
            width = max(0, int(self.GetWidth()))
            height = max(0, int(self.GetHeight()))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return
        if width <= 0 or height <= 0:
            return
        # x1..y2 is only Cinema's invalidation rectangle. A tile-sized
        # offscreen buffer can retain fragments from older layouts when the
        # window is resized during interaction, which looks like several
        # differently scaled frames. Always repaint one full double-buffered
        # canvas and derive all geometry from its stable dimensions.
        self.OffScreenOn()
        canvas_x1, canvas_y1 = 0, 0
        canvas_x2, canvas_y2 = width - 1, height - 1
        self.DrawSetPen(c4d.Vector(0.055, 0.06, 0.07))
        self.DrawRectangle(canvas_x1, canvas_y1, canvas_x2, canvas_y2)
        drawn = False
        if self.view_mode == VIEW_COMPARE:
            middle = max(1, width) // 2
            drawn = self._draw_bitmap(
                self.source_bitmap, canvas_x1, canvas_y1,
                middle - 2, canvas_y2,
                self.source_label)
            drawn = self._draw_bitmap(
                self.ai_bitmap, middle + 1, canvas_y1,
                canvas_x2, canvas_y2, "") or drawn
        elif self.view_mode == VIEW_SOURCE:
            drawn = self._draw_bitmap(
                self.source_bitmap, canvas_x1, canvas_y1,
                canvas_x2, canvas_y2, self.source_label)
        else:
            drawn = self._draw_bitmap(
                self.ai_bitmap, canvas_x1, canvas_y1,
                canvas_x2, canvas_y2, "")
            if not drawn:
                drawn = self._draw_bitmap(
                    self.source_bitmap, canvas_x1, canvas_y1,
                    canvas_x2, canvas_y2,
                    self.source_label)
        if not drawn and self.caption:
            self.DrawSetTextCol(c4d.Vector(0.65), c4d.COLOR_TRANS)
            tw = self.DrawGetTextWidth(self.caption)
            self.DrawText(
                self.caption, max(8, (width - tw) // 2),
                max(8, (height - self.DrawGetFontHeight()) // 2))

    def GetMinSize(self):
        return 320, 180


class LucySettingsDialog(gui.GeDialog):
    """Small modal dialog kept separate so setup never falls below the preview."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def CreateLayout(self):
        self.SetTitle("AI Render — Settings")
        self.GroupBegin(3000, c4d.BFH_SCALEFIT, 2, 1)
        self.AddStaticText(3010, c4d.BFH_LEFT, name="API key")
        self.AddEditText(ID_API_KEY, c4d.BFH_SCALEFIT, initw=420,
                         editflags=getattr(c4d, "EDITTEXT_PASSWORD", 0))
        self.GroupEnd()

        self.GroupBegin(3001, c4d.BFH_RIGHT, 3, 1)
        self.AddButton(ID_SAVE_KEY, c4d.BFH_FIT, name="Save key")
        self.AddButton(ID_INSTALL, c4d.BFH_FIT, name="Install deps")
        self.AddButton(ID_SETTINGS_CLOSE, c4d.BFH_FIT, name="Close")
        self.GroupEnd()
        self.AddStaticText(ID_SETTINGS_STATUS, c4d.BFH_SCALEFIT, name="")
        return True

    def InitValues(self):
        self.SetString(ID_API_KEY, self.owner.api_key)
        dependency_state = ("Dependencies installed" if VENDOR_READY.is_file()
                            else "Dependencies not installed")
        self.SetString(ID_SETTINGS_STATUS, dependency_state)
        self.SetTimer(250)
        return True

    def _save_key(self):
        self.owner.api_key = self.GetString(ID_API_KEY).strip()
        if self.owner._save_settings() is None:
            self.SetString(ID_SETTINGS_STATUS, self.owner.last_status)
            return False
        self.SetString(ID_SETTINGS_STATUS, "API key saved locally")
        return True

    def Command(self, cid, msg):
        if cid == ID_SAVE_KEY:
            self._save_key()
        elif cid == ID_INSTALL:
            if self.owner._install_deps():
                self.SetString(
                    ID_SETTINGS_STATUS,
                    "Installer opened — wait for Done in the console")
            else:
                self.SetString(ID_SETTINGS_STATUS, self.owner.last_status)
        elif cid == ID_SETTINGS_CLOSE:
            self.Close()
        return True

    def Timer(self, msg):
        status = self.owner._poll_installer()
        if status:
            self.SetString(ID_SETTINGS_STATUS, status)


class LucyDialog(gui.GeDialog):
    def __init__(self):
        super().__init__()
        self.preview = PreviewArea()
        self.reference_preview = ReferenceArea()
        self.brand_logo = BrandLogoArea()
        self.proc = None
        self.worker_log = None
        self.installer_proc = None
        self.installer_started_at = 0.0
        self.settings_dialog = None
        self.api_key = ""
        self.reference_path = ""
        self.reference_revision = None
        self.published_active_rect = None
        self.published_input_resolution = INPUT_RESOLUTION_SPEED
        self.auto_resync = True
        self.follow_view = FOLLOW_ACTIVE_VIEW
        self.workspace_weights = c4d.BaseContainer()
        self.workspace_weights.SetInt32(
            c4d.GROUPWEIGHTS_PERCENT_H_CNT, 2)
        self.workspace_weights.SetFloat(
            c4d.GROUPWEIGHTS_PERCENT_H_VAL, 1.0)
        self.workspace_weights.SetFloat(
            c4d.GROUPWEIGHTS_PERCENT_H_VAL + 1, -96.0)
        self.last_status = "Ready"
        self.running = False
        self.auto_paused = False
        self.auto_pause_armed = False
        self.last_activity_at = 0.0
        self.auto_paused_document = None
        self.auto_paused_scene_signature = None
        self.auto_resume_pending = False
        self.auto_resume_source_refresh = False
        self.render_frames_active = False
        self.render_frames_cancel_requested = False
        self.render_frames_phase = ""
        self.render_frames_source_document = None
        self.render_frames_document = None
        self.render_frames_render_data = None
        self.render_frames_fps = 0
        self.render_frames_start_frame = 0
        self.render_frames_frame = 0
        self.render_frames_end_frame = 0
        self.render_frames_active_rect = DEFAULT_ACTIVE_RECT
        self.render_frames_directory = None
        self.render_frames_manifest_frames = []
        self.auto_animation_phase = ""
        self.auto_animation_destination = ""
        self.auto_animation_replay_revision = 0
        self.auto_animation_replay_deadline = 0.0
        self.auto_animation_tail_deadline = 0.0
        self.auto_animation_tail_timeout = 0.0
        self.auto_animation_output_start_sequence = 0
        self.sequence_source_active = False
        self.sequence_playback_ended = False
        self.sequence_waiting_for_start = False
        self.sequence_started_at = 0.0
        self.sequence_replay_on_recording_ack = False
        self.source_sequence_manifest = ""
        self.source_sequence_revision = 0
        self.source_sequence_start_at = 0.0
        self.last_sequence_telemetry_signature = None
        self.retired_sequence_directories = []
        self.render_frames_abort_message = ""
        self.published_source_revision = 0
        self.ai_output_sequence = 0
        self.timer_busy = False
        self.render_timer_started_at = 0.0
        self.render_timer_elapsed = 0.0
        self.render_timer_active = False
        self.recording_active = False
        self.recording_stopping = False
        self.recording_id = ""
        self.recording_work_path = None
        self.recording_final_path = ""
        self.recording_capture_enabled = True
        self.recording_capture_after = 0.0
        self.recording_start_acknowledged = False
        self.recording_start_requested_at = 0.0
        self.recording_stop_requested_at = 0.0
        self.last_recording_status_signature = None
        self.last_capture = 0.0
        self.capture_duration_ms = 0.0
        self.capture_duration_ema_ms = 0.0
        self.effective_interval_ms = float(DEFAULT_INTERVAL_MS)
        self.next_capture_at = 0.0
        self.last_output_signature = None
        self.last_output_at = 0.0
        self.next_status_poll_at = 0.0
        self.worker_stage = ""
        self.capture_thread = None
        self.capture_pending = False
        self.capture_pending_clean = False
        self.manual_capture_pending = False
        self.final_clean_pending = False
        self.resolution_capture_pending = False
        self.editor_move_active = False
        self.last_editor_move_at = 0.0
        self.interactive_until = 0.0
        self.interactive_next_capture_at = 0.0
        self.interactive_output_poll_at = 0.0
        self.interactive_pending_phase = None
        self.realtime_event_queued = False
        self.realtime_pump_busy = False
        self.last_scene_signature = None
        self.last_observed_scene_signature = None
        self.source_activity_generation = 0
        self.last_settled_fingerprint = None
        self.resync_pending_at = 0.0
        self.last_auto_resync_at = 0.0
        self.followed_render_views = []
        self.source_document = None
        self.view_context_initialized = False
        self.last_source_view_index = None
        self.last_source_camera = None
        self.last_source_projection = None
        self.interactive_snapshot = None
        self.resync_cut_armed = False
        self.root_state = _state_root()
        self.state = self.root_state / "sessions" / str(os.getpid())
        self.state.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root_state / "config.json"
        self.control_path = self.state / "control.json"
        self.input_path = self.state / "viewport.jpg"
        self.output_path = self.state / "lucy_output.jpg"
        self.telemetry_path = self.state / "telemetry.json"
        self.recording_status_path = self.state / "recording_status.json"

    def CreateLayout(self):
        self.SetTitle("AI Render")
        self.GroupBegin(1999, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0)

        # Keep only the primary session and AI controls on the first row.
        self.GroupBegin(2002, c4d.BFH_SCALEFIT | c4d.BFV_FIT, 5, 1)
        self.AddButton(ID_START, c4d.BFH_FIT | c4d.BFV_FIT,
                       name="▶ Start")
        self.AddButton(ID_RESET, c4d.BFH_FIT | c4d.BFV_FIT,
                       name="↻ Reset")
        self.AddCheckbox(ID_PRESERVE_COMPOSITION,
                         c4d.BFH_FIT | c4d.BFV_FIT,
                         0, 0, "Lock composition")
        self.AddCheckbox(ID_PROMPT_EXPANSION,
                         c4d.BFH_FIT | c4d.BFV_FIT,
                         0, 0, "Expand prompt")
        self.AddButton(ID_SETTINGS, c4d.BFH_FIT | c4d.BFV_FIT,
                       name="Settings…")
        self.GroupEnd()

        # Auto refresh owns its interval vertically; secondary viewport
        # controls stay on the same compact row.
        self.GroupBegin(2003, c4d.BFH_SCALEFIT | c4d.BFV_FIT, 3, 1)
        self.GroupBegin(
            ID_REFRESH_GROUP, c4d.BFH_FIT | c4d.BFV_FIT, 1, 0)
        self.AddCheckbox(ID_AUTO, c4d.BFH_FIT | c4d.BFV_FIT,
                         0, 0, "Auto refresh")
        self.GroupBegin(2017, c4d.BFH_FIT | c4d.BFV_FIT, 2, 1)
        self.AddEditNumberArrows(ID_INTERVAL,
                                 c4d.BFH_FIT | c4d.BFV_FIT,
                                 initw=70, inith=0)
        self.AddStaticText(2010, c4d.BFH_LEFT | c4d.BFV_FIT, name="ms")
        self.GroupEnd()
        self.GroupEnd()
        self.AddCheckbox(ID_CLEAN_FEED, c4d.BFH_FIT | c4d.BFV_FIT,
                         0, 0, "Clean feed")
        self.GroupBegin(2018, c4d.BFH_RIGHT | c4d.BFV_FIT, 2, 1)
        self.AddStaticText(2012, c4d.BFH_RIGHT | c4d.BFV_FIT, name="View")
        self.AddComboBox(ID_VIEW_MODE, c4d.BFH_FIT | c4d.BFV_FIT,
                         initw=100, inith=0)
        self.AddChild(ID_VIEW_MODE, VIEW_AI, "AI")
        self.AddChild(ID_VIEW_MODE, VIEW_SOURCE, "Source")
        self.AddChild(ID_VIEW_MODE, VIEW_COMPARE, "Compare")
        self.GroupEnd()
        self.GroupEnd()

        self.GroupBegin(
             ID_RENDER_INFO_GROUP,
             c4d.BFH_SCALEFIT | c4d.BFV_FIT,
             4, 1)
        self.AddStaticText(ID_RENDER_TIME,
                           c4d.BFH_SCALEFIT | c4d.BFV_FIT,
                           name="Render time: 0.0 s")
        self.AddButton(ID_RENDER_FRAMES, c4d.BFH_RIGHT | c4d.BFV_FIT,
                       name="Render Frames")
        self.AddButton(ID_SAVE_FRAME, c4d.BFH_RIGHT | c4d.BFV_FIT,
                       name="Save Frame")
        self.AddButton(ID_RECORD, c4d.BFH_RIGHT | c4d.BFV_FIT,
                       name="● REC")
        self.GroupEnd()

        # Cinema's weighted grid provides the native draggable divider between
        # the render and the prompt/reference composer.
        self.GroupBegin(
            ID_WORKSPACE_SPLIT,
            c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
            1, 0,
            groupflags=c4d.BFV_GRIDGROUP_ALLOW_WEIGHTS,
        )
        self.GroupBegin(2000, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0)
        self.AddUserArea(ID_PREVIEW, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                         initw=640, inith=360)
        self.AttachUserArea(self.preview, ID_PREVIEW)
        self.GroupEnd()

        # Keep the render view dominant. The composer starts at only 64 px,
        # then may grow when the user gives the docked window more height.
        self.GroupBegin(2001, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 2, 1)
        self.GroupBegin(2005, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0)
        self.AddStaticText(2011, c4d.BFH_LEFT | c4d.BFV_FIT,
                           name="Prompt")
        self.AddMultiLineEditText(
            ID_PROMPT, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
            initw=420, inith=48,
            style=getattr(c4d, "DR_MULTILINE_WORDWRAP", 0))
        self.AddUserArea(
            ID_BRAND_LOGO, c4d.BFH_LEFT | c4d.BFV_FIT,
            initw=BRAND_LOGO_AREA_SIZE[0],
            inith=BRAND_LOGO_AREA_SIZE[1])
        self.AttachUserArea(self.brand_logo, ID_BRAND_LOGO)
        self.GroupEnd()

        self.GroupBegin(2006, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0)
        self.AddStaticText(2014, c4d.BFH_LEFT | c4d.BFV_FIT,
                           name="Reference")
        self.GroupBegin(
            2007, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 2, 1)
        self.AddUserArea(
            ID_REFERENCE_PREVIEW,
            c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
            initw=64, inith=64)
        self.AttachUserArea(self.reference_preview, ID_REFERENCE_PREVIEW)
        self.GroupBegin(2008, c4d.BFH_FIT | c4d.BFV_FIT, 1, 0)
        self.AddButton(ID_REFERENCE_LOAD, c4d.BFH_FIT | c4d.BFV_FIT,
                       name="Load...")
        self.AddButton(ID_REFERENCE_CLEAR, c4d.BFH_FIT | c4d.BFV_FIT,
                       name="Clear")
        self.GroupEnd()
        self.GroupEnd()
        self.GroupEnd()
        self.GroupEnd()
        self.GroupWeightsLoad(ID_WORKSPACE_SPLIT, self.workspace_weights)
        self.GroupEnd()
        self.GroupEnd()
        return True

    def InitValues(self):
        # Start polling first. A malformed old config must never disable worker
        # lifecycle/error updates for the whole dialog.
        self.state.mkdir(parents=True, exist_ok=True)
        self.SetTimer(UI_TIMER_MS)
        cfg = _read_json(self.config_path, {})
        prompt = cfg.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = DEFAULT_PROMPT

        interval_valid = True
        try:
            interval = int(cfg.get("interval_ms", DEFAULT_INTERVAL_MS))
        except (TypeError, ValueError):
            interval_valid = False
            interval = DEFAULT_INTERVAL_MS
        try:
            config_version = int(cfg.get("version", 1))
        except (TypeError, ValueError):
            config_version = 1
        if config_version < CONFIG_VERSION and interval == 500:
            interval = DEFAULT_INTERVAL_MS
        if not MIN_INTERVAL_MS <= interval <= MAX_INTERVAL_MS:
            interval_valid = False
            interval = DEFAULT_INTERVAL_MS

        self.SetString(ID_PROMPT, prompt)
        self.api_key = str(cfg.get("api_key") or "")
        self.reference_path = ""
        self.reference_revision = None
        self.reference_preview.clear()
        reference_path = str(cfg.get("reference_image_path") or "").strip()
        if reference_path:
            candidate = Path(reference_path).expanduser()
            if (candidate.suffix.lower() in REFERENCE_EXTENSIONS and
                    candidate.is_file() and
                    self.reference_preview.set_image(candidate)):
                self.reference_path = str(candidate.resolve())
                self.reference_revision = _file_revision(candidate)
        # interval=0/Auto=False is the exact corrupt state written when the old
        # layout aborted before these gadgets were created.
        self.SetBool(ID_AUTO, bool(cfg.get("auto", True)) if interval_valid else True)
        self.SetBool(ID_CLEAN_FEED, bool(cfg.get("clean_feed", True)))
        self.SetBool(
            ID_PROMPT_EXPANSION,
            bool(cfg.get("enable_prompt_expansion", False)),
        )
        self.SetBool(
            ID_PRESERVE_COMPOSITION,
            bool(cfg.get("preserve_composition", True)),
        )
        self.auto_resync = bool(cfg.get("auto_resync", True))
        try:
            view_mode = int(cfg.get("view_mode", VIEW_AI))
        except (TypeError, ValueError):
            view_mode = VIEW_AI
        if view_mode not in (VIEW_AI, VIEW_SOURCE, VIEW_COMPARE):
            view_mode = VIEW_AI
        self.SetInt32(ID_VIEW_MODE, view_mode)
        self.preview.set_view_mode(view_mode)
        try:
            follow_view = int(cfg.get("follow_view", FOLLOW_ACTIVE_VIEW))
        except (TypeError, ValueError):
            follow_view = FOLLOW_ACTIVE_VIEW
        if follow_view not in (FOLLOW_ACTIVE_VIEW, FOLLOW_RENDER_VIEW):
            follow_view = FOLLOW_ACTIVE_VIEW
        self.follow_view = follow_view
        self.SetInt32(ID_INTERVAL, interval, min=MIN_INTERVAL_MS,
                      max=MAX_INTERVAL_MS, step=25,
                      min2=MIN_INTERVAL_MS, max2=MAX_INTERVAL_MS)
        self.Enable(ID_INTERVAL, self.GetBool(ID_AUTO))
        self._update_render_time()
        if not VENDOR_READY.is_file():
            self._set_status("Open Settings… and click Install deps")
        elif not (self.api_key or os.environ.get("FAL_KEY", "").strip()):
            self._set_status("Open Settings… and enter an API key")
        return True

    def _save_settings(self):
        cfg = {
            "version": CONFIG_VERSION,
            "prompt": self.GetString(ID_PROMPT),
            "api_key": self.api_key.strip(),
            "reference_image_path": self.reference_path,
            "auto": self.GetBool(ID_AUTO),
            "clean_feed": self.GetBool(ID_CLEAN_FEED),
            "enable_prompt_expansion": self.GetBool(ID_PROMPT_EXPANSION),
            "preserve_composition": self.GetBool(ID_PRESERVE_COMPOSITION),
            "auto_resync": self.auto_resync,
            "follow_view": self.follow_view,
            "view_mode": self.GetInt32(ID_VIEW_MODE),
            "interval_ms": self.GetInt32(ID_INTERVAL),
        }
        try:
            _atomic_write_json(self.config_path, cfg)
        except OSError as exc:
            self._set_status("Could not save settings: %s" % exc)
            return None
        return cfg

    def _active_rect(self, document=None):
        if document is None:
            document = self.source_document
        if document is None:
            document = documents.GetActiveDocument()
        return _active_rect_for_aspect(_render_film_aspect(document))

    def _source_resolution(self, document=None):
        _x, _y, width, height = self._active_rect(document)
        return width, height

    def _render_elapsed(self, now=None):
        elapsed = max(0.0, float(self.render_timer_elapsed))
        if self.render_timer_active:
            current = time.monotonic() if now is None else float(now)
            elapsed += max(
                0.0, current - float(self.render_timer_started_at))
        return elapsed

    def _update_render_time(self, now=None):
        elapsed = self._render_elapsed(now)
        self.SetString(ID_RENDER_TIME, "Render time: %.1f s" % elapsed)
        return elapsed

    def _begin_render_timer(self, pressed_at):
        self.render_timer_started_at = float(pressed_at)
        self.render_timer_elapsed = 0.0
        self.render_timer_active = True
        self._update_render_time(pressed_at)

    def _freeze_render_timer(self, now=None, update_ui=True):
        if self.render_timer_active:
            current = time.monotonic() if now is None else float(now)
            self.render_timer_elapsed = self._render_elapsed(current)
            self.render_timer_started_at = 0.0
            self.render_timer_active = False
        if update_ui:
            self._update_render_time(now)
        return self.render_timer_elapsed

    def _resume_render_timer(self, now):
        if not self.render_timer_active:
            self.render_timer_started_at = float(now)
            self.render_timer_active = True
        self._update_render_time(now)
        return self.render_timer_elapsed

    def _maybe_auto_pause(self, now, document):
        if (not self.running or self.auto_paused or
                self.render_frames_active or
                self.sequence_waiting_for_start or
                not self.auto_pause_armed or
                self.last_activity_at <= 0.0 or
                now - self.last_activity_at < AUTO_PAUSE_SECONDS):
            return False
        if self.recording_active or self.recording_stopping:
            return False

        self._cancel_background_capture()
        self._freeze_render_timer(now)
        worker_stopped = self._terminate_worker_process()
        self._stop(
            update_ui=False, recording_grace=0.0,
            terminate_worker=False)
        self.SetString(ID_RENDER_FRAMES, "Render Frames")
        if not worker_stopped or self.proc is not None:
            self.SetString(ID_START, "▶ Start")
            self._set_status(
                "Auto-pause could not stop the render worker")
            return True
        self.auto_paused = True
        self.auto_paused_document = (
            document if _sdk_object_alive(document) else None)
        self.auto_paused_scene_signature = self._scene_signature(
            self.auto_paused_document)
        self.SetString(ID_START, "■ Stop · Paused")
        self._set_status(
            "Auto-paused after %g seconds without scene activity" %
            AUTO_PAUSE_SECONDS)
        return True

    def _quiesce_c4d_threads(self):
        """Join Cinema evaluation threads before cloning animation state."""
        if self.capture_thread is not None:
            return False
        stop_all = getattr(c4d, "StopAllThreads", None)
        if not callable(stop_all):
            return False
        try:
            stop_all()
        except (AttributeError, RuntimeError, TypeError):
            return False
        return True

    def _clear_live_capture_requests(self):
        """Drop queued live-view captures while an animation feed owns input."""
        self.capture_pending = False
        self.capture_pending_clean = False
        self.manual_capture_pending = False
        self.final_clean_pending = False
        self.resolution_capture_pending = False
        self.editor_move_active = False
        self.interactive_until = 0.0
        self.interactive_output_poll_at = 0.0
        self.interactive_pending_phase = None
        self.realtime_event_queued = False

    @staticmethod
    def _native_play_active(document):
        checker = getattr(c4d, "IsAnimationRunning", None)
        if not callable(checker) or document is None:
            return False
        try:
            return bool(checker(document))
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            return False

    @staticmethod
    def _conflicting_c4d_task_running():
        """Avoid cancelling an unrelated Picture Viewer or editor render."""
        checker = getattr(c4d, "CheckIsRunning", None)
        if not callable(checker):
            return False
        for name in (
                "CHECKISRUNNING_EDITORRENDERING",
                "CHECKISRUNNING_EXTERNALRENDERING",
                "CHECKISRUNNING_INTERACTIVERENDERING",
                "CHECKISRUNNING_BAKING",
                "CHECKISRUNNING_PAINTERUPDATING"):
            flag = getattr(c4d, name, None)
            if flag is None:
                continue
            try:
                if checker(flag):
                    return True
            except (AttributeError, RuntimeError, TypeError):
                continue
        return False

    def _clear_auto_animation(self):
        self.auto_animation_phase = ""
        self.auto_animation_destination = ""
        self.auto_animation_replay_revision = 0
        self.auto_animation_replay_deadline = 0.0
        self.auto_animation_tail_deadline = 0.0
        self.auto_animation_tail_timeout = 0.0
        self.auto_animation_output_start_sequence = 0
        self.source_sequence_start_at = 0.0
        self.recording_capture_after = 0.0

    def _select_recording_destination(
            self, title="Save Recording",
            default_name="Render_Recording_%Y%m%d_%H%M%S.mp4"):
        try:
            selected = storage.SaveDialog(
                type=getattr(
                    c4d, "FILESELECTTYPE_ANYTHING",
                    c4d.FILESELECTTYPE_IMAGES),
                title=title,
                force_suffix="mp4",
                def_file=time.strftime(default_name),
            )
            return Path(selected) if selected else None
        except Exception as exc:
            gui.MessageDialog(
                "Could not open the recording save dialog: %s" % exc)
            return None

    def _begin_render_frames(self):
        """Queue a private Loop Range render without touching native Play."""
        if self.render_frames_active:
            return self._request_render_frames_cancel()
        if self.auto_animation_phase == "replay":
            if (self.recording_active and
                    self.recording_start_acknowledged and
                    self._request_recording_finish()):
                self.auto_animation_phase = "saving"
                self.SetString(ID_RENDER_FRAMES, "Saving MP4...")
                self.Enable(ID_RENDER_FRAMES, False)
                return True
            return False
        if self.auto_animation_phase in (
                "recording_start", "tail", "saving"):
            return False
        if self.sequence_source_active:
            return self._stop_sequence_source("Animation feed stopped")
        if not self.running or self.auto_paused:
            gui.MessageDialog("Press Start before rendering frames.")
            return False
        if self.recording_active or self.recording_stopping:
            gui.MessageDialog(
                "Stop REC and wait for the MP4 before rendering frames.")
            return False
        document = documents.GetActiveDocument()
        if not _sdk_object_alive(document):
            gui.MessageDialog("No active Cinema 4D document is available.")
            return False
        if self._native_play_active(document):
            gui.MessageDialog(
                "Stop Cinema 4D playback before using Render Frames. "
                "The plugin will not stop or control the Play button.")
            return False
        try:
            fps = int(document.GetFps())
            first = int(document.GetLoopMinTime().GetFrame(fps))
            last = int(document.GetLoopMaxTime().GetFrame(fps))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            gui.MessageDialog(
                "Render Frames requires a valid Loop Range and project FPS.")
            return False
        if fps <= 0 or last < first:
            gui.MessageDialog(
                "Render Frames requires a valid Loop Range and project FPS.")
            return False

        destination = self._select_recording_destination(
            title="Save Rendered Animation",
            default_name="Render_Animation_%Y%m%d_%H%M%S.mp4")
        if destination is None:
            self.SetString(ID_RENDER_FRAMES, "Render Frames")
            self.Enable(ID_RENDER_FRAMES, True)
            return False

        self._clear_auto_animation()
        self.auto_animation_phase = "capture"
        self.auto_animation_destination = str(destination)
        self.render_frames_active = True
        self.render_frames_cancel_requested = False
        self.render_frames_phase = "drain_capture"
        self.render_frames_source_document = document
        self.render_frames_fps = fps
        self.render_frames_start_frame = first
        self.render_frames_frame = first
        self.render_frames_end_frame = last
        self.render_frames_manifest_frames = []
        self.render_frames_directory = None
        self.render_frames_abort_message = ""
        self.last_activity_at = time.monotonic()
        self._clear_live_capture_requests()
        self._cancel_background_capture()
        self.SetString(ID_RENDER_FRAMES, "Cancel Frames")
        self._set_status(
            "Preparing Loop Range %d–%d at %d FPS…" %
            (first, last, fps))
        return True

    def _request_render_frames_cancel(self):
        if not self.render_frames_active:
            return False
        self.render_frames_cancel_requested = True
        self._cancel_background_capture()
        self._set_status("Cancelling Render Frames…")
        return True

    def _drain_render_frames_capture(self):
        """Join a completed/cancelled frame without publishing it as live."""
        thread = self.capture_thread
        if thread is None:
            return True
        try:
            running = bool(thread.IsRunning())
        except Exception:
            return False
        if running:
            thread.cancelled = True
            try:
                thread.End(False)
            except Exception:
                pass
            return False
        return self._join_capture_thread(thread)

    def _join_capture_thread(self, thread):
        """Release an SDK thread only after it has confirmed completion.

        A Python C4DThread can set its result and still need the GIL to return
        from Main(). Calling End(True) from a Python UI callback in that window
        can deadlock the host. Running threads therefore receive only the
        non-blocking break request; Timer/CoreMessage retries the final release.
        """
        if thread is None:
            return True
        checker = getattr(thread, "IsRunning", None)
        if not callable(checker):
            return False
        try:
            running = bool(checker())
        except Exception:
            return False
        if running:
            try:
                thread.cancelled = True
            except Exception:
                pass
            try:
                thread.End(False)
            except Exception:
                pass
            return False
        try:
            thread.End(True)
        except Exception:
            return False
        try:
            self._restore_capture_view_state(thread)
        except Exception:
            pass
        if self.capture_thread is thread:
            self.capture_thread = None
            _discard_deferred_capture_owner(self)
        return True

    def _prepare_render_frames_clone(self):
        document = self.render_frames_source_document
        if not _sdk_object_alive(document):
            return self._abort_render_frames(
                "Render Frames stopped: source document was closed")
        if self._native_play_active(document):
            return self._abort_render_frames(
                "Render Frames stopped because Cinema 4D playback started. "
                "Stop Play and try again.")
        if self._conflicting_c4d_task_running():
            return self._abort_render_frames(
                "Render Frames stopped because another Cinema 4D render is "
                "running. Finish it and try again.")
        if not self._quiesce_c4d_threads():
            return self._abort_render_frames(
                "Render Frames stopped: Cinema threads did not stop")
        try:
            active_rect = self._active_rect(document)
            _x, _y, width, height = active_rect
            snapshot, render_data = self._make_clean_snapshot(
                document, width, height,
                clean_feed=self.GetBool(ID_CLEAN_FEED),
                evaluate_live=False)
        except Exception as exc:
            return self._abort_render_frames(
                "Render Frames stopped while cloning: %s" % exc)
        if snapshot is None or render_data is None:
            return self._abort_render_frames(
                "Render Frames stopped: safe document clone failed")

        directory = self.state / (
            "render_frames_%d_%d" % (os.getpid(), time.time_ns()))
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            return self._abort_render_frames(
                "Render Frames could not create its frame folder: %s" % exc)

        self.render_frames_document = snapshot
        self.render_frames_render_data = render_data
        self.render_frames_active_rect = active_rect
        self.render_frames_directory = directory
        self.render_frames_phase = "capture"
        return True

    def _start_render_frames_capture(self, now):
        document = self.render_frames_document
        render_data = self.render_frames_render_data
        if document is None or render_data is None:
            return self._abort_render_frames(
                "Render Frames stopped: private document was lost")
        frame = int(self.render_frames_frame)
        fps = int(self.render_frames_fps)
        try:
            document.SetTime(c4d.BaseTime(frame, fps))
            evaluated = document.ExecutePasses(
                None, True, True, True,
                getattr(
                    c4d, "BUILDFLAGS_EXTERNALRENDERER",
                    getattr(c4d, "BUILDFLAGS_NONE", 0)))
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError) as exc:
            return self._abort_render_frames(
                "Render Frames could not evaluate frame %d: %s" %
                (frame, exc))
        if evaluated is False:
            return self._abort_render_frames(
                "Render Frames could not evaluate frame %d" % frame)

        _x, _y, width, height = self.render_frames_active_rect
        bitmap = bitmaps.BaseBitmap()
        if bitmap.Init(width, height, 24) != c4d.IMAGERESULT_OK:
            return self._abort_render_frames(
                "Render Frames could not create frame %d" % frame)
        index = len(self.render_frames_manifest_frames)
        target = self.render_frames_directory / (
            "frame_%06d.jpg" % index)
        thread = SourceCaptureThread(
            document, render_data, bitmap, target,
            "Loop Range %d/%d" % (
                index + 1,
                self.render_frames_end_frame -
                self.render_frames_start_frame + 1),
            clean_feed=self.GetBool(ID_CLEAN_FEED),
            isolated=True)
        thread.started_at = float(now)
        thread.render_frames_capture = True
        self.capture_thread = thread
        self.render_frames_phase = "wait_capture"
        if not thread.Start():
            self.capture_thread = None
            return self._abort_render_frames(
                "Render Frames could not start frame %d" % frame)
        self.SetString(
            ID_RENDER_FRAMES,
            "Cancel %d/%d" %
            (index + 1,
             self.render_frames_end_frame -
             self.render_frames_start_frame + 1))
        self._set_status(
            "Rendering viewport frames: %d/%d" %
            (index + 1,
             self.render_frames_end_frame -
             self.render_frames_start_frame + 1))
        return True

    @staticmethod
    def _save_render_frames_bitmap(thread):
        target = Path(thread.target)
        tmp = target.with_name(
            ".%s.%d.%d.tmp.jpg" %
            (target.stem, os.getpid(), time.time_ns()))
        try:
            settings = c4d.BaseContainer()
            settings[getattr(c4d, "JPGSAVER_QUALITY", 100)] = 100
            result = thread.bitmap.Save(
                str(tmp), c4d.FILTER_JPG, settings)
            if result != c4d.IMAGERESULT_OK:
                return False, "Viewport JPEG error: %s" % result
            _replace_with_retry(tmp, target)
            return True, ""
        except Exception as exc:
            return False, "Viewport frame publish exception: %s" % exc
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _finish_render_frames_capture(self, now):
        thread = self.capture_thread
        if thread is None:
            return False
        try:
            if thread.IsRunning():
                return False
        except Exception:
            return self._abort_render_frames(
                "Render Frames stopped: viewport thread state was lost")
        if not self._join_capture_thread(thread):
            self.render_frames_cancel_requested = True
            self.render_frames_abort_message = (
                "Render Frames stopped: the viewport render did not shut "
                "down cleanly")
            self.render_frames_phase = "abort_drain"
            self._set_status("Waiting for the viewport render to stop…")
            return False
        if self.render_frames_cancel_requested:
            return self._abort_render_frames("Render Frames cancelled")
        if not thread.success:
            return self._abort_render_frames(
                thread.error or "Viewport frame render failed")
        saved, error = self._save_render_frames_bitmap(thread)
        if not saved:
            return self._abort_render_frames(error)

        self.render_frames_manifest_frames.append(thread.target.name)
        if self.render_frames_frame >= self.render_frames_end_frame:
            return self._publish_render_frames_sequence(now)
        self.render_frames_frame += 1
        self.render_frames_phase = "capture"
        return True

    def _publish_render_frames_sequence(self, now):
        directory = self.render_frames_directory
        if directory is None or not self.render_frames_manifest_frames:
            return self._abort_render_frames(
                "Render Frames produced no viewport frames")
        manifest_path = directory / "manifest.json"
        payload = {
            "id": directory.name,
            "fps": self.render_frames_fps,
            "active_rect": _normalized_active_rect(
                self.render_frames_active_rect),
            "frames": list(self.render_frames_manifest_frames),
        }
        try:
            _atomic_write_json(manifest_path, payload)
        except OSError as exc:
            return self._abort_render_frames(
                "Render Frames could not publish its video feed: %s" % exc)

        automatic = bool(
            self.auto_animation_phase == "capture" and
            self.auto_animation_destination)
        self.source_sequence_manifest = str(manifest_path)
        self.published_active_rect = self.render_frames_active_rect
        self.last_activity_at = float(now)
        self.auto_pause_armed = True
        first_frame = directory / self.render_frames_manifest_frames[0]

        if automatic:
            destination = Path(self.auto_animation_destination)
            self.auto_animation_phase = "recording_start"
            self.auto_animation_replay_deadline = 0.0
            self.sequence_source_active = False
            self.sequence_playback_ended = False
            self.sequence_waiting_for_start = False
            self.sequence_started_at = 0.0
            self.last_sequence_telemetry_signature = None
            self.SetString(ID_RENDER_FRAMES, "Starting animation...")
            self.Enable(ID_RENDER_FRAMES, False)
            if not self._start_recording(
                    destination, capture_enabled=False,
                    replay_on_ack=True):
                self.source_sequence_manifest = ""
                return self._abort_render_frames(
                    "Render Frames could not start the MP4 recorder")

            self.render_frames_active = False
            self.render_frames_cancel_requested = False
            self.render_frames_abort_message = ""
            self.render_frames_phase = ""
            self.render_frames_source_document = None
            self.render_frames_document = None
            self.render_frames_render_data = None
            if self.preview.view_mode != VIEW_AI:
                self.preview.set_source_image(first_frame)
            self._set_status(
                "Viewport frames ready · waiting for the MP4 recorder...")
            return True

        previous_revision = self.source_sequence_revision
        self.source_sequence_revision += 1
        self.source_sequence_start_at = 0.0
        self.sequence_source_active = True
        self.sequence_playback_ended = False
        self.sequence_waiting_for_start = True
        self.sequence_started_at = float(now)
        self.sequence_replay_on_recording_ack = False
        self.last_sequence_telemetry_signature = None
        if not self._write_control():
            self.sequence_source_active = False
            self.sequence_playback_ended = False
            self.sequence_waiting_for_start = False
            self.sequence_started_at = 0.0
            self.source_sequence_manifest = ""
            self.source_sequence_revision = previous_revision
            return self._abort_render_frames(
                "Render Frames could not start its animation feed")

        self.render_frames_active = False
        self.render_frames_cancel_requested = False
        self.render_frames_abort_message = ""
        self.render_frames_phase = ""
        self.render_frames_source_document = None
        self.render_frames_document = None
        self.render_frames_render_data = None
        if self.preview.view_mode != VIEW_AI:
            self.preview.set_source_image(first_frame)
        self.SetString(ID_RENDER_FRAMES, "■ Stop Frames")
        self._set_status(
            "Starting %d viewport frames at %d FPS…" %
            (len(self.render_frames_manifest_frames),
             self.render_frames_fps))
        return True

    def _finalize_render_frames_abort(self, message):
        directory = self.render_frames_directory
        self.render_frames_active = False
        self.render_frames_cancel_requested = False
        self.render_frames_abort_message = ""
        self.render_frames_phase = ""
        self.render_frames_source_document = None
        self.render_frames_document = None
        self.render_frames_render_data = None
        self.render_frames_directory = None
        self.render_frames_manifest_frames = []
        if (self.auto_animation_phase in ("capture", "recording_start") and
                not self.recording_active and
                not self.recording_stopping):
            self._clear_auto_animation()
        self.Enable(ID_RENDER_FRAMES, True)
        self.SetString(
            ID_RENDER_FRAMES,
            "■ Stop Frames" if self.sequence_source_active
            else "Render Frames")
        if directory is not None and not self.sequence_source_active:
            self._delete_render_frames_directory(directory)
        self.final_clean_pending = bool(
            self.running and self.GetBool(ID_AUTO) and
            not self.sequence_source_active)
        self.next_capture_at = 0.0
        self._set_status(message)
        return False

    def _abort_render_frames(self, message):
        thread = self.capture_thread
        if (thread is not None and
                getattr(thread, "render_frames_capture", False)):
            try:
                thread.cancelled = True
            except Exception:
                pass
            if not self._join_capture_thread(thread):
                try:
                    thread.End(False)
                except Exception:
                    pass
                self.render_frames_active = True
                self.render_frames_cancel_requested = True
                self.render_frames_abort_message = str(message)
                self.render_frames_phase = "abort_drain"
                self._set_status("Waiting for the viewport render to stop…")
                return False
        return self._finalize_render_frames_abort(message)

    def _delete_render_frames_directory(self, directory):
        """Delete only a generated take contained by this session folder."""
        try:
            candidate = Path(directory).resolve()
            state_root = self.state.resolve()
            candidate.relative_to(state_root)
            if not candidate.name.startswith("render_frames_"):
                return False
            if candidate.is_dir():
                shutil.rmtree(str(candidate))
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def _retire_sequence_directory(self, manifest_path, now=None):
        try:
            directory = Path(manifest_path).parent
        except (TypeError, ValueError):
            return False
        retired_at = time.monotonic() if now is None else float(now)
        if not any(path == directory
                   for path, _stamp in self.retired_sequence_directories):
            self.retired_sequence_directories.append((directory, retired_at))
        return True

    def _cleanup_retired_sequence_directories(self, now=None, force=False):
        current = (
            0.0 if force and now is None
            else time.monotonic() if now is None
            else float(now))
        remaining = []
        for directory, retired_at in self.retired_sequence_directories:
            if (not force and
                    current - retired_at < RETIRED_SEQUENCE_GRACE_SECONDS):
                remaining.append((directory, retired_at))
                continue
            if not self._delete_render_frames_directory(directory):
                remaining.append((directory, retired_at))
        self.retired_sequence_directories = remaining
        return not remaining

    def _stop_sequence_source(self, message=None):
        if not self.sequence_source_active:
            return False
        previous_playback_ended = self.sequence_playback_ended
        previous_waiting = self.sequence_waiting_for_start
        previous_started_at = self.sequence_started_at
        previous_manifest = self.source_sequence_manifest
        previous_replay = self.sequence_replay_on_recording_ack
        previous_start_at = self.source_sequence_start_at
        self.sequence_source_active = False
        self.sequence_playback_ended = False
        self.sequence_waiting_for_start = False
        self.sequence_started_at = 0.0
        self.sequence_replay_on_recording_ack = False
        self.source_sequence_manifest = ""
        self.source_sequence_start_at = 0.0
        self.last_sequence_telemetry_signature = None
        if self.running and not self._write_control():
            self.sequence_source_active = True
            self.sequence_playback_ended = previous_playback_ended
            self.sequence_waiting_for_start = previous_waiting
            self.sequence_started_at = previous_started_at
            self.sequence_replay_on_recording_ack = previous_replay
            self.source_sequence_manifest = previous_manifest
            self.source_sequence_start_at = previous_start_at
            self.SetString(ID_RENDER_FRAMES, "■ Stop Frames")
            self._set_status(
                "Could not stop the animation feed; please try again")
            return False

        self.SetString(ID_RENDER_FRAMES, "Render Frames")
        if previous_manifest:
            self._retire_sequence_directory(previous_manifest)
        self.render_frames_directory = None
        self.render_frames_manifest_frames = []
        self.final_clean_pending = bool(
            self.running and self.GetBool(ID_AUTO))
        self.next_capture_at = 0.0
        if self.preview.view_mode != VIEW_AI and self.input_path.is_file():
            self.preview.set_source_image(self.input_path)
        if message:
            self._set_status(message)
        return True

    def _fail_auto_animation(self, message):
        message = str(message)
        if self.recording_active or self.recording_stopping:
            self._freeze_render_timer()
            self._stop(recording_grace=5.0)
        else:
            if self.sequence_source_active:
                self._stop_sequence_source()
            self._clear_auto_animation()
            self.Enable(ID_RENDER_FRAMES, True)
            if not self.sequence_source_active:
                self.SetString(ID_RENDER_FRAMES, "Render Frames")
        self._set_status(message)
        gui.MessageDialog(message)
        return True

    def _poll_sequence_telemetry(self, now):
        if not self.sequence_source_active:
            return False
        telemetry = _read_json(self.telemetry_path, {})
        try:
            revision = int(
                telemetry.get("source_sequence_revision", -1))
            index = int(telemetry.get("source_sequence_index", 0))
            total = int(telemetry.get("source_sequence_total", 0))
        except (TypeError, ValueError):
            return False
        if revision != self.source_sequence_revision:
            return False
        state = str(
            telemetry.get("source_sequence_state") or "idle")
        signature = (
            revision, state, index, total, telemetry.get("updated"))
        changed = signature != self.last_sequence_telemetry_signature
        self.last_sequence_telemetry_signature = signature
        if state == "playing":
            self.sequence_playback_ended = False
            self.sequence_waiting_for_start = False
            self.sequence_started_at = 0.0
            if (self.auto_animation_phase in ("replay", "tail") and
                    revision == self.auto_animation_replay_revision):
                self.auto_animation_phase = "replay"
                self.auto_animation_tail_deadline = 0.0
                self.SetString(ID_RENDER_FRAMES, "Stop & Save")
                self.Enable(ID_RENDER_FRAMES, True)
            if changed:
                self.last_activity_at = float(now)
            if changed and total > 0:
                self._set_status(
                    "Playing viewport frames: %d/%d" %
                    (min(total, index + 1), total))
            return True
        if state == "ended":
            self.sequence_waiting_for_start = False
            self.sequence_started_at = 0.0
            automatic_replay_ended = bool(
                self.auto_animation_phase == "replay" and
                revision == self.auto_animation_replay_revision)
            if automatic_replay_ended:
                self.auto_animation_phase = "tail"
                self.auto_animation_tail_deadline = (
                    float(now) + RENDER_FRAMES_RECORD_TAIL_SECONDS)
                self.auto_animation_tail_timeout = (
                    float(now) + RENDER_FRAMES_OUTPUT_TIMEOUT_SECONDS)
                self.SetString(
                    ID_RENDER_FRAMES, "Finishing AI frames...")
                self.Enable(ID_RENDER_FRAMES, False)
            if not self.sequence_playback_ended:
                self.sequence_playback_ended = True
                self.last_activity_at = float(now)
                if automatic_replay_ended:
                    self._set_status(
                        "Viewport frames complete · finishing AI output...")
                else:
                    self._set_status(
                        "Viewport animation complete · %d frames" % total)
            return True
        if state == "error":
            if (self.auto_animation_phase == "recording_start" and
                    self.auto_animation_replay_revision <= 0):
                self._set_status(
                    "Recorder is starting · animation will replay from "
                    "frame 1")
                return False
            if (self.auto_animation_phase in ("replay", "tail") and
                    revision == self.auto_animation_replay_revision):
                self._fail_auto_animation(
                    "Rendered animation feed failed; recording stopped")
                return False
            self._stop_sequence_source(
                "Animation feed failed; live viewport restored")
            return False
        return changed

    def _tick_auto_animation(self, now):
        if (self.auto_animation_phase != "tail" or
                self.auto_animation_tail_deadline <= 0.0 or
                float(now) < self.auto_animation_tail_deadline):
            return False
        if (self.ai_output_sequence <=
                self.auto_animation_output_start_sequence):
            if (self.auto_animation_tail_timeout <= 0.0 or
                    float(now) < self.auto_animation_tail_timeout):
                self._set_status(
                    "Viewport frames complete · waiting for AI output...")
                return False
            return self._fail_auto_animation(
                "Rendered animation received no AI frames; "
                "recording stopped")
        if self.recording_stopping:
            self.auto_animation_phase = "saving"
            return True
        if (not self.recording_active or
                not self._request_recording_finish()):
            return False
        self.auto_animation_phase = "saving"
        self.SetString(ID_RENDER_FRAMES, "Saving MP4...")
        self.Enable(ID_RENDER_FRAMES, False)
        self._set_status("Saving rendered animation...")
        return True

    def _check_sequence_start_timeout(self, now):
        if (not self.sequence_source_active or
                not self.sequence_waiting_for_start or
                self.sequence_started_at <= 0.0 or
                float(now) - self.sequence_started_at <
                SEQUENCE_START_TIMEOUT_SECONDS):
            return False
        if (self.auto_animation_phase == "recording_start" or
                (self.auto_animation_phase == "replay" and
                 self.source_sequence_revision ==
                 self.auto_animation_replay_revision)):
            return self._fail_auto_animation(
                "Rendered animation did not start in time; "
                "recording stopped")
        return self._stop_sequence_source(
            "Animation feed did not start in time; live viewport restored")

    def _tick_render_frames(self, now):
        if not self.render_frames_active:
            return False
        self.last_activity_at = float(now)
        if self.render_frames_cancel_requested:
            if not self._drain_render_frames_capture():
                return True
            message = (
                self.render_frames_abort_message or
                "Render Frames cancelled")
            return self._finalize_render_frames_abort(message)
        if self.render_frames_phase == "drain_capture":
            if not self._drain_render_frames_capture():
                return True
            return self._prepare_render_frames_clone()
        if self.render_frames_phase == "capture":
            return self._start_render_frames_capture(now)
        if self.render_frames_phase == "wait_capture":
            if self.capture_thread is None:
                return self._abort_render_frames(
                    "Render Frames stopped: viewport thread was lost")
            return self._finish_render_frames_capture(now)
        return self._abort_render_frames(
            "Render Frames stopped: invalid internal state")

    def _resume_auto_paused(self, now, refresh_source=True):
        if not self.auto_paused:
            return False
        # A MOVE message can be waiting while Timer resumes the worker, or
        # CoreMessage can itself be the resume path. _start() resets the
        # realtime bridge, so preserve that tiny piece of Python state across
        # the reconnect and restore it before returning to Cinema's event loop.
        pending_phase = self.interactive_pending_phase
        pending_event = self.realtime_event_queued
        pump_was_busy = self.realtime_pump_busy
        document = documents.GetActiveDocument()
        if not _sdk_object_alive(document):
            return False
        force_source_refresh = bool(self.auto_resume_source_refresh)
        self.auto_resume_pending = False
        self.auto_resume_source_refresh = False
        if not self._start(
                reuse_source=True, preserve_preview=True,
                allow_stale_source=True):
            self.interactive_pending_phase = pending_phase
            self.realtime_event_queued = pending_event
            self.realtime_pump_busy = pump_was_busy
            self.auto_paused = True
            self.auto_paused_document = document
            self.auto_paused_scene_signature = self._scene_signature(
                document)
            self.auto_resume_pending = False
            self.auto_resume_source_refresh = force_source_refresh
            self.SetString(ID_START, "■ Stop · Paused")
            return False
        self.last_activity_at = now
        self._resume_render_timer(now)
        if force_source_refresh:
            self.resolution_capture_pending = True
            self.last_scene_signature = None
            self.next_capture_at = 0.0
        elif refresh_source and self.GetBool(ID_AUTO):
            self.capture_pending = True
            self.final_clean_pending = True
            self.last_scene_signature = None
            self.next_capture_at = 0.0
        if pending_phase is not None:
            self.source_activity_generation += 1
            self._latch_interactive_phase(pending_phase, now)
            self.interactive_pending_phase = pending_phase
        self.realtime_event_queued = pending_event
        self.realtime_pump_busy = pump_was_busy
        return True

    def _maybe_auto_resume(self, now):
        if not self.auto_paused:
            return False
        document = documents.GetActiveDocument()
        source_changed = bool(self.auto_resume_pending)
        if not source_changed and self.GetBool(ID_AUTO):
            if not _sdk_object_alive(document):
                return False
            source_changed = bool(
                not _same_sdk_object(
                    document, self.auto_paused_document) or
                self._scene_signature(document) !=
                self.auto_paused_scene_signature)
        if not source_changed:
            return False
        return self._resume_auto_paused(now, refresh_source=True)

    def _load_reference(self):
        previous = Path(self.reference_path).parent if self.reference_path else ""
        try:
            selected = storage.LoadDialog(
                type=getattr(c4d, "FILESELECTTYPE_IMAGES", 0),
                title="Choose reference image",
                flags=getattr(c4d, "FILESELECT_LOAD", 0),
                def_path=str(previous),
            )
        except (AttributeError, OSError, RuntimeError, TypeError):
            selected = ""
        if not selected:
            return False

        path = Path(selected).expanduser()
        try:
            path = path.resolve()
        except OSError:
            pass
        if path.suffix.lower() not in REFERENCE_EXTENSIONS:
            gui.MessageDialog(
                "Reference must be a JPEG, PNG, or WebP image.")
            return False
        revision = _file_revision(path)
        if revision is None:
            gui.MessageDialog("Reference file is no longer available.")
            return False
        if revision[2] > REFERENCE_MAX_BYTES:
            gui.MessageDialog("Reference image must be 16 MB or smaller.")
            return False
        if not self.reference_preview.set_image(path):
            gui.MessageDialog(
                self.reference_preview.error
                or "Could not load this reference image.")
            return False

        changed = self.reference_revision != revision
        self.reference_path = str(path)
        self.reference_revision = revision
        self._save_settings()
        self._write_control()
        if self.running and changed:
            self._set_status("Uploading reference…")
        else:
            self._set_status("Reference loaded: %s" % path.name)
        return True

    def _clear_reference(self):
        changed = bool(self.reference_path or self.reference_preview.bitmap)
        self.reference_path = ""
        self.reference_revision = None
        self.reference_preview.clear()
        self._save_settings()
        self._write_control()
        if self.running and changed:
            self._set_status("Clearing reference…")
        else:
            self._set_status("Reference cleared")
        return changed

    def _set_status(self, value):
        if value == self.last_status:
            return
        self.last_status = value

    def _open_settings(self):
        if self.running:
            gui.MessageDialog("Click Stop first.")
            return False
        self.settings_dialog = LucySettingsDialog(self)
        try:
            return self.settings_dialog.Open(
                c4d.DLG_TYPE_MODAL, pluginid=0, xpos=-2, ypos=-2,
                defaultw=560, defaulth=180)
        finally:
            self.settings_dialog = None

    def _configure_preview_render_data(self, rd, width, height,
                                       clean_feed=False, source_view=None):
        """Configure a detached RenderData for a safe viewport preview."""
        set_resolution = getattr(rd, "SetResolution", None)
        if callable(set_resolution):
            try:
                set_resolution(float(width), float(height), 1.0)
            except (TypeError, RuntimeError):
                set_resolution = None
        if not callable(set_resolution):
            rd[c4d.RDATA_XRES] = width
            rd[c4d.RDATA_YRES] = height
            pixel_aspect = getattr(c4d, "RDATA_PIXELASPECT", None)
            film_aspect = getattr(c4d, "RDATA_FILMASPECT", None)
            if pixel_aspect is not None:
                rd[pixel_aspect] = 1.0
            if film_aspect is not None:
                rd[film_aspect] = float(width) / float(height)

        save_image = getattr(c4d, "RDATA_SAVEIMAGE", None)
        if save_image is not None:
            rd[save_image] = False
        if clean_feed:
            show_hud = getattr(c4d, "RDATA_SHOWHUD", None)
            if show_hud is not None:
                rd[show_hud] = False

        preview_engine = getattr(
            c4d, "RDATA_RENDERENGINE_PREVIEWHARDWARE", None)
        if preview_engine is None:
            return rd
        rd[c4d.RDATA_RENDERENGINE] = preview_engine

        preview_post = None
        get_first = getattr(rd, "GetFirstVideoPost", None)
        post = get_first() if callable(get_first) else None
        while post is not None:
            get_type = getattr(post, "GetType", None)
            if callable(get_type) and get_type() == preview_engine:
                preview_post = post
                break
            get_next = getattr(post, "GetNext", None)
            post = get_next() if callable(get_next) else None

        if preview_post is None:
            constructor = getattr(documents, "BaseVideoPost", None)
            if not callable(constructor):
                constructor = getattr(c4d, "BaseList2D", None)
            insert = getattr(rd, "InsertVideoPostLast", None)
            if callable(constructor) and callable(insert):
                try:
                    preview_post = constructor(preview_engine)
                    insert(preview_post)
                except (MemoryError, TypeError, RuntimeError):
                    preview_post = None

        preview_changed = False
        if preview_post is not None:
            preview_changed = _copy_viewport_effects(
                source_view, preview_post)
            for name, value in AI_VIDEOPOST_PARAMETERS:
                parameter_id = getattr(c4d, name, None)
                if parameter_id is not None:
                    try:
                        preview_post[parameter_id] = value
                        preview_changed = True
                    except (KeyError, TypeError, RuntimeError):
                        pass
        if clean_feed and preview_post is not None:
            for name, value in CLEAN_FEED_VIDEOPOST_PARAMETERS:
                parameter_id = getattr(c4d, name, None)
                if parameter_id is not None:
                    try:
                        preview_post[parameter_id] = value
                        preview_changed = True
                    except (KeyError, TypeError, RuntimeError):
                        pass
        if preview_changed and preview_post is not None:
            change_message = getattr(c4d, "MSG_CHANGE", None)
            message = getattr(preview_post, "Message", None)
            if change_message is not None and callable(message):
                try:
                    message(change_message)
                except (AttributeError, ReferenceError, RuntimeError, TypeError):
                    pass
        return rd

    @staticmethod
    def _basedraw_index(document, target):
        """Return the editor index for a BaseDraw, if the SDK exposes it."""
        if target is None:
            return None
        get_count = getattr(document, "GetBaseDrawCount", None)
        get_view = getattr(document, "GetBaseDraw", None)
        if not callable(get_view):
            return None
        try:
            count = int(get_count()) if callable(get_count) else 4
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            count = 4
        for index in range(max(0, count)):
            try:
                candidate = get_view(index)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                continue
            if _same_sdk_object(candidate, target):
                return index
        return None

    @staticmethod
    def _render_view_data(document):
        getter = getattr(document, "GetDataInstance", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except (AttributeError, ReferenceError, RuntimeError):
            return None

    @staticmethod
    def _read_render_view_index(data):
        if data is None:
            return None
        try:
            getter = getattr(data, "GetInt32", None)
            return int(getter(DOCUMENT_RENDER_VIEW_ID) if callable(getter)
                       else data[DOCUMENT_RENDER_VIEW_ID])
        except (AttributeError, KeyError, ReferenceError, RuntimeError,
                TypeError, ValueError):
            return None

    def _sync_followed_view(self, document):
        """Temporarily make Active View the Hardware Preview render source."""
        if (not _sdk_object_alive(document) or
                self.follow_view != FOLLOW_ACTIVE_VIEW):
            return False
        active = _exact_basedraw(document, "GetActiveBaseDraw")
        render = _exact_basedraw(document, "GetRenderBaseDraw")
        same_view = _same_sdk_object(active, render)
        if active is None or same_view:
            return active is not None
        index = self._basedraw_index(document, active)
        data = self._render_view_data(document)
        if index is None or data is None:
            return False

        entry = next((item for item in self.followed_render_views
                      if _same_sdk_object(item[0], document)), None)
        if entry is None:
            original = self._read_render_view_index(data)
            if original is None:
                return False
            self.followed_render_views.append((document, original))
        try:
            data[DOCUMENT_RENDER_VIEW_ID] = int(index)
            return True
        except (AttributeError, KeyError, ReferenceError, RuntimeError,
                TypeError, ValueError):
            return False

    def _restore_followed_views(self, target_document=None):
        """Restore saved Render View selections, optionally for one document."""
        entries, remaining = self.followed_render_views, []
        for stored_document, original in entries:
            if (target_document is not None and
                    not _same_sdk_object(stored_document, target_document)):
                remaining.append((stored_document, original))
                continue
            if not _sdk_object_alive(stored_document):
                continue
            data = self._render_view_data(stored_document)
            if data is None:
                continue
            try:
                data[DOCUMENT_RENDER_VIEW_ID] = int(original)
            except (AttributeError, KeyError, ReferenceError, RuntimeError,
                    TypeError, ValueError):
                pass
        self.followed_render_views = remaining

    def _note_view_context(self, document, arm_change=True):
        """Track actual viewport/camera switches without treating motion as cuts."""
        basedraw = _source_basedraw(document, self.follow_view)
        index = self._basedraw_index(document, basedraw)
        projection = _basedraw_projection_value(basedraw)
        # GetEditorCamera can return a new temporary wrapper while its transform
        # is being edited. That is motion, not a camera cut. View index changes
        # still identify editor-view switches; only a real scene camera is used
        # for camera identity.
        camera = (_basedraw_scene_camera(document, basedraw)
                  if basedraw is not None else None)
        previous_projection = getattr(
            self, "last_source_projection", projection)
        changed = bool(
            self.view_context_initialized and (
                index != self.last_source_view_index or
                projection != previous_projection or
                not _same_sdk_object(camera, self.last_source_camera)
            )
        )
        self.view_context_initialized = True
        self.last_source_view_index = index
        self.last_source_camera = camera
        self.last_source_projection = projection
        if changed and arm_change:
            self.resync_cut_armed = True
        return changed

    def _prepare_source_document(self, document):
        """Switch capture ownership safely when the active document changes."""
        if not _sdk_object_alive(document):
            document = None
        previous = self.source_document
        if _same_sdk_object(previous, document):
            return False
        replacing_existing_source = bool(
            previous is not None or self.resync_cut_armed)

        # A render thread may still own the previous BaseDocument. Reject its
        # unpublished bitmap immediately, but never wait for a running Python
        # C4DThread here. The next CoreMessage/Timer pass finishes the release
        # and retries the document switch.
        if self.capture_thread is not None:
            self._cancel_background_capture()
            self._finish_background_capture()
            if self.capture_thread is not None:
                _retain_deferred_capture_owner(self)
                return False
        if previous is not None:
            self._restore_followed_views(previous)
            self.resync_cut_armed = True
        self._retire_interactive_snapshot()

        self.source_document = document
        if replacing_existing_source:
            self.last_scene_signature = None
            self.last_observed_scene_signature = None
        self.view_context_initialized = False
        self.last_source_view_index = None
        self.last_source_camera = None
        self.last_source_projection = None
        self.capture_pending = bool(
            replacing_existing_source and document is not None and
            self.running and self.GetBool(ID_AUTO))
        self.final_clean_pending = self.capture_pending
        if document is not None:
            self._note_view_context(document, arm_change=False)
        return True

    def _retire_interactive_snapshot(self, expected=None):
        """Prevent further reuse without touching a C4DThread-owned document."""
        snapshot = self.interactive_snapshot
        if snapshot is None or (expected is not None and snapshot is not expected):
            return False
        snapshot.retired = True
        self.interactive_snapshot = None
        return True

    def _interactive_snapshot_view(self, document, view_index):
        if view_index is None:
            return None
        get_view = getattr(document, "GetBaseDraw", None)
        if not callable(get_view):
            return None
        try:
            return get_view(int(view_index))
        except (AttributeError, ReferenceError, RuntimeError, TypeError,
                ValueError):
            return None

    def _remember_interactive_snapshot(
            self, source_document, render_document, render_data, active_rect,
            clean_feed):
        """Adopt a fresh clone only when its hierarchy is safely pairable."""
        source_view = _source_basedraw(source_document, self.follow_view)
        view_index = self._basedraw_index(source_document, source_view)
        snapshot_view = self._interactive_snapshot_view(
            render_document, view_index)
        source_entries = _document_object_entries(source_document)
        snapshot_entries = _document_object_entries(render_document)
        source_topology = _object_topology_signature(source_entries)
        snapshot_topology = _object_topology_signature(snapshot_entries)
        if (source_view is None or snapshot_view is None or
                source_topology is None or
                source_topology != snapshot_topology):
            return None
        state = InteractiveSnapshot(
            source_document=source_document,
            document=render_document,
            render_data=render_data,
            active_rect=active_rect,
            clean_feed=clean_feed,
            follow_view=self.follow_view,
            view_index=view_index,
            scene_camera=_basedraw_scene_camera(
                source_document, source_view),
            view_projection=_basedraw_projection_value(source_view),
            topology_signature=source_topology,
            source_nodes=tuple(entry[2] for entry in source_entries),
            snapshot_nodes=tuple(entry[2] for entry in snapshot_entries),
            non_matrix_signature=_non_matrix_dirty_signature(
                entry[2] for entry in source_entries),
            hierarchy_revision=_hierarchy_revision(source_document),
            document_non_matrix_revision=_document_non_matrix_revision(
                source_document),
        )
        self._retire_interactive_snapshot()
        self.interactive_snapshot = state
        return state

    def _reuse_interactive_snapshot(
            self, source_document, active_rect, clean_feed):
        """Synchronize and return the cache only while no render owns it."""
        state = self.interactive_snapshot
        # This guard is deliberately redundant with the scheduler. It makes
        # the ownership rule local and testable if a future caller changes.
        if state is None or state.retired or self.capture_thread is not None:
            return None
        if (not _same_sdk_object(state.source_document, source_document) or
                not _sdk_object_alive(source_document) or
                not _sdk_object_alive(state.document) or
                tuple(active_rect) != state.active_rect or
                bool(clean_feed) != state.clean_feed or
                int(self.follow_view) != state.follow_view):
            self._retire_interactive_snapshot(state)
            return None

        source_view = _source_basedraw(source_document, self.follow_view)
        view_index = self._basedraw_index(source_document, source_view)
        scene_camera = _basedraw_scene_camera(source_document, source_view)
        if (source_view is None or view_index != state.view_index or
                _basedraw_projection_value(source_view) !=
                state.view_projection or
                not _same_sdk_object(scene_camera, state.scene_camera)):
            # ``None`` identifies the default editor camera; _same_sdk_object
            # deliberately treats that same sentinel as stable.
            self._retire_interactive_snapshot(state)
            return None

        snapshot_view = self._interactive_snapshot_view(
            state.document, state.view_index)
        current_hierarchy_revision = _hierarchy_revision(source_document)
        hierarchy_known_unchanged = bool(
            state.hierarchy_revision is not None and
            current_hierarchy_revision is not None and
            current_hierarchy_revision == state.hierarchy_revision)
        if not hierarchy_known_unchanged:
            source_entries = _document_object_entries(source_document)
            source_topology = _object_topology_signature(source_entries)
            if (source_topology is None or
                    source_topology != state.topology_signature or
                    len(source_entries) != len(state.source_nodes) or
                    any(not _same_sdk_object(entry[2], stored)
                        for entry, stored in zip(
                            source_entries, state.source_nodes))):
                self._retire_interactive_snapshot(state)
                return None
            state.source_nodes = tuple(entry[2] for entry in source_entries)
            state.hierarchy_revision = current_hierarchy_revision

        if state.document_non_matrix_revision is not None:
            current_document_revision = _document_non_matrix_revision(
                source_document)
            non_matrix_changed = bool(
                current_document_revision !=
                state.document_non_matrix_revision)
            if non_matrix_changed:
                previous = state.document_non_matrix_revision
                object_revision_only = bool(
                    current_document_revision is not None and
                    len(current_document_revision) == len(previous) and
                    current_document_revision[1:] == previous[1:])
                if object_revision_only:
                    current_non_matrix = _non_matrix_dirty_signature(
                        state.source_nodes)
                    non_matrix_changed = bool(
                        state.non_matrix_signature is None or
                        current_non_matrix is None or
                        current_non_matrix != state.non_matrix_signature)
                    if not non_matrix_changed:
                        state.document_non_matrix_revision = (
                            current_document_revision)
                        state.non_matrix_signature = current_non_matrix
        else:
            current_non_matrix = _non_matrix_dirty_signature(
                state.source_nodes)
            non_matrix_changed = bool(
                state.non_matrix_signature is not None and
                current_non_matrix != state.non_matrix_signature)
        if non_matrix_changed:
            # Point edits, primitive/deformer parameters, tags and materials
            # need one fresh clone; matrix-only motion stays on the fast path.
            self._retire_interactive_snapshot(state)
            return None

        for source_node, snapshot_node in zip(
                state.source_nodes, state.snapshot_nodes):
            if not _copy_node_matrix(
                    source_node, snapshot_node, required=True):
                self._retire_interactive_snapshot(state)
                return None
        if not _copy_basedraw_state(source_view, snapshot_view):
            self._retire_interactive_snapshot(state)
            return None

        source_camera = _basedraw_camera(source_document, source_view)
        snapshot_camera = _basedraw_camera(state.document, snapshot_view)
        if ((source_camera is None) != (snapshot_camera is None) or
                (source_camera is not None and
                 not _copy_node_matrix(
                     source_camera, snapshot_camera, required=True))):
            self._retire_interactive_snapshot(state)
            return None
        if not _copy_camera_parameters(source_camera, snapshot_camera):
            self._retire_interactive_snapshot(state)
            return None

        # RenderDocument can alter detached render/view flags. Restore our
        # isolated policy only after the previous C4DThread has been joined.
        self._configure_preview_render_data(
            state.render_data, active_rect[2], active_rect[3],
            clean_feed=state.clean_feed, source_view=source_view)
        _apply_ai_render_views(
            state.document, clean_feed=state.clean_feed)
        return state

    def _make_clean_snapshot(
            self, document, width, height, clean_feed=True,
            evaluate_live=True):
        """Create a private document snapshot for Hardware Preview."""
        get_clone = getattr(document, "GetClone", None)
        render_data_type = getattr(documents, "RenderData", None)
        if not callable(get_clone) or not callable(render_data_type):
            return None, None
        # Ordinary final captures can refresh live caches. Render Frames clones
        # first and evaluates only that detached document, so it must never
        # execute passes on the live scene.
        if evaluate_live:
            # Cinema's evaluation pipeline is threaded. A settled/main-thread
            # capture must quiesce it before ExecutePasses/GetClone touches the
            # active document. Interactive samples never enter this path.
            stop_all = getattr(c4d, "StopAllThreads", None)
            if callable(stop_all):
                try:
                    stop_all()
                except (AttributeError, RuntimeError, TypeError):
                    return None, None
        execute_passes = (
            getattr(document, "ExecutePasses", None)
            if evaluate_live else None)
        if callable(execute_passes):
            try:
                execute_passes(
                    None, True, True, True,
                    getattr(c4d, "BUILDFLAGS_NONE", 0),
                )
            except (AttributeError, RuntimeError, TypeError):
                pass
        try:
            snapshot = get_clone(getattr(c4d, "COPYFLAGS_DOCUMENT", 0))
        except TypeError:
            snapshot = get_clone()
        except RuntimeError:
            snapshot = None
        if snapshot is None:
            return None, None

        try:
            rd = render_data_type()
            source_view = _source_basedraw(
                document, self.follow_view)
            if self.follow_view == FOLLOW_ACTIVE_VIEW:
                active_view = _exact_basedraw(
                    document, "GetActiveBaseDraw")
                render_view = _exact_basedraw(
                    document, "GetRenderBaseDraw")
                if (active_view is not None and
                        not _same_sdk_object(active_view, render_view)):
                    active_index = self._basedraw_index(
                        document, active_view)
                    snapshot_data = self._render_view_data(snapshot)
                    if active_index is None or snapshot_data is None:
                        return None, None
                    snapshot_data[DOCUMENT_RENDER_VIEW_ID] = int(active_index)
            self._configure_preview_render_data(
                rd, width, height, clean_feed=bool(clean_feed),
                source_view=source_view)
            insert = getattr(snapshot, "InsertRenderDataLast", None)
            if not callable(insert):
                insert = getattr(snapshot, "InsertRenderData", None)
            set_active = getattr(snapshot, "SetActiveRenderData", None)
            if not callable(insert) or not callable(set_active):
                return None, None
            insert(rd)
            set_active(rd)
            # Geometry Only is unconditional for every frame sent to AI.
            # Clean Feed remains an additional, user-controlled filter layer.
            _apply_ai_render_views(
                snapshot, clean_feed=bool(clean_feed))
            return snapshot, rd
        except (AttributeError, MemoryError, TypeError, RuntimeError):
            return None, None

    def _scene_signature(self, document):
        """Return dirty counters plus explicit camera/object transforms."""
        dirty_all = getattr(c4d, "DIRTYFLAGS_ALL", -1)
        values = []

        def append_dirty(node, *args):
            getter = getattr(node, "GetDirty", None)
            if not callable(getter):
                return
            try:
                values.append(int(getter(*args)))
            except TypeError:
                try:
                    values.append(int(getter()))
                except (AttributeError, TypeError, RuntimeError, ValueError):
                    pass
            except (AttributeError, RuntimeError, ValueError):
                pass

        append_dirty(document, dirty_all)
        get_render_data = getattr(document, "GetActiveRenderData", None)
        render_data = get_render_data() if callable(get_render_data) else None
        if render_data is not None:
            append_dirty(render_data, dirty_all)
        values.append((
            "render-aspect",
            round(_render_film_aspect(document), 6),
        ))

        follow_mode = self.follow_view
        basedraw = _source_basedraw(document, follow_mode)
        if basedraw is not None:
            values.append(("view-index", self._basedraw_index(document, basedraw)))
            append_dirty(basedraw, dirty_all)
            for getter_name in ("GetMg", "GetBaseMatrix"):
                getter = getattr(basedraw, getter_name, None)
                if not callable(getter):
                    continue
                try:
                    signature = _matrix_signature(getter())
                except (AttributeError, RuntimeError, TypeError):
                    signature = None
                if signature is not None:
                    values.append((getter_name, signature))
            projection = None
            get_projection = getattr(basedraw, "GetProjection", None)
            if callable(get_projection):
                try:
                    projection = int(get_projection())
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    projection = None
            if projection is None:
                parameter_id = getattr(c4d, "BASEDRAW_DATA_PROJECTION", None)
                if parameter_id is not None:
                    try:
                        projection = int(basedraw[parameter_id])
                    except (AttributeError, KeyError, RuntimeError, TypeError,
                            ValueError):
                        projection = None
            values.append(("projection", projection))

            get_view_parameter = getattr(basedraw, "GetViewParameter", None)
            if callable(get_view_parameter):
                try:
                    view_parameter = _value_signature(get_view_parameter())
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    view_parameter = None
                if view_parameter is not None:
                    values.append(("view", view_parameter))

            camera = _basedraw_camera(document, basedraw)
            if camera is not None:
                append_dirty(camera, dirty_all)
                get_matrix = getattr(camera, "GetMg", None)
                if callable(get_matrix):
                    try:
                        signature = _matrix_signature(get_matrix())
                    except (AttributeError, RuntimeError, TypeError):
                        signature = None
                    if signature is not None:
                        values.append(("camera", signature))

        objects = []
        object_selection_available = False
        get_active_objects = getattr(document, "GetActiveObjects", None)
        if callable(get_active_objects):
            object_selection_available = True
            try:
                objects = list(get_active_objects(0) or [])
            except (AttributeError, RuntimeError, TypeError):
                objects = []
        if not objects:
            get_active_object = getattr(document, "GetActiveObject", None)
            if callable(get_active_object):
                object_selection_available = True
                try:
                    active_object = get_active_object()
                except (AttributeError, RuntimeError):
                    active_object = None
                if active_object is not None:
                    objects = [active_object]
        if object_selection_available:
            values.append(("selected-count", len(objects)))
        for node in objects[:64]:
            append_dirty(node, dirty_all)
            get_matrix = getattr(node, "GetMg", None)
            if callable(get_matrix):
                try:
                    signature = _matrix_signature(get_matrix())
                except (AttributeError, RuntimeError, TypeError):
                    signature = None
                if signature is not None:
                    values.append(("object", signature))

        get_time = getattr(document, "GetTime", None)
        if callable(get_time):
            try:
                document_time = get_time()
                get_value = getattr(document_time, "Get", None)
                if callable(get_value):
                    values.append(("time", float(get_value())))
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
        return tuple(values) if values else None

    def _idle_capture_needed(self, document, now):
        signature = self._scene_signature(document)
        if signature is None or signature != self.last_scene_signature:
            return True
        return (self.last_capture <= 0.0 or
                now - self.last_capture >= IDLE_SAFETY_REFRESH_SECONDS)

    def _observe_scene_motion(self, document, now):
        """Poll transforms so camera/object drags survive missing SDK events."""
        context_changed = self._note_view_context(document)
        signature = self._scene_signature(document)
        if signature is None:
            if not context_changed:
                return False
        else:
            previous = self.last_observed_scene_signature
            if (not context_changed and
                    (previous is None or signature == previous)):
                self.last_observed_scene_signature = signature
                return False
            capture = self.capture_thread
            if (not context_changed and capture is not None and
                    _semantic_scene_signature(signature) ==
                    getattr(
                        capture, "semantic_scene_signature",
                        _semantic_scene_signature(
                            getattr(capture, "scene_signature", None)))):
                # Hardware Preview can bump document dirty counters before its
                # background thread is joined. Do not mistake that internal
                # side effect for camera/object activity.
                return False
            self.last_observed_scene_signature = signature

        self.capture_pending = True
        self.final_clean_pending = bool(
            self.running and self.GetBool(ID_AUTO))
        self.source_activity_generation += 1
        self.interactive_until = now + INTERACTIVE_QUIET_SECONDS
        # An idle frame was prepared before the latest transform. Reject it
        # without blocking the UI and let latest-only scheduling replace it.
        self._cancel_background_capture(interactive=False)
        return True

    def _register_settled_frame(self, bitmap, now, allow_reset=True):
        """Reset only after a real document/view/camera switch plus an RGB cut."""
        fingerprint = _bitmap_fingerprint(bitmap)
        if fingerprint is None:
            return None
        difference = _fingerprint_difference(
            self.last_settled_fingerprint, fingerprint)
        self.last_settled_fingerprint = fingerprint
        hard_cut = self.resync_cut_armed
        self.resync_cut_armed = False
        if (not allow_reset or difference is None or
                not self.auto_resync or
                not hard_cut or
                difference < AUTO_RESYNC_THRESHOLD or
                now - self.last_auto_resync_at < AUTO_RESYNC_COOLDOWN_SECONDS):
            return difference
        requested_at = now + AUTO_RESYNC_DELAY_SECONDS
        if self.resync_pending_at <= 0.0:
            self.resync_pending_at = requested_at
            self._set_status(
                "Large camera change detected — AI auto-resync queued")
        else:
            self.resync_pending_at = min(
                self.resync_pending_at, requested_at)
        return difference

    def _maybe_auto_resync(self, now):
        if self.resync_pending_at <= 0.0 or now < self.resync_pending_at:
            return False
        if not self.running or not self.auto_resync:
            self.resync_pending_at = 0.0
            return False
        if self.recording_active or self.recording_stopping:
            self.resync_pending_at = now + STATUS_POLL_SECONDS
            return False
        if (self.capture_thread is not None or self.editor_move_active or
                now < self.interactive_until or self.final_clean_pending):
            return False
        if now - self.last_auto_resync_at < AUTO_RESYNC_COOLDOWN_SECONDS:
            self.resync_pending_at = 0.0
            return False
        if self.worker_stage != "receiving":
            self.resync_pending_at = now + STATUS_POLL_SECONDS
            return False
        self.resync_pending_at = 0.0
        self.last_auto_resync_at = now
        return self._reset_context(automatic=True, reuse_source=True)

    def _start_background_capture(self, now, interactive=False,
                                  clean_feed=None, exact_clean=False,
                                  document=None, direct_move=False):
        """Start one latest-only source capture on a C4DThread."""
        if (self.render_frames_active or self.sequence_source_active):
            return False
        doc = (documents.GetActiveDocument()
               if document is None else document)
        self._prepare_source_document(doc)
        doc = self.source_document
        if doc is None:
            self._set_status("No active document")
            return False
        if self.capture_thread is not None:
            self.capture_pending = True
            if clean_feed:
                self.capture_pending_clean = True
            return False
        active_rect = self._active_rect(doc)
        _active_x, _active_y, width, height = active_rect
        bmp = bitmaps.BaseBitmap()
        if bmp.Init(width, height, 24) != c4d.IMAGERESULT_OK:
            self._set_status("Could not create the viewport bitmap")
            return False
        use_clean_feed = (self.GetBool(ID_CLEAN_FEED)
                          if clean_feed is None else bool(clean_feed))
        # Every AI source frame must come from a private document. A direct
        # MOVE callback may reuse an already joined private snapshot, but it
        # must never fall back to the live BaseDocument: Hardware Preview also
        # reads that document's BaseDraw and would capture HUD, handles and
        # other editor-only overlays. If no cache is ready, defer one clean
        # clone to CoreMessage/Timer instead.
        interactive_state = None
        render_doc = None
        rd = None
        direct_move_requires_cache = bool(interactive and direct_move)
        if interactive:
            interactive_state = self._reuse_interactive_snapshot(
                doc, active_rect, use_clean_feed)
            if interactive_state is None:
                if direct_move_requires_cache:
                    self.capture_pending = True
                    self._post_realtime_event()
                    return False
                else:
                    # Deferred CoreMessage/Timer may build a fresh private
                    # seed, but only after Cinema's draw/evaluation workers are
                    # quiescent.
                    if (not self._realtime_pump_context_safe() or
                            not self._quiesce_c4d_threads()):
                        self.capture_pending = True
                        self.interactive_next_capture_at = max(
                            self.interactive_next_capture_at,
                            now + INTERACTIVE_INTERVAL_MS / 1000.0)
                        return False
                    render_doc, rd = self._make_clean_snapshot(
                        doc, width, height,
                        clean_feed=use_clean_feed,
                        evaluate_live=False)
                    if render_doc is not None and rd is not None:
                        interactive_state = (
                            self._remember_interactive_snapshot(
                                doc, render_doc, rd, active_rect,
                                use_clean_feed))
        # A final/idle render always creates its own fresh clone below. Keep the
        # previous joined drag cache alive until that clone succeeds; otherwise
        # a drag which interrupts an idle render would lose its fast path.
        if interactive_state is not None:
            render_doc = interactive_state.document
            rd = interactive_state.render_data
        elif not interactive:
            render_doc, rd = self._make_clean_snapshot(
                doc, width, height,
                clean_feed=use_clean_feed,
                evaluate_live=not interactive)
        isolated = bool(
            render_doc is not None and rd is not None)
        if render_doc is None or rd is None:
            self._set_status(
                "Viewport capture stopped: safe document clone failed")
            return False
        thread = SourceCaptureThread(
            render_doc, rd, bmp, self.input_path,
            _render_view_label(doc, self.follow_view),
            interactive=interactive,
            clean_feed=use_clean_feed,
            isolated=isolated)
        thread.scene_signature = self._scene_signature(doc)
        thread.semantic_scene_signature = _semantic_scene_signature(
            thread.scene_signature)
        thread.source_activity_generation = self.source_activity_generation
        thread.source_document = doc
        thread.active_rect = active_rect
        thread.interactive_snapshot = interactive_state
        thread.started_at = now
        self.capture_thread = thread
        self.capture_pending = False
        self.capture_pending_clean = False
        if interactive:
            self.interactive_next_capture_at = (
                now + INTERACTIVE_INTERVAL_MS / 1000.0)
        if not thread.Start():
            thread.source_view = None
            thread.source_view_state = ()
            self.capture_thread = None
            if interactive_state is not None:
                self._retire_interactive_snapshot(interactive_state)
            self._set_status("Could not start background capture")
            return False
        return True

    @staticmethod
    def _restore_capture_view_state(thread):
        if thread is None:
            return False
        source_view = getattr(thread, "source_view", None)
        state = getattr(thread, "source_view_state", ())
        # Clear first so repeated cleanup paths cannot apply stale settings.
        thread.source_view = None
        thread.source_view_state = ()
        return _restore_render_mutated_viewport_state(source_view, state)

    def _finish_background_capture(self):
        """Publish a completed thread result from the main/UI thread."""
        thread = self.capture_thread
        if thread is None:
            return False
        try:
            thread_running = bool(thread.IsRunning())
        except (AttributeError, ReferenceError, RuntimeError):
            thread_running = True
        # Main() posts SpecialEventAdd in its finally block just before the
        # native wrapper flips IsRunning() to false. Never End(True) in that
        # narrow window: the UI thread could otherwise wait while the Python
        # worker still needs the GIL to return from Main().
        if thread_running:
            if getattr(thread, "main_completed", False):
                self._post_realtime_event()
            return False
        # C4DThread has an explicit lifetime: Maxon requires every started
        # thread to be closed, even after Main() has already returned.
        thread.End(True)
        self._restore_capture_view_state(thread)
        activity_unchanged = (
            getattr(
                thread, "source_activity_generation",
                self.source_activity_generation) ==
            self.source_activity_generation)
        live_document = getattr(thread, "document", None)
        post_signature = (
            self._scene_signature(live_document)
            if (not getattr(thread, "isolated", False) and
                _sdk_object_alive(live_document))
            else None)
        semantic_unchanged = bool(
            post_signature is not None and
            _semantic_scene_signature(post_signature) ==
            getattr(
                thread, "semantic_scene_signature",
                _semantic_scene_signature(
                    getattr(thread, "scene_signature", None))))
        if activity_unchanged and semantic_unchanged:
            # RenderDocument and viewport restoration can both change dirty
            # counters. Adopt the post-render state only if no real source
            # activity arrived while this capture was in flight.
            thread.scene_signature = post_signature
        self.capture_thread = None
        # A source switch/stop can retain this owner while this exact thread is
        # running. Remove it before the pump starts a replacement, otherwise a
        # later Timer would cancel that fresh capture as deferred cleanup.
        _discard_deferred_capture_owner(self)
        finished = time.monotonic()
        thread.finished_at = finished
        thread.duration_ms = max(
            0.0, (finished - thread.started_at) * 1000.0)

        # Cancellation is also the publication barrier. Main() may have
        # completed successfully in the few instructions between IsRunning()
        # and MOVE_END; such a bitmap still represents an obsolete drag state.
        if thread.cancelled:
            thread.success = False

        # BaseBitmap.Save and filesystem publication stay on Cinema 4D's main
        # thread. The C4DThread is responsible only for RenderDocument; this
        # prevents an old in-drag render from racing the final clean frame.
        if thread.success and not thread.cancelled:
            tmp = thread.target.with_name(
                ".%s.%d.%d.tmp.jpg" %
                (thread.target.stem, os.getpid(), time.time_ns()))
            try:
                jpg_settings = c4d.BaseContainer()
                jpg_settings[getattr(c4d, "JPGSAVER_QUALITY", 100)] = 90
                save_result = thread.bitmap.Save(
                    str(tmp), c4d.FILTER_JPG, jpg_settings)
                if save_result != c4d.IMAGERESULT_OK:
                    thread.success = False
                    thread.error = "Viewport JPEG error: %s" % save_result
                else:
                    _replace_with_retry(tmp, thread.target)
            except Exception as exc:
                thread.success = False
                thread.error = "Viewport publish exception: %s" % exc
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass

        self.capture_duration_ms = thread.duration_ms
        if self.capture_duration_ema_ms <= 0.0:
            self.capture_duration_ema_ms = thread.duration_ms
        else:
            self.capture_duration_ema_ms = (
                self.capture_duration_ema_ms * 0.75 +
                thread.duration_ms * 0.25)
        requested_ms = min(
            MAX_INTERVAL_MS,
            max(MIN_INTERVAL_MS, self.GetInt32(ID_INTERVAL)),
        )
        adaptive_ms = max(
            self.capture_duration_ms,
            self.capture_duration_ema_ms,
        ) / CAPTURE_DUTY_CYCLE
        self.effective_interval_ms = max(requested_ms, adaptive_ms)
        if thread.success:
            self.next_capture_at = max(
                thread.started_at + self.effective_interval_ms / 1000.0,
                finished + UI_TIMER_MS / 1000.0,
            )
        else:
            self.next_capture_at = (
                finished + self.effective_interval_ms / 1000.0)
        if not thread.success:
            failed_snapshot = getattr(thread, "interactive_snapshot", None)
            if failed_snapshot is not None:
                self._retire_interactive_snapshot(failed_snapshot)
            if thread.error and not thread.cancelled:
                self._set_status(thread.error)
            return False
        thread_active_rect = getattr(
            thread, "active_rect", DEFAULT_ACTIVE_RECT)
        self.published_active_rect = thread_active_rect
        if (self.resolution_capture_pending and
                thread_active_rect == self._active_rect(live_document)):
            self.resolution_capture_pending = False
        self.preview.set_source_label(thread.source_label)
        refresh_source = bool(
            self.preview.view_mode != VIEW_AI or
            self.preview.ai_bitmap is None)
        if (refresh_source and
                not self.preview.set_source_image(self.input_path)):
            self._set_status("Could not open the viewport capture")
            return False
        self.last_capture = finished
        self.last_scene_signature = getattr(
            thread, "scene_signature", self.last_scene_signature)
        self.last_observed_scene_signature = self.last_scene_signature
        if not thread.interactive:
            self._register_settled_frame(
                thread.bitmap, finished,
                allow_reset=self.running)
            source_document = getattr(thread, "source_document", None)
            current_signature = (
                self._scene_signature(source_document)
                if (_sdk_object_alive(source_document) and
                    _same_sdk_object(source_document, self.source_document))
                else None)
            semantic_signature = _semantic_scene_signature(current_signature)
            if (activity_unchanged and semantic_signature is not None and
                    semantic_signature ==
                    getattr(thread, "semantic_scene_signature", None)):
                # A joined, successful final/idle frame is the safest seed for
                # the next drag. MOVE_END still rendered a fresh full clone;
                # only subsequent interactive captures may borrow it.
                self._remember_interactive_snapshot(
                    source_document, thread.document, thread.render_data,
                    thread_active_rect, thread.clean_feed)
        self.published_source_revision += 1
        self._write_control()
        return True

    def _cancel_background_capture(self, interactive=None):
        thread = self.capture_thread
        if thread is None:
            return False
        if interactive is not None and thread.interactive != interactive:
            return False
        # Mark first and regardless of IsRunning(). The render can finish
        # between the caller's completion check and this method; publication
        # must still reject that stale bitmap.
        thread.cancelled = True
        if thread.IsRunning():
            thread.End(False)
        return True

    def _post_realtime_event(self):
        """Queue one coalesced main-thread pump without touching the scene."""
        if self.realtime_event_queued:
            return False
        post_event = getattr(c4d, "SpecialEventAdd", None)
        if not callable(post_event):
            return False
        self.realtime_event_queued = True
        try:
            post_event(PLUGIN_ID)
        except (AttributeError, RuntimeError, TypeError):
            self.realtime_event_queued = False
            return False
        return True

    def _latch_interactive_phase(self, phase, now):
        """Update only Python state for the newest editor-move phase."""
        move_end = phase == getattr(c4d, "MOVE_END", 2)
        if phase in (getattr(c4d, "MOVE_START", 0),
                     getattr(c4d, "MOVE_CONTINUE", 1)):
            self.editor_move_active = True
            self.interactive_until = 0.0
            self.capture_pending = True
            self.final_clean_pending = False
        elif move_end:
            self.editor_move_active = False
            self.interactive_until = 0.0
            self.capture_pending = False
            self.final_clean_pending = True
        else:
            # Older SDK/fallback path: silence can end the burst, but its final
            # pass is also asynchronous and therefore cannot freeze the mouse.
            self.interactive_until = now + INTERACTIVE_QUIET_SECONDS
            self.capture_pending = True
            self.final_clean_pending = True
        return move_end

    def _request_interactive_capture(self, phase=None, aggressive=False):
        """Record the newest MOVE phase and optionally pump it synchronously.

        ``aggressive`` enables the direct editor-drag path: Cinema can starve
        both GeDialog.Timer and custom CoreMessage events while its native Move
        tool owns the mouse. In that mode the MOVE callback itself harvests,
        publishes and starts a Hardware Preview capture only when a reusable
        private snapshot is already available.
        """
        now = time.monotonic()
        self.last_editor_move_at = now
        if self.running or self.auto_paused:
            self.last_activity_at = now
        if self.running:
            self.source_activity_generation += 1
        if self.auto_paused:
            self.auto_resume_pending = True
        if not self.running and not self.auto_paused:
            return False
        move_end = self._latch_interactive_phase(phase, now)
        thread = self.capture_thread
        if thread is not None:
            # Pure Python publication barrier only. End/IsRunning are native
            # calls and are deliberately deferred to CoreMessage.
            if move_end or not getattr(thread, "interactive", False):
                thread.cancelled = True

        self.interactive_pending_phase = phase
        if (aggressive and not move_end and
                (self.running or self.auto_paused)):
            # Consume this phase here so a delayed SpecialEvent cannot apply it
            # a second time. Thread completion events may still use CoreMessage
            # to harvest opportunistically between MOVE callbacks.
            if self.realtime_pump_busy:
                self._post_realtime_event()
                return True
            self.realtime_pump_busy = True
            try:
                if self.auto_paused:
                    if not self._resume_auto_paused(
                            now, refresh_source=False):
                        self._post_realtime_event()
                        return True
                    # _start() resets bridge state and _resume_auto_paused()
                    # restores the phase. This callback still owns the pump.
                    self.realtime_pump_busy = True
                    phase = self.interactive_pending_phase
                self.interactive_pending_phase = None
                self._apply_deferred_move_phase(phase)
                self._pump_interactive_move(
                    phase, now, aggressive=True)
            finally:
                self.realtime_pump_busy = False
            return True

        self._post_realtime_event()
        return True

    def _apply_deferred_move_phase(self, phase):
        """Perform phase-specific native work outside the Move callback."""
        if phase in (getattr(c4d, "MOVE_START", 0),
                     getattr(c4d, "MOVE_CONTINUE", 1)):
            # An idle source frame represents the scene before this drag.
            self._cancel_background_capture(interactive=False)
        elif phase == getattr(c4d, "MOVE_END", 2):
            # Mark/cancel even an already completed but unpublished drag frame.
            self._cancel_background_capture()
            self._retire_interactive_snapshot()
            self.final_clean_pending = bool(
                self.running and self.GetBool(ID_AUTO))

    def _pump_interactive_move(
            self, phase=None, now=None, aggressive=False):
        """Advance realtime capture from CoreMessage or the dialog timer.

        The aggressive path also runs from EVMSG_ASYNCEDITORMOVE, but it never
        waits for an active render. It may harvest an already completed thread,
        poll one AI bitmap, and launch one single-flight capture from a detached
        private snapshot. If no clean cache exists, the callback queues a safe
        clone for the next CoreMessage/Timer pass.

        GeDialog.Timer can be starved for the whole lifetime of Cinema's native
        Move tool. A completed interactive render therefore hands directly to
        its replacement while the drag is still active. RenderDocument itself
        is the cadence limiter: there is never more than one capture in flight.
        """
        current = time.monotonic() if now is None else float(now)
        if (not self.running or self.auto_paused or
                self.render_frames_active or self.sequence_source_active):
            return False

        progressed = False
        harvested_during_drag = False
        thread = self.capture_thread
        if thread is not None:
            try:
                thread_running = bool(thread.IsRunning())
            except (AttributeError, ReferenceError, RuntimeError):
                thread_running = True
            if not thread_running:
                published = self._finish_background_capture()
                harvested = self.capture_thread is None
                progressed = bool(published or progressed)
                if harvested and self.editor_move_active:
                    # MOVE_CONTINUE may already have armed a replacement. If
                    # no new message arrived, a successful interactive sample
                    # still keeps the capture chain alive until MOVE_END.
                    if (self.capture_pending or
                            (getattr(thread, "interactive", False) and
                             getattr(thread, "success", False) and
                             not getattr(thread, "cancelled", False))):
                        self.capture_pending = True
                        harvested_during_drag = True
            elif getattr(thread, "main_completed", False):
                self._post_realtime_event()

        if current >= self.interactive_output_poll_at:
            self.interactive_output_poll_at = (
                current + UI_TIMER_MS / 1000.0)
            progressed = bool(self._poll_output(current) or progressed)

        if phase == getattr(c4d, "MOVE_END", 2):
            return progressed
        phase_requests_frame = phase in (
            getattr(c4d, "MOVE_START", 0),
            getattr(c4d, "MOVE_CONTINUE", 1),
        )
        if (self.capture_thread is not None or
                not self.capture_pending or
                not self.GetBool(ID_AUTO) or
                (current < self.interactive_next_capture_at and
                 not phase_requests_frame and
                 not harvested_during_drag)):
            return progressed

        document = documents.GetActiveDocument()
        started = self._start_background_capture(
            current, interactive=True,
            clean_feed=self.GetBool(ID_CLEAN_FEED),
            document=document,
            direct_move=aggressive)
        return bool(started or progressed)

    def _poll_output(self, now=None):
        """Load the newest AI frame from either Timer or editor move events."""
        current = time.monotonic() if now is None else now
        try:
            stat = self.output_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if (signature != self.last_output_signature and
                    self.preview.set_ai_image(self.output_path)):
                self.last_output_signature = signature
                self.last_output_at = current
                self.ai_output_sequence += 1
                if not self.auto_pause_armed:
                    self.auto_pause_armed = True
                    self.last_activity_at = current
                return True
        except OSError:
            pass
        return False

    def _save_frame(self):
        """Save the displayed AI frame as PNG and show that file in C4D."""
        if self.preview.ai_bitmap is None:
            self._poll_output()
        bitmap = self.preview.ai_bitmap
        if bitmap is None:
            gui.MessageDialog("No rendered frame is available yet.")
            return False

        try:
            selected = storage.SaveDialog(
                type=c4d.FILESELECTTYPE_IMAGES,
                title="Save Frame",
                force_suffix="png",
                def_file=time.strftime("Render_Frame_%Y%m%d_%H%M%S.png"),
            )
            if not selected:
                return False
            destination = Path(selected)
        except Exception as exc:
            gui.MessageDialog(
                "Could not open the Save Frame dialog: %s" % exc)
            return False
        temporary = destination.with_name(
            ".%s.%d.%d.tmp.png" % (
                destination.stem, os.getpid(), time.time_ns()))
        try:
            result = bitmap.Save(
                str(temporary), c4d.FILTER_PNG, None)
            if result != c4d.IMAGERESULT_OK:
                gui.MessageDialog(
                    "Could not save the frame (image error %s)." % result)
                return False
            _replace_with_retry(temporary, destination)
        except Exception as exc:
            gui.MessageDialog("Could not save the frame: %s" % exc)
            return False
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

        try:
            saved_bitmap = bitmaps.BaseBitmap()
            result, is_movie = saved_bitmap.InitWith(str(destination))
            if result != c4d.IMAGERESULT_OK or is_movie:
                gui.MessageDialog(
                    "The frame was saved, but it could not be opened "
                    "in Picture Viewer.")
                return False
            bitmaps.ShowBitmap(saved_bitmap)
            return True
        except Exception as exc:
            gui.MessageDialog(
                "The frame was saved, but it could not be opened "
                "in Picture Viewer: %s" % exc)
            return False

    def _start_recording(
            self, destination, capture_enabled=True,
            replay_on_ack=None):
        if self.recording_active or self.recording_stopping:
            return False
        destination = Path(destination)
        self.recording_id = "%d-%d" % (os.getpid(), time.time_ns())
        self.recording_work_path = destination.with_name(
            ".%s.%s.part.mp4" % (
                destination.stem, self.recording_id))
        self.recording_final_path = str(destination)
        self.sequence_replay_on_recording_ack = (
            bool(self.sequence_source_active)
            if replay_on_ack is None else bool(replay_on_ack))
        self.recording_capture_enabled = bool(
            capture_enabled and
            not self.sequence_replay_on_recording_ack)
        self.recording_capture_after = 0.0
        self.recording_active = True
        self.recording_stopping = False
        self.recording_start_acknowledged = False
        self.recording_start_requested_at = time.monotonic()
        self.recording_stop_requested_at = 0.0
        self.auto_animation_replay_deadline = 0.0
        self.last_recording_status_signature = None
        for path in (self.recording_status_path,
                     self.recording_work_path):
            try:
                path.unlink()
            except OSError:
                pass
        if not self._write_control():
            self._reset_recording_ui()
            return False
        self.SetString(ID_RECORD, "Starting REC…")
        self.Enable(ID_RECORD, False)
        return True

    def _toggle_recording(self):
        if not self.recording_active and self.render_frames_active:
            gui.MessageDialog(
                "Render Frames now records and saves its animation "
                "automatically.")
            return False
        if (not self.recording_active and
                (not self.running or self.preview.ai_bitmap is None)):
            gui.MessageDialog(
                "Start the render and wait for the first frame "
                "before recording.")
            return False
        if self.recording_active:
            return self._request_recording_finish()

        destination = self._select_recording_destination()
        if destination is None:
            return False
        return self._start_recording(destination)

    def _request_recording_finish(self, update_ui=True):
        """Ask the worker to finalize REC without blocking Cinema 4D."""
        if not self.recording_active:
            return self.recording_stopping
        self.recording_active = False
        self.recording_stopping = True
        self.recording_stop_requested_at = time.monotonic()
        writer = (
            self._write_control
            if update_ui else self._write_recording_control)
        if not writer():
            self.recording_active = True
            self.recording_stopping = False
            self.recording_stop_requested_at = 0.0
            return False
        if update_ui:
            self.SetString(ID_RECORD, "Saving MP4…")
            self.Enable(ID_RECORD, False)
        return True

    def _wait_for_recording_terminal(self, timeout=5.0):
        """Wait only during forced teardown, never during normal REC use."""
        recording_id = self.recording_id
        if not recording_id:
            return None
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            status = _read_json(self.recording_status_path, {})
            if str(status.get("recording_id") or "") == recording_id:
                state = str(status.get("state") or "")
                if state in ("saved", "error", "cancelled"):
                    return state
            if self.proc is None:
                return None
            try:
                if self.proc.poll() is not None:
                    return None
            except Exception:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def _finish_recording_before_worker_stop(
            self, timeout=5.0, update_ui=True):
        """Give the encoder a bounded grace period before killing its worker."""
        if not (self.recording_active or self.recording_stopping):
            return None
        try:
            if (self.recording_active and
                    not self._request_recording_finish(
                        update_ui=update_ui)):
                return None
            return self._wait_for_recording_terminal(timeout)
        finally:
            self._reset_recording_ui(update_ui=update_ui)

    def _reset_recording_ui(self, update_ui=True):
        self.recording_active = False
        self.recording_stopping = False
        self.recording_id = ""
        self.recording_work_path = None
        self.recording_final_path = ""
        self.recording_capture_enabled = True
        self.recording_capture_after = 0.0
        self.recording_start_acknowledged = False
        self.recording_start_requested_at = 0.0
        self.recording_stop_requested_at = 0.0
        self.auto_animation_replay_deadline = 0.0
        self.sequence_replay_on_recording_ack = False
        if update_ui:
            self.SetString(ID_RECORD, "● REC")
            self.Enable(ID_RECORD, True)

    def _check_recording_watchdog(self, now):
        if (self.recording_active and
                not self.recording_start_acknowledged and
                self.recording_start_requested_at > 0.0 and
                now - self.recording_start_requested_at >=
                RECORDING_START_TIMEOUT_SECONDS):
            self._freeze_render_timer(now)
            self._stop(recording_grace=0.0)
            gui.MessageDialog(
                "REC could not start. The render session was stopped safely.")
            return True
        if (self.recording_stopping and
                self.recording_stop_requested_at > 0.0 and
                now - self.recording_stop_requested_at >=
                RECORDING_FINALIZE_TIMEOUT_SECONDS):
            self._freeze_render_timer(now)
            self._stop(recording_grace=0.0)
            gui.MessageDialog(
                "MP4 finalization timed out. The render session was stopped "
                "to protect the recording file.")
            return True
        return False

    @staticmethod
    def _open_recording_externally(destination):
        fallback = getattr(storage, "GeExecuteFile", None)
        if not callable(fallback):
            return False
        try:
            return bool(fallback(str(destination)))
        except Exception:
            return False

    def _open_recording(self, destination):
        """Validate a movie before asking Cinema's Picture Viewer to load it."""
        try:
            if (not destination.is_file() or
                    destination.stat().st_size <= 0):
                raise OSError("missing or empty")
        except OSError:
            gui.MessageDialog(
                "The MP4 was reported as saved, but the file is missing "
                "or empty:\n%s" % destination)
            return False

        probe = bitmaps.BaseBitmap()
        try:
            result, is_movie = probe.InitWith(str(destination))
        except Exception:
            result, is_movie = None, False
        if result != c4d.IMAGERESULT_OK or not is_movie:
            if not self._open_recording_externally(destination):
                gui.MessageDialog(
                    "The MP4 was saved, but Cinema 4D could not validate "
                    "or open it:\n%s" % destination)
                return False
            return True

        try:
            documents.LoadFile(str(destination))
            return True
        except Exception as exc:
            if not self._open_recording_externally(destination):
                gui.MessageDialog(
                    "The MP4 was saved, but it could not be opened:\n%s\n%s"
                    % (destination, exc))
                return False
            return True

    def _replay_sequence_after_recording_ack(self, now):
        """Replay a cached take only after the MP4 recorder is actually ready."""
        if not self.sequence_replay_on_recording_ack:
            return True
        automatic_pending = bool(
            self.auto_animation_phase == "recording_start" and
            self.source_sequence_manifest)
        if not self.sequence_source_active and not automatic_pending:
            self.sequence_replay_on_recording_ack = False
            return True
        previous_revision = self.source_sequence_revision
        previous_source_active = self.sequence_source_active
        previous_ended = self.sequence_playback_ended
        previous_waiting = self.sequence_waiting_for_start
        previous_started_at = self.sequence_started_at
        previous_capture_enabled = self.recording_capture_enabled
        previous_sequence_start_at = self.source_sequence_start_at
        previous_capture_after = self.recording_capture_after
        self.source_sequence_revision += 1
        self.sequence_source_active = True
        self.sequence_playback_ended = False
        self.sequence_waiting_for_start = True
        self.sequence_started_at = float(now)
        if automatic_pending or previous_source_active:
            synchronized_start = (
                time.time() + RENDER_FRAMES_WARMUP_SECONDS)
            self.source_sequence_start_at = synchronized_start
            self.recording_capture_enabled = True
            self.recording_capture_after = synchronized_start
        else:
            self.source_sequence_start_at = 0.0
            self.recording_capture_after = 0.0
        self.last_sequence_telemetry_signature = None
        self.last_activity_at = float(now)
        if not self._write_control():
            self.source_sequence_revision = previous_revision
            self.sequence_source_active = previous_source_active
            self.sequence_playback_ended = previous_ended
            self.sequence_waiting_for_start = previous_waiting
            self.sequence_started_at = previous_started_at
            self.recording_capture_enabled = previous_capture_enabled
            self.source_sequence_start_at = previous_sequence_start_at
            self.recording_capture_after = previous_capture_after
            self._set_status(
                "REC is ready, but the animation replay is waiting to start")
            return False
        self.sequence_replay_on_recording_ack = False
        self.auto_animation_replay_deadline = 0.0
        if self.auto_animation_phase == "recording_start":
            self.auto_animation_phase = "replay"
            self.auto_animation_replay_revision = (
                self.source_sequence_revision)
            self.auto_animation_replay_deadline = 0.0
            self.auto_animation_tail_deadline = 0.0
            self.auto_animation_tail_timeout = 0.0
            self.auto_animation_output_start_sequence = (
                self.ai_output_sequence)
            self.SetString(ID_RENDER_FRAMES, "Stop & Save")
            self.Enable(ID_RENDER_FRAMES, True)
        self._set_status(
            "REC ready · playing viewport frames from frame 1")
        return True

    def _check_auto_animation_replay_timeout(self, now):
        if (not self.recording_start_acknowledged or
                not self.sequence_replay_on_recording_ack or
                self.auto_animation_replay_deadline <= 0.0 or
                float(now) < self.auto_animation_replay_deadline):
            return False
        if self.auto_animation_phase == "recording_start":
            return self._fail_auto_animation(
                "Rendered animation could not start after the recorder "
                "became ready")
        self._freeze_render_timer(now)
        self._stop(recording_grace=0.0)
        gui.MessageDialog(
            "REC could not synchronize the prepared animation. "
            "The render session was stopped safely.")
        return True

    def _poll_recording_status(self):
        if not self.recording_id:
            return False
        now = time.monotonic()
        if (self.recording_start_acknowledged and
                self.sequence_replay_on_recording_ack):
            if not self._replay_sequence_after_recording_ack(now):
                if self._check_auto_animation_replay_timeout(now):
                    return True
        try:
            stat = self.recording_status_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return self._check_recording_watchdog(now)
        if signature == self.last_recording_status_signature:
            return self._check_recording_watchdog(now)
        status = _read_json(self.recording_status_path, {})
        if str(status.get("recording_id") or "") != self.recording_id:
            return self._check_recording_watchdog(now)
        self.last_recording_status_signature = signature
        state = str(status.get("state") or "")
        if state == "recording" and self.recording_active:
            self.recording_start_acknowledged = True
            self.recording_start_requested_at = 0.0
            if (self.sequence_replay_on_recording_ack and
                    self.auto_animation_replay_deadline <= 0.0):
                self.auto_animation_replay_deadline = (
                    now + RENDER_FRAMES_REPLAY_TIMEOUT_SECONDS)
            self.SetString(ID_RECORD, "■ Stop REC")
            self.Enable(ID_RECORD, True)
            if not self._replay_sequence_after_recording_ack(now):
                self._check_auto_animation_replay_timeout(now)
            return True
        if state == "saved":
            destination = Path(str(
                status.get("destination")
                or self.recording_final_path))
            automatic = bool(self.auto_animation_phase)
            self.last_activity_at = now
            self._reset_recording_ui()
            if self.running:
                self._write_control()
            if automatic:
                source_stopped = True
                if self.sequence_source_active:
                    source_stopped = self._stop_sequence_source(
                        "Animation saved · %s" % destination.name)
                self._clear_auto_animation()
                self.Enable(ID_RENDER_FRAMES, True)
                if source_stopped:
                    self.SetString(ID_RENDER_FRAMES, "Render Frames")
            self._open_recording(destination)
            return True
        if state in ("error", "cancelled"):
            message = str(
                status.get("message")
                or "Could not finish the MP4 recording.")
            automatic = bool(self.auto_animation_phase)
            self.last_activity_at = now
            self._reset_recording_ui()
            if self.running:
                self._write_control()
            if automatic:
                source_stopped = True
                if self.sequence_source_active:
                    source_stopped = self._stop_sequence_source()
                self._clear_auto_animation()
                self.Enable(ID_RENDER_FRAMES, True)
                if source_stopped:
                    self.SetString(ID_RENDER_FRAMES, "Render Frames")
            gui.MessageDialog(message)
            return True
        return self._check_recording_watchdog(now)

    def _stop_capture_thread(self):
        thread = self.capture_thread
        joined = True
        if thread is not None:
            joined = self._join_capture_thread(thread)
        self.capture_pending = False
        self.capture_pending_clean = False
        self.manual_capture_pending = False
        self.final_clean_pending = False
        self.resolution_capture_pending = False
        self.editor_move_active = False
        self.interactive_until = 0.0
        if joined:
            self._retire_interactive_snapshot()
            _discard_deferred_capture_owner(self)
        return joined

    def _release_stopped_render_frames_resources(
            self, delete_directories=False):
        """Release clones only after capture join; frames only after worker stop."""
        self.render_frames_source_document = None
        self.render_frames_document = None
        self.render_frames_render_data = None
        if not delete_directories:
            return
        if self.render_frames_directory is not None:
            self._delete_render_frames_directory(
                self.render_frames_directory)
        for directory in self.state.glob("render_frames_*"):
            if directory.is_dir():
                self._delete_render_frames_directory(directory)
        self._cleanup_retired_sequence_directories(force=True)
        self.render_frames_directory = None
        self.render_frames_manifest_frames = []

    def _capture(self):
        doc = documents.GetActiveDocument()
        self._prepare_source_document(doc)
        doc = self.source_document
        if doc is None:
            self._set_status("No active document")
            return False
        active_rect = self._active_rect(doc)
        _active_x, _active_y, width, height = active_rect
        bmp = bitmaps.BaseBitmap()
        if bmp.Init(width, height, 24) != c4d.IMAGERESULT_OK:
            self._set_status("Could not create the bitmap")
            return False

        render_doc, rd = self._make_clean_snapshot(
            doc, width, height,
            clean_feed=self.GetBool(ID_CLEAN_FEED),
            evaluate_live=True)
        if render_doc is None or rd is None:
            self._set_status(
                "Viewport capture stopped: safe document clone failed")
            return False
        flags = c4d.RENDERFLAGS_EXTERNAL | c4d.RENDERFLAGS_NODOCUMENTCLONE
        try:
            result = documents.RenderDocument(
                render_doc, rd.GetDataInstance(), bmp, flags)
        except Exception as exc:
            self._set_status("Viewport render exception: %s" % exc)
            return False
        if result != c4d.RENDERRESULT_OK:
            self._set_status("Viewport render error: %s" % result)
            return False
        tmp = self.input_path.with_suffix(".tmp.jpg")
        jpg_settings = c4d.BaseContainer()
        jpg_settings[getattr(c4d, "JPGSAVER_QUALITY", 100)] = 90
        save_result = bmp.Save(str(tmp), c4d.FILTER_JPG, jpg_settings)
        if save_result != c4d.IMAGERESULT_OK:
            self._set_status("Could not save the viewport")
            return False
        try:
            _replace_with_retry(tmp, self.input_path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            self._set_status("Could not update the viewport file: %s" % exc)
            return False
        # Keep the exact 16:9 source visible for Source/Compare and while the
        # first remote frame is still negotiating.
        self.preview.set_source_label(
            _render_view_label(doc, self.follow_view))
        self.preview.set_source_image(self.input_path)
        self.last_capture = time.monotonic()
        self.published_active_rect = active_rect
        self.last_scene_signature = self._scene_signature(doc)
        self.last_observed_scene_signature = self.last_scene_signature
        # The initial synchronous frame already owns a fully rendered private
        # clone. Keep it as the first drag cache so MOVE_START can launch
        # immediately even when Cinema suppresses dialog timer messages.
        self._remember_interactive_snapshot(
            doc, render_doc, rd, active_rect, self.GetBool(ID_CLEAN_FEED))
        self._register_settled_frame(
            bmp, self.last_capture, allow_reset=self.running)
        return True

    def _capture_scheduled(self):
        """Capture once and pace future captures from measured render cost."""
        started = time.monotonic()
        success = self._capture()
        finished = time.monotonic()
        duration_ms = max(0.0, (finished - started) * 1000.0)
        self.capture_duration_ms = duration_ms
        if self.capture_duration_ema_ms <= 0.0:
            self.capture_duration_ema_ms = duration_ms
        else:
            self.capture_duration_ema_ms = (
                self.capture_duration_ema_ms * 0.75 +
                duration_ms * 0.25)
        if success:
            self.last_capture = finished

        requested_ms = min(
            MAX_INTERVAL_MS,
            max(MIN_INTERVAL_MS, self.GetInt32(ID_INTERVAL)),
        )
        adaptive_ms = max(
            self.capture_duration_ms,
            self.capture_duration_ema_ms,
        ) / CAPTURE_DUTY_CYCLE
        self.effective_interval_ms = max(requested_ms, adaptive_ms)
        if success:
            self.next_capture_at = max(
                started + self.effective_interval_ms / 1000.0,
                finished + UI_TIMER_MS / 1000.0,
            )
        else:
            # A failed render can still be expensive. Back off from completion
            # instead of immediately blocking C4D's main thread again.
            self.next_capture_at = (
                finished + self.effective_interval_ms / 1000.0)
        return success

    def _start(self, reuse_source=False, preserve_preview=False,
               allow_stale_source=False):
        if (self.proc is not None and
                not self._terminate_worker_process()):
            gui.MessageDialog(
                "The previous render worker is still shutting down. "
                "Try Start again in a moment.")
            return False
        if self.installer_proc and self.installer_proc.poll() is None:
            gui.MessageDialog("Wait for Install deps to finish.")
            return False
        cfg = self._save_settings()
        if cfg is None:
            return False
        if not VENDOR_READY.is_file():
            self._set_status("Install dependencies in Settings… first")
            self._open_settings()
            return False
        api_key = cfg["api_key"] or os.environ.get("FAL_KEY", "").strip()
        if not api_key:
            self._set_status("Enter an API key in Settings…")
            self._open_settings()
            return False
        interpreter = _find_worker_python()
        if interpreter is None:
            gui.MessageDialog(
                "Python runtime not found in the Cinema 4D installation folder. "
                "Run Repair for Cinema 4D in Maxon App.")
            return False
        if not preserve_preview:
            try:
                self.output_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._set_status("Could not clear the previous preview: %s" % exc)
                return False
        for path in (self.telemetry_path,):
            try:
                path.unlink()
            except OSError:
                pass
        if not preserve_preview:
            self.last_output_signature = None
        self.last_output_at = 0.0
        self.interactive_output_poll_at = 0.0
        self.interactive_pending_phase = None
        self.realtime_event_queued = False
        self.realtime_pump_busy = False
        self.auto_paused = False
        self.auto_pause_armed = False
        self.last_activity_at = time.monotonic()
        self.auto_paused_document = None
        self.auto_paused_scene_signature = None
        self.auto_resume_pending = False
        self.auto_resume_source_refresh = False
        if not preserve_preview:
            self.preview.clear_ai()
        document = documents.GetActiveDocument()
        self._prepare_source_document(document)
        document = self.source_document
        can_reuse_source = bool(
            reuse_source and
            self.input_path.is_file() and
            (allow_stale_source or
             self.published_active_rect == self._active_rect(document))
        )
        if not can_reuse_source:
            if not self._capture_scheduled():
                self._restore_followed_views()
                return False
        elif document is not None:
            self.last_observed_scene_signature = self._scene_signature(document)
        if not self._write_control():
            self._restore_followed_views()
            return False
        worker = ROOT / "lucy_worker.py"
        env = os.environ.copy()
        env["FAL_KEY"] = api_key
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            (self.state / "status.json").unlink()
        except OSError:
            pass
        self.worker_stage = "starting"
        try:
            self.worker_log = open(
                self.state / "worker.log", "a", encoding="utf-8",
                buffering=1)
            self.worker_log.write(
                "\n--- worker start %s ---\n" %
                time.strftime("%Y-%m-%d %H:%M:%S"))
            self.proc = subprocess.Popen(
                [str(interpreter), str(worker), "--state", str(self.state),
                 "--parent-pid", str(os.getpid())],
                cwd=str(ROOT), env=env, creationflags=creationflags,
                stdout=self.worker_log, stderr=subprocess.STDOUT)
        except OSError as exc:
            self.proc = None
            self._close_worker_log()
            self._set_status("Could not start the worker: %s" % exc)
            gui.MessageDialog(
                "Could not start the worker. "
                "Check your Cinema 4D installation.")
            self._restore_followed_views()
            return False
        self.running = True
        self.SetString(ID_START, "■ Stop")
        self._set_status("Connecting…")
        return True

    def _terminate_worker_process(self):
        """Best-effort terminate/kill without losing a still-live handle."""
        process = self.proc
        if process is None:
            return True
        stopped = False
        try:
            try:
                stopped = process.poll() is not None
            except Exception:
                stopped = False
            if not stopped:
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                        stopped = True
                    except Exception:
                        stopped = False
                except Exception:
                    stopped = False
            if not stopped:
                try:
                    process.kill()
                    try:
                        process.wait(timeout=2.0)
                        stopped = True
                    except Exception:
                        stopped = False
                except Exception:
                    stopped = False
            if not stopped:
                try:
                    stopped = process.poll() is not None
                except Exception:
                    stopped = False
        finally:
            self.proc = None if stopped else process
        return stopped

    def _stop(self, update_ui=True, recording_grace=5.0,
              terminate_worker=True):
        recording_work_path = self.recording_work_path
        self.running = False
        self.auto_paused = False
        self.auto_pause_armed = False
        self.auto_resume_pending = False
        self.auto_resume_source_refresh = False
        self.interactive_output_poll_at = 0.0
        self.interactive_pending_phase = None
        self.realtime_event_queued = False
        self.realtime_pump_busy = False
        self.auto_paused_document = None
        self.auto_paused_scene_signature = None
        self.render_frames_active = False
        self.render_frames_cancel_requested = True
        self.render_frames_abort_message = ""
        self.render_frames_phase = "shutdown_drain"
        self.sequence_source_active = False
        self.sequence_playback_ended = False
        self.sequence_waiting_for_start = False
        self.sequence_started_at = 0.0
        self.sequence_replay_on_recording_ack = False
        self.source_sequence_manifest = ""
        self.last_sequence_telemetry_signature = None
        self._clear_auto_animation()
        capture_joined = self._stop_capture_thread()
        if capture_joined:
            self.render_frames_cancel_requested = False
            self.render_frames_phase = ""
            self._release_stopped_render_frames_resources(
                delete_directories=False)
            _discard_deferred_capture_owner(self)
        else:
            _retain_deferred_capture_owner(self)
        try:
            recording_state = self._finish_recording_before_worker_stop(
                timeout=recording_grace, update_ui=update_ui)
        except Exception:
            recording_state = None
            self._reset_recording_ui(update_ui=False)
        if terminate_worker:
            self._terminate_worker_process()
        if self.proc is None and capture_joined:
            self._release_stopped_render_frames_resources(
                delete_directories=True)
        if recording_state != "saved" and recording_work_path is not None:
            _unlink_with_retry(
                recording_work_path, attempts=10, delay=0.02)
        self.worker_stage = ""
        self.resync_pending_at = 0.0
        self._close_worker_log()
        if capture_joined:
            self._restore_followed_views()
        self.source_document = None
        self.view_context_initialized = False
        self.last_source_view_index = None
        self.last_source_camera = None
        self.last_source_projection = None
        self.resync_cut_armed = False
        self.last_settled_fingerprint = None
        try:
            self.telemetry_path.unlink()
        except OSError:
            pass
        if update_ui:
            self.SetString(ID_START, "▶ Start")
            self.SetString(ID_RENDER_FRAMES, "Render Frames")
            self.Enable(ID_RENDER_FRAMES, True)
            self._set_status("Stopped")

    def _reset_context(self, automatic=False, reuse_source=True):
        """Reconnect Lucy so a large scene cut cannot cling to old geometry."""
        if not self.running:
            self._set_status("Press Start first")
            return False
        if self.recording_active or self.recording_stopping:
            if not automatic:
                gui.MessageDialog(
                    "Stop REC and wait for the MP4 before resetting.")
            return False
        self._set_status(
            "Auto resync…" if automatic else "Resetting AI context…")
        self._stop(update_ui=False)
        self.SetString(ID_START, "▶ Start")
        restarted = self._start(
            reuse_source=bool(reuse_source and self.input_path.is_file()),
            preserve_preview=bool(automatic))
        if not restarted:
            self._freeze_render_timer()
        return self.running

    def _enable_low_latency(self):
        self.SetBool(ID_AUTO, True)
        self.SetInt32(
            ID_INTERVAL, RESPONSIVE_INTERVAL_MS,
            min=MIN_INTERVAL_MS, max=MAX_INTERVAL_MS, step=25,
            min2=MIN_INTERVAL_MS, max2=MAX_INTERVAL_MS,
        )
        self.effective_interval_ms = float(RESPONSIVE_INTERVAL_MS)
        self.next_capture_at = 0.0
        if self._save_settings() is not None:
            self._set_status(
                "Responsive: idle min 75 ms · active drag min 125 ms")

    def _close_worker_log(self):
        if self.worker_log is not None:
            try:
                self.worker_log.close()
            except OSError:
                pass
            self.worker_log = None

    def _write_control(self):
        try:
            _atomic_write_json(self.control_path, {
                "prompt": self.GetString(ID_PROMPT),
                "enable_prompt_expansion": self.GetBool(ID_PROMPT_EXPANSION),
                "preserve_composition": self.GetBool(ID_PRESERVE_COMPOSITION),
                "reference_image_path": self.reference_path,
                "canvas_size": [CANVAS_WIDTH, CANVAS_HEIGHT],
                "active_rect": _normalized_active_rect(
                    self.published_active_rect or self._active_rect()),
                "source_revision": self.published_source_revision,
                "source_mode": (
                    "sequence" if self.sequence_source_active else "still"),
                "source_sequence_manifest": (
                    self.source_sequence_manifest
                    if self.sequence_source_active else ""),
                "source_sequence_revision": self.source_sequence_revision,
                "source_sequence_start_at": self.source_sequence_start_at,
                "input_path": str(self.input_path),
                "output_path": str(self.output_path),
                "recording": self.recording_active,
                "recording_capture": self.recording_capture_enabled,
                "recording_capture_after": self.recording_capture_after,
                "recording_id": self.recording_id,
                "recording_work_path": (
                    str(self.recording_work_path)
                    if self.recording_work_path else ""),
                "recording_final_path": self.recording_final_path,
                "updated": time.time(),
            })
        except OSError as exc:
            self._set_status("Could not update the control file: %s" % exc)
            return False
        return True

    def _write_recording_control(self):
        """Update REC during teardown without touching destroyed C4D gadgets."""
        payload = _read_json(self.control_path, {})
        payload.update({
            "recording": self.recording_active,
            "recording_capture": self.recording_capture_enabled,
            "recording_capture_after": self.recording_capture_after,
            "recording_id": self.recording_id,
            "recording_work_path": (
                str(self.recording_work_path)
                if self.recording_work_path else ""),
            "recording_final_path": self.recording_final_path,
            "updated": time.time(),
        })
        try:
            _atomic_write_json(self.control_path, payload)
        except Exception:
            return False
        return True

    def _install_deps(self):
        if self.running:
            gui.MessageDialog("Click Stop first.")
            return False
        if self.installer_proc and self.installer_proc.poll() is None:
            gui.MessageDialog("The installer is already running.")
            return False
        interpreter = _find_c4dpy()
        if interpreter is None:
            message = ("c4dpy.exe was not found in the Cinema 4D installation "
                       "folder. Run Repair in Maxon App.")
            self._set_status(message)
            gui.MessageDialog(message)
            return False
        installer = ROOT / "install_deps.py"
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            self.installer_proc = subprocess.Popen(
                [str(interpreter), "g_disableConsoleOutput=false", str(installer)],
                cwd=str(ROOT), creationflags=creationflags)
            self.installer_started_at = time.time()
        except OSError as exc:
            self.installer_proc = None
            self._set_status("Could not open the installer: %s" % exc)
            gui.MessageDialog(
                "Could not start c4dpy to install the dependencies.")
            return False
        self._set_status(
            "The installer opened in a separate console; restart C4D when it finishes")
        return True

    def _poll_installer(self):
        if self.installer_proc is None:
            return None
        result = self.installer_proc.poll()
        if result is None:
            try:
                marker_is_new = (VENDOR_READY.stat().st_mtime >=
                                 self.installer_started_at - 1.0)
            except OSError:
                marker_is_new = False
            if marker_is_new:
                message = "Dependencies installed — press Enter in the console, then restart C4D"
                self._set_status(message)
                return message
            return None
        self.installer_proc = None
        if result == 0 and VENDOR_READY.is_file():
            message = "Dependencies installed; restart C4D"
        else:
            message = "Installation did not finish — check the console message"
        self._set_status(message)
        return message

    @staticmethod
    def _realtime_pump_context_safe():
        """Require Cinema's main thread outside viewport drawing."""
        c4d_threading = getattr(c4d, "threading", None)
        checker = getattr(
            c4d_threading, "GeIsMainThreadAndNoDrawThread", None)
        if not callable(checker):
            return True
        try:
            return bool(checker())
        except (AttributeError, RuntimeError, TypeError):
            return False

    def CoreMessage(self, cid, msg):
        if cid == PLUGIN_ID:
            # MOVE and capture completion can post redundant events. Clearing
            # first lets a nested MOVE queue the next main-loop turn instead of
            # recursively entering document/render/UI code.
            self.realtime_event_queued = False
            if self.realtime_pump_busy:
                self._post_realtime_event()
                return True
            if not self._realtime_pump_context_safe():
                # START/CONTINUE are pumped synchronously by Message; repeatedly
                # re-posting their completion event during a native drag only
                # competes with the MOVE stream. The next MOVE callback harvests
                # the ready frame. MOVE_END/idle still keep one deferred retry.
                if not self.editor_move_active:
                    self._post_realtime_event()
                return True

            phase = self.interactive_pending_phase
            self.realtime_pump_busy = True
            try:
                now = time.monotonic()
                if (self.auto_paused and phase is not None and
                        self.auto_resume_pending):
                    if not self._resume_auto_paused(
                            now, refresh_source=False):
                        return True
                    # _start() deliberately resets bridge guards. This event is
                    # still executing, so restore the recursion barrier.
                    self.realtime_pump_busy = True
                    phase = self.interactive_pending_phase
                if self.auto_paused:
                    return True
                self.interactive_pending_phase = None
                if phase is not None:
                    self._apply_deferred_move_phase(phase)
                progressed = self._pump_interactive_move(phase, now)

                # MOVE_END is a stale-frame barrier. Once a cancelled drag
                # thread drains, seed one settled frame without depending on
                # GeDialog.Timer waking first.
                if (self.running and not self.auto_paused and
                        not self.editor_move_active and
                        self.capture_thread is None and
                        self.final_clean_pending and
                        self.GetBool(ID_AUTO)):
                    self.final_clean_pending = False
                    started = self._start_background_capture(
                        now, interactive=False,
                        clean_feed=self.GetBool(ID_CLEAN_FEED))
                    if not started and self.capture_thread is None:
                        self.final_clean_pending = True
                    progressed = bool(started or progressed)
            finally:
                self.realtime_pump_busy = False
            if self.interactive_pending_phase is not None:
                self._post_realtime_event()
            return True
        return gui.GeDialog.CoreMessage(self, cid, msg)

    def Message(self, msg, result):
        try:
            if (msg.GetId() == c4d.BFM_SYNC_MESSAGE and
                    msg.GetInt32(c4d.BFM_CORE_ID) ==
                    c4d.EVMSG_ASYNCEDITORMOVE and
                    not self.render_frames_active and
                    not self.sequence_source_active):
                # Maxon's high-frequency editor-move example deliberately does
                # not pass this stream through CheckCoreMessage: several phases
                # can share one core-message age and must all reach us.
                phase = _editor_move_phase(msg)
                self._request_interactive_capture(
                    phase, aggressive=True)
        except (AttributeError, TypeError, RuntimeError):
            pass
        return gui.GeDialog.Message(self, msg, result)

    def Command(self, cid, msg):
        if cid == ID_START:
            if self.auto_paused:
                pressed_at = time.monotonic()
                self._freeze_render_timer(pressed_at)
                self._stop()
                return True
            if (self.running and
                    (self.recording_active or self.recording_stopping)):
                gui.MessageDialog(
                    "Stop REC and wait for the MP4 before stopping "
                    "the render.")
                return True
            pressed_at = time.monotonic()
            if self.running:
                self._freeze_render_timer(pressed_at)
                self._stop()
            elif self._start():
                self._begin_render_timer(pressed_at)
        elif cid == ID_RESET:
            if self.auto_paused:
                self._resume_auto_paused(
                    time.monotonic(), refresh_source=False)
            elif self.recording_active or self.recording_stopping:
                gui.MessageDialog(
                    "Stop REC and wait for the MP4 before resetting.")
            elif self.render_frames_active or self.sequence_source_active:
                gui.MessageDialog(
                    "Cancel or stop Render Frames before resetting.")
            else:
                self._reset_context()
        elif cid == ID_SAVE_FRAME:
            self._save_frame()
        elif cid == ID_RENDER_FRAMES:
            self._begin_render_frames()
        elif cid == ID_RECORD:
            self._toggle_recording()
        elif cid == ID_LOW_LATENCY:
            self._enable_low_latency()
        elif cid == ID_CAPTURE:
            if self.auto_paused:
                self.last_activity_at = time.monotonic()
                self.auto_resume_pending = True
                self.auto_resume_source_refresh = True
            elif not self.running:
                try:
                    if self._capture_scheduled():
                        self._write_control()
                finally:
                    self._restore_followed_views()
                    self.source_document = None
                    self.view_context_initialized = False
                    self.last_source_view_index = None
                    self.last_source_camera = None
                    self.last_source_projection = None
            else:
                now = time.monotonic()
                self.last_activity_at = now
                self._finish_background_capture()
                if self.capture_thread is not None:
                    self.manual_capture_pending = True
                    self.capture_pending_clean = bool(
                        self.capture_pending_clean or
                        self.GetBool(ID_CLEAN_FEED))
                    self._set_status("Capture queued for the next frame")
                else:
                    self._start_background_capture(
                        now,
                        interactive=self.editor_move_active,
                        clean_feed=self.GetBool(ID_CLEAN_FEED),
                        exact_clean=not self.editor_move_active)
        elif cid == ID_SETTINGS:
            self._open_settings()
        elif cid == ID_REFERENCE_LOAD:
            if self._load_reference():
                self.last_activity_at = time.monotonic()
                if self.auto_paused:
                    self.auto_resume_pending = True
        elif cid == ID_REFERENCE_CLEAR:
            if self._clear_reference():
                self.last_activity_at = time.monotonic()
                if self.auto_paused:
                    self.auto_resume_pending = True
        elif cid in (ID_AUTO, ID_INTERVAL, ID_CLEAN_FEED):
            if cid == ID_AUTO:
                self.Enable(ID_INTERVAL, self.GetBool(ID_AUTO))
            if self.running:
                self.last_activity_at = time.monotonic()
            elif (self.auto_paused and
                  cid in (ID_AUTO, ID_CLEAN_FEED)):
                self.last_activity_at = time.monotonic()
                self.auto_resume_pending = True
                self.auto_resume_source_refresh = bool(
                    self.auto_resume_source_refresh or
                    cid in (ID_AUTO, ID_CLEAN_FEED))
            self.next_capture_at = 0.0
            self._save_settings()
        elif cid == ID_VIEW_MODE:
            view_mode = self.GetInt32(ID_VIEW_MODE)
            self.preview.set_view_mode(view_mode)
            if view_mode != VIEW_AI and self.input_path.is_file():
                self.preview.set_source_image(self.input_path)
            self._save_settings()
        elif cid in (ID_PROMPT, ID_PROMPT_EXPANSION,
                     ID_PRESERVE_COMPOSITION):
            self.last_activity_at = time.monotonic()
            if self.auto_paused:
                self.auto_resume_pending = True
            self._save_settings()
            if self.running:
                self._write_control()
        return True

    def Timer(self, msg):
        if self.timer_busy:
            return
        self.timer_busy = True
        try:
            _drain_deferred_capture_owners()
            return self._timer_tick(msg)
        finally:
            self.timer_busy = False

    def _timer_tick(self, msg):
        self._poll_installer()
        now = time.monotonic()
        self._cleanup_retired_sequence_directories(now)
        self._update_render_time(now)
        self._poll_recording_status()
        if self.auto_paused:
            self._maybe_auto_resume(now)
            return
        if not self.running:
            if self.capture_thread is not None:
                if self._stop_capture_thread():
                    self.render_frames_cancel_requested = False
                    self.render_frames_phase = ""
                    self._restore_followed_views()
                    self._release_stopped_render_frames_resources(
                        delete_directories=self.proc is None)
            return
        if self.proc and self.proc.poll() is not None:
            exit_code = self.proc.poll()
            previous = _read_json(self.state / "status.json", {}).get(
                "message", "no status")
            error = (
                "Worker exited (code %s). Last stage: %s" %
                (exit_code, previous))
            self._freeze_render_timer(now)
            self._stop()
            self._set_status(error)
            return
        if self.render_frames_active:
            self._tick_render_frames(now)
            document = self.render_frames_source_document
            if document is None:
                document = self.source_document
        elif self.sequence_source_active:
            document = self.source_document
        else:
            document = documents.GetActiveDocument()
            previous_document = self.source_document
            source_changed = self._prepare_source_document(document)
            document = self.source_document
            if (source_changed and document is not None and
                    not _same_sdk_object(previous_document, document)):
                # Switching/opening the live source is real user activity.
                self.last_activity_at = now
                self.source_activity_generation += 1
            self._finish_background_capture()
        if now >= self.next_status_poll_at:
            self.next_status_poll_at = now + STATUS_POLL_SECONDS
            status = _read_json(self.state / "status.json", {})
            self.worker_stage = str(status.get("stage") or "")
            if (status.get("message") and
                    not self.render_frames_active and
                    not self.sequence_source_active):
                self._set_status(status["message"])
        self._poll_output(now)

        if self.render_frames_active:
            return
        if self.auto_animation_phase == "recording_start":
            return
        if self.sequence_source_active:
            self._poll_sequence_telemetry(now)
            if self._tick_auto_animation(now):
                return
            if self._check_sequence_start_timeout(now):
                return
            self._maybe_auto_pause(now, document)
            return

        mouse_down = _mouse_buttons_pressed()
        if mouse_down:
            self.last_activity_at = now
        scene_changed = bool(
            document is not None and self._observe_scene_motion(document, now))
        if scene_changed:
            self.last_activity_at = now

        if self._maybe_auto_pause(now, document):
            return

        # Recover when Cinema 4D or a third-party tool drops MOVE_END. This is
        # especially common for editor-camera/SpaceMouse movement under load.
        if (self.editor_move_active and not mouse_down and
                self.last_editor_move_at > 0.0 and
                now - self.last_editor_move_at >=
                INTERACTIVE_LOST_END_SECONDS):
            self.editor_move_active = False
            self.interactive_until = 0.0
            self.capture_pending = False
            self.final_clean_pending = bool(self.GetBool(ID_AUTO))
            self._cancel_background_capture(interactive=True)
            self._retire_interactive_snapshot()

        if (self.interactive_until > 0.0 and
                now >= self.interactive_until and
                mouse_down):
            # Older/fallback message payloads cannot expose MOVE_END. Do not
            # interpret a pause as mouse-up while the button is still held.
            self.interactive_until = now + INTERACTIVE_QUIET_SECONDS
        interaction_active = bool(
            self.editor_move_active or now < self.interactive_until)
        if self.capture_thread is not None:
            return
        if interaction_active:
            if (self.capture_pending and self.capture_thread is None and
                    self.GetBool(ID_AUTO) and
                    now >= self.interactive_next_capture_at):
                self._start_background_capture(
                    now, interactive=True,
                    clean_feed=self.GetBool(ID_CLEAN_FEED),
                    document=document)
            return
        if self.resolution_capture_pending:
            if now >= self.next_capture_at:
                self._start_background_capture(
                    now, interactive=False,
                    clean_feed=self.GetBool(ID_CLEAN_FEED),
                    document=document)
            return
        if self.final_clean_pending and self.GetBool(ID_AUTO):
            self.final_clean_pending = False
            self._start_background_capture(
                now, interactive=False,
                clean_feed=self.GetBool(ID_CLEAN_FEED),
                document=document)
            return
        if self._maybe_auto_resync(now):
            return
        if self.manual_capture_pending:
            self.manual_capture_pending = False
            clean_feed = bool(
                self.capture_pending_clean or self.GetBool(ID_CLEAN_FEED))
            self.capture_pending_clean = False
            self._start_background_capture(
                now, interactive=False, clean_feed=clean_feed,
                exact_clean=True, document=document)
            return
        if self.capture_pending and self.GetBool(ID_AUTO):
            self._start_background_capture(
                now, interactive=False,
                clean_feed=self.GetBool(ID_CLEAN_FEED),
                document=document)
            return
        if (self.GetBool(ID_AUTO) and
                time.monotonic() >= self.next_capture_at):
            if mouse_down and not scene_changed:
                self.next_capture_at = now + UI_TIMER_MS / 1000.0
                return
            if (document is not None and
                    not self._idle_capture_needed(document, now)):
                requested_ms = min(
                    MAX_INTERVAL_MS,
                    max(MIN_INTERVAL_MS, self.GetInt32(ID_INTERVAL)),
                )
                self.next_capture_at = now + requested_ms / 1000.0
                return
            self._start_background_capture(
                now, interactive=False,
                clean_feed=self.GetBool(ID_CLEAN_FEED),
                document=document)

    def AskClose(self):
        if self.recording_active or self.recording_stopping:
            gui.MessageDialog(
                "Stop REC and wait for the MP4 before closing the window.")
            return True
        self._freeze_render_timer()
        self._stop()
        if self.capture_thread is not None:
            _retain_deferred_capture_owner(self)
            self._set_status(
                "Waiting for the viewport render to stop; close was cancelled")
            return True
        if self.proc is not None:
            self._set_status(
                "Could not stop the render worker; close was cancelled")
            return True
        for path in (self.control_path, self.input_path, self.output_path,
                     self.telemetry_path, self.recording_status_path,
                     self.state / "status.json",
                     self.state / "worker.log"):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            self.state.rmdir()
        except OSError:
            pass
        return False

    def DestroyWindow(self):
        # Cinema 4D calls this when a docked layout is replaced, a path which
        # does not necessarily pass through AskClose. Never leave a paid fal.ai
        # realtime session running after the render view disappears.
        try:
            self._freeze_render_timer(update_ui=False)
        finally:
            try:
                self._stop(update_ui=False)
            except Exception:
                pass
            for _attempt in range(2):
                if self.capture_thread is None:
                    break
                if self._stop_capture_thread():
                    self._restore_followed_views()
                    self._release_stopped_render_frames_resources(
                        delete_directories=self.proc is None)
                    break
            if self.capture_thread is not None:
                _retain_deferred_capture_owner(self)


class LucyCommand(plugins.CommandData):
    dialog = None

    def Execute(self, doc):
        _drain_deferred_capture_owners()
        dialog = type(self).dialog
        if dialog is None:
            dialog = LucyDialog()
            type(self).dialog = dialog
        return dialog.Open(c4d.DLG_TYPE_ASYNC, PLUGIN_ID,
                           defaultw=720, defaulth=560)

    def RestoreLayout(self, secret):
        _drain_deferred_capture_owners()
        dialog = type(self).dialog
        if dialog is None:
            dialog = LucyDialog()
            type(self).dialog = dialog
        return dialog.Restore(PLUGIN_ID, secret)


def PluginMessage(message_id, data):
    """Release threads and the paid realtime session before plugin teardown."""
    shutdown_ids = {
        getattr(c4d, "C4DPL_ENDACTIVITY", None),
        getattr(c4d, "C4DPL_SHUTDOWNTHREADS", None),
        getattr(c4d, "C4DPL_RELOADPYTHONPLUGINS", None),
    }
    shutdown_ids.discard(None)
    if message_id not in shutdown_ids:
        return False
    strict_shutdown_ids = {
        getattr(c4d, "C4DPL_SHUTDOWNTHREADS", None),
        getattr(c4d, "C4DPL_RELOADPYTHONPLUGINS", None),
    }
    strict_shutdown_ids.discard(None)
    must_drain_capture = message_id in strict_shutdown_ids

    dialog = LucyCommand.dialog
    if dialog is not None:
        try:
            try:
                try:
                    dialog._freeze_render_timer(update_ui=False)
                finally:
                    try:
                        dialog._stop(update_ui=False)
                    finally:
                        close = getattr(dialog, "Close", None)
                        if callable(close):
                            close()
            except Exception:
                pass
        finally:
            if dialog.capture_thread is not None:
                _retain_deferred_capture_owner(dialog)
    if must_drain_capture:
        _drain_all_capture_owners_for_shutdown()
    else:
        _drain_deferred_capture_owners()
    if (dialog is not None and
            dialog not in _DEFERRED_CAPTURE_OWNERS):
        LucyCommand.dialog = None
    return True


if __name__ == "__main__":
    plugins.RegisterCommandPlugin(PLUGIN_ID, PLUGIN_NAME, 0, None,
                                  "AI realtime viewport renderer", LucyCommand())
