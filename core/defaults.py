"""
core/defaults.py
────────────────
The settings the panels start with, as plain dicts in the shape of their
get_settings() / get_active_effects(). No UI imports.

The Qt UI uses them for the panels it has not ported yet (see
docs/pyside6-migration-plan.md). Keep them equal to the CustomTkinter panels'
starting values: ui/panels/engine_panel.py, customization_panel.py and
effects_panel.py.
"""

from core.constants import (EDGE_VOICES, KOKORO_LANGS, KOKORO_VOICES, MELO_VOICES,
                            PIPER_VOICES)


def default_engine_name(engine_names):
    """The engine selected at start: the first Kokoro engine, else the first one."""
    for n in engine_names:
        if "Kokoro" in n:
            return n
    return engine_names[0] if engine_names else "No TTS engines available"


def engine_settings(engine_names, system_voices) -> dict:
    """EnginePanel.get_settings() as it is right after start.

    engine_names: available engine names, in registry order.
    system_voices: [(name, voice_id)] from Pyttsx3Engine.load_system_voices().
    """
    import torch
    lang = "American English"
    return {
        "engine_name":      default_engine_name(engine_names),
        "use_gpu":          torch.cuda.is_available(),
        "gtts_lang_key":    "English (US)",
        "edge_voice_key":   next(iter(EDGE_VOICES)),
        "edge_rate":        0,
        "kokoro_lang_code": KOKORO_LANGS[lang],
        "kokoro_voice_key": next(iter(KOKORO_VOICES[KOKORO_LANGS[lang]])),
        "kokoro_speed":     1.0,
        "piper_voice_key":  PIPER_VOICES[next(iter(PIPER_VOICES))],
        "melo_voice_cfg":   MELO_VOICES[next(iter(MELO_VOICES))],
        "melo_speed":       1.0,
        "pyttsx3_voice_id": system_voices[0][1] if system_voices else "",
        "pyttsx3_rate":     175,
        "pyttsx3_volume":   0.9,
    }


def customization_settings() -> dict:
    """CustomizationPanel.get_settings() as it is right after start."""
    return {
        "skip_silences":    False,
        "speed":            1.0,
        "env_choice":       "None",
        "env_volume":       0.5,
        "custom_amb_path":  None,
        "ambiance_channel": None,
        "auto_subtitle":    False,
        "eq_enabled":       False,
        "eq_bass":          0.0,
        "eq_mids":          0.0,
        "eq_treble":        0.0,
        "sub_segmentation": "word",
        "clone_voice":      {},
    }


def active_effects(registry) -> dict:
    """EffectsPanel.get_active_effects() as it is right after start."""
    return {e["key"]: bool(e.get("default", False)) for e in registry}
