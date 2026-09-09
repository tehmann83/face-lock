# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (one-time; dlib compiles from source — takes minutes)
bash install.sh

# Enroll face
python3 faceguard.py --enroll

# Run monitor (with preview window)
python3 faceguard.py

# Run headless
python3 faceguard.py --no-preview

# Reset saved face data
python3 faceguard.py --reset
```

No test suite. No build step.

## Architecture

Single-file app: `faceguard.py`. All logic lives there.

**Data flow:**
1. `main()` parses args, loads `.env`, dispatches to `enroll()` or `monitor()`
2. `enroll()` — interactive OpenCV window; user presses Space to capture frames; `face_recognition.face_encodings()` extracts 128-d embeddings; saved as list to `~/.faceguard_encodings.pkl` via pickle
3. `monitor()` — polling loop at `--interval` seconds; each iteration:
   - reads a frame via `cv2.VideoCapture`
   - detects faces with `face_recognition.face_locations()` (HOG model)
   - encodes detected faces, compares against known encodings via `face_recognition.compare_faces()` + `face_distance()`
   - tracks two independent streak counters: `unknown_streak` and `no_face_streak`
   - calls `lock_screen()` when streak limit hit and cooldown elapsed

**Lock trigger logic** (`monitor()` lines 404–435):
- Unknown face seen AND owner NOT present AND owner NOT seen within `owner_grace` (5.0s, `--owner-grace`) → increment `unknown_streak`; lock when streak ≥ `--streak`
- Grace-suppressed unknown faces log `⏳ Unknown face ignored`, throttled to 1/s
- No face visible → increment `no_face_streak`; lock when streak ≥ `--no-face-streak`
- Before locking, `_still_unknown_at_full_res()` re-detects and re-encodes on the full-resolution frame. Detection at `DETECT_SCALE` can return a partial box on an occluded face, scoring the owner as unknown (measured 0.72 vs 0.37 at full res). If the re-check recognizes anyone, the lock is cancelled and the frame counts as an owner sighting. Costs ~410ms, paid only on the lock frame.
- 15-second `LOCK_COOLDOWN` prevents rapid re-locking

**`lock_screen()`** tries three macOS methods in order: CGSession → AppleScript Cmd+Ctrl+Q → `pmset sleepnow`. First success wins. Called *before* `send_telegram_alert()` — network I/O must never delay the lock.

**Telegram alerts** (`send_telegram_alert()`): uses only stdlib `urllib.request`; multipart POST to `api.telegram.org/bot{token}/sendPhoto`. Credentials loaded from `.env` via `_load_dotenv()` (does not override existing env vars).

**Snapshot saving**: `save_snapshot()` writes JPEG to `~/.faceguard_snapshots/intruder_YYYYMMDD_HHMMSS.jpg` before every lock. Always the clean frame — preview annotations are drawn on a separate copy.

## Key constants (top of file, overridable via flags)

| Constant | Default | CLI flag |
|---|---|---|
| `TOLERANCE` | 0.55 | `--tolerance` |
| `CHECK_INTERVAL` | 0.5s | `--interval` |
| `FRAMES_TO_LOCK` | 2 | `--streak` |
| `NO_FACE_FRAMES_TO_LOCK` | 6 | `--no-face-streak` |
| `LOCK_COOLDOWN` | 15s | — |
| `OWNER_GRACE` | 5.0s | `--owner-grace` |
| `DETECT_SCALE` | 0.5 | — | (do not lower — see lock trigger logic)

## Runtime permissions required (macOS)

- **Camera**: System Settings → Privacy & Security → Camera → Terminal
- **Accessibility** (for AppleScript lock method): System Settings → Privacy & Security → Accessibility
