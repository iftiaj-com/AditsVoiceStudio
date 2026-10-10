"""
effects/registry.py
────────────────────
Master registry of every voice effect available in VoiceCraft.

To add a new effect:
  1. Add the DSP method to effects/audio_effects.py.
  2. Add an entry to EFFECTS_REGISTRY below.
  3. Done — the UI panel reads this list automatically.

Registry entry keys:
  key       – unique internal identifier (must match the BooleanVar attribute
               name suffix used in the app: self._fx_<key>)
  label     – display label shown in the UI toggle card
  desc      – short description shown below the toggle
  fn        – the callable (AudioEffects static method)
  kwargs    – extra keyword arguments passed to fn (optional)
"""

from effects.audio_effects import AudioEffects

EFFECTS_REGISTRY = [
    # Shown first in the UI. Processing order is unaffected: the pipeline
    # (ui/app.py) skips "normalize" in the effects loop and always applies
    # it as the final pass after ambiance mixing.
    {
        "key":    "normalize",
        "label":  "🔊  Normalize",
        "desc":   "Consistent volume levels",
        "fn":     AudioEffects.normalize,
        "default": True,      # pre-checked in the UI
    },
    {
        "key":    "vocoder",
        "label":  "🤖  Vocoder",
        "desc":   "Polyphonic synth chords & Daft Punk robotic harmonies",
        "fn":     AudioEffects.vocoder,
    },
    {
        "key":    "vocoder2",
        "label":  "🤖  Vocoder 2",
        "desc":   "Daft Punk robotic synth chords & vocal reinforcement",
        "fn":     AudioEffects.vocoder2,
    },
    {
        "key":    "robotic",
        "label":  "🤖  Robotic",
        "desc":   "Extreme pitch quantization & zero retune speed (pop autotune)",
        "fn":     AudioEffects.robotic,
    },
    {
        "key":    "demonic",
        "label":  "👹  Demonic",
        "desc":   "Pitch down an octave for a dark menacing voice (-12 semitones)",
        "fn":     AudioEffects.demonic,
    },
    {
        "key":    "demonic2",
        "label":  "👹  Demonic 2",
        "desc":   "Octave-down dark voice with clear word intelligibility",
        "fn":     AudioEffects.demonic2,
    },
    {
        "key":    "darth_vader",
        "label":  "⚔️  Darth Vader",
        "desc":   "Deep Sith baritone, helmet cavity resonance & mechanical respirator",
        "fn":     AudioEffects.darth_vader,
    },
    {
        "key":    "harmonizer",
        "label":  "👥  Harmonizer",
        "desc":   "Automatic vocal chords & massive angelic choir ensemble",
        "fn":     AudioEffects.harmonizer,
    },
    {
        "key":    "glitch",
        "label":  "🔀  Glitch",
        "desc":   "Random audio chunk repeats",
        "fn":     AudioEffects.glitch,
    },
    {
        "key":    "stutter",
        "label":  "✂️  Stutter",
        "desc":   "Microscopic rhythmic snippets, rapid looping & ambient clouds",
        "fn":     AudioEffects.stutter,
    },
    {
        "key":    "bitcrush",
        "label":  "👾  Bitcrush",
        "desc":   "Harsh 8-bit retro video game & hyperpop downsampler",
        "fn":     AudioEffects.bitcrusher,
    },
    {
        "key":    "ringmod",
        "label":  "📡  Ring Mod",
        "desc":   "Dalek-like choppy effect",
        "fn":     AudioEffects.ring_modulator,
    },
    {
        "key":    "reverb",
        "label":  "🌊  Reverb",
        "desc":   "Echoing spacious sound",
        "fn":     AudioEffects.reverb,
    },
    {
        "key":    "delay",
        "label":  "🔁  Delay",
        "desc":   "Tight rhythmic slapback echo with zero clutter to fill space",
        "fn":     AudioEffects.delay,
    },
    {
        "key":    "plate_reverb",
        "label":  "💿  Plate Reverb",
        "desc":   "Lush, bright metallic plate reverb & shimmering pop tail",
        "fn":     AudioEffects.plate_reverb,
    },
    {
        "key":    "shimmer_reverb",
        "label":  "🌌  Shimmer Reverb",
        "desc":   "Pitched-up octave tail blooming into a celestial angelic synth pad",
        "fn":     AudioEffects.shimmer_reverb,
    },
    {
        "key":    "reverse_reverb",
        "label":  "👻  Reverse Reverb",
        "desc":   "Backward crescendo reverb & ghostly pre-vocal swoosh",
        "fn":     AudioEffects.reverse_reverb,
    },
    {
        "key":    "chorus",
        "label":  "🎭  Chorus",
        "desc":   "Multiplies voice with subtle pitch & timing drift for a large group sound",
        "fn":     AudioEffects.chorus,
    },
    {
        "key":    "climax",
        "label":  "🚀  Climax",
        "desc":   "Dramatic sweeping metallic notches & intense transitional vocal riser",
        "fn":     AudioEffects.climax,
    },
    {
        "key":    "pitch",
        "label":  "🎵  Pitch Shift",
        "desc":   "Shift pitch up (+3 semitones)",
        "fn":     AudioEffects.pitch_shift,
        "kwargs": {"semitones": 3},
    },
    {
        "key":    "pilot",
        "label":  "✈️  Pilot",
        "desc":   "Cockpit intercom (VHF 3kHz bandpass, PTT clicks & avionics hum)",
        "fn":     AudioEffects.pilot_radio,
    },
    {
        "key":    "hyperpop",
        "label":  "🎶  Hyperpop",
        "desc":   "Fast glitchy pop sound",
        "fn":     AudioEffects.hyperpop,
    },
    {
        "key":    "melancholic",
        "label":  "🌧  Melancholic",
        "desc":   "Slow intimate muffled",
        "fn":     AudioEffects.melancholic,
    },
    {
        "key":    "natgeo",
        "label":  "🎥  NatGeo",
        "desc":   "Deep, prestigious documentary narrator voice (MKH 416 warmth)",
        "fn":     AudioEffects.natgeo_narrator,
    },
    {
        "key":    "seductive_m",
        "label":  "🍷  Seductive (M)",
        "desc":   "Intimate, deep velvety masculine voice (close-mic warmth & zero phase distortion)",
        "fn":     AudioEffects.seductive_male,
    },
    {
        "key":    "seductive_f",
        "label":  "💋  Seductive (F)",
        "desc":   "Intimate, warm sensual female voice (close-mic breathiness & zero phase distortion)",
        "fn":     AudioEffects.seductive_female,
    },
    {
        "key":    "saas_flash",
        "label":  "⚡  SaaS Flash",
        "desc":   "Snappy 195 WPM breathless marketing voice",
        "fn":     AudioEffects.saas_flash,
    },
    {
        "key":    "cinematic",
        "label":  "🎬  Cinematic",
        "desc":   "Soft, soothing & warm intimate voiceover (TLM 103 condenser)",
        "fn":     AudioEffects.cinematic,
    },
    {
        "key":    "arjun",
        "label":  "🏹  Arjun",
        "desc":   "Warm & crisp YouTube reviewer (SM7B broadcast tone)",
        "fn":     AudioEffects.arjun,
    },
    {
        "key":    "dramatic_ads",
        "label":  "📢  Dramatic Ads",
        "desc":   "Authoritative & punchy commercial broadcast voice (SM7B exciter)",
        "fn":     AudioEffects.dramatic_ads,
    },
    {
        "key":    "techy",
        "label":  "💻  Techy",
        "desc":   "Deep, calm & articulate developer tutorial voice (clean SM7B)",
        "fn":     AudioEffects.techy,
    },
    {
        "key":    "telephone",
        "label":  "☎️  Telephone",
        "desc":   "Vintage 3.4kHz limited line voice",
        "fn":     AudioEffects.telephone,
    },
    {
        "key":    "radio",
        "label":  "📻  Radio",
        "desc":   "Vintage AM broadcast voice (resonant speaker & warm RF saturation)",
        "fn":     AudioEffects.radio,
    },
    {
        "key":    "megaphone",
        "label":  "📣  Megaphone",
        "desc":   "Thin, nasal horn midrange & intimate lo-fi vocal projection",
        "fn":     AudioEffects.megaphone,
    },
]

# Cloned voices (Customization → Clone Voice) join the list as "clone:<slug>"
# entries right after "normalize", so conversion runs before other effects.
# A damaged voice folder or missing runtime must never stop the app starting.
try:
    from effects.cloned_voices import extend_with_cloned_voices
    extend_with_cloned_voices(EFFECTS_REGISTRY)
except Exception as _clone_err:   # noqa: BLE001
    print(f"Cloned voices not loaded: {_clone_err}")
