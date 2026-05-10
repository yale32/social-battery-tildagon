# Social Battery — Tildagon badge app
# Displays social energy as a car fuel-gauge needle on the 240×240 round display.
# Auto-drains/charges at a configurable rate; supports a BLE HID media remote.
#
# Author: Yale32  |  youtube.com/@yale32

import app
import asyncio
import time
import math
from events.input import Buttons, BUTTON_TYPES
from tildagonos import tildagonos
from app_components import clear_background
from system.eventbus import eventbus
from system.patterndisplay.events import PatternDisable, PatternEnable

# ── Gauge geometry ──────────────────────────────────────────────────────────────
# 240×240 display, origin at centre.
# Arc sweeps 250° clockwise: lower-left (0%) → top (50%) → lower-right (100%).
_ARC_R    = 108
_ARC_W    = 20
_STROKE_R = _ARC_R - _ARC_W // 2   # stroke-centre radius
_NEEDLE   = _ARC_R - _ARC_W - 2    # needle tip at inner edge of arc band

_A_EMPTY = math.radians(145)        # 0%   lower-left
_A_FULL  = math.radians(395)        # 100% lower-right (35° + 360° so arc_to > arc_from)
_A_20    = math.radians(195)        # 20%  zone boundary
_A_50    = math.radians(270)        # 50%  zone boundary (top of arc)

_TAU = 2 * math.pi

# ── Timing ──────────────────────────────────────────────────────────────────────
_SEQ_WINDOW      = 0.65   # click-sequence detection window (seconds) — badge buttons
_BLE_SEQ_WINDOW  = 1.0    # wider window for remote (3 deliberate presses takes longer)
_INTERVAL_NORMAL = 30.0   # seconds per auto-tick  (single press on LEFT/RIGHT)
_INTERVAL_FAST   = 10.0   # seconds per auto-tick  (double press)
_INTERVAL_BURST  = 1.0    # seconds per auto-tick  (triple press)
_BURST_TICKS     = 5      # number of 1 s ticks before reverting to normal interval
_ANIM_FRAMES     = 24     # direction animation length  (~1.2 s at 20 fps)
_ANIM_SCROLL_PX  = 4      # pixels scrolled per animation frame
_INTRO_MAX_SCROLL = 185   # max pixels the intro can scroll down

# ── LED layout ──────────────────────────────────────────────────────────────────
# Active LEDs follow the gauge arc from 0 % (LED 9) to 100 % (LED 4).
# LEDs 5-8 sit behind the badge and are kept dark.
_LED_ARC        = (9, 10, 11, 12, 1, 2, 3, 4)   # arc order: index 0 = 0%, index 7 = 100%
_LED_OFF        = (5, 6, 7, 8)                   # always dark
_LED_BRIGHTNESS = 0.8                            # scale factor applied to all LED colours


def _pct_to_angle(pct):
    """Map 0–100 % to a clockwise arc angle in radians (145° → 395°)."""
    return math.radians(145 + pct * 2.5)


class SocialBatteryApp(app.App):

    def __init__(self):
        self.battery   = 100
        self.direction = -1       # -1 = draining, +1 = charging
        self.interval  = _INTERVAL_NORMAL
        self.paused    = False

        self._timer     = 0.0    # seconds since last auto-tick
        self._low_count = 0      # consecutive decreases while below 20 %
        self._warn_left = 0      # warning flash frames remaining

        # Direction-change animation
        self._anim_frames = 0    # frames remaining  (0 = idle)
        self._anim_dir    = -1   # direction of the animation in progress

        self._led_pct     = -1   # last battery % written to LEDs (skip unchanged writes)
        self._flash_timer = 0.0  # running clock used to drive the status-arrow blink

        # Burst mode — the animation plays first, then burst begins when it ends.
        # _burst_pending arms the burst so update() starts it the frame the
        # animation hits zero, keeping the two effects cleanly sequential.
        self._burst_ticks   = 0     # ticks remaining (counts 5→0 at 1 s each)
        self._burst_pending = False

        # Click-sequence detection for LEFT / RIGHT
        self._seq_dir   = 0      # direction being accumulated
        self._seq_count = 0      # presses so far in window
        self._seq_timer = 0.0    # seconds since first press

        # BLE remote — initialised on the first update() frame so the display
        # is already up before the BLE stack starts scanning.
        self._ble_remote       = None
        self._ble_init_done    = False
        self._ble_delta        = 0      # ±1 step queued by remote Vol Up/Down
        self._ble_toggle_pause = False  # queued Play/Pause press

        # BLE remote direction-press sequence (mirrors badge button seq logic)
        self._ble_seq_dir   = 0      # direction being accumulated
        self._ble_seq_count = 0      # presses so far in window
        self._ble_seq_timer = 0.0    # seconds since first press
        # HID remotes emit repeated key-down events while held; _ble_key_held
        # tracks which code is currently down so repeats can be ignored until
        # a 0x00 (key-up) clears it.
        self._ble_key_held  = None

        # Intro screen
        self._show_intro   = True
        self._intro_scroll = 0

        self.button_states = Buttons(self)
        eventbus.emit(PatternDisable())

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def run(self, render_update):
        # render_update() returns True when this app has just regained the
        # foreground. Use that to re-suppress the LED swirl after returning
        # from the main menu (PatternDisable was re-enabled when we left).
        last_time = time.ticks_ms()
        while True:
            cur_time = time.ticks_ms()
            delta = time.ticks_diff(cur_time, last_time)
            if self.update(delta) is not False:
                regained = await render_update()
                if regained:
                    eventbus.emit(PatternDisable())
            else:
                await asyncio.sleep(0.05)
            last_time = cur_time

    # ── update ─────────────────────────────────────────────────────────────────

    def update(self, delta):
        dt = delta / 1000   # Tildagon passes delta in ms; work in seconds

        if not self._ble_init_done:
            self._ble_init_done = True
            self._init_ble()

        b = self.button_states

        if self._show_intro:
            if b.get(BUTTON_TYPES["UP"]):
                b.clear()
                self._intro_scroll = max(0, self._intro_scroll - 25)
            elif b.get(BUTTON_TYPES["DOWN"]):
                b.clear()
                self._intro_scroll = min(_INTRO_MAX_SCROLL, self._intro_scroll + 25)
            elif b.get(BUTTON_TYPES["CONFIRM"]):
                b.clear()
                self._show_intro = False
            return

        if b.get(BUTTON_TYPES["CANCEL"]):
            b.clear()
            for i in range(1, 13):           # clear all LEDs before handing back
                tildagonos.leds[i] = (0, 0, 0)
            tildagonos.leds.write()
            if self._ble_remote is not None:
                self._ble_remote.close()
            eventbus.emit(PatternEnable())
            self.minimise()
            return

        if self._ble_delta != 0:
            self._step(self._ble_delta)
            self._ble_delta = 0
        if self._ble_toggle_pause:
            self.paused = not self.paused
            self._ble_toggle_pause = False

        if b.get(BUTTON_TYPES["CONFIRM"]):
            b.clear()
            self.paused = not self.paused

        if b.get(BUTTON_TYPES["UP"]):
            b.clear()
            self._step(+1)
        elif b.get(BUTTON_TYPES["DOWN"]):
            b.clear()
            self._step(-1)

        # LEFT / RIGHT use click-sequence detection to choose interval
        if b.get(BUTTON_TYPES["RIGHT"]):
            b.clear()
            self._register_dir_press(+1)
        elif b.get(BUTTON_TYPES["LEFT"]):
            b.clear()
            self._register_dir_press(-1)

        # Tick sequence window and commit when it expires
        if self._seq_count > 0:
            self._seq_timer += dt
            if self._seq_timer >= _SEQ_WINDOW:
                self._commit_sequence()

        if self._ble_seq_count > 0:
            self._ble_seq_timer += dt
            if self._ble_seq_timer >= _BLE_SEQ_WINDOW:
                self._commit_ble_sequence()

        # When a queued burst animation finishes, start the 5-second burst
        if self._burst_pending and self._anim_frames == 0:
            self._burst_pending = False
            self.interval       = _INTERVAL_BURST
            self._burst_ticks   = _BURST_TICKS
            self._timer         = 0.0

        # Auto-update: frozen during animations and while paused
        if not self.paused and self._anim_frames == 0:
            self._timer += dt
            if self._timer >= self.interval:
                self._timer = 0.0
                self._step(self.direction)
                if self._burst_ticks > 0:
                    self._burst_ticks -= 1
                    if self._burst_ticks == 0:
                        self.interval = _INTERVAL_NORMAL

        self._flash_timer += dt
        self._update_leds()

    # ── BLE ────────────────────────────────────────────────────────────────────

    def _init_ble(self):
        # ImportError if firmware was built without MICROPY_PY_BLUETOOTH — fall
        # back gracefully so the app works on stock firmware with no BLE.
        try:
            # Resolve the package dynamically so the import works both when
            # running locally (apps.social_battery) and when installed from
            # the store (apps.Yale32_social_battery_tildagon, etc.).
            _pkg = __name__.rsplit(".", 1)[0] if "." in __name__ else ""
            _mod = __import__(
                _pkg + ".ble_remote" if _pkg else "ble_remote",
                fromlist=["BLERemote"],
            )
            BLERemote = _mod.BLERemote
            self._ble_remote = BLERemote(self._on_ble_key)
        except Exception:
            self._ble_remote = None

    def _on_ble_key(self, code):
        # 0x00 is the HID key-up report; clear the held state so the next
        # physical press registers as a fresh event.
        if code == 0x00:
            self._ble_key_held = None
            return
        if self._ble_key_held == code:
            return   # key-repeat while held — ignore
        self._ble_key_held = code

        if code == 0xE9:
            self._ble_delta = 1
        elif code == 0xEA:
            self._ble_delta = -1
        elif code == 0xCD:
            self._ble_toggle_pause = True
        elif code == 0xB5:
            self.paused = False
            self._register_ble_dir_press(+1)
        elif code == 0xB6:
            self.paused = False
            self._register_ble_dir_press(-1)

    def _register_ble_dir_press(self, direction):
        if self._ble_seq_count == 0 or self._ble_seq_dir != direction:
            self._ble_seq_dir   = direction
            self._ble_seq_count = 1
            self._ble_seq_timer = 0.0
        else:
            self._ble_seq_count += 1
            if self._ble_seq_count >= 3:
                self._commit_ble_sequence()

    def _commit_ble_sequence(self):
        count = self._ble_seq_count
        d     = self._ble_seq_dir
        self._ble_seq_count = 0
        self._ble_seq_timer = 0.0

        self.direction = d
        self._timer    = 0.0

        if count == 1:
            self.interval       = _INTERVAL_NORMAL
            self._burst_pending = False
            self._burst_ticks   = 0
            self._anim_frames   = _ANIM_FRAMES
            self._anim_dir      = d
        elif count == 2:
            self.interval       = _INTERVAL_FAST
            self._burst_pending = False
            self._burst_ticks   = 0
        else:
            self._burst_pending = True
            self._burst_ticks   = 0
            self._anim_frames   = _ANIM_FRAMES
            self._anim_dir      = d

    # ── click-sequence logic ────────────────────────────────────────────────────

    def _register_dir_press(self, direction):
        """Record one LEFT or RIGHT press for sequence detection."""
        if self._seq_count == 0 or self._seq_dir != direction:
            # New sequence or direction flip — start fresh
            self._seq_dir   = direction
            self._seq_count = 1
            self._seq_timer = 0.0
        else:
            self._seq_count += 1
            if self._seq_count >= 3:
                self._commit_sequence()

    def _commit_sequence(self):
        """Apply the accumulated click sequence and reset state."""
        count = self._seq_count
        d     = self._seq_dir
        self._seq_count = 0
        self._seq_timer = 0.0

        self.direction = d

        self._timer = 0.0   # always start timing from the moment of activation

        if count == 1:
            # Single press: normal interval + direction animation
            self.interval     = _INTERVAL_NORMAL
            self._anim_frames = _ANIM_FRAMES
            self._anim_dir    = d
        elif count == 2:
            # Double press: fast interval, no animation
            self.interval = _INTERVAL_FAST
        else:
            # Triple press: play animation, then burst (1 %/s for 5 s) once it ends
            self._anim_frames   = _ANIM_FRAMES
            self._anim_dir      = d
            self._burst_pending = True   # burst starts the moment the animation finishes

    # ── battery helpers ─────────────────────────────────────────────────────────

    def _step(self, delta):
        prev = self.battery
        self.battery = max(0, min(100, self.battery + delta))
        if self.battery < prev:
            self._check_warning(prev, self.battery)

    def _check_warning(self, prev, new):
        if new >= 20:
            self._low_count = 0
            return
        self._low_count += 1
        # Fire on every other decrease below 20 % to avoid being intrusive
        if self._low_count % 2 == 1:
            self._warn_left = 9   # 3 flashes × 3 frames each

    # ── LEDs ───────────────────────────────────────────────────────────────────

    def _update_leds(self):
        pct = self.battery
        if pct == self._led_pct:
            return
        self._led_pct = pct

        lit = round(pct / 100 * 8)

        if pct < 20:
            raw = (255, 0, 0)
        elif pct < 50:
            raw = (255, 100, 0)
        else:
            raw = (0, 200, 0)
        colour = tuple(int(v * _LED_BRIGHTNESS) for v in raw)

        for idx, led in enumerate(_LED_ARC):
            tildagonos.leds[led] = colour if idx < lit else (0, 0, 0)

        for led in _LED_OFF:
            tildagonos.leds[led] = (0, 0, 0)

        tildagonos.leds.write()

    # ── draw ───────────────────────────────────────────────────────────────────

    def draw(self, ctx):
        clear_background(ctx)

        if self._show_intro:
            self._draw_intro(ctx)
            return

        if self._warn_left > 0:
            self._draw_warning(ctx)
            self._warn_left -= 1
            return

        if self._anim_frames > 0:
            self._draw_animation(ctx)
            self._anim_frames -= 1
            return

        self._draw_arc(ctx)
        self._draw_needle(ctx)
        self._draw_pivot(ctx)
        self._draw_labels(ctx)

    def _draw_intro(self, ctx):
        ctx.text_align = ctx.CENTER
        y = -95 - self._intro_scroll
        for size, color, text in (
            (20, (1.0, 1.0, 1.0),    "Social Battery"),
            ( 8, None,                None),
            (13, (0.8, 0.8, 0.8),    "Track social energy"),
            (13, (0.8, 0.8, 0.8),    "as a fuel gauge."),
            (16, None,                None),
            (13, (0.4, 0.7, 1.0),    "-- Controls --"),
            (12, (0.75, 0.75, 0.75), "A: scroll up"),
            (12, (0.75, 0.75, 0.75), "D: scroll down"),
            (12, (0.75, 0.75, 0.75), "C: pause / resume"),
            (12, (0.75, 0.75, 0.75), "UP/DN: adjust +/-1%"),
            (12, (0.75, 0.75, 0.75), "L/R: direction & speed"),
            (11, (0.6,  0.6,  0.6),  "1x 30s  2x 10s  3x burst"),
            (16, None,                None),
            (13, (0.4, 0.7, 1.0),    "-- BLE Remote --"),
            (12, (0.75, 0.75, 0.75), "Vol+/-: step  Play: pause"),
            (12, (0.75, 0.75, 0.75), "Next/Prev: direction"),
            (11, (0.6,  0.6,  0.6),  "1x 30s  2x 10s  3x burst"),
            (24, None,                None),
            (15, (0.1, 0.9, 0.3),    "Press C to start"),
            (28, None,                None),
            (13, (0.7, 0.5, 1.0),    "Created by Yale32"),
            (12, (0.7, 0.5, 1.0),    "youtube.com/@yale32"),
            ( 8, None,                None),
        ):
            if text is None:
                y += size
            else:
                ctx.font_size = size
                ctx.rgb(*color)
                ctx.move_to(0, y)
                ctx.text(text)
                y += size + 5

        if self._intro_scroll < _INTRO_MAX_SCROLL:
            ctx.rgb(0.35, 0.35, 0.35)
            ctx.font_size = 10
            ctx.move_to(0, 108)
            ctx.text("scroll down")

    def _draw_arc(self, ctx):
        ctx.line_width = _ARC_W

        ctx.begin_path()
        ctx.arc(0, 0, _STROKE_R, _A_EMPTY, _A_20, False)
        ctx.rgb(0.97, 0.0, 0.0)
        ctx.stroke()

        ctx.begin_path()
        ctx.arc(0, 0, _STROKE_R, _A_20, _A_50, False)
        ctx.rgb(1.0, 0.55, 0.0)
        ctx.stroke()

        ctx.begin_path()
        ctx.arc(0, 0, _STROKE_R, _A_50, _A_FULL, False)
        ctx.rgb(0.0, 0.76, 0.0)
        ctx.stroke()

    def _draw_needle(self, ctx):
        a  = _pct_to_angle(self.battery)
        ca = math.cos(a)
        sa = math.sin(a)
        cp = math.cos(a + math.pi / 2)
        sp = math.sin(a + math.pi / 2)

        tip_x = _NEEDLE * ca
        tip_y = _NEEDLE * sa
        bw    = 5
        tail  = 12
        hw    = 4

        ctx.rgb(0.97, 0.0, 0.0)

        # Main blade: wide at pivot, tapers to tip
        ctx.begin_path()
        ctx.move_to( bw * cp,  bw * sp)
        ctx.line_to(-bw * cp, -bw * sp)
        ctx.line_to(tip_x, tip_y)
        ctx.fill()

        # Counter-balance tail
        ctx.begin_path()
        ctx.move_to( hw * cp,  hw * sp)
        ctx.line_to(-hw * cp, -hw * sp)
        ctx.line_to(-tail * ca, -tail * sa)
        ctx.fill()

    def _draw_pivot(self, ctx):
        ctx.rgb(0.77, 0.77, 0.77)
        ctx.begin_path()
        ctx.arc(0, 0, 10, 0, _TAU, False)
        ctx.fill()

        ctx.rgb(0.22, 0.22, 0.22)
        ctx.begin_path()
        ctx.arc(0, 0, 7, 0, _TAU, False)
        ctx.fill()

        # Centre dot: red = paused, silver = running
        if self.paused:
            ctx.rgb(0.97, 0.0, 0.0)
        else:
            ctx.rgb(0.77, 0.77, 0.77)
        ctx.begin_path()
        ctx.arc(0, 0, 3, 0, _TAU, False)
        ctx.fill()

    def _draw_labels(self, ctx):
        ctx.text_align = ctx.CENTER

        # Percentage readout
        ctx.rgb(1.0, 1.0, 1.0)
        ctx.font_size = 32
        ctx.move_to(0, 35)
        ctx.text("{:d}%".format(self.battery))

        # "Social Battery" label
        ctx.rgb(0.75, 0.75, 0.75)
        ctx.font_size = 26
        ctx.move_to(0, 62)
        ctx.text("Social")
        ctx.move_to(0, 90)
        ctx.text("Battery")

        # BLE status indicator
        if self._ble_remote is not None:
            ctx.font_size = 13
            ctx.move_to(0, 110)
            if self._ble_remote.connected:
                ctx.rgb(0.0, 0.5, 1.0)
                ctx.text("BLE")
            elif self._ble_remote.scanning:
                ctx.rgb(0.25, 0.25, 0.25)
                ctx.text("BLE?")

        # Flashing direction arrow inside the gauge (above pivot)
        # When paused the pivot dot turns red — no arrow needed
        if not self.paused:
            self._draw_status_arrow(ctx)

    def _draw_status_arrow(self, ctx):
        """
        1 arrow = normal (30 s), 2 = fast (10 s), 3 = burst (1 s).
        Flash period scales with interval: slow at 30 s, rapid at burst.
        """
        flash_period = max(0.25, self.interval / 15.0)
        if (self._flash_timer % flash_period) >= flash_period / 2:
            return   # off half of every cycle

        if self._burst_ticks > 0:
            count = 3
        elif self.interval == _INTERVAL_FAST:
            count = 2
        else:
            count = 1

        upward = self.direction > 0
        if upward:
            ctx.rgb(0.0, 0.76, 0.0)
        else:
            ctx.rgb(0.97, 0.0, 0.0)

        spacing = 20
        center_y = -50
        offsets = [0] if count == 1 else ([-10, 10] if count == 2 else [-spacing, 0, spacing])
        for dy in offsets:
            self._draw_arrow(ctx, 0, center_y + dy, 18, upward=upward)

    # ── direction animation ─────────────────────────────────────────────────────

    def _draw_animation(self, ctx):
        """Scrolling arrow animation triggered by a single LEFT / RIGHT press."""
        elapsed = _ANIM_FRAMES - self._anim_frames
        offset  = (elapsed * _ANIM_SCROLL_PX) % 40
        ctx.text_align = ctx.CENTER

        if self._anim_dir > 0:
            # Charging: green arrows scroll upward, label at top
            ctx.rgb(0.0, 0.76, 0.0)
            ctx.font_size = 22
            ctx.move_to(0, -90)
            ctx.text("Charging")
            for row in range(-3, 5):
                y = row * 40 - offset
                if -65 <= y <= 110:    # leave room for top label
                    self._draw_arrow(ctx, 0, y, 18, upward=True)
        else:
            # Draining: red arrows scroll downward, label at bottom
            ctx.rgb(0.97, 0.0, 0.0)
            ctx.font_size = 18
            ctx.move_to(0, 90)
            ctx.text("Battery under load")
            for row in range(-4, 4):
                y = row * 40 + offset
                if -110 <= y <= 65:    # leave room for bottom label
                    self._draw_arrow(ctx, 0, y, 18, upward=False)

    def _draw_arrow(self, ctx, x, y, size, upward):
        h = size
        w = int(size * 0.7)
        if upward:
            ctx.begin_path()
            ctx.move_to(x,          y - h // 2)
            ctx.line_to(x - w // 2, y + h // 2)
            ctx.line_to(x + w // 2, y + h // 2)
            ctx.fill()
        else:
            ctx.begin_path()
            ctx.move_to(x,          y + h // 2)
            ctx.line_to(x - w // 2, y - h // 2)
            ctx.line_to(x + w // 2, y - h // 2)
            ctx.fill()

    # ── low-battery warning ─────────────────────────────────────────────────────

    def _draw_warning(self, ctx):
        """Flash 'Charge immediately!!' — on for 2 frames, off for 1."""
        if self._warn_left % 3 != 0:
            ctx.text_align = ctx.CENTER
            ctx.rgb(0.97, 0.0, 0.0)
            ctx.font_size = 40
            ctx.move_to(0, -15)
            ctx.text("Charge")
            ctx.font_size = 22
            ctx.move_to(0, 30)
            ctx.text("immediately!!")


__app_export__ = SocialBatteryApp
