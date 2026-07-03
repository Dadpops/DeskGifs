"""
DeskGifs - a frameless, transparent, always-on-top desktop animation widget.

Loops an animated GIF / WebP / APNG anywhere on your desktop, with a small
control panel to resize, change opacity, crop, add a frame, remove a baked-in
background color, and save named loadouts. Settings persist in settings.json.

On the animation itself:
  - Left-click drag   : move it
  - Mouse wheel       : resize
  - Right-click       : context menu (show controls / quit)

Uses Pillow + NumPy to decode/process frames and PyQt6 to display them.
"""

import ctypes
import json
import os
import sys
from collections import Counter

# Configure the Win32 SetWindowPos signature so 64-bit window handles aren't
# truncated to 32 bits (the default ctypes int), which would make it fail
# silently on the wrong window.
if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    _HWND_TOPMOST = wintypes.HWND(-1)
    _HWND_NOTOPMOST = wintypes.HWND(-2)
    _SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE

import numpy as np
from PIL import Image, ImageSequence, ImageDraw
from PyQt6.QtCore import Qt, QTimer, QSharedMemory
from PyQt6.QtGui import QImage, QPixmap, QAction
from PyQt6.QtWidgets import (
    QApplication, QLabel, QMenu, QWidget, QVBoxLayout, QHBoxLayout,
    QSlider, QPushButton, QFileDialog, QGroupBox, QComboBox, QCheckBox,
    QListWidget, QInputDialog,
)


# ---------------------------------------------------------------------------
# Configuration - defaults used the first time you run (before settings.json
# exists). After that, your live changes are saved and reused automatically.
# ---------------------------------------------------------------------------

ANIMATION_PATH = "robin_dance.gif"  # "" = start with no image
START_SIZE = 200        # longest side in px; None = the file's native size
START_OPACITY = 1.0     # 0.1 - 1.0
START_FRAME = "None"    # see FRAME_STYLES below

# ---------------------------------------------------------------------------

# When bundled by PyInstaller, files live next to the .exe, not in the temp
# unpack dir - so keep settings.json / the gif beside the executable.
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "settings.json")

FRAME_STYLES = ["None", "Thin line", "Thick line", "Black line",
                "Rounded", "Rounded thick", "Double line", "Corners"]

# Keys that make up a single "loadout" (a saved snapshot of the widget).
STATE_KEYS = ["image_path", "size", "opacity", "crop", "frame",
              "pos", "remove_bg", "bg_tol"]


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        print(f"Could not save settings: {e}")


# ---------------------------------------------------------------------------
# Frame decoding & image processing
# ---------------------------------------------------------------------------

def load_source_frames(path):
    """Decode an animated image into original RGBA PIL frames + durations (ms)."""
    img = Image.open(path)
    frames, durations = [], []
    for frame in ImageSequence.Iterator(img):
        frames.append(frame.convert("RGBA"))
        d = int(frame.info.get("duration", 100))
        durations.append(d if d > 0 else 100)
    if not frames:
        raise ValueError(f"No frames decoded from {path!r}")
    return frames, durations


def detect_bg_color(arr):
    """Most common of the four corner pixels (RGB)."""
    corners = [tuple(arr[0, 0, :3]), tuple(arr[0, -1, :3]),
               tuple(arr[-1, 0, :3]), tuple(arr[-1, -1, :3])]
    return Counter(corners).most_common(1)[0][0]

def remove_background(im, tol, bg_color=None):
    """Make pixels close to the background color transparent (color keying)."""
    arr = np.array(im)
    if arr.shape[2] < 4:
        return im
    rgb = arr[:, :, :3].astype(np.int32)  # int32 avoids overflow in the square
    bg = np.array(bg_color if bg_color else detect_bg_color(arr), dtype=np.int32)
    dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2))
    arr[dist <= tol, 3] = 0
    return Image.fromarray(arr, "RGBA")


WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)


def apply_frame(im, style):
    """Return a new RGBA image with a decorative frame; background stays clear."""
    if style == "None" or style not in FRAME_STYLES:
        return im

    m = 8  # margin the frame lives in
    w, h = im.size
    canvas = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
    canvas.paste(im, (m, m), im)
    d = ImageDraw.Draw(canvas)
    W, H = canvas.width, canvas.height

    if style == "Thin line":
        d.rectangle([2, 2, W - 3, H - 3], outline=WHITE, width=3)
    elif style == "Thick line":
        d.rectangle([4, 4, W - 5, H - 5], outline=WHITE, width=7)
    elif style == "Black line":
        d.rectangle([2, 2, W - 3, H - 3], outline=BLACK, width=3)
    elif style == "Rounded":
        d.rounded_rectangle([2, 2, W - 3, H - 3], radius=14, outline=WHITE, width=3)
    elif style == "Rounded thick":
        d.rounded_rectangle([4, 4, W - 5, H - 5], radius=18, outline=WHITE, width=7)
    elif style == "Double line":
        d.rectangle([1, 1, W - 2, H - 2], outline=WHITE, width=2)
        d.rectangle([7, 7, W - 8, H - 8], outline=WHITE, width=2)
    elif style == "Corners":
        length = max(10, min(W, H) // 4)
        bw = 4
        for cx, cy, dx, dy in [(2, 2, 1, 1), (W - 3, 2, -1, 1),
                               (2, H - 3, 1, -1), (W - 3, H - 3, -1, -1)]:
            d.line([cx, cy, cx + dx * length, cy], fill=WHITE, width=bw)
            d.line([cx, cy, cx, cy + dy * length], fill=WHITE, width=bw)

    return canvas


def pil_to_pixmap(im):
    """Convert an RGBA PIL image to a QPixmap (copy detaches the buffer)."""
    im = im.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    qimg = QImage(data, im.width, im.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class DeskGif(QLabel):
    def __init__(self, settings):
        super().__init__()
        self._src_frames = []
        self._durations = []
        self._pixmaps = []
        self._index = 0
        self._drag_offset = None
        self._ready = False

        self._path = settings.get("image_path", ANIMATION_PATH)
        self._max_dim = settings.get("size", START_SIZE)
        self._crop = settings.get("crop", [0.0, 0.0, 0.0, 0.0])
        self._frame = settings.get("frame", START_FRAME)
        self._pos = settings.get("pos", None)
        self._remove_bg = settings.get("remove_bg", False)
        self._bg_tol = settings.get("bg_tol", 40)
        self._loadouts = settings.get("loadouts", {})

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(settings.get("opacity", START_OPACITY))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next_frame)

        # The Windows taskbar is also a topmost window, so a plain always-on-top
        # hint can still lose the z-order to it. Periodically re-raise so the
        # widget stays in front of everything, including the taskbar.
        self._top_timer = QTimer(self)
        self._top_timer.timeout.connect(self._keep_on_top)
        self._top_timer.start(300)

    def showEvent(self, event):
        super().showEvent(event)
        self._keep_on_top()  # assert top-most immediately, not after a tick

    def _keep_on_top(self):
        if not self.isVisible():
            return
        if sys.platform == "win32":
            try:
                # A single HWND_TOPMOST call re-stacks us to the top of the
                # topmost band (above the taskbar) without ever dropping out of
                # it - so it comes back on top with no flicker. (The earlier
                # NOTOPMOST->TOPMOST toggle is what caused the flicker.)
                hwnd = wintypes.HWND(int(self.winId()))
                _user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_FLAGS)
                return
            except Exception:
                pass
        self.raise_()

    # --- loading / rendering ----------------------------------------------

    def load(self, path):
        self._src_frames, self._durations = load_source_frames(path)
        self._path = path
        self._index = 0
        self.rebuild()
        self.save()

    def rebuild(self):
        if not self._src_frames:
            return
        self._pixmaps = []
        for im in self._src_frames:
            work = im
            if self._remove_bg:
                work = remove_background(work, self._bg_tol)

            w, h = work.width, work.height
            l, t, r, b = self._crop
            box = (int(w * l), int(h * t), int(w * (1 - r)), int(h * (1 - b)))
            if box[2] - box[0] >= 1 and box[3] - box[1] >= 1:
                work = work.crop(box)

            if self._max_dim is not None:
                cw, ch = work.width, work.height
                scale = self._max_dim / max(cw, ch)
                new_size = (max(1, round(cw * scale)), max(1, round(ch * scale)))
                work = work.resize(new_size, Image.LANCZOS)

            work = apply_frame(work, self._frame)
            self._pixmaps.append(pil_to_pixmap(work))

        self._index %= len(self._pixmaps)
        pixmap = self._pixmaps[self._index]
        self.resize(pixmap.size())
        self.setPixmap(pixmap)
        self._timer.start(self._durations[self._index])

    def _next_frame(self):
        if not self._pixmaps:
            return
        self._index = (self._index + 1) % len(self._pixmaps)
        self.setPixmap(self._pixmaps[self._index])
        self._timer.start(self._durations[self._index])

    # --- persistence & loadouts -------------------------------------------

    def current_state(self):
        return {
            "image_path": self._path,
            "size": self._max_dim,
            "opacity": round(self.windowOpacity(), 3),
            "crop": self._crop,
            "frame": self._frame,
            "pos": [self.x(), self.y()],
            "remove_bg": self._remove_bg,
            "bg_tol": self._bg_tol,
        }

    def save(self):
        if not self._ready:
            return
        data = self.current_state()
        data["loadouts"] = self._loadouts
        save_settings(data)

    def apply_state(self, state):
        """Restore a full snapshot (used by loadouts and startup)."""
        self._max_dim = state.get("size", self._max_dim)
        self._crop = state.get("crop", [0.0, 0.0, 0.0, 0.0])
        self._frame = state.get("frame", "None")
        self._remove_bg = state.get("remove_bg", False)
        self._bg_tol = state.get("bg_tol", 40)
        self.setWindowOpacity(state.get("opacity", 1.0))

        path = state.get("image_path")
        if path:
            resolved = path if os.path.isabs(path) else os.path.join(HERE, path)
            if os.path.exists(resolved):
                self._src_frames, self._durations = load_source_frames(resolved)
                self._path = path
                self._index = 0

        self.rebuild()
        self.show()
        pos = state.get("pos")
        if pos:
            self.move(int(pos[0]), int(pos[1]))
        self.save()

    def restore_position(self):
        if self._pos:
            self.move(int(self._pos[0]), int(self._pos[1]))

    # --- runtime setters (called by the control panel) --------------------

    def set_size(self, max_dim):
        self._max_dim = max_dim
        self.rebuild()
        self.save()

    def set_opacity(self, value):
        self.setWindowOpacity(value)
        self.save()

    def set_crop(self, l, t, r, b):
        self._crop = [l, t, r, b]
        self.rebuild()
        self.save()

    def set_frame(self, style):
        self._frame = style
        self.rebuild()
        self.save()

    def set_remove_bg(self, enabled):
        self._remove_bg = enabled
        self.rebuild()
        self.save()

    def set_bg_tol(self, tol):
        self._bg_tol = tol
        if self._remove_bg:
            self.rebuild()
        self.save()

    # --- mouse: drag, wheel-resize, context menu --------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            self.save()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._show_controls()
            event.accept()

    def wheelEvent(self, event):
        if self._max_dim is None:
            self._max_dim = max(self.width(), self.height())
        step = 15 if event.angleDelta().y() > 0 else -15
        self._max_dim = max(40, min(1000, self._max_dim + step))
        self.rebuild()
        self.save()
        if self._panel_size_setter:
            self._panel_size_setter(self._max_dim)

    _panel_size_setter = None
    _show_controls = lambda self: None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        controls = QAction("Show controls", self)
        controls.triggered.connect(lambda: self._show_controls())
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(quit_app)
        menu.addAction(controls)
        menu.addSeparator()
        menu.addAction(quit_action)
        menu.exec(event.globalPos())


class ControlPanel(QWidget):
    def __init__(self, gif):
        super().__init__()
        self.gif = gif
        self.setWindowTitle("DeskGifs - Controls")
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
        )

        layout = QVBoxLayout(self)

        open_btn = QPushButton("Open image…")
        open_btn.clicked.connect(self._open_image)
        layout.addWidget(open_btn)

        self.size_slider = self._slider(40, 1000,
                                        gif._max_dim or 200, self._on_size)
        layout.addWidget(self._row("Size", self.size_slider))

        self.opacity_slider = self._slider(10, 100,
                                           int(gif.windowOpacity() * 100),
                                           self._on_opacity)
        layout.addWidget(self._row("Opacity", self.opacity_slider))

        self.frame_combo = QComboBox()
        self.frame_combo.addItems(FRAME_STYLES)
        if gif._frame in FRAME_STYLES:
            self.frame_combo.setCurrentText(gif._frame)
        self.frame_combo.currentTextChanged.connect(self.gif.set_frame)
        layout.addWidget(self._row("Frame", self.frame_combo))

        # Background removal
        bg_box = QGroupBox("Background")
        bg_layout = QVBoxLayout(bg_box)
        self.bg_check = QCheckBox("Remove background color")
        self.bg_check.setChecked(gif._remove_bg)
        self.bg_check.toggled.connect(self.gif.set_remove_bg)
        bg_layout.addWidget(self.bg_check)
        self.bg_tol = self._slider(0, 150, gif._bg_tol, self._on_bg_tol)
        bg_layout.addWidget(self._row("Tolerance", self.bg_tol))
        layout.addWidget(bg_box)

        # Crop
        crop_box = QGroupBox("Crop (%)")
        crop_layout = QVBoxLayout(crop_box)
        cl, ct, cr, cb = (int(v * 100) for v in gif._crop)
        self.crop_l = self._slider(0, 90, cl, self._on_crop)
        self.crop_t = self._slider(0, 90, ct, self._on_crop)
        self.crop_r = self._slider(0, 90, cr, self._on_crop)
        self.crop_b = self._slider(0, 90, cb, self._on_crop)
        crop_layout.addWidget(self._row("Left", self.crop_l))
        crop_layout.addWidget(self._row("Top", self.crop_t))
        crop_layout.addWidget(self._row("Right", self.crop_r))
        crop_layout.addWidget(self._row("Bottom", self.crop_b))
        reset = QPushButton("Reset crop")
        reset.clicked.connect(self._reset_crop)
        crop_layout.addWidget(reset)
        layout.addWidget(crop_box)

        # Loadouts
        lo_box = QGroupBox("Loadouts")
        lo_layout = QVBoxLayout(lo_box)
        self.loadout_list = QListWidget()
        self.loadout_list.itemClicked.connect(self._load_loadout)
        lo_layout.addWidget(self.loadout_list)
        lo_btns = QHBoxLayout()
        save_lo = QPushButton("Save current…")
        save_lo.clicked.connect(self._save_loadout)
        del_lo = QPushButton("Delete")
        del_lo.clicked.connect(self._delete_loadout)
        lo_btns.addWidget(save_lo)
        lo_btns.addWidget(del_lo)
        lo_layout.addLayout(lo_btns)
        layout.addWidget(lo_box)
        self._reload_loadout_list()

        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(quit_app)
        layout.addWidget(quit_btn)

        self.resize(280, self.sizeHint().height())
        gif._panel_size_setter = self._sync_size

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    # --- widget helpers ----------------------------------------------------

    @staticmethod
    def _slider(lo, hi, val, on_change):
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setValue(int(val))
        s.valueChanged.connect(on_change)
        return s

    @staticmethod
    def _row(label, widget):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label)
        lbl.setFixedWidth(65)
        h.addWidget(lbl)
        h.addWidget(widget)
        return row

    # --- callbacks ---------------------------------------------------------

    def _open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an animation", "",
            "Animations (*.gif *.webp *.png *.apng);;All files (*.*)",
        )
        if path:
            try:
                self.gif.load(path)
                self.gif.show()
            except Exception as e:
                print(f"Could not load {path}: {e}")

    def _on_size(self, value):
        self.gif.set_size(value)

    def _sync_size(self, value):
        self.size_slider.blockSignals(True)
        self.size_slider.setValue(value)
        self.size_slider.blockSignals(False)

    def _on_opacity(self, value):
        self.gif.set_opacity(value / 100.0)

    def _on_bg_tol(self, value):
        self.gif.set_bg_tol(value)

    def _on_crop(self, _):
        self.gif.set_crop(
            self.crop_l.value() / 100.0,
            self.crop_t.value() / 100.0,
            self.crop_r.value() / 100.0,
            self.crop_b.value() / 100.0,
        )

    def _reset_crop(self):
        for s in (self.crop_l, self.crop_t, self.crop_r, self.crop_b):
            s.blockSignals(True)
            s.setValue(0)
            s.blockSignals(False)
        self.gif.set_crop(0, 0, 0, 0)

    # --- loadouts ----------------------------------------------------------

    def _reload_loadout_list(self):
        self.loadout_list.clear()
        self.loadout_list.addItems(sorted(self.gif._loadouts.keys()))

    def _save_loadout(self):
        name, ok = QInputDialog.getText(self, "Save loadout", "Name:")
        name = name.strip()
        if ok and name:
            self.gif._loadouts[name] = self.gif.current_state()
            self.gif.save()
            self._reload_loadout_list()

    def _load_loadout(self, item):
        state = self.gif._loadouts.get(item.text())
        if state:
            self.gif.apply_state(state)
            self.refresh_controls()

    def _delete_loadout(self):
        item = self.loadout_list.currentItem()
        if item and item.text() in self.gif._loadouts:
            del self.gif._loadouts[item.text()]
            self.gif.save()
            self._reload_loadout_list()

    def refresh_controls(self):
        """Sync every control to the gif's current state (after a loadout)."""
        g = self.gif
        pairs = [
            (self.size_slider, g._max_dim or 200),
            (self.opacity_slider, int(g.windowOpacity() * 100)),
            (self.bg_tol, g._bg_tol),
            (self.crop_l, int(g._crop[0] * 100)),
            (self.crop_t, int(g._crop[1] * 100)),
            (self.crop_r, int(g._crop[2] * 100)),
            (self.crop_b, int(g._crop[3] * 100)),
        ]
        for widget, val in pairs:
            widget.blockSignals(True)
            widget.setValue(val)
            widget.blockSignals(False)
        for w, setter in [(self.bg_check, lambda: self.bg_check.setChecked(g._remove_bg)),
                          (self.frame_combo, lambda: self.frame_combo.setCurrentText(g._frame))]:
            w.blockSignals(True)
            setter()
            w.blockSignals(False)


def quit_app():
    app = QApplication.instance()
    for w in app.topLevelWidgets():
        if isinstance(w, DeskGif):
            w.save()
    app.quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Single-instance lock: if a copy is already running, bail out so we don't
    # stack multiple gifs. (Kept alive on `app` so it isn't garbage collected.)
    app._lock = QSharedMemory("DeskGifs_single_instance_lock")
    if not app._lock.create(1):
        print("DeskGifs is already running.")
        return 0

    settings = load_settings()
    gif = DeskGif(settings)
    panel = ControlPanel(gif)

    def show_panel():
        panel.show()
        panel.raise_()
        panel.activateWindow()

    gif._show_controls = show_panel

    path = gif._path
    if path:
        if not os.path.isabs(path):
            path = os.path.join(HERE, path)
        if os.path.exists(path):
            gif.load(path)
            gif.show()
            gif.restore_position()
        else:
            print(f"Image not found: {path} - pick one from the panel.")

    gif._ready = True
    panel.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
