"""Triple Vertical Player - desktop wrapper (1x3 synced video wall).

Runs triple-vertical-player/index.html inside a native window using
pywebview (Edge WebView2 on Windows).

Notes:
- File picking uses the page's own <input type=file> / webkitdirectory
  inputs: WebView2 shows its native picker for those, no permission popups.
  (pywebview's Window.create_file_dialog does not exist on the
  edgechromium backend, so dialogs are NOT routed through Python.)
- Videos passed on the command line (drag onto exe / "Open with") auto-load
  via Api.get_startup, streamed from Python over a local http://127.0.0.1
  server with Range-request support so seeking works.

Build exe (Windows):
    pip install pywebview pyinstaller
    pyinstaller --noconfirm --onefile --windowed --name TripleVerticalPlayer --add-data "index.html;." app.py
    -> dist/TripleVerticalPlayer.exe
"""
import hashlib
import itertools
import mimetypes
import os
import pathlib
import queue
import sys
import threading
import time
from multiprocessing.connection import Client, Listener
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

try:
    import webview
except ImportError:  # friendly message when deps are missing
    print("pywebview is not installed. Run: pip install pywebview")
    sys.exit(1)

VIDEO_EXTS = (
    ".mp4", ".m4v", ".webm", ".mov", ".mkv", ".avi",
    ".mpg", ".mpeg", ".ogv", ".flv", ".wmv", ".ts",
    ".m2ts", ".mts", ".3gp",
)
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
MIME_OVERRIDES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".mkv": "video/x-matroska",
    ".m2ts": "video/mp2t",
    ".mts": "video/mp2t",
    ".ts": "video/mp2t",
    ".ogv": "video/ogg",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
}

_USER_ID = os.environ.get("USERNAME") or os.environ.get("USER") or str(pathlib.Path.home())
_INSTANCE_SUFFIX = hashlib.sha256(_USER_ID.casefold().encode("utf-8")).hexdigest()[:16]
_INSTANCE_MUTEX_NAME = f"Local\\TripleVerticalPlayer-{_INSTANCE_SUFFIX}"
_OPEN_PIPE_NAME = rf"\\.\pipe\TripleVerticalPlayer-{_INSTANCE_SUFFIX}"
_OPEN_PIPE_AUTHKEY = hashlib.sha256(
    f"TripleVerticalPlayer:{_INSTANCE_SUFFIX}".encode("ascii")
).digest()
_PENDING_OPEN_REQUESTS = queue.Queue()
_OPEN_REQUEST_LISTENER = None
_INSTANCE_MUTEX_HANDLE = None

# token -> absolute Path, served at /v/<token>
_registry = {}
_token_seq = itertools.count(1)


def register(path: pathlib.Path) -> str:
    token = str(next(_token_seq))
    _registry[token] = path
    return token


def _command_line_paths(argv=None):
    """Absolute non-option paths supplied by Open With / drag-and-drop."""
    args = sys.argv[1:] if argv is None else argv
    paths = []
    for arg in args:
        value = str(arg).strip()
        if not value or value.startswith("-"):
            continue
        try:
            paths.append(str(pathlib.Path(value).expanduser().resolve()))
        except (OSError, RuntimeError):
            continue
    return paths


def _claim_single_instance():
    """Return (is_primary, handle), using a per-user Windows mutex."""
    if os.name != "nt":
        return True, None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        create_mutex.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = create_mutex(None, False, _INSTANCE_MUTEX_NAME)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.get_last_error() != 183, handle
    except Exception as e:
        _dbg("single-instance mutex failed %r" % e)
        return None, None


def _start_open_request_server():
    """Listen for paths from later launches and queue them for the UI."""
    global _OPEN_REQUEST_LISTENER
    if os.name != "nt":
        return True
    try:
        listener = Listener(
            _OPEN_PIPE_NAME, family="AF_PIPE", authkey=_OPEN_PIPE_AUTHKEY
        )
    except Exception as e:
        _dbg("open-request listener failed %r" % e)
        return False
    _OPEN_REQUEST_LISTENER = listener

    def _serve():
        while True:
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                return
            try:
                payload = connection.recv()
                if not isinstance(payload, list):
                    connection.send(False)
                    continue
                paths = [str(p) for p in payload if str(p).strip()]
                _PENDING_OPEN_REQUESTS.put(paths)
                connection.send(True)
            except Exception as e:
                _dbg("open-request handoff failed %r" % e)
            finally:
                try:
                    connection.close()
                except Exception:
                    pass

    threading.Thread(target=_serve, name="OpenRequestPipe", daemon=True).start()
    return True


def _forward_open_request(paths, timeout=8.0):
    """Send file paths to the active player instead of starting another UI."""
    if os.name != "nt":
        return False
    deadline = time.monotonic() + timeout
    while True:
        try:
            connection = Client(
                _OPEN_PIPE_NAME, family="AF_PIPE", authkey=_OPEN_PIPE_AUTHKEY
            )
            try:
                connection.send(paths)
                return bool(connection.recv())
            finally:
                connection.close()
        except (OSError, EOFError, ValueError) as e:
            if time.monotonic() >= deadline:
                _dbg("open-request forward timed out %r" % e)
                return False
            time.sleep(0.1)


def _normalize_dialog_result(result):
    """Normalize pywebview create_file_dialog output across versions.

    Modern pywebview returns a tuple of paths (or None when cancelled),
    but older releases returned a plain string for FOLDER/SAVE dialogs.
    Iterating a string yields single characters, which silently finds no
    videos — exactly the "Open folder shows nothing" bug. Always return
    a list of Paths (empty list == cancelled).
    """
    if result is None:
        return []
    if isinstance(result, (str, bytes, pathlib.Path)):
        result = [result]
    paths = []
    for r in result:
        try:
            if isinstance(r, bytes):
                r = r.decode("utf-8", errors="replace")
            if isinstance(r, pathlib.Path):
                paths.append(r)
            else:
                s = str(r).strip()
                if s:
                    paths.append(pathlib.Path(s))
        except Exception:
            continue
    return paths


def video_files_in(folder: pathlib.Path, recursive=True):
    """All playable videos in a folder, sorted A-Z.

    recursive=True (default): scans subfolders too via rglob, so a folder
    containing only subfolders full of videos still queues them. The old
    top-level-only scan returned [] for such folders, making "Open folder"
    look broken while "Open video(s)" (pick files directly) worked.
    """
    try:
        folder = pathlib.Path(folder)
        if not folder.is_dir():
            return []
        if recursive:
            entries = folder.rglob("*")
        else:
            entries = folder.iterdir()
    except OSError:
        return []
    vids = []
    try:
        for p in entries:
            try:
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                    vids.append(p)
            except OSError:
                continue
    except OSError:
        pass
    try:
        base = folder.resolve()
    except OSError:
        base = folder
    def _key(p):
        try:
            return p.relative_to(base).as_posix().lower()
        except ValueError:
            return p.name.lower()
    return sorted(vids, key=_key)


def photo_files_in(folder: pathlib.Path, recursive=True):
    """All supported original photos in a folder, including subfolders."""
    try:
        folder = pathlib.Path(folder)
        if not folder.is_dir():
            return []
        entries = folder.rglob("*") if recursive else folder.iterdir()
    except OSError:
        return []
    photos = []
    try:
        for path in entries:
            try:
                if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
                    photos.append(path)
            except OSError:
                continue
    except OSError:
        pass
    try:
        base = folder.resolve()
    except OSError:
        base = folder
    def _key(path):
        try:
            return path.relative_to(base).as_posix().casefold()
        except ValueError:
            return path.name.casefold()
    return sorted(photos, key=_key)


class VideoHandler(BaseHTTPRequestHandler):
    server_version = "TripleWall/1.0"

    def log_message(self, *args):  # keep the console clean
        pass

    def _send_file(self, head_only=False):
        token = unquote(urlsplit(self.path).path)
        if not token.startswith("/v/"):
            self.send_error(404)
            return
        path = _registry.get(token[3:])
        if path is None or not path.is_file():
            self.send_error(404)
            return
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404)
            return
        ctype = MIME_OVERRIDES.get(
            path.suffix.lower(),
            mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        )
        start, end = 0, size - 1
        ranged = False
        range_h = self.headers.get("Range")
        if range_h and range_h.startswith("bytes="):
            try:
                spec = range_h[6:].split(",")[0].strip()
                if spec.startswith("-"):
                    start = max(0, size - int(spec[1:]))
                else:
                    a, _, b = spec.partition("-")
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                if 0 <= start < size and start <= end:
                    ranged = True
                else:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            except (ValueError, IndexError):
                start, end = 0, size - 1
        length = end - start + 1
        try:
            self.send_response(206 if ranged else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if head_only:
                return
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass  # user skipped to another video; harmless

    def do_GET(self):
        self._send_file(head_only=False)

    def do_HEAD(self):
        self._send_file(head_only=True)


def _dbg(msg):
    try:
        import tempfile
        import os
        log = os.path.join(tempfile.gettempdir(), "TripleVerticalPlayer.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _msgbox(text, title="Triple Vertical Player"):
    """Show a native Windows message box. Works even in --windowed exe
    (no console), so a missing WebView2 / startup failure is visible
    instead of a silent 'nothing happens' double-click."""
    try:
        import ctypes
        MB_ICONWARNING = 0x30
        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), MB_ICONWARNING)
    except Exception:
        try:
            print(text)
        except Exception:
            pass


def _webview2_installed() -> bool:
    """True if the Edge WebView2 Runtime is present.

    Checks (in order): registry key, loader DLL, WebView2 folder.
    Missing runtime is the #1 reason the exe 'does nothing' on
    another laptop (it is preinstalled on Win11 + updated Win10,
    but not on fresh/lite Windows builds).
    """
    import os
    # 1. Registry (system + user level)
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (
                r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            ):
                try:
                    with winreg.OpenKey(root, sub):
                        return True
                except OSError:
                    continue
    except Exception:
        pass
    # 2. Runtime folder
    for p in (
        r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
        r"C:\Program Files\Microsoft\EdgeWebView\Application",
    ):
        try:
            if os.path.isdir(p):
                return True
        except Exception:
            continue
    # 3. Client DLL loadable
    try:
        import ctypes
        from ctypes import wintypes  # noqa: F401
        for dll in ("WebView2Loader.dll", "WebView2LoaderStatic.dll"):
            try:
                ctypes.WinDLL(dll)
                return True
            except OSError:
                continue
    except Exception:
        pass
    return False


def _ensure_webview2() -> bool:
    """If WebView2 is missing, tell the user plainly and open the
    download page. Returns True when it is safe to start."""
    if _webview2_installed():
        return True
    _msgbox(
        "Microsoft Edge WebView2 Runtime was not found on this PC.\n\n"
        "The player needs it once (free, ~2 min, from Microsoft).\n"
        "Your browser will now open the download page — install "
        "\"WebView2 Runtime\", then start Triple Vertical Player again.",
        "Triple Vertical Player - WebView2 needed (one-time)",
    )
    try:
        import webbrowser
        webbrowser.open(
            "https://developer.microsoft.com/microsoft-edge/webview2/"
            "#download-section"
        )
    except Exception:
        pass
    return False


def _force_dark_chrome(form):
    """Black title bar + black window border, always (even on light Windows
    theme). A white OS title bar/border reads as 'white edges' around the
    player, and pywebview's exit-fullscreen path resets the border to the
    light default — so re-apply after every toggle too."""
    try:
        from webview.platforms import winforms as _wf
        hwnd = form.Handle.ToInt32()
        _wf.DwmSetWindowAttribute(hwnd, 20, 1)        # immersive dark title bar
        _wf.DwmSetWindowAttribute(hwnd, 38, 2)        # dark mode (20H1+)
        _wf.DwmSetWindowAttribute(hwnd, 34, 0x000000)  # black border, never white
        _dbg("force_dark_chrome OK hwnd=%s" % hwnd)
    except Exception as e:
        _dbg("force_dark_chrome FAIL %r" % e)


def _patch_dark_chrome():
    try:
        from webview.platforms import winforms as _wf
        # pywebview 6.x nests the real Form as BrowserView.BrowserForm
        # (older releases exposed it as BrowserForm directly).
        view = getattr(_wf, "BrowserView", None)
        form_cls = getattr(view, "BrowserForm", None) or getattr(_wf, "BrowserForm", None)
        if form_cls is not None:
            form_cls.update_title_bar_theme = (
                lambda self: _force_dark_chrome(self)
            )
            _dbg("patch applied to %r" % (form_cls,))
        else:
            _dbg("patch FAIL: no form class found")
    except Exception as e:
        _dbg("patch FAIL %r" % e)


class Api:
    """Called from JS (pywebview runs these on a worker thread).

    - get_startup / get_pending_open: package startup and forwarded paths.
    - toggle_fullscreen: public Window.toggle_fullscreen plus a fullscreen
      gap guard (over-cover + black chrome) marshalled to the UI thread.
    - pick_files / pick_photo / pick_folder / pick_photo_folder: native Windows
      file dialogs via Window.create_file_dialog. The HTML <input type=file>
      pickers remain as a browser fallback.
    """

    def __init__(self):
        self.base = ""

    def _pack(self, paths, index=0):
        urls, names = [], []
        for p in paths:
            p = pathlib.Path(p)
            if p.is_file():
                urls.append(f"{self.base}/v/{register(p.resolve())}")
                names.append(p.name)
        return {"urls": urls, "names": names, "index": max(0, min(index, len(urls) - 1)) if urls else 0}

    def toggle_fullscreen(self):
        wins = webview.windows
        if not wins:
            return False
        try:
            # Public Window API (works across pywebview versions/backends).
            wins[0].toggle_fullscreen()
        except Exception:
            return False
        try:
            form = getattr(wins[0], "native", None)
            if form is None:
                return False
            on = bool(getattr(form, "is_fullscreen", False))
        except Exception:
            return False
        # Fullscreen gap guard: over-cover the monitor by 4px on every side.
        # If DPI/monitor-bounds rounding leaves even a 1px gap, the (usually
        # light) desktop shows through as a "white edge" — oversizing buries
        # that possibility. Also keep chrome black on the way out, because
        # pywebview's exit path resets the border to the light OS default.
        try:
            from webview.platforms import winforms as _wf

            def _fix():
                try:
                    if on:
                        form.TopMost = True
                        form.WindowState = _wf.WinForms.FormWindowState.Normal
                        scr = _wf.WinForms.Screen.FromControl(form).Bounds
                        form.Location = _wf.Point(scr.X - 4, scr.Y - 4)
                        form.Size = _wf.Size(scr.Width + 8, scr.Height + 8)
                        _dbg("fs ENTER bounds=%s over-cover applied" % (scr,))
                    else:
                        form.TopMost = False
                        _dbg("fs EXIT")
                    _force_dark_chrome(form)
                except Exception as e:
                    _dbg("fs fixup FAIL %r" % e)

            if form.InvokeRequired:
                from System import Action
                form.Invoke(Action(_fix))
            else:
                _fix()
        except Exception:
            pass
        return on

    def pick_files(self):
        """Native Open-video dialog. Returns packed http urls or None."""
        try:
            wins = webview.windows
            if not wins:
                return None
            file_types = (
                "Video files (*.mp4;*.m4v;*.webm;*.mov;*.mkv;*.avi;*.mpg;*.mpeg;*.ogv;*.flv;*.wmv;*.ts;*.m2ts;*.mts;*.3gp)",
                "All files (*.*)",
            )
            result = wins[0].create_file_dialog(
                webview.FileDialog.OPEN, allow_multiple=True, file_types=file_types
            )
        except Exception:
            return None
        paths = _normalize_dialog_result(result)
        if not paths:
            return None  # user cancelled
        vids = [p for p in paths if p.suffix.lower() in VIDEO_EXTS]
        if not vids:
            return {"urls": [], "names": [], "index": 0, "error": "no-videos"}
        if len(vids) == 1 and vids[0].is_file():
            # Single file picked: queue the whole folder so ⏮ ⏭ / N / P
            # step through sibling videos (same as drag-onto-exe behavior).
            folder = vids[0].parent
            sibs = video_files_in(folder)
            if sibs:
                try:
                    idx = sibs.index(vids[0].resolve())
                except ValueError:
                    # sibs holds unresolved paths; compare by resolved string
                    try:
                        want = str(vids[0].resolve()).lower()
                        idx = next(
                            i for i, s in enumerate(sibs)
                            if str(s.resolve()).lower() == want
                        )
                    except (ValueError, StopIteration, OSError):
                        idx = 0
                return self._pack(sibs, idx)
        return self._pack(sorted(vids, key=lambda p: p.name.lower()), 0)

    def pick_photo(self):
        """Pick one original photo and serve it without modifying its bytes."""
        try:
            wins = webview.windows
            if not wins:
                return None
            file_types = (
                "Image files (*.jpg;*.jpeg;*.png;*.webp;*.gif;*.bmp;*.avif)",
                "All files (*.*)",
            )
            result = wins[0].create_file_dialog(
                webview.FileDialog.OPEN, allow_multiple=False, file_types=file_types
            )
        except Exception:
            return None
        paths = _normalize_dialog_result(result)
        if not paths:
            return None  # user cancelled
        photos = [p for p in paths if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
        if not photos:
            return {"urls": [], "names": [], "index": 0, "error": "no-photos"}
        return self._pack(photos[:1], 0)

    def pick_photo_folder(self):
        """Native Open-photo-folder dialog, including nested folders."""
        try:
            wins = webview.windows
            if not wins:
                return None
            try:
                result = wins[0].create_file_dialog(
                    webview.FileDialog.FOLDER, allow_multiple=True
                )
            except TypeError:
                result = wins[0].create_file_dialog(webview.FileDialog.FOLDER)
        except Exception as e:
            _dbg("pick_photo_folder dialog FAIL %r" % (e,))
            return None
        raw = _normalize_dialog_result(result)
        if not raw:
            return None
        paths, picked = [], []
        for item in raw:
            folder = pathlib.Path(item)
            try:
                if folder.is_dir():
                    picked.append(str(folder))
                    paths.extend(photo_files_in(folder, recursive=True))
                elif folder.is_file() and folder.suffix.lower() in IMAGE_EXTS:
                    paths.append(folder)
            except OSError:
                continue
        seen, unique = set(), []
        for path in sorted(paths, key=lambda p: str(p).casefold()):
            try:
                key = str(path.resolve()).casefold()
            except OSError:
                key = str(path).casefold()
            if key not in seen:
                seen.add(key)
                unique.append(path)
        if not unique:
            return {
                "urls": [],
                "names": [],
                "index": 0,
                "error": "no-photos",
                "folder": "; ".join(picked) if picked else "",
            }
        return self._pack(unique, 0)

    def pick_folder(self):
        """Native Open-folder dialog. Returns packed http urls or None.

        Scans recursively (subfolders included) and tolerates old/new
        pywebview return shapes (tuple vs plain string). Returns an
        {"error": "no-videos"} dict — instead of silent None — when the
        folder was picked but contains no playable videos, so the UI can
        tell the user instead of sitting on a black screen.
        """
        try:
            wins = webview.windows
            if not wins:
                return None
            try:
                result = wins[0].create_file_dialog(
                    webview.FileDialog.FOLDER, allow_multiple=True
                )
            except TypeError:
                # Old pywebview (<6): FOLDER dialog takes no allow_multiple.
                result = wins[0].create_file_dialog(webview.FileDialog.FOLDER)
        except Exception as e:
            try:
                _dbg("pick_folder dialog FAIL %r" % (e,))
            except Exception:
                pass
            return None
        raw = _normalize_dialog_result(result)
        try:
            _dbg("pick_folder raw=%r" % ([str(p) for p in raw],))
        except Exception:
            pass
        if not raw:
            return None  # user cancelled
        paths = []
        picked = []
        for f in raw:
            folder = pathlib.Path(f)
            try:
                if folder.is_dir():
                    picked.append(str(folder))
                    found = video_files_in(folder, recursive=True)
                    try:
                        _dbg("pick_folder scan %s -> %d videos" % (folder, len(found)))
                    except Exception:
                        pass
                    paths.extend(found)
                elif folder.is_file() and folder.suffix.lower() in VIDEO_EXTS:
                    paths.append(folder)
            except OSError:
                continue
        if not paths:
            return {
                "urls": [],
                "names": [],
                "index": 0,
                "error": "no-videos",
                "folder": "; ".join(picked) if picked else "",
            }
        # Dedupe (same file via two picked folders / symlinks) then sort.
        seen, uniq = set(), []
        for p in sorted(paths, key=lambda p: p.name.lower()):
            try:
                key = str(p.resolve()).lower()
            except OSError:
                key = str(p).lower()
            if key not in seen:
                seen.add(key)
                uniq.append(p)
        return self._pack(uniq, 0)

    def _pack_startup_paths(self, raw_paths):
        """Resolve files/folders passed on the command line or by IPC."""
        args = [pathlib.Path(a) for a in raw_paths if not str(a).startswith("-")]
        if not args:
            return None
        if len(args) == 1 and args[0].is_file():
            # Queue the whole folder so it plays continuously from this file.
            folder = args[0].parent
            vids = video_files_in(folder)
            if vids:
                try:
                    idx = vids.index(args[0].resolve())
                except ValueError:
                    try:
                        want = str(args[0].resolve()).lower()
                        idx = next(
                            i for i, s in enumerate(vids)
                            if str(s.resolve()).lower() == want
                        )
                    except (ValueError, StopIteration, OSError):
                        idx = 0
                return self._pack(vids, idx)
            return self._pack([args[0]], 0)
        paths = []
        for a in args:
            if a.is_file():
                paths.append(a)
            elif a.is_dir():
                paths.extend(video_files_in(a, recursive=True))
        if not paths:
            return None
        return self._pack(paths, 0)

    def get_startup(self):
        """Files/folders passed on the command line (drag onto exe / Open with)."""
        return self._pack_startup_paths(_command_line_paths())

    def get_pending_open(self):
        """Drain file-open requests forwarded from later app launches."""
        try:
            paths = _PENDING_OPEN_REQUESTS.get_nowait()
        except queue.Empty:
            return None
        while True:
            try:
                paths.extend(_PENDING_OPEN_REQUESTS.get_nowait())
            except queue.Empty:
                break
        return self._pack_startup_paths(paths)


def find_index_html() -> pathlib.Path:
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(pathlib.Path(sys._MEIPASS) / "index.html")
    here = pathlib.Path(__file__).resolve().parent
    candidates.append(here / "index.html")
    candidates.append(pathlib.Path.cwd() / "index.html")
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(
        'index.html not found next to app.py. Keep index.html beside app.py '
        'or rebuild the exe with --add-data "index.html;."'
    )


def main() -> None:
    global _INSTANCE_MUTEX_HANDLE
    is_primary, mutex_handle = _claim_single_instance()
    if is_primary is None:
        _msgbox(
            "Could not check whether Triple Vertical Player is already running.\n\n"
            "The player was not opened to avoid creating a duplicate window.",
            "Triple Vertical Player - startup failed",
        )
        raise SystemExit(1)
    if not is_primary:
        if not _forward_open_request(_command_line_paths()):
            _msgbox(
                "Triple Vertical Player is already running, but could not receive "
                "the selected video. Keep the existing player open and try again.",
                "Triple Vertical Player - open failed",
            )
        return
    _INSTANCE_MUTEX_HANDLE = mutex_handle
    if not _start_open_request_server():
        _msgbox(
            "Could not start the single-window file-open service.\n\n"
            "The player was not opened to avoid duplicate windows.",
            "Triple Vertical Player - startup failed",
        )
        raise SystemExit(1)

    # On other laptops a missing WebView2 used to look like "exe does
    # nothing" (windowed exe = no console = silent crash). Check first
    # and guide the user instead.
    try:
        if sys.platform.startswith("win") and not _ensure_webview2():
            sys.exit(2)
    except SystemExit:
        raise
    except Exception:
        pass  # never block startup on the check itself

    server = ThreadingHTTPServer(("127.0.0.1", 0), VideoHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()

    api = Api()
    api.base = f"http://127.0.0.1:{port}"

    try:
        index = find_index_html()
    except FileNotFoundError as e:
        _msgbox(str(e), "Triple Vertical Player - file missing")
        raise SystemExit(1)
    _patch_dark_chrome()  # black title bar + border even on light Windows theme
    webview.create_window(
        "Triple Vertical Player - 9:16 x3 Synced",
        url=index.as_uri(),
        width=1280,
        height=860,
        resizable=True,
        text_select=False,
        js_api=api,
        background_color="#000000",  # WebView2 + window chrome stay black: no white edge lines
    )
    if "--test-fs" in sys.argv:
        # TEST HOOK (not used in normal runs): enter fullscreen once shown,
        # so automated checks can photograph the fullscreen wall.
        def _auto_fs(*a):
            try:
                _dbg("auto-fs toggle ENTER...")
                api.toggle_fullscreen()
                import time as _t
                _t.sleep(2)
                _dbg("auto-fs toggle EXIT...")
                api.toggle_fullscreen()
            except Exception as e:
                _dbg("auto-fs FAIL %r" % e)

        webview.windows[0].events.shown += _auto_fs
    try:
        webview.start(gui="edgechromium", debug=False)
    except Exception as e:
        _msgbox(
            "Could not start the video window.\n\n"
            f"Details: {e}\n\n"
            "Most common fix: install the free Microsoft Edge WebView2 "
            "Runtime, then try again.",
            "Triple Vertical Player - startup failed",
        )
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # last resort: never die silently in windowed exe
        try:
            _msgbox(f"Unexpected error:\n\n{e}",
                      "Triple Vertical Player - error")
        except Exception:
            pass
        raise
