"""
core/voice_selection.py
───────────────────────
Engine and voice selections as plain data, shared by both UIs' engine panels
and the Clone Voice code. No UI imports.

A *selection* is what a cloned voice stores as its base voice: the TTS Engine
dropdown values, e.g.
    {"engine": "Kokoro-82M (Fast Local AI)", "kind": "kokoro",
     "lang": "American English", "voice": "af_heart", "speed": 1.2}
A *settings* dict is EnginePanel.get_settings().

Moved here from the CustomTkinter engine panel (PySide6 migration, phase 3);
ui's EnginePanel exposes the same names and calls these.
"""

from core.constants import (EDGE_VOICES, GTTS_LANGUAGES, KOKORO_LANGS, KOKORO_VOICES,
                            MELO_VOICES, PIPER_VOICES)

KIND_BY_KEYWORD = {"gTTS": "gtts", "Edge": "edge", "Kokoro": "kokoro",
                   "Piper": "piper", "Melo": "melo", "pyttsx3": "pyttsx3"}
# kind -> (lowest, highest, default) of the engine's speed control; Piper and gTTS have none.
SPEED_RANGES = {"kokoro": (0.5, 2.0, 1.0), "melo": (0.5, 2.0, 1.0),
                "edge": (-50, 50, 0), "pyttsx3": (50, 350, 175)}
# kind -> get_settings() key of its speed control
SPEED_KEYS = {"kokoro": "kokoro_speed", "melo": "melo_speed",
              "edge": "edge_rate", "pyttsx3": "pyttsx3_rate"}
SYNTH_KEYS = {"kokoro": ("kokoro_lang_code", "kokoro_voice_key", "kokoro_speed"),
              "melo": ("melo_voice_cfg", "melo_speed"), "edge": ("edge_voice_key", "edge_rate"),
              "piper": ("piper_voice_key",), "gtts": ("gtts_lang_key",),
              "pyttsx3": ("pyttsx3_voice_id", "pyttsx3_rate", "pyttsx3_volume")}


def engine_kind(engine_name):
    """"kokoro", "piper", ... for an engine's display name; None if unknown."""
    for keyword, kind in KIND_BY_KEYWORD.items():
        if keyword in (engine_name or ""):
            return kind
    return None


def format_speed(kind, value) -> str:
    """'+10%' (Edge), '175 WPM' (pyttsx3) or '1.2x' / '0.85x' (Kokoro, Melo)."""
    if kind == "edge":
        return f"{int(round(float(value))):+d}%"
    if kind == "pyttsx3":
        return f"{int(round(float(value)))} WPM"
    text = f"{float(value):.2f}"
    return (text[:-1] if text.endswith("0") else text) + "x"


def describe_selection(sel) -> str:
    """'Kokoro-82M · am_onyx · 1.2x' for labels; '' when sel is empty."""
    if not sel or not sel.get("engine"):
        return ""
    parts = [sel["engine"].split(" (")[0]]
    if sel.get("voice"):
        parts.append(str(sel["voice"]))
    speed = sel.get("speed")
    if speed is not None:
        parts.append(format_speed(sel.get("kind") or engine_kind(sel["engine"]), speed))
    return " · ".join(parts)


def synthesis_key(settings) -> dict:
    """The part of a get_settings() dict that decides how its engine sounds.

    Two settings dicts with the same key speak alike, so a saved preview
    can be reused (core.voice_cloning.previews). Other engines' values
    and the GPU switch are left out.
    """
    name = settings["engine_name"]
    out = {"engine": name}
    for k in SYNTH_KEYS.get(engine_kind(name), ()):
        v = settings.get(k)
        out[k] = round(v, 2) if isinstance(v, float) else v
    return out


def voice_options(kind, system_voices) -> dict:
    """{dropdown value: underlying value} for an engine kind (not Kokoro)."""
    if kind == "edge":
        return dict(EDGE_VOICES)
    if kind == "piper":
        return dict(PIPER_VOICES)
    if kind == "melo":
        return {k: v.get("speaker") for k, v in MELO_VOICES.items()}
    if kind == "pyttsx3":
        return {n: vid for n, vid in system_voices} or {"Default": ""}
    if kind == "gtts":
        return dict(GTTS_LANGUAGES)
    return {}


def voice_choices(kind, system_voices, lang=None) -> list:
    """Dropdown values for an engine kind's voices (Kokoro: of language *lang*)."""
    if kind == "kokoro":
        return list(KOKORO_VOICES[KOKORO_LANGS.get(lang, "a")].keys())
    return list(voice_options(kind, system_voices))


def resolve_selection(sel, engine_names, system_voices):
    """(engine name, kind, Kokoro language, voice dropdown value) for sel, or a str saying why not.

    A voice may be given as the dropdown value or the underlying value
    (older voices stored e.g. a Piper model key or a Melo speaker id).
    """
    if not sel or not sel.get("engine"):
        return "no base voice is recorded"
    kind = sel.get("kind") or engine_kind(sel["engine"])
    name = sel["engine"] if sel["engine"] in engine_names else next(
        (e for e in engine_names if kind and engine_kind(e) == kind), None)
    if name is None:
        return f"{sel['engine'].split(' (')[0]} is not available"
    voice = sel.get("voice")

    if kind == "kokoro":
        lang = sel.get("lang")
        if lang not in KOKORO_LANGS or voice not in KOKORO_VOICES[KOKORO_LANGS[lang]]:
            lang = next((ln for ln, code in KOKORO_LANGS.items()
                         if voice in KOKORO_VOICES.get(code, {})), None)
        if lang is None:
            return f"Kokoro voice '{voice}' is not available"
        return name, kind, lang, voice

    target = None
    if voice is not None:
        options = voice_options(kind, system_voices)
        target = voice if voice in options else next(
            (k for k, v in options.items() if v == voice), None)
        if target is None:
            return f"voice '{voice}' is not available in {name.split(' (')[0]}"
    return name, kind, None, target


def clamp_speed(kind, value):
    """value clamped to the engine's speed range; int for Edge and pyttsx3."""
    lo, hi, _default = SPEED_RANGES[kind]
    v = max(lo, min(hi, float(value)))
    return int(round(v)) if kind in ("edge", "pyttsx3") else v


def settings_for_selection(settings, sel, engine_names, system_voices):
    """*settings* (a get_settings() dict) with sel's engine, voice and speed in place.

    Returns (new settings, None), or (None, why not). *settings* is not changed.
    """
    r = resolve_selection(sel, engine_names, system_voices)
    if isinstance(r, str):
        return None, r
    name, kind, lang, voice = r
    s = dict(settings)
    s["engine_name"] = name
    if kind == "kokoro":
        s["kokoro_lang_code"] = KOKORO_LANGS[lang]
        s["kokoro_voice_key"] = voice
    elif voice is not None:
        if kind == "edge":
            s["edge_voice_key"] = voice
        elif kind == "piper":
            s["piper_voice_key"] = PIPER_VOICES[voice]
        elif kind == "melo":
            s["melo_voice_cfg"] = MELO_VOICES[voice]
        elif kind == "pyttsx3":
            s["pyttsx3_voice_id"] = voice_options(kind, system_voices).get(voice, "")
        elif kind == "gtts":
            s["gtts_lang_key"] = voice
    key = SPEED_KEYS.get(kind)
    speed = sel.get("speed")
    if key and speed is not None:
        s[key] = clamp_speed(kind, speed)
    return s, None
