"""Enroll an audio file as a voice profile — no mic needed.

    .venv/Scripts/python tools/enroll_clip.py NAME path/to/clip.wav

Writes NAME -> {emb, f0} into PROFILES_PATH (default
server/data/profiles.json), exactly like a mic enrollment, so the web UI
lists it and clone can target it. With MODEL_MODULE=server.vc_model the
(level-normalized) clip itself is stored as the zero-shot reference; with
the built-in model the spectral feature vector is stored.

Non-WAV sources (YouTube rips, mp3/m4a): convert first, e.g.

    ffmpeg -i trump.mp4 -ar 16000 -ac 1 trump.wav
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.audio import resample_linear
from server.config import get_settings
from server.model import load_model
from server.profiles import ProfileStore
from server.vc_model import read_wav


def enroll(name: str, wav_path: str) -> tuple[float, float | None]:
    s = get_settings()
    clip, sr = read_wav(wav_path)
    if sr != s.model_sr:
        clip = resample_linear(clip, sr, s.model_sr)
    model = load_model()
    emb = model.embed(clip)
    analyze = getattr(model, "analyze_clip", None)
    f0 = analyze(clip).get("f0") if analyze else None
    store = ProfileStore(s.profiles_path)
    store.add(name, emb, f0)
    return clip.size / s.model_sr, f0


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    name, wav = sys.argv[1], sys.argv[2]
    secs, f0 = enroll(name, wav)
    s = get_settings()
    print(f"enrolled {name!r}: {secs:.1f}s clip, f0={f0} "
          f"-> {s.profiles_path}")
    print("profiles:", ", ".join(ProfileStore(s.profiles_path).names()))


if __name__ == "__main__":
    main()
