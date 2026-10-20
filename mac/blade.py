# Makes mac/blade.wav, Levi's sound: a blade drawn fast (rising swish of noise), then a bright metal ring.
import math, random, struct, wave
R, N = 44100, int(44100 * 0.55)
random.seed(7)
out, lp = [], 0.0
for i in range(N):
    t = i / R
    # swish: noise through a one-pole filter whose cutoff rises over 80 ms, with a quick swell and fade
    a = min(1, t / 0.08) * max(0, 1 - (t - 0.06) / 0.1) if t < 0.16 else 0
    k = 0.15 + 0.8 * min(1, t / 0.08)
    lp += k * (random.uniform(-1, 1) - lp)
    swish = (random.uniform(-1, 1) - lp) * a * 0.35
    # ring: inharmonic partials like struck steel, starting as the swish peaks
    r = t - 0.07
    ring = sum(g * math.sin(2 * math.pi * f * r) * math.exp(-r * d)
               for f, g, d in [(2960, .30, 7), (4410, .22, 10), (6280, .14, 14), (8870, .08, 20)]) if r > 0 else 0
    out.append(max(-1, min(1, swish + ring * min(1, r / 0.004) if r > 0 else swish)))
with wave.open(__file__.replace("blade.py", "blade.wav"), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(R)
    w.writeframes(b"".join(struct.pack("<h", int(s * 26000)) for s in out))
