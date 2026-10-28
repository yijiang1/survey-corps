"""Dictation: tap Left Option, speak, tap it again -> the text is pasted into the focused app.

Speech-to-text runs on this Mac (mlx-whisper). Survey Corps starts this script (Settings -> Dictation), shows its
state and tidies each transcript with Argo; without the app running, the raw transcript is pasted.
Setup, in this folder: python3 -m venv .venv && .venv/bin/pip install mlx-whisper sounddevice pynput

    .venv/bin/python dictate.py            # run by hand (permissions then belong to your terminal app)
    .venv/bin/python dictate.py some.wav   # transcribe one file
"""
import json, os, queue, subprocess, sys, threading, time, urllib.request

import mlx_whisper
import numpy as np
import sounddevice as sd
from pynput import keyboard

MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
HOTKEY = keyboard.Key.alt_l  # tap it alone to start recording, tap again to stop
RATE = 16000
# Words Whisper should spell right; the app passes the list from Settings.
VOCAB = os.environ.get("DICTATE_VOCAB") or "ptychography, ptychographic, STEM, EMPAD, Argonne, APS, PtychoHub"
APP = "http://127.0.0.1:8765/api/dictate"
# Loudest 0.1 s of a clip below this = silence, skipped (Whisper would invent "Thank you." or "Subtitled by").
# Tune for your mic: each dictation prints its level.
MIN_LEVEL = float(os.environ.get("DICTATE_MIN_LEVEL", "0.01"))
MAX_SECS = float(os.environ.get("DICTATE_MAX_SECS", "300"))  # a recording left on stops (and is typed) after this


def tell(**d):
    """Report to Survey Corps. Returns its reply, or {} when it isn't running."""
    req = urllib.request.Request(APP, json.dumps(d).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except Exception:
        return {}


def transcribe(audio):
    return mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL, initial_prompt=VOCAB)["text"].strip()


def paste(text):
    old = subprocess.run("pbpaste", capture_output=True).stdout
    subprocess.run("pbcopy", input=text.encode())
    subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'])
    time.sleep(0.3)  # let the target app read the clipboard before restoring it
    subprocess.run("pbcopy", input=old)


chunks, recording, take = [], False, 0  # take: which recording this is, so a cap timer only stops its own


live = threading.Event()  # set by a recording's first real (not silent) audio


def on_audio(data, *_):
    if recording:
        chunks.append(data.copy())
        if data.any():
            live.set()


tapped, combo = 0.0, False


def on_press(key):
    global tapped, combo
    if key == HOTKEY:
        tapped, combo = time.time(), False
    else:
        combo = True  # Option used as a modifier (⌥→, ⌥-letter): not a tap


def on_release(key):
    # ponytail: the keyboard listener can't see the mouse, so a quick ⌥-click also toggles; add a mouse listener if it bites
    if key == HOTKEY and not combo and time.time() - tapped < 0.4:
        toggle()


def toggle():
    global recording, take
    if not recording:
        chunks.clear()
        recording, take = True, take + 1
        live.clear()
        mic_jobs.put("start")  # the mic thread reports "recording" once sound really comes in
        cap = threading.Timer(MAX_SECS, time_up, args=(take,))
        cap.daemon = True
        cap.start()
    else:
        recording = False
        mic_jobs.put("stop")  # the mic thread closes the mic, then hands the audio on


mic_jobs, mic_busy = queue.Queue(), 0.0  # "start" / "stop" for mic_thread; when its current job began (0 = idle)


def mic_thread():
    """Opens the mic for each recording and closes it after, so macOS shows its mic icon only while you dictate.
    Only this thread touches the audio stream: stopping it from the hotkey listener's thread deadlocked CoreAudio."""
    global mic_busy
    stream = None
    while True:
        job = mic_jobs.get()
        mic_busy = time.time()
        try:
            if job == "start" and stream is None:
                sd._terminate(); sd._initialize()  # 5 ms: use macOS's mic now (e.g. AirPods connected since we started)
                stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", callback=on_audio)
                stream.start()
                # AirPods send ~1 s of pure silence while switching to call mode: the pill turns red (and the
                # sound mutes) only when real sound arrives, so you know when to talk. 2 s cap: a mic that's
                # not allowed stays silent, and stopping then reports that.
                live.wait(2)
                tell(state="recording")
            elif job == "stop" and stream is not None:
                stream.stop(); stream.close(); stream = None
                audio = np.concatenate(chunks)[:, 0] if chunks else np.zeros(0, dtype=np.float32)
                threading.Thread(target=handle_safely, args=(audio,)).start()
        except Exception as e:  # no input device, device unplugged mid-recording...
            stream = None
            print(f"microphone error: {type(e).__name__}: {e}", flush=True)
            tell(state="error", error=f"microphone: {e}")
        mic_busy = 0.0


def mic_watchdog():
    while True:
        time.sleep(1)
        if mic_busy and time.time() - mic_busy > 5:  # CoreAudio hung (it has before): start over rather than freeze
            print("Error: the microphone stopped responding, restarting dictation", flush=True)
            tell(state="error", error="the microphone stopped responding; restarting dictation")
            os._exit(1)  # Survey Corps starts us again


def time_up(t):
    if recording and take == t:  # still the same recording: stop it as if Left Option was tapped
        print(f"recording hit the {MAX_SECS:.0f} s cap", flush=True)
        toggle()


def handle(audio):
    if len(audio) < RATE * 0.3:  # accidental tap
        return tell(state="ready")
    n = RATE // 10
    level = np.sqrt(np.mean(audio[:len(audio) // n * n].reshape(-1, n) ** 2, axis=1)).max()
    if level == 0:  # exact digital silence: macOS gives this when the mic isn't allowed
        return tell(state="error", error="the microphone gives only silence: allow Survey Corps under "
                                         "Privacy & Security → Microphone, then reopen it")
    if level < MIN_LEVEL:
        print(f"[level {level:.3f}] too quiet, skipped", flush=True)
        return tell(state="ready")
    tell(state="transcribing")
    t = time.time()
    raw = transcribe(audio)
    if not raw:
        return tell(state="ready")
    # the app tidies it (fixing misheard VOCAB words) and logs it
    text = tell(text=raw, secs=round(time.time() - t, 1), vocab=VOCAB).get("text") or raw
    print(f"[{time.time() - t:.1f}s, level {level:.3f}] {text}", flush=True)
    paste(text + " ")


def handle_safely(audio):
    try:
        handle(audio)
    except Exception as e:  # one dictation failed: say so (the pill and Settings show it) and keep listening
        print(f"dictation failed: {type(e).__name__}: {e}", flush=True)
        tell(state="error", error=f"{type(e).__name__}: {e}")


def exit_with_parent(ppid=os.getppid()):
    while os.getppid() == ppid:
        time.sleep(2)
    os._exit(0)  # Survey Corps quit: don't leave the hotkey listening


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(transcribe(sys.argv[1]))
        sys.exit()
    threading.Thread(target=exit_with_parent, daemon=True).start()
    transcribe(np.zeros(RATE, dtype=np.float32))  # load the model up front
    for f in (mic_thread, mic_watchdog):
        threading.Thread(target=f, daemon=True).start()
    tell(state="ready")
    print("Ready. Tap Left Option to dictate.", flush=True)
    with keyboard.Listener(on_press=on_press, on_release=on_release) as l:
        l.join()
