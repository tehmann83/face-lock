# FaceGuard

Local, free, camera-based screen locker for macOS. Locks your screen the moment an unrecognized face appears — no cloud, no subscription.

## Requirements

- macOS (tested on Ventura+)
- Python 3.9+
- A webcam (built-in or USB)

## Quick Start

```bash
# 1. Install dependencies (takes a few minutes — dlib compiles from source)
bash install.sh

# 2. Enroll your face
python3 faceguard.py --enroll

# 3. Start monitoring
python3 faceguard.py
```

During enrollment, look at the camera and press **Space** to capture your face from several angles (6–10 shots recommended). Press **Q** when done.

## Options

| Flag | Default | Description |
|---|---|---|
| `--enroll` | — | Enroll or re-enroll your face |
| `--reset` | — | Delete saved face data and exit |
| `--tolerance FLOAT` | `0.55` | Match strictness — lower is stricter |
| `--interval FLOAT` | `0.5` | Seconds between recognition checks |
| `--streak INT` | `2` | Consecutive unknown-face frames before locking |
| `--no-face-streak INT` | `6` | Consecutive no-face frames before locking |
| `--no-preview` | off | Run silently without the camera window |

## Telegram Alerts

Receive a photo of the intruder on your phone each time the screen locks.

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → follow the prompts → copy the bot token.
2. Send any message to your new bot to open a chat with it.
3. Get your chat ID — open this URL in a browser (replace `<TOKEN>` with yours):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Look for `"chat":{"id": <YOUR_CHAT_ID>}`.
4. Fill in `.env`:
   ```
   FACEGUARD_TG_TOKEN="123456789:ABCdef..."
   FACEGUARD_TG_CHAT_ID="123456789"
   ```

`.env` is loaded automatically on startup. It is listed in `.gitignore` and will never be committed.

## How It Works

1. The camera is read on each interval tick.
2. Each detected face is compared against your enrolled encodings.
3. If an **unknown** face is seen (and you are **not** present) for `--streak` consecutive frames, the screen locks.
4. If **no face** is visible for `--no-face-streak` consecutive frames, the screen locks (covers walking away or blocking the camera).
5. A 15-second cooldown prevents rapid re-locking.
6. If you are present alongside a guest, locking is suppressed — even if the detector momentarily misses your face, you get a 5-second grace window.

## File Locations

| Path | Contents |
|---|---|
| `~/.faceguard_encodings.pkl` | Your enrolled face data |
| `~/.faceguard_snapshots/` | Intruder JPEGs, named by timestamp |
| `.env` | Telegram credentials (never committed) |

## Troubleshooting

**"Cannot open camera"** — go to System Settings → Privacy & Security → Camera and grant access to Terminal (or your IDE).

**Too many false locks** — increase `--tolerance` (e.g. `0.6`) or `--streak` (e.g. `5`). Re-enrolling in the same lighting conditions as your workspace also helps.

**Screen doesn't lock** — FaceGuard tries three methods (CGSession → AppleScript → pmset). The AppleScript method requires Accessibility permission: System Settings → Privacy & Security → Accessibility.
# face-lock
