"""Word-level transcription of candidate windows with faster-whisper.

Captions are English only: words are marked `en` when Whisper heard English, and english.py decides
per clip whether the captions are clean English. Other languages are never translated or romanized
on screen (that gave garbled text); those clips simply get no captions.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .util import add_nvidia_dll_dirs, log, require_tool, run

_MODEL = None
_LATIN = re.compile(r"^[\x00-\x7FÀ-ɏ‘-‟…]*$")


def is_latin(text: str) -> bool:
    return bool(_LATIN.match(text))


def romanize(word: str) -> str:
    """Casual romanization, the way people type Telugu/Hindi in English letters."""
    if is_latin(word):
        return word
    try:
        from unidecode import unidecode
    except ImportError:
        return ""
    s = unidecode(word).lower()
    s = re.sub(r"([a-z])\1+", r"\1", s)        # neenu -> nenu, deevuddaa -> devuda
    s = re.sub(r"[^a-z0-9'?!.,-]", "", s)
    return s


def needs_redo(words: list[dict], cfg: dict) -> bool:
    """Cached words from an older version (no English marking, or translated/romanized text)."""
    return bool(words) and any("en" not in w for w in words)


def load_model(cfg: dict):
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    add_nvidia_dll_dirs()
    from faster_whisper import WhisperModel
    wc = cfg["whisper"]
    if wc["device"] in ("auto", "cuda"):
        try:
            _MODEL = WhisperModel(wc["model"], device="cuda", compute_type="float16")
            log(f"Whisper '{wc['model']}' on CUDA.")
            return _MODEL
        except Exception as e:
            if wc["device"] == "cuda":
                raise
            log(f"CUDA not usable ({type(e).__name__}: {str(e)[:120]}); falling back to CPU.")
    _MODEL = WhisperModel(wc["cpu_model"], device="cpu", compute_type="int8")
    log(f"Whisper '{wc['cpu_model']}' on CPU.")
    return _MODEL


def _run(model, wav: str, cfg: dict, task: str, language: str | None):
    return model.transcribe(
        wav, task=task, language=language, word_timestamps=True, condition_on_previous_text=False,
        vad_filter=True,
        # Lower threshold so quieter voices (Discord friends, teammates) are not cut as silence.
        vad_parameters={"threshold": float(cfg["whisper"].get("vad_threshold", 0.3)),
                        "min_silence_duration_ms": 300, "speech_pad_ms": 200},
    )


def transcribe_window(audio: Path, start: float, end: float, cfg: dict) -> list[dict]:
    """Return words [{w, s, e, en}] with absolute stream timestamps.

    No translation (it produced garbled English from Telugu): speech is transcribed as it is, and each
    word is marked `en` only when Whisper heard English. Captions, transcripts for the agent and titles
    use English words only (see english.py); non-English words are kept just for timing (music ducking)."""
    require_tool("ffmpeg")
    model = load_model(cfg)
    wc = cfg["whisper"]
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "seg.wav"
        # dynaudnorm lifts quiet voices (Discord) before Whisper hears them.
        proc = run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(audio),
                    "-t", f"{end - start:.3f}", "-vn", "-af", "dynaudnorm=f=150:g=15:p=0.9:m=10",
                    "-ac", "1", "-ar", "16000", str(wav)])
        if proc.returncode != 0 or not wav.exists():
            return []
        segments, info = _run(model, str(wav), cfg, "transcribe", wc.get("language"))
        spoken, prob = info.language, float(info.language_probability or 0)
        english = spoken == "en" and prob >= float(wc.get("english_min_probability", 0.6))
        words = []
        for seg in segments:
            for w in seg.words or []:
                txt = w.word.strip()
                if txt:
                    words.append({"w": txt, "s": round(start + w.start, 2), "e": round(start + w.end, 2),
                                  "en": english and is_latin(txt)})
    if not english and words:
        log(f"   speech is {spoken} ({prob:.0%}): no captions for this part (English only)")
    return words
