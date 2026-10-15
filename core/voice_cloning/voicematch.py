"""
core/voice_cloning/voicematch.py
─────────────────────────────────
"Voice match": find the engine voice that sounds most like a clip, so it can
be used as the clone's base voice.

Voice bank (built once, then only for new voices)
    Every local engine voice (Kokoro, MeloTTS, Piper, system voices) says a
    short sentence in its own language at speed 1.0. For each sample we store:
      * voiceprint: 192-dim CAMPPlus embedding (Seed-VC worker, CPU)
      * pitch:      median F0 (pitch.median_f0)
      * speed:      articulation rate, words (or CJK characters) per second
                    of non-silent audio
    Stored in <runtime root>/voice_bank/bank.json with the WAVs in samples/.

Clip analysis
    The same three measurements on the user's clip (trimmed, and cleaned when
    music removal is on), plus language and transcript from Whisper.

Ranking
    Candidates in the clip's language (when Whisper is confident and the bank
    has that language) and of the clip's gender, scored by voiceprint
    similarity minus a small pitch penalty: score = cosine - 0.015 x
    |semitones apart|. The gender is the user's choice (Male / Female) or, in
    Auto, decided from the clip (decide_gender).

Gender
    A bank voice's gender comes from what is known about it (Kokoro's
    af_/am_/bf_/bm_ prefix, the Piper dataset, MeloTTS English, Windows voice
    names), and from its pitch only when nothing else is known. Pitch alone is
    not enough: in the bank af_alloy measures 142 Hz while am_eric and
    am_santa measure 154-155 Hz.

Nothing here imports torch; the voiceprints come from the worker.
"""

import hashlib
import json
import math
import os
import re
import shutil
import time
from pathlib import Path

from core.voice_cloning import runtime

BANK_SCHEMA = 1
PITCH_PENALTY = 0.015          # score lost per semitone of pitch difference
LANG_CONFIDENCE = 0.6          # Whisper language probability needed to filter by language
GENDERS = ("auto", "male", "female")
# Clip pitch bands for Auto. Between them the voiceprint decides (decide_gender).
CLIP_MALE_BELOW = 145.0
CLIP_FEMALE_ABOVE = 180.0
VOICE_GENDER_SPLIT = 165.0     # bank voices with no known gender: below = male
# pYIN parks breathy, whispered or music-backed frames at its 65 Hz floor. Measured:
# Video-8115.mp4 had 54% of its voiced frames under 75 Hz (median 69 Hz) while its
# voiceprint is clearly female. Past this share the median is not trusted.
F0_FLOOR_HZ = 75.0
F0_FLOOR_SHARE = 0.3
# Voiceprint vote: below this gap between the male and female scores Auto compares
# all voices. Measured gaps on real clips: 0.04 to 0.17, one unclear clip 0.01.
GENDER_MARGIN = 0.02

# One sentence per language, ~5-7 s at speed 1.0. Changing a sentence changes
# the bank version, so the bank is rebuilt.
SENTENCES = {
    "en": "The quick brown fox jumps over the lazy dog, while the rain keeps falling softly outside the window.",
    "es": "El rápido zorro marrón salta sobre el perro perezoso, mientras la lluvia cae suavemente afuera.",
    "fr": "Le renard brun rapide saute par-dessus le chien paresseux, pendant que la pluie tombe doucement.",
    "hi": "तेज़ भूरी लोमड़ी आलसी कुत्ते के ऊपर कूदती है, जबकि बाहर धीरे-धीरे बारिश हो रही है।",
    "it": "La rapida volpe marrone salta sopra il cane pigro, mentre fuori la pioggia cade piano.",
    "ja": "素早い茶色の狐がのんびりした犬を飛び越え、外では静かに雨が降っています。",
    "pt": "A rápida raposa marrom pula sobre o cão preguiçoso, enquanto a chuva cai suavemente lá fora.",
    "zh": "敏捷的棕色狐狸跳过了懒惰的狗，窗外的雨正在轻轻地下着。",
    "ko": "빠른 갈색 여우가 게으른 개를 뛰어넘고, 밖에서는 비가 조용히 내리고 있습니다.",
}
LANG_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "hi": "Hindi", "it": "Italian",
              "ja": "Japanese", "pt": "Portuguese", "zh": "Chinese", "ko": "Korean"}
_KOKORO_ISO = {"a": "en", "b": "en", "e": "es", "f": "fr", "h": "hi", "i": "it", "j": "ja", "p": "pt", "z": "zh"}
_MELO_ISO = {"EN": "en", "ES": "es", "FR": "fr", "ZH": "zh", "JP": "ja", "KR": "ko"}
_CJK = {"ja", "zh"}
# The analyzer is English-only (user decision, 2026-10-10): only English engine
# voices go into the bank and the ranking.
BANK_LANGS = {"en"}
# MeloTTS languages that synthesize in the app's venv (torch 2.5.1). French failed
# after an 850 MB download: its BERT checkpoint is a .bin file and transformers
# refuses torch.load on torch < 2.6. Chinese / Japanese / Korean pull similar
# BERT checkpoints (assumed to fail the same way, not each verified), so the
# bank skips them rather than download gigabytes onto C:.
MELO_BANK_LANGS = {"EN"}


def _bank_version():
    return hashlib.sha1(json.dumps(SENTENCES, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]


# ── candidates ──────────────────────────────────────────────────────

def candidates(engine_names, system_voices):
    """Every local engine voice as {"id", "engine", "kind", "lang", "label", "selection", "overrides"}.

    engine_names: names in the TTS Engine dropdown. system_voices: [(name, id)] for pyttsx3.
    Online engines (gTTS, Edge) are left out.
    """
    from core.constants import KOKORO_LANGS, KOKORO_VOICES, MELO_VOICES, PIPER_VOICES
    from core.voice_selection import engine_kind
    out = []
    by_kind = {}
    for name in engine_names:
        kind = engine_kind(name)
        if kind in ("kokoro", "melo", "piper", "pyttsx3"):
            by_kind.setdefault(kind, name)
    short = {k: v.split(" (")[0] for k, v in by_kind.items()}

    if "kokoro" in by_kind:
        for lang_label, code in KOKORO_LANGS.items():
            iso = _KOKORO_ISO.get(code)
            for voice in KOKORO_VOICES.get(code, {}):
                out.append({"id": f"kokoro:{code}:{voice}", "engine": by_kind["kokoro"], "kind": "kokoro",
                            "lang": iso, "label": f"{short['kokoro']} · {voice} ({lang_label})",
                            "selection": {"engine": by_kind["kokoro"], "kind": "kokoro", "lang": lang_label,
                                          "voice": voice},
                            "overrides": {"kokoro_lang_code": code, "kokoro_voice_key": voice, "kokoro_speed": 1.0}})
    if "melo" in by_kind:
        for disp, cfg in MELO_VOICES.items():
            if cfg.get("language") not in MELO_BANK_LANGS:
                continue
            out.append({"id": f"melo:{cfg.get('speaker')}", "engine": by_kind["melo"], "kind": "melo",
                        "lang": _MELO_ISO.get(cfg.get("language")), "label": f"{short['melo']} · {disp}",
                        "selection": {"engine": by_kind["melo"], "kind": "melo", "voice": disp},
                        "overrides": {"melo_voice_cfg": cfg, "melo_speed": 1.0}})
    if "piper" in by_kind:
        for disp, key in PIPER_VOICES.items():
            out.append({"id": f"piper:{key}", "engine": by_kind["piper"], "kind": "piper",
                        "lang": "en" if str(key).startswith("en") else None, "label": f"{short['piper']} · {disp}",
                        "selection": {"engine": by_kind["piper"], "kind": "piper", "voice": disp},
                        "overrides": {"piper_voice_key": key}})
    if "pyttsx3" in by_kind:
        for disp, vid in system_voices or []:
            low = disp.lower()
            lang = next((iso for iso, n in LANG_NAMES.items() if n.lower() in low), None)
            out.append({"id": "sys:" + hashlib.sha1(str(vid).encode()).hexdigest()[:12], "engine": by_kind["pyttsx3"],
                        "kind": "pyttsx3", "lang": lang, "label": f"System · {disp}",
                        "selection": {"engine": by_kind["pyttsx3"], "kind": "pyttsx3", "voice": disp},
                        "overrides": {"pyttsx3_voice_id": vid, "pyttsx3_rate": 175, "pyttsx3_volume": 0.9}})
    return [c for c in out if c["lang"] in BANK_LANGS]


# ── bank storage ────────────────────────────────────────────────────

def bank_dir() -> Path:
    return runtime.runtime_root() / "voice_bank"


def load_bank() -> dict:
    try:
        with open(bank_dir() / "bank.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema") == BANK_SCHEMA and data.get("version") == _bank_version():
            return data
    except (OSError, ValueError):
        pass
    return {"schema": BANK_SCHEMA, "version": _bank_version(), "voices": {}}


def save_bank(bank) -> None:
    d = bank_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "bank.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bank, f, ensure_ascii=False)
    os.replace(tmp, d / "bank.json")


def prune(bank, cands) -> int:
    """Drop bank entries (and sample WAVs) for voices that are no longer candidates."""
    keep = {c["id"] for c in cands}
    gone = [vid for vid in bank["voices"] if vid not in keep]
    for vid in gone:
        wav = bank["voices"][vid].get("wav")
        if wav:
            try:
                (bank_dir() / "samples" / wav).unlink()
            except OSError:
                pass
        del bank["voices"][vid]
    if gone:
        save_bank(bank)
    return len(gone)


def _pkg_version(name):
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:
        return "none"


def _file_sig(path):
    try:
        return f"{os.path.basename(str(path))}:{os.path.getsize(path)}"
    except OSError:
        return f"{os.path.basename(str(path))}:missing"


_KIND_FP = {}


def engine_fingerprint(c) -> str:
    """What decides how a bank sample sounds: engine package versions and model files.

    When it changes (engine update, new Kokoro weights, a replaced Piper voice),
    that voice's sample is synthesized and measured again automatically.
    """
    kind = c["kind"]
    if kind in ("kokoro", "melo") and kind in _KIND_FP:
        return _KIND_FP[kind]
    if kind == "kokoro":
        try:
            from engines.kokoro_engine import _weight_paths
            files = [_file_sig(f) for f in _weight_paths()]
        except Exception:
            files = ["weights:unknown"]
        parts = ["kokoro", _pkg_version("kokoro"), _pkg_version("kokoro-onnx")] + files
    elif kind == "melo":
        parts = ["melo", _pkg_version("melotts")]
    elif kind == "piper":
        from core import paths
        key = c["overrides"].get("piper_voice_key", "")
        parts = ["piper", _pkg_version("piper-tts"),
                 _file_sig(paths.bundle_dir() / "models" / "piper" / f"{key}.onnx")]
    else:
        parts = [kind, _pkg_version("pyttsx3"), str(c["overrides"].get("pyttsx3_voice_id", ""))]
    fp = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    if kind in ("kokoro", "melo"):
        _KIND_FP[kind] = fp
    return fp


def _app_version():
    from core.version import __version__
    return __version__


def missing(bank, cands):
    """Candidates whose sample must be (re)made, decided automatically:

      * not in the bank yet (a new voice);
      * made with a different engine or model version (fingerprint changed);
      * failed under an earlier app version (retried once per app version).

    Entries from before fingerprints existed adopt the current one instead of
    being rebuilt.
    """
    v = bank["voices"]
    out = []
    for c in cands:
        e = v.get(c["id"])
        if e is None:
            out.append(c)
            continue
        fp = engine_fingerprint(c)
        if "fp" not in e:
            e["fp"] = fp                      # migrate: assume it was made with the current engine
        if e.get("error"):
            if e.get("app_version") != _app_version():
                out.append(c)
        elif e["fp"] != fp:
            out.append(c)
    return out


def needs_work(bank, cands) -> bool:
    """True when the bank must be updated before ranking."""
    if missing(bank, cands):
        return True
    return any(c["id"] in bank["voices"] and bank["voices"][c["id"]].get("wav")
               and not bank["voices"][c["id"]].get("emb") and not bank["voices"][c["id"]].get("error")
               for c in cands)


def clear_bank() -> None:
    shutil.rmtree(bank_dir(), ignore_errors=True)


# ── measurements ────────────────────────────────────────────────────

def _units(text, lang):
    """Words, or characters for Chinese / Japanese."""
    text = text or ""
    if lang in _CJK:
        return len(re.findall(r"[぀-ヿ㐀-鿿]", text))
    return len(re.findall(r"\w+", text))


def speech_seconds(path) -> float:
    """Seconds of non-silent audio (pauses over 0.3 s removed)."""
    from pydub import silence as pydub_silence
    from core.audio_io import load_segment
    seg = load_segment(str(path)).set_channels(1)
    if seg.dBFS == float("-inf"):
        return 0.0
    spans = pydub_silence.detect_nonsilent(seg, min_silence_len=300, silence_thresh=min(-40.0, seg.dBFS - 16))
    return sum(b - a for a, b in spans) / 1000.0


def rate(text, lang, path):
    secs = speech_seconds(path)
    n = _units(text, lang)
    return round(n / secs, 2) if secs > 0.5 and n else None


def gender_guess(f0, unsure=False):
    """A rough label from median pitch only. Pitch overlaps between groups."""
    if not f0:
        return "unknown"
    if unsure:
        return "pitch unsteady, not used for gender"
    if f0 < 145:
        return "likely male"
    if f0 < 175:
        return "unclear (low female or high male)"
    if f0 < 255:
        return "likely female"
    return "high: likely female or a child"


# Known engine voice genders. Piper names its voices after the training dataset.
# LJSpeech, Lessac, Amy, Kathleen, Kristin, Jenny (Dioco) are female speakers;
# Ryan, Joe, John, Kusal, Alan, Northern English Male are male.
_PIPER_GENDER = {"ljspeech": "female", "lessac": "female", "amy": "female", "kathleen": "female",
                 "kristin": "female", "jenny_dioco": "female", "hfc_female": "female",
                 "ryan": "male", "joe": "male", "john": "male", "kusal": "male", "alan": "male",
                 "hfc_male": "male", "norman": "male", "northern_english_male": "male"}
# Windows (SAPI5 / OneCore) voice first names.
_SYSTEM_GENDER = {"david": "male", "mark": "male", "george": "male", "james": "male", "richard": "male",
                  "ravi": "male", "sean": "male", "guy": "male",
                  "zira": "female", "hazel": "female", "susan": "female", "heera": "female",
                  "catherine": "female", "linda": "female", "eva": "female", "aria": "female",
                  "jenny": "female", "helen": "female"}


def voice_gender(vid, entry):
    """'male' or 'female' for a bank voice (None when unknown and unmeasured)."""
    kind = entry.get("kind")
    if kind == "kokoro":
        voice = vid.rsplit(":", 1)[-1]
        if len(voice) > 1 and voice[1] in "fm":
            return "female" if voice[1] == "f" else "male"
    elif kind == "melo" and entry.get("lang") == "en":
        return "female"                       # all five English speakers (bank: 171-234 Hz)
    elif kind == "piper":
        key = vid.split(":", 1)[-1].lower()
        name = key.split("-")[1] if key.count("-") >= 2 else key
        if name in _PIPER_GENDER:
            return _PIPER_GENDER[name]
    elif kind == "pyttsx3":
        words = re.findall(r"[a-z]+", (entry.get("label") or "").lower())
        if "female" in words or "woman" in words:
            return "female"
        if "male" in words or "man" in words:
            return "male"
        for w in words:
            if w in _SYSTEM_GENDER:
                return _SYSTEM_GENDER[w]
    f0 = entry.get("f0")
    if f0:
        return "male" if f0 < VOICE_GENDER_SPLIT else "female"
    return None


def decide_gender(bank, cands, clip):
    """Auto mode: (gender, reason) for the clip, gender 'male', 'female' or None.

    Clear pitch decides on its own. In the overlap band (145-180 Hz) the clip's
    voiceprint is compared with the bank: the mean similarity of the 3 closest
    male voices against the 3 closest female voices.
    """
    import numpy as np
    f0 = clip.get("f0")
    if clip.get("f0_unsure"):
        f0 = None                             # many frames at the tracker's floor: voiceprint decides
    if f0 and f0 < CLIP_MALE_BELOW:
        return "male", f"pitch {f0:.0f} Hz"
    if f0 and f0 > CLIP_FEMALE_ABOVE:
        return "female", f"pitch {f0:.0f} Hz"
    if not clip.get("emb"):
        if f0:
            return ("male" if f0 < VOICE_GENDER_SPLIT else "female"), f"pitch {f0:.0f} Hz (unclear)"
        return None, "pitch not measured"
    c = np.asarray(clip["emb"], dtype=np.float32)
    ids = {x["id"] for x in cands}
    sims = {"male": [], "female": []}
    for vid, e in bank["voices"].items():
        g = voice_gender(vid, e)
        if vid in ids and e.get("emb") and g in sims:
            sims[g].append(float(np.dot(c, np.asarray(e["emb"], dtype=np.float32))))
    top = {g: (sum(sorted(v, reverse=True)[:3]) / min(3, len(v)) if v else None) for g, v in sims.items()}
    pitch = (f"pitch {f0:.0f} Hz is in between" if f0
             else "pitch unsteady" if clip.get("f0_unsure") else "pitch not measured")
    if top["male"] is None or top["female"] is None:
        if f0:
            return ("male" if f0 < VOICE_GENDER_SPLIT else "female"), f"{pitch}, decided by pitch"
        return None, pitch
    if abs(top["male"] - top["female"]) < GENDER_MARGIN:
        return None, (f"{pitch}, voiceprint unclear ({top['male']:.2f} male vs {top['female']:.2f} female), "
                      "comparing all voices. Pick Male or Female to choose")
    g = "male" if top["male"] > top["female"] else "female"
    return g, f"{pitch}, voiceprint closer to {g} voices ({top[g]:.2f} vs {top['female' if g == 'male' else 'male']:.2f})"


def rate_unit(lang):
    return "characters/s" if lang in _CJK else "words/s"


# ── building the bank ───────────────────────────────────────────────

def build_bank(synth, eng_settings, cands, progress=None, check_cancel=None):
    """Synthesize and measure every candidate missing from the bank. Resumable.

    synth: the app's core.synth.Synthesizer.
    eng_settings: EnginePanel.get_settings() snapshot used as defaults.
    progress(fraction, message).
    Returns the bank.
    """
    from core.audio_io import load_segment
    from core.voice_cloning import get_client
    from core.voice_cloning import pitch as pitch_mod

    say = progress or (lambda f, m: None)
    bank = load_bank()
    prune(bank, cands)
    samples = bank_dir() / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    todo = missing(bank, cands)              # new, engine changed, or failed in an older version
    t0 = time.monotonic()

    for i, c in enumerate(todo):
        if check_cancel:
            check_cancel()
        left = ""
        if i >= 2:
            per = (time.monotonic() - t0) / i
            from core.voice_cloning.estimate import fmt
            left = f" · {fmt(per * (len(todo) - i) + 3 * len(todo))} left"
        say(0.85 * i / max(1, len(todo)), f"Voice bank {i + 1}/{len(todo)}: {c['label']}{left}")
        entry = {"label": c["label"], "kind": c["kind"], "lang": c["lang"], "engine": c["engine"],
                 "fp": engine_fingerprint(c), "app_version": _app_version(),
                 "selection": c["selection"]}
        text = SENTENCES.get(c["lang"] or "en", SENTENCES["en"])
        tmp = Path(bank_dir()) / "tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            settings = dict(eng_settings, engine_name=c["engine"], **c["overrides"])
            res = synth.synthesize(text, str(tmp), settings,
                                   check_cancel=lambda: bool(check_cancel and _cancelled(check_cancel)))
            wav = samples / (hashlib.sha1(c["id"].encode()).hexdigest()[:16] + ".wav")
            load_segment(res["playback_file"]).set_channels(1).export(str(wav), format="wav")
            entry.update(wav=wav.name, f0=pitch_mod.median_f0(str(wav), max_seconds=10),
                         rate=rate(text, c["lang"] or "en", str(wav)), emb=None)
        except Exception as e:
            if check_cancel and _cancelled(check_cancel):
                raise
            entry["error"] = f"{type(e).__name__}: {e}"[:200]
        bank["voices"][c["id"]] = entry
        save_bank(bank)                       # resumable after cancel or crash
    shutil.rmtree(Path(bank_dir()) / "tmp", ignore_errors=True)

    need = [vid for vid, e in bank["voices"].items() if e.get("wav") and not e.get("emb") and not e.get("error")]
    if need:
        say(0.88, f"Measuring {len(need)} voiceprints…")
        embs = get_client().embed([str(samples / bank["voices"][vid]["wav"]) for vid in need],
                                  status_cb=lambda m: say(0.9, m))
        for vid, emb in zip(need, embs):
            if emb:
                bank["voices"][vid]["emb"] = emb
            else:
                bank["voices"][vid]["error"] = "voiceprint failed"
        save_bank(bank)
    say(1.0, "Voice bank ready")
    return bank


def _cancelled(check_cancel):
    try:
        check_cancel()
        return False
    except Exception:
        return True


_SPEED_KEYS = {"kokoro": "kokoro_speed", "melo": "melo_speed", "pyttsx3": "pyttsx3_rate"}


def speak_settings(eng_settings, cand, speed=None) -> dict:
    """The EnginePanel.get_settings()-style dict speak() uses for *cand* at *speed*."""
    overrides = dict(cand["overrides"])
    key = _SPEED_KEYS.get(cand["kind"])
    if key and speed is not None:
        overrides[key] = speed
    return dict(eng_settings, engine_name=cand["engine"], **overrides)


def speak(synth, eng_settings, cand, text, out_wav, speed=None, check_cancel=None):
    """Synthesize text with one candidate voice into a mono WAV at out_wav.

    speed: a suggested_speed() value (Kokoro / Melo speed, pyttsx3 WPM); None keeps
    the candidate's default. Used by the analyzer's Result (the clip's transcript).
    """
    import tempfile
    from core.audio_io import load_segment
    settings = speak_settings(eng_settings, cand, speed)
    tmp = tempfile.mkdtemp(prefix="adits_vs_speak_", dir=os.path.dirname(str(out_wav)))
    try:
        res = synth.synthesize(text, tmp, settings,
                               check_cancel=lambda: bool(check_cancel and _cancelled(check_cancel)))
        load_segment(res["playback_file"]).set_channels(1).export(str(out_wav), format="wav")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return str(out_wav)


# ── clip analysis and ranking ───────────────────────────────────────

def analyze_clip(path, embed_fn, progress=None):
    """{"f0", "f0_unsure", "gender", "lang", "lang_prob", "rate", "text", "emb", "seconds"} for one WAV.

    f0_unsure: over F0_FLOOR_SHARE of the voiced frames sit at the tracker's
    floor; f0 is then the median of the frames above it.
    """
    import threading
    from core.subtitles import get_whisper_model
    from core.voice_cloning import pitch as pitch_mod
    say = progress or (lambda f, m: None)
    import numpy as np
    # The voiceprint comes from the Seed-VC worker, a separate process, so it
    # runs while this thread measures pitch and Whisper transcribes.
    emb_box = {}

    def _embed():
        try:
            emb_box["emb"] = embed_fn(str(path))
        except BaseException as e:      # re-raised below, on the caller's thread
            emb_box["err"] = e
    emb_thread = threading.Thread(target=_embed, daemon=True, name="voice-match-embed")
    emb_thread.start()
    say(0.1, "Measuring pitch…")
    frames = pitch_mod.voiced_f0(str(path), max_seconds=45)
    f0, unsure = (float(np.median(frames)) if frames.size >= 20 else None), False
    if f0 and float(np.mean(frames < F0_FLOOR_HZ)) > F0_FLOOR_SHARE:
        unsure = True
        above = frames[frames >= F0_FLOOR_HZ]
        if above.size >= 20:
            f0 = float(np.median(above))
    say(0.35, "Transcribing to measure speaking speed and language…")
    model = get_whisper_model()
    segments, info = model.transcribe(str(path), vad_filter=True)
    text = " ".join(s.text.strip() for s in segments)
    lang = info.language
    say(0.75, "Measuring the voiceprint…")
    emb_thread.join()
    if "err" in emb_box:
        raise emb_box["err"]
    emb = emb_box.get("emb")
    return {"f0": f0, "f0_unsure": unsure, "gender": gender_guess(f0, unsure), "lang": lang,
            "lang_prob": round(float(info.language_probability or 0), 2),
            "rate": rate(text, lang, str(path)), "text": text, "emb": emb,
            "seconds": round(speech_seconds(str(path)), 1)}


def _semitones(a, b):
    return abs(12 * math.log2(a / b)) if a and b else None


def suggested_speed(entry, clip):
    """Engine speed so the voice talks at the clip's pace (None if not applicable)."""
    if not entry.get("rate") or not clip.get("rate") or entry.get("lang") != clip.get("lang"):
        return None
    ratio = clip["rate"] / entry["rate"]
    kind = entry.get("kind")
    if kind in ("kokoro", "melo"):
        return round(min(2.0, max(0.5, ratio)) * 20) / 20       # 0.05 steps
    if kind == "pyttsx3":
        return int(min(350, max(50, round(175 * ratio))))
    return None


def rank(bank, cands, clip, top=3, gender=None):
    """Top matches: [{"id", "label", "score", "sim", "st", "speed", "selection", "reason", "f0", "wav", "gender"}].

    gender: 'male' or 'female' keeps only those voices; None compares all.
    """
    import numpy as np
    ids = {c["id"] for c in cands}
    pool = [(vid, e) for vid, e in bank["voices"].items() if vid in ids and e.get("emb")]
    if gender:
        pool = [(vid, e) for vid, e in pool if voice_gender(vid, e) == gender]
    lang = clip.get("lang")
    if lang and clip.get("lang_prob", 0) >= LANG_CONFIDENCE and any(e.get("lang") == lang for _, e in pool):
        pool = [(vid, e) for vid, e in pool if e.get("lang") == lang]
    if not pool or not clip.get("emb"):
        return []
    c_emb = np.asarray(clip["emb"], dtype=np.float32)
    rows = []
    for vid, e in pool:
        sim = float(np.dot(c_emb, np.asarray(e["emb"], dtype=np.float32)))
        st = _semitones(clip.get("f0"), e.get("f0"))
        score = sim - PITCH_PENALTY * (st or 0)
        rows.append((score, sim, st, vid, e))
    rows.sort(key=lambda r: r[0], reverse=True)
    best_sim = max(r[1] for r in rows)
    out = []
    for score, sim, st, vid, e in rows[:top]:
        speed = suggested_speed(e, clip)
        parts = ["closest timbre" if sim >= best_sim - 1e-6 else f"timbre {sim:.2f}"]
        if e.get("f0") and clip.get("f0"):
            parts.append(f"pitch {e['f0']:.0f} Hz vs {clip['f0']:.0f} Hz")
        if speed is not None:
            parts.append(f"speed {speed}{'x' if e['kind'] != 'pyttsx3' else ' WPM'}")
        sel = dict(e["selection"])
        if speed is not None:
            sel["speed"] = speed
        out.append({"id": vid, "label": e["label"], "score": round(score, 3), "sim": round(sim, 3),
                    "st": None if st is None else round(st, 1), "speed": speed, "selection": sel,
                    "reason": ", ".join(parts), "f0": e.get("f0"), "wav": e.get("wav"), "lang": e.get("lang"),
                    "gender": voice_gender(vid, e)})
    return out
