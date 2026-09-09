# FaceGuard — Technical Report

*What this application is, exactly. Derived from reading `faceguard.py` (522 lines) on branch `fix/lock-latency`, plus `install.sh`, `.env.example`, `.gitignore`.*

---

## 1. One-sentence definition

FaceGuard is a single-file Python 3 command-line daemon for macOS that continuously watches the webcam and locks the screen when it sees a face that does not match a locally enrolled face, saving a JPEG of the intruder and optionally sending it to a Telegram chat.

It is **not** a login system, **not** a background service (no launchd plist, no daemonization), **not** networked except for the optional outbound Telegram call, and **not** cross-platform.

---

## 2. Shape of the thing

| Property | Value |
|---|---|
| Language | Python 3 (3.9+ per README) |
| Total source | 1 file, `faceguard.py`, 522 lines |
| Entry point | `python3 faceguard.py [flags]` — foreground process, terminal-bound |
| Build step | none |
| Test suite | none |
| Packaging | none (no `setup.py`, `pyproject.toml`, `requirements.txt`) |
| Third-party deps | `opencv-python`, `face_recognition` (wraps `dlib`), `numpy` |
| Stdlib only for networking | `urllib.request` — no `requests` |
| OS coupling | macOS-only (lock commands, `CGSession` path, AVFoundation camera) |
| Persistence | two paths under `$HOME`, plus repo-local `.env` |

`install.sh` bootstraps Homebrew → `cmake` → `python3` → the three pip packages, then verifies imports and copies `.env.example` → `.env` if absent. dlib compiles from source, which is why installation takes minutes.

---

## 3. Modes of operation

Dispatch happens in `main()` (line 477), which loads `.env` first, then parses args and branches:

| Invocation | Behavior |
|---|---|
| `--reset` | Deletes `~/.faceguard_encodings.pkl` and exits. Does **not** touch snapshots. |
| `--enroll` | Interactive enrollment window, then exit. |
| *(no mode flag)* | Enters the monitor loop. Requires prior enrollment or it exits with an error. |

### 3.1 Enrollment (`enroll()`, line 237)

1. Opens `cv2.VideoCapture(0)`; hard-exits if the camera cannot be opened.
2. Live-renders every frame: converts BGR→RGB, runs `face_recognition.face_locations(rgb, model="hog")` on the **full-resolution** frame, draws green boxes plus a status line, downscales to 320 px width, shows it.
3. Keyboard:
   - **Space** — if at least one face is located, `face_recognition.face_encodings()` computes 128-dimension embeddings for *every* face in the frame and appends *all* of them to the list. (Consequence: a bystander in frame during enrollment gets permanently enrolled as an authorized face.)
   - **Q** — finish.
4. On exit, if the list is empty the program exits with an error. Otherwise the raw Python list of NumPy vectors is `pickle.dump`ed to `~/.faceguard_encodings.pkl`.

There is no naming, no multi-user support, and no incremental enrollment — each `--enroll` run **overwrites** the file wholesale.

### 3.2 Monitoring (`monitor()`, line 303)

Loads the pickle, prints a config banner, opens the camera, optionally creates the preview window, and starts a `_FrameGrabber` background thread. Then loops until `Ctrl+C`, `q` in the preview window, or an unhandled exception.

---

## 4. The monitor loop, step by step

Per iteration:

1. **Recovery check.** If a lock happened on the previous iteration, stop the grabber, block until unlock, and `os.execv` — see §7.
2. **Grab frame.** `grabber.read()` returns a copy of the newest frame the background thread captured. On no frame yet: sleep 50 ms, continue.
3. **Downscale.** `cv2.resize(frame, fx=0.5, fy=0.5)` — `DETECT_SCALE`. Detection runs on the quarter-area image (~98 ms/frame on a 1080p source against ~400 ms at full res); box coordinates are multiplied back up only for drawing.
4. **Detect.** `face_recognition.face_locations(rgb, model="hog")` — HOG + linear SVM, CPU. The CNN model is never used.
5. **No faces →** reset `unknown_streak` to 0, render "No face" preview, `sleep(interval)`, continue. **A frame with no face never contributes to any lock decision.**
6. **Encode & compare.** For each detected face:
   - `face_recognition.face_encodings(rgb, locations)` → 128-d vector.
   - `compare_faces(known, enc, tolerance)` → per-enrolled-encoding booleans; `is_known = any(matches)`. A match against **any one** stored encoding authorizes the face.
   - `face_distance(known, enc)` is computed on every face but is used **only** to render the numeric label `UNKNOWN (0.63)` in the preview. It does not affect the decision.
   - Sets `owner_present` / `intruder_detected` flags.
7. **Lock decision** — see §5.
8. **Preview render** — status bar, streak progress bar, `imshow` at 320 px width, `waitKey(1)` for the `q` handler.
9. **Pace.** `sleep(interval)` **only if `unknown_streak == 0`**. While a streak is building, the loop runs flat out with no sleep, so the streak completes as fast as the hardware allows.

---

## 5. Lock decision logic — exact

```python
owner_recently_seen = (time.time() - last_owner_seen) < owner_grace   # 5.0 s
if intruder_detected and not owner_present and not owner_recently_seen:
    unknown_streak += 1
    if unknown_streak >= streak_limit:                                # 2
        unknown_streak = 0
        if not _still_unknown_at_full_res(clean_frame, ...):          # see §5.1
            last_owner_seen = now                                     # false alarm
        elif now - last_lock_time > LOCK_COOLDOWN:                    # 15 s
            snap = save_snapshot(clean_frame)
            lock_screen()                  # lock first — see §8
            last_lock_time = now
            needs_recovery = True
            send_telegram_alert(snap)      # network I/O never gates the lock
else:
    # unknown face swallowed by the grace window is logged, throttled to 1/s
    unknown_streak = 0
```

Read as rules:

- **Locks only when all four hold:** an unrecognized face is in frame, no recognized face is in frame, no recognized face has been in frame within the last `--owner-grace` seconds (default 5.0), and the full-resolution re-check agrees the face is unrecognized.
- **Owner + guest in frame → never locks.** The guest is reported in the log as "owner present with guest".
- **Owner grace (5.0 s, `--owner-grace`)** covers momentary detector dropout on the owner's face — e.g. head turn, glare, occlusion — preventing a false lock in the instant the owner is missed but a colleague behind them is not. It suppresses the streak outright, so it is also the single largest contributor to time-to-lock; lower it for a faster reaction, at the cost of less cover for detector dropout. Suppressed frames now print `⏳ Unknown face ignored — owner seen Ns ago`, throttled to one line per second.
- **Cooldown (15 s)** is checked *after* the streak is already reset, so a suppressed lock does not leave a hot streak behind.
- **Time to lock, in practice:** measured on a 1080p camera with 14 enrolled encodings —

  | Step | Cost |
  |---|---|
  | grace-window suppression (owner seen recently) | 0–5000 ms |
  | idle `sleep(interval)` before the first qualifying frame | 0–200 ms |
  | 2 × (HOG detect 98 ms + encode 5 ms), no sleep between | 206 ms |
  | full-resolution re-check (§5.1), once, on the lock frame | ~410 ms |
  | `save_snapshot` imwrite | 4 ms |
  | `lock_screen` CGSession subprocess | ~100 ms |
  | **total** | **~720 ms typical, ~5.7 s worst case** |

  The Telegram upload (414 ms measured, 15 s timeout) used to sit in this budget and no longer does.
- **Walking away does not lock.** No-face frames reset the streak (line 361). See §10.

### 5.1 Full-resolution re-check

`_still_unknown_at_full_res()` runs on the frame that would otherwise lock, before the snapshot is taken. Detection normally runs on a `DETECT_SCALE` copy, and on a partially occluded face — a hand over the mouth, for instance — that small copy can yield a box covering only part of the face. Encoding that strip produces a meaningless embedding: one such frame scored the enrolled owner at **0.72** (well past the 0.55 tolerance) while the same frame at full resolution scored **0.37**. The re-check re-detects and re-encodes at full resolution; if any face there is recognized, the lock is cancelled and the frame counts as an owner sighting instead.

It costs ~410 ms on a 1080p frame, paid only on a frame already headed for a lock, so the idle path is unaffected. Replayed over the 28 saved snapshots it cancels no genuine intrusion: all 10 real intruders still lock, at distances 0.64–0.72.

---

## 6. Locking mechanism (`lock_screen()`, line 186)

Three subprocess attempts in order; the first with exit code 0 wins:

1. `/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession -suspend` — direct fast-user-switch lock, no UI side effects.
2. `osascript -e 'tell application "System Events" to keystroke "q" using {command down, control down}'` — synthesizes ⌘⌃Q. **Requires Accessibility permission** for the terminal/IDE running the script.
3. `pmset sleepnow` — sleeps the display; only locks if "require password after sleep" is enabled in system settings.

Each attempt has a 5-second timeout and swallows exceptions. If all three fail it prints a warning and continues running.

---

## 7. Post-lock recovery — the unusual part

After locking, the process does not simply keep looping. It sets `needs_recovery = True`, and on the next iteration:

1. `grabber.stop()` — flips a flag; the background thread exits its loop and releases the `VideoCapture` **itself**. The capture object is never released from another thread, deliberately.
2. `_wait_for_unlock()` — sleeps 3 s to let the lock engage, then every 2 s spawns `sys.executable -c "<probe>"`, a throwaway subprocess that opens camera 0, does one `read()`, releases, and exits 0/1. Success means the camera is readable again, which is treated as the signal that the screen is unlocked.
3. `os.execv(sys.executable, [sys.executable] + sys.argv)` — the process **replaces its own image** and restarts from scratch with the identical arguments.

The rationale, per the in-file comments: OpenCV/AVFoundation on macOS is fragile across the lock/unlock boundary and across threads, so rather than repairing state, FaceGuard throws the whole process away and starts a clean one. Camera probing is pushed into a subprocess so the parent never touches OpenCV during the wait.

Practical consequences: the banner reprints after every lock; `last_lock_time` and all streak state reset to zero on restart (the 15 s cooldown does not survive the restart, though the ≥3 s recovery sleep plus unlock polling covers most of it in practice); and any shell wrapping the process sees one long-lived PID, not a crash-restart loop.

---

## 8. Evidence capture and alerting

**Snapshot** (`save_snapshot()`, line 138) — writes `~/.faceguard_snapshots/intruder_YYYYMMDD_HHMMSS.jpg` from the current full-resolution frame. A clean copy of the frame is taken before any preview annotation is drawn, so snapshots are raw camera images in both preview and `--no-preview` modes. (Until this was fixed, preview-mode snapshots carried the boxes and labels, which made the saved evidence unusable for re-running detection against it.)

**Telegram** (`send_telegram_alert()`, line 148) — reads `FACEGUARD_TG_TOKEN` and `FACEGUARD_TG_CHAT_ID` from the environment; returns immediately if either is missing, making alerts opt-in. Builds a `multipart/form-data` body by hand (fixed boundary `FaceGuardBoundary`) and POSTs to `https://api.telegram.org/bot<TOKEN>/sendPhoto` with fields `chat_id`, `caption`, `photo`. 15-second timeout, all exceptions caught and printed. No retry.

This call is **synchronous on the main loop**, but it now runs *after* `lock_screen()`, so a slow or unreachable Telegram API delays only the post-lock recovery, never the lock itself. It previously ran before the lock and cost a measured 414 ms on a healthy network, with a 15 s ceiling on a bad one.

**Credentials** (`_load_dotenv()`, line 74) — hand-rolled parser for the repo-local `.env`: strips comments, splits on the first `=`, strips surrounding quotes, and uses `os.environ.setdefault`, so a real environment variable always wins over the file. `.env` is gitignored.

---

## 9. Concurrency and performance design

- **`_FrameGrabber`** (line 103) is a daemon thread that calls `cap.read()` in a tight loop and keeps only the latest frame under a `threading.Lock`. Purpose: OpenCV's internal capture buffer otherwise hands the main loop stale frames, adding latency between "intruder appears" and "intruder recognized". `read()` returns a `.copy()` so the consumer never races the producer. On read failure it sleeps 10 ms instead of spinning.
- **Ownership rule:** the grabber thread owns the `VideoCapture` and is the only code that releases it (line 124). `stop()` joins with a 3-second timeout.
- **Half-scale detection** (`DETECT_SCALE = 0.5`) is the main CPU saving — 98 ms/frame against ~400 ms at full resolution, with detection coordinates upscaled only for display. Lowering it further was tried and reverted: 0.33 (42 ms) and 0.25 (24 ms) both raise the rate of partial detection boxes on occluded faces, which is what §5.1 exists to catch. 0.25 additionally drops faces below ~150 px outright.
- **Adaptive pacing** — 200 ms idle sleep between checks, dropped entirely while a streak is building.
- **Fixed 320 px preview width** keeps the window small and the `imshow` cost low; the aspect ratio is preserved.

Everything is CPU-only. No GPU, no ML runtime, no model download at run time (dlib ships its own weights).

---

## 10. Configuration surface — as actually implemented

Constants (top of file, lines 90–99):

| Constant | Value | Meaning |
|---|---|---|
| `ENCODINGS_FILE` | `~/.faceguard_encodings.pkl` | Enrolled embeddings |
| `SNAPSHOTS_DIR` | `~/.faceguard_snapshots` | Intruder JPEGs |
| `TOLERANCE` | `0.55` | Max face distance to count as a match |
| `CHECK_INTERVAL` | `0.2` s | Idle sleep between checks |
| `FRAMES_TO_LOCK` | `2` | Consecutive qualifying frames to lock |
| `DETECT_SCALE` | `0.5` | Pre-detection downscale factor |
| `PREVIEW_WIDTH` | `320` px | Preview window width |
| `LOCK_COOLDOWN` | `15` s | Minimum gap between locks |
| `OWNER_GRACE` | `5.0` s | Suppression window after last owner sighting (`--owner-grace`) |

CLI flags, verbatim from `argparse` (lines 483–497) — this is the **complete** list:

```
--enroll                 Enroll (or re-enroll) your face
--reset                  Delete saved face data and exit
--tolerance FLOAT        default 0.55
--interval FLOAT         default 0.2
--streak INT             default 2
--owner-grace FLOAT      default 5.0
--no-preview             Run without camera preview window
```

`DETECT_SCALE`, `PREVIEW_WIDTH`, and `LOCK_COOLDOWN` are **not** exposed as flags — edit the source to change them.

### Documentation drift (verified against the code)

The shipped docs describe behavior the code does not have:

| Claim | Where | Reality |
|---|---|---|
| `--no-face-streak INT` flag, default 6 | `README.md` options table, `CLAUDE.md` constants table | **No such flag.** Not in `argparse`, not in `monitor()`'s signature. |
| "If no face is visible for `--no-face-streak` consecutive frames, the screen locks (covers walking away…)" | `README.md` § How It Works, step 4 | **False.** Line 361–370: no-face frames reset `unknown_streak` and `continue`. Walking away never locks the screen. |
| `NO_FACE_FRAMES_TO_LOCK = 6`, `no_face_streak` counter, lock logic at "lines 283–355" | `CLAUDE.md` | Not present in the current file. |
| `--interval` default `0.5` | `README.md`, `CLAUDE.md` | Actual default is `0.2`. |
| `--interval` default `1.0`, `--streak` default `3` | `faceguard.py` module docstring | Actual defaults are `0.2` and `2`. |

The no-face feature appears to have been removed (or never landed) while the prose describing it stayed. Anyone relying on FaceGuard to lock when they walk away from the desk is relying on a feature that does not exist.

---

## 11. Security properties and limits

**What it actually gives you:** a fast, local, opportunistic screen lock when a stranger's face enters the camera's view, plus a timestamped photo and a push alert.

**What it does not give you:**

- **No liveness detection.** A printed photo or a phone screen showing the enrolled face satisfies the matcher. Conversely, nothing stops an attacker from simply staying out of frame.
- **Nothing happens if no face is seen.** Covering the camera, tilting the lid, or an attacker approaching from outside the field of view all produce "no face", which is treated as benign.
- **Recognition quality is dlib's HOG + ResNet embedding at tolerance 0.55.** Loose enough to be robust to lighting and angle; loose enough that similar-looking faces, and especially close relatives, can match. `--tolerance 0.45` tightens this at the cost of false locks.
- **The pickle is a code-execution vector.** `pickle.load` on `~/.faceguard_encodings.pkl` (line 314) executes arbitrary code if an attacker can write that file. That attacker already has your user account, so this is a defense-in-depth gap rather than a primary hole — but a security tool loading untrusted pickles is worth knowing about.
- **No tamper resistance.** It is a foreground process in a terminal; `Ctrl+C` or closing the window disables it. There is no launchd job, no watchdog, no restart-on-death (the `execv` path only covers post-lock recovery).
- **Lock method 3 (`pmset sleepnow`) may not actually lock** if "require password after sleep/screen saver" is off. The success check is the exit code, which reports that the command ran, not that the screen is secured.
- **Failure mode is silent-open.** Any unhandled exception in the loop prints a message and exits the `while` — the process stops guarding rather than failing closed.

**Privacy posture is genuinely good:** face embeddings never leave the machine, recognition is entirely local, and the only outbound traffic is the optional Telegram POST — which does send your intruder photo to Telegram's servers, so that one is a deliberate privacy trade you opt into by filling in `.env`.

---

## 12. Required macOS permissions

| Permission | Needed for | Path |
|---|---|---|
| **Camera** | Everything | System Settings → Privacy & Security → Camera → *(Terminal / iTerm / IDE)* |
| **Accessibility** | Lock method 2 (AppleScript keystroke) only | System Settings → Privacy & Security → Accessibility |

The permission is granted to the *host application* running Python — the terminal emulator or IDE — not to `faceguard.py`. Running it from a different terminal means re-granting.

---

## 13. Files it touches

| Path | Written by | Notes |
|---|---|---|
| `~/.faceguard_encodings.pkl` | `enroll()` | Pickled list of 128-d NumPy vectors. Removed by `--reset`. |
| `~/.faceguard_snapshots/*.jpg` | `save_snapshot()` | Created on demand; never pruned or size-capped. Grows unbounded. |
| `<repo>/.env` | `install.sh` (copy of `.env.example`) | Read-only at runtime. Gitignored. |

---

## 14. Summary judgment

FaceGuard is a tight, deliberate ~520-line tool with a clear thesis: *use the webcam as a presence sensor, and when the wrong presence shows up, lock the machine immediately and take a photo*. The engineering choices are consistent with that thesis — half-scale detection and a frame-grabber thread to minimize the time between "intruder visible" and "screen locked"; skipping the idle sleep while a streak builds; locking before the network alert rather than after; owner-grace and cooldown windows to keep false locks from making it unusable in an office.

The two things to know before trusting it: it **only reacts to unrecognized faces, never to your absence** (contrary to its own README), and it is a **foreground convenience tool, not a hardened control** — no liveness check, no tamper resistance, and a fail-open error path.
