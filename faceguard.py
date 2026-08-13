#!/usr/bin/env python3
"""
FaceGuard — local, free, camera-based screen locker.

Locks your macOS screen the moment an unrecognized face appears in the camera.
No cloud, no subscriptions, no Claude required after setup.

QUICK START
-----------
1. Install dependencies (run once):
       bash install.sh

2. Enroll your face (run once):
       python3 faceguard.py --enroll

3. Start monitoring:
       python3 faceguard.py

OPTIONAL FLAGS
--------------
  --enroll              Enroll / re-enroll your face
  --tolerance FLOAT     Match strictness (default 0.55; lower = stricter)
  --interval FLOAT      Seconds between camera checks (default 1.0)
  --streak INT          Consecutive unknown frames before locking (default 3)
  --no-preview          Run silently without showing the camera window
  --reset               Delete saved face data and exit

SNAPSHOTS
---------
  Each time the screen is locked, a JPEG of the intruder is saved to:
      ~/.faceguard_snapshots/intruder_YYYYMMDD_HHMMSS.jpg

TELEGRAM ALERTS
---------------
  Add your credentials to .env (copy .env.example if you don't have one yet):

      FACEGUARD_TG_TOKEN="123456789:ABCdef..."
      FACEGUARD_TG_CHAT_ID="123456789"

  Setup (one-time):
    1. Open Telegram → message @BotFather → /newbot → follow prompts
       Copy the bot token  (looks like  123456789:ABCdef...)
    2. Start a chat with your new bot (send it any message)
    3. Get your chat ID — visit in a browser:
         https://api.telegram.org/bot<TOKEN>/getUpdates
       Look for  "chat":{"id": <YOUR_CHAT_ID>}
    4. Paste both values into .env as shown above.

DEPENDENCIES (all free, all local)
-----------------------------------
  opencv-python         Camera capture + live preview
  face_recognition      Face detection & recognition (wraps dlib)
  numpy                 Array math (pulled in automatically)

Install via:  bash install.sh
"""

import argparse
import os
import pickle
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import face_recognition
import numpy as np

import urllib.request


def _load_dotenv() -> None:
    """Load key=value pairs from .env into os.environ (does not override existing vars)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), val)


# ── Defaults ──────────────────────────────────────────────────────────────────
ENCODINGS_FILE  = Path.home() / ".faceguard_encodings.pkl"
SNAPSHOTS_DIR   = Path.home() / ".faceguard_snapshots"
TOLERANCE       = 0.55   # 0.4 = very strict, 0.6 = lenient
CHECK_INTERVAL  = 0.2    # seconds between recognition checks (idle; skipped while streak builds)
FRAMES_TO_LOCK       = 2   # consecutive unknown-face frames needed to trigger lock
DETECT_SCALE         = 0.5 # resize factor before face detection — 4× faster, same accuracy
LOCK_COOLDOWN        = 15  # seconds to wait before locking again
OWNER_GRACE          = 5.0 # seconds: suppress locking if owner was seen this recently


# ── Background frame grabber ──────────────────────────────────────────────────
class _FrameGrabber:
    """Drains the camera buffer on a background thread so cap.read() lag never
    delays recognition — the main loop always processes the freshest frame."""

    def __init__(self, cap: cv2.VideoCapture) -> None:
        self._cap = cap
        self._frame = None
        self._lock = threading.Lock()
        self._running = True
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self._thread = t

    def _run(self) -> None:
        while self._running:
            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.01)  # avoid spin when camera unavailable
        self._cap.release()  # thread owns cap; releases it here, never from outside

    def read(self):
        with self._lock:
            if self._frame is None:
                return False, None
            return True, self._frame.copy()

    def stop(self) -> None:
        self._running = False
        self._thread.join(timeout=3)  # thread exits and releases cap itself


# ── Intruder snapshot ─────────────────────────────────────────────────────────
def save_snapshot(frame: np.ndarray) -> Path:
    """Save the current camera frame to SNAPSHOTS_DIR and return the path."""
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    path = SNAPSHOTS_DIR / f"intruder_{timestamp}.jpg"
    cv2.imwrite(str(path), frame)
    return path


# ── Telegram alert ────────────────────────────────────────────────────────────
def send_telegram_alert(snap_path: Path) -> None:
    """Send the intruder snapshot to yourself via Telegram bot API (no extra libs)."""
    token   = os.environ.get("FACEGUARD_TG_TOKEN")
    chat_id = os.environ.get("FACEGUARD_TG_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url      = f"https://api.telegram.org/bot{token}/sendPhoto"
        boundary = b"FaceGuardBoundary"
        caption  = b"\xf0\x9f\x94\x92 FaceGuard: screen locked \xe2\x80\x94 unknown face detected."
        with open(snap_path, "rb") as f:
            photo_data = f.read()
        body = (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="chat_id"\r\n\r\n' +
            chat_id.encode() + b"\r\n"
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="caption"\r\n\r\n' +
            caption + b"\r\n"
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="photo"; filename="intruder.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n\r\n" +
            photo_data + b"\r\n"
            b"--" + boundary + b"--\r\n"
        )
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
        print("  📨 Telegram alert sent.")
    except Exception as exc:
        print(f"  ⚠  Telegram alert failed: {exc}")


# ── macOS screen lock ──────────────────────────────────────────────────────────
def lock_screen() -> None:
    """Lock the macOS screen, trying multiple methods until one succeeds."""
    methods = [
        # CGSession: most direct lock, no UI side-effects
        ["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession", "-suspend"],
        # AppleScript Cmd+Ctrl+Q shortcut (requires Accessibility permission)
        ["osascript", "-e", 'tell application "System Events" to keystroke "q" using {command down, control down}'],
        # pmset sleep: sleeps the display/system; locks if "require password" is on
        ["pmset", "sleepnow"],
    ]
    for cmd in methods:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=5)
            if result.returncode == 0:
                return
        except Exception:
            continue
    print("  ⚠  All lock methods failed — please lock the screen manually.")


# ── Post-lock recovery ───────────────────────────────────────────────────────
# Probe script run in a subprocess — keeps OpenCV camera access off the main process
# thread entirely, avoiding AVFoundation cross-thread crashes.
_CAMERA_PROBE = (
    "import cv2, sys; "
    "c = cv2.VideoCapture(0); "
    "ret, _ = c.read(); "
    "c.release(); "
    "sys.exit(0 if ret else 1)"
)

def _wait_for_unlock(poll_secs: float = 2.0) -> None:
    """Block until the screen is unlocked (camera readable again).
    Probes via subprocess so the main process never touches OpenCV during the wait."""
    time.sleep(3.0)  # let the lock fully engage before polling
    print("  ⏸  Monitoring paused — waiting for screen unlock...")
    while True:
        try:
            r = subprocess.run(
                [sys.executable, "-c", _CAMERA_PROBE],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                print("  ↺  Screen unlocked — restarting...")
                return
        except Exception:
            pass
        time.sleep(poll_secs)


# ── Enrollment ────────────────────────────────────────────────────────────────
def enroll() -> None:
    """
    Interactive enrollment: show a live camera window, let the user press
    SPACE to capture their face from several angles, then save the encodings.
    """
    print("\n╔══════════════════════════════╗")
    print("║   FaceGuard — Enrollment     ║")
    print("╚══════════════════════════════╝")
    print("Look at the camera.")
    print("Press SPACE to capture (aim for 6–10 captures, various angles).")
    print("Press Q when done.\n")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        sys.exit("ERROR: Cannot open camera. Check System Settings → Privacy → Camera.")

    cv2.namedWindow("FaceGuard — Enrollment", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("FaceGuard — Enrollment", 480, 320)

    encodings: list = []
    captured = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb, model="hog")

        display = frame.copy()
        for top, right, bottom, left in locations:
            cv2.rectangle(display, (left, top), (right, bottom), (0, 220, 90), 2)

        status = f"Captured: {captured}   |   SPACE = snap   Q = finish"
        cv2.putText(display, status, (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 220, 90), 2)
        cv2.imshow("FaceGuard — Enrollment", display)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        elif key == ord(" "):
            if locations:
                new_encs = face_recognition.face_encodings(rgb, locations)
                encodings.extend(new_encs)
                captured += 1
                print(f"  ✓ Captured #{captured}")
            else:
                print("  ✗ No face detected — try again.")

    cap.release()
    cv2.destroyAllWindows()

    if not encodings:
        sys.exit("\nNo faces captured — enrollment failed. Please try again.")

    with open(ENCODINGS_FILE, "wb") as f:
        pickle.dump(encodings, f)

    print(f"\n✅ Saved {len(encodings)} face encodings → {ENCODINGS_FILE}")
    print("Run  python3 faceguard.py  to start monitoring.\n")


# ── Monitor ───────────────────────────────────────────────────────────────────
def monitor(tolerance: float, interval: float, streak_limit: int,
            show_preview: bool) -> None:
    """
    Continuously read from the camera. If a face is detected that does NOT
    match the enrolled encodings for `streak_limit` consecutive checks,
    lock the screen.
    """
    if not ENCODINGS_FILE.exists():
        sys.exit("No enrollment found. Run:  python3 faceguard.py --enroll")

    with open(ENCODINGS_FILE, "rb") as f:
        known_encodings: list = pickle.load(f)

    print("\n╔══════════════════════════════╗")
    print("║   FaceGuard — Monitoring     ║")
    print("╚══════════════════════════════╝")
    print(f"  Encodings loaded : {len(known_encodings)}")
    print(f"  Tolerance        : {tolerance}  (lower = stricter)")
    print(f"  Check interval   : {interval}s")
    print(f"  Streak to lock   : {streak_limit} frames")
    print(f"  Preview window   : {'yes' if show_preview else 'no'}")
    print("\nPress Ctrl+C to stop.\n")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        sys.exit("ERROR: Cannot open camera. Check System Settings → Privacy → Camera.")

    if show_preview:
        cv2.namedWindow("FaceGuard — Monitor", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("FaceGuard — Monitor", 480, 320)

    grabber = _FrameGrabber(cap)

    last_lock_time = 0.0
    last_owner_seen = 0.0
    unknown_streak = 0
    needs_recovery = False

    try:
        while True:
            if needs_recovery:
                grabber.stop()          # sets flag; thread releases cap itself
                _wait_for_unlock()      # subprocess probe — no OpenCV in main process
                os.execv(sys.executable, [sys.executable] + sys.argv)
                # execv replaces the process image: fresh camera, fresh threads, no state

            ret, frame = grabber.read()
            if not ret:
                time.sleep(0.05)
                continue

            # Scale down for faster detection — coordinates later scaled back for display
            small = cv2.resize(frame, (0, 0), fx=DETECT_SCALE, fy=DETECT_SCALE)
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            locations = face_recognition.face_locations(rgb, model="hog")

            # ── No face visible ───────────────────────────────────────────────
            if not locations:
                unknown_streak = 0
                if show_preview:
                    _draw_status(frame, "No face", (180, 180, 180), 0, 1)
                    cv2.imshow("FaceGuard — Monitor", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break
                time.sleep(interval)
                continue

            # ── Check each detected face ──────────────────────────────────────
            face_encodings = face_recognition.face_encodings(rgb, locations)
            intruder_detected = False
            owner_present     = False

            for enc, loc in zip(face_encodings, locations):
                matches = face_recognition.compare_faces(known_encodings, enc, tolerance=tolerance)
                distances = face_recognition.face_distance(known_encodings, enc)
                is_known = any(matches)
                color = (0, 220, 90) if is_known else (0, 60, 255)

                if show_preview:
                    top, right, bottom, left = loc
                    # scale coordinates back to original frame size for drawing
                    top, right, bottom, left = (
                        int(top / DETECT_SCALE), int(right / DETECT_SCALE),
                        int(bottom / DETECT_SCALE), int(left / DETECT_SCALE),
                    )
                    label = "You" if is_known else f"UNKNOWN ({min(distances):.2f})"
                    cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
                    cv2.putText(frame, label, (left, top - 8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

                if is_known:
                    owner_present = True
                else:
                    intruder_detected = True

            if owner_present:
                last_owner_seen = time.time()

            # ── Update streak & maybe lock ────────────────────────────────────
            # Don't lock if the owner is present or was seen recently — covers
            # frames where detection momentarily misses the owner's face.
            owner_recently_seen = (time.time() - last_owner_seen) < OWNER_GRACE
            if intruder_detected and not owner_present and not owner_recently_seen:
                unknown_streak += 1
                print(f"  ⚠  Unknown face — streak {unknown_streak}/{streak_limit}")
                if unknown_streak >= streak_limit:
                    unknown_streak = 0
                    now = time.time()
                    if now - last_lock_time > LOCK_COOLDOWN:
                        snap = save_snapshot(frame)
                        print(f"  📸 Snapshot saved → {snap}")
                        send_telegram_alert(snap)
                        print("  🔒 LOCKING SCREEN")
                        lock_screen()
                        last_lock_time = now
                        needs_recovery = True
            else:
                if unknown_streak > 0:
                    msg = "owner present with guest" if intruder_detected else "known face confirmed"
                    print(f"  ✓  {msg} — resetting streak.")
                unknown_streak = 0

            if show_preview:
                streak_label = f"Streak: {unknown_streak}/{streak_limit}"
                status_color = (0, 60, 255) if unknown_streak > 0 else (0, 220, 90)
                _draw_status(frame, streak_label, status_color, unknown_streak, streak_limit)
                cv2.imshow("FaceGuard — Monitor", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            # Skip sleep while tracking unknown face — process at full speed
            if unknown_streak == 0:
                time.sleep(interval)

    except KeyboardInterrupt:
        print("\n\nMonitoring stopped. Goodbye.")
    except Exception as exc:
        print(f"\n\nUnexpected error: {exc}")
    finally:
        grabber.stop()  # thread releases cap internally
        if show_preview:
            cv2.destroyAllWindows()


def _draw_status(frame: np.ndarray, text: str, color: tuple,
                 streak: int, limit: int) -> None:
    """Draw a status bar at the bottom of the preview frame."""
    h, w = frame.shape[:2]
    bar_h = 36
    cv2.rectangle(frame, (0, h - bar_h), (w, h), (30, 30, 30), -1)
    cv2.putText(frame, text, (10, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)
    cv2.putText(frame, "Q = quit", (w - 110, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (160, 160, 160), 1)
    # Streak bar
    if limit > 0:
        bar_w = int((w - 20) * min(streak / limit, 1.0))
        cv2.rectangle(frame, (10, h - bar_h - 6), (10 + bar_w, h - bar_h - 2), color, -1)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description="FaceGuard — lock your screen when an unknown face is detected.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--enroll",      action="store_true",
                        help="Enroll (or re-enroll) your face")
    parser.add_argument("--reset",       action="store_true",
                        help="Delete saved face data and exit")
    parser.add_argument("--tolerance",   type=float, default=TOLERANCE,
                        help=f"Match tolerance (default {TOLERANCE})")
    parser.add_argument("--interval",    type=float, default=CHECK_INTERVAL,
                        help=f"Seconds between checks (default {CHECK_INTERVAL})")
    parser.add_argument("--streak",      type=int,   default=FRAMES_TO_LOCK,
                        help=f"Consecutive unknown frames to trigger lock (default {FRAMES_TO_LOCK})")
    parser.add_argument("--no-preview",  action="store_true",
                        help="Run silently without camera preview window")
    args = parser.parse_args()

    if args.reset:
        if ENCODINGS_FILE.exists():
            ENCODINGS_FILE.unlink()
            print(f"Deleted {ENCODINGS_FILE}")
        else:
            print("Nothing to delete.")
        return

    if args.enroll:
        enroll()
        return

    monitor(
        tolerance=args.tolerance,
        interval=args.interval,
        streak_limit=args.streak,
        show_preview=not args.no_preview,
    )


if __name__ == "__main__":
    main()
