"""SHUTDOWN: the announcer call under the ELIMINATED card (the alive page,
settings.shutdownSound).

A female voice -- Windows' own Zira, so nothing to license -- made into a
game announcer: tightened and brightened, doubled, a short arena echo, on
an impact, with a power-down whine falling away after the word.

    python overlay/assets/sfx/make_shutdown_sfx.py

Windows only (it speaks through System.Speech). Writes shutdown.wav here.
"""
import os
import subprocess
import tempfile
import wave

import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "shutdown.wav")
SR = 44100
VOICE = "Microsoft Zira Desktop"
# Said as one word, stressed on the first syllable -- SHUT-down, the way a
# game announces it.
SSML = ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">'
        '<prosody rate="-12%" pitch="+6%" volume="x-loud"><emphasis level="strong">Shutdown!</emphasis></prosody>'
        '</speak>')


def speak():
    """The raw call, mono at SR."""
    path = os.path.join(tempfile.mkdtemp(), "voice.wav")
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.SelectVoice('%s');"
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(%d, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono);"
        "$s.SetOutputToWaveFile('%s', $f);"
        "$s.SpeakSsml('%s');"
        "$s.Dispose()" % (VOICE, SR, path, SSML.replace("'", "''"))
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    return read_wav(path)


def read_wav(path):
    with wave.open(path, "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768
        if w.getnchannels() == 2:
            data = data.reshape(-1, 2).mean(axis=1)
    return data


def trim(x, floor_db=-45):
    loud = np.flatnonzero(np.abs(x) > 10 ** (floor_db / 20))
    return x[max(0, loud[0] - 200):loud[-1] + 400] if len(loud) else x


def peaking(x, f0, gain_db, q=1.0):
    """RBJ peaking EQ."""
    a = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * f0 / SR
    alpha = np.sin(w0) / (2 * q)
    b = [1 + alpha * a, -2 * np.cos(w0), 1 - alpha * a]
    den = [1 + alpha / a, -2 * np.cos(w0), 1 - alpha / a]
    return signal.lfilter(b, den, x)


def compress(x, threshold_db=-20, ratio=4.0, attack=0.003, release=0.08):
    env = np.zeros_like(x)
    ga, gr = np.exp(-1 / (attack * SR)), np.exp(-1 / (release * SR))
    level = 0.0
    for i, v in enumerate(np.abs(x)):
        level = (ga if v > level else gr) * level + (1 - (ga if v > level else gr)) * v
        env[i] = level
    over = 20 * np.log10(np.maximum(env, 1e-6)) - threshold_db
    gain_db = np.where(over > 0, -over * (1 - 1 / ratio), 0.0)
    return x * 10 ** (gain_db / 20)


def shifted(x, speed):
    """The same take a touch faster or slower -- a second voice for the double."""
    return signal.resample(x, int(len(x) / speed))


def place(bed, x, at):
    start = int(at * SR)
    end = min(len(bed), start + len(x))
    bed[start:end] += x[:end - start]


def voice_chain(raw):
    v = trim(raw)
    v = signal.sosfilt(signal.butter(2, 110, "highpass", fs=SR, output="sos"), v)
    v = peaking(v, 250, -2.5, 0.9)          # less box
    v = peaking(v, 3200, 5.0, 0.8)          # presence: cuts through the game audio
    v = peaking(v, 9000, 3.0, 0.7)          # air
    v = v / np.max(np.abs(v))
    v = compress(v, -22, 5.0)
    v = v / np.max(np.abs(v))
    v = np.tanh(2.2 * v) / np.tanh(2.2)     # drive: the announcer edge
    # Doubled: two copies a hair off in pitch and time, under the dry take.
    dbl = np.zeros(len(v) + SR // 10)
    place(dbl, v, 0)
    place(dbl, 0.28 * shifted(v, 1.010), 0.016)
    place(dbl, 0.28 * shifted(v, 0.990), 0.027)
    return dbl


def arena(x):
    """A short arena: two echoes, darker each time, and a tail."""
    out = np.zeros(len(x) + int(1.6 * SR))
    place(out, x, 0)
    lp = signal.butter(2, 3500, "lowpass", fs=SR, output="sos")
    place(out, 0.22 * signal.sosfilt(lp, x), 0.135)
    place(out, 0.11 * signal.sosfilt(lp, signal.sosfilt(lp, x)), 0.27)
    rng = np.random.default_rng(7)
    n = int(1.1 * SR)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.22 * SR))
    ir = signal.sosfilt(signal.butter(2, [300, 5000], "bandpass", fs=SR, output="sos"), ir)
    tail = signal.fftconvolve(x, ir)[:len(out)]
    tail = tail / np.max(np.abs(tail))
    place(out, 0.16 * tail, 0.02)
    return out


def impact():
    """The hit under the first syllable: a falling sub and a short thump."""
    n = int(0.75 * SR)
    t = np.arange(n) / SR
    freq = 38 + 52 * np.exp(-t / 0.09)
    sub = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t / 0.22)
    rng = np.random.default_rng(3)
    thump = signal.sosfilt(signal.butter(2, 900, "lowpass", fs=SR, output="sos"),
                           rng.standard_normal(n)) * np.exp(-t / 0.025)
    thump = thump / np.max(np.abs(thump))
    return 0.9 * sub + 0.35 * thump


def power_down(seconds=1.0):
    """The whine of something switching off: a buzzy tone falling away."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    freq = 55 + 700 * np.exp(-t / (seconds * 0.28))
    phase = 2 * np.pi * np.cumsum(freq) / SR
    tone = signal.sawtooth(phase) * 0.6 + np.sin(phase * 0.5) * 0.4
    sweep = signal.sosfilt(signal.butter(2, 2200, "lowpass", fs=SR, output="sos"), tone)
    env = np.minimum(1, t / 0.03) * (1 - t / seconds) ** 1.6
    return sweep * env


def build(raw):
    voice = arena(voice_chain(raw))
    word = len(trim(raw)) / SR
    bed = np.zeros(len(voice) + SR // 2)
    place(bed, 0.55 * impact(), 0)
    place(bed, voice, 0.02)
    place(bed, 0.16 * power_down(), 0.02 + word * 0.82)
    bed = bed[:int(2.0 * SR)]
    fade = int(0.35 * SR)
    bed[-fade:] *= np.linspace(1, 0, fade) ** 2
    return bed / np.max(np.abs(bed)) * 10 ** (-1 / 20)     # -1 dBFS


def write_wav(path, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


if __name__ == "__main__":
    write_wav(OUT, build(speak()))
    print("wrote", OUT)
