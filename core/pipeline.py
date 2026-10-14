"""
core/pipeline.py
────────────────
The generation pipeline: synthesize, then silence skipping, speed, voice
effects, EQ, ambiance, normalization, subtitles and the MP3 encode.

Runs on a worker thread. It takes plain settings captured on the UI thread
and reports through callbacks, so it never touches a widget and works the
same under any UI toolkit.
"""

import os
import re
from dataclasses import dataclass

import numpy as np
from pydub import AudioSegment
from pydub import silence as pydub_silence

from core.audio_io import load_segment, export_mp3
from effects.audio_effects import AudioEffects
from effects.registry import EFFECTS_REGISTRY
from effects.cloned_voices import is_clone_entry, slug_of


def snippet_of(text, max_chars=150):
    """What ▶ Preview speaks: the first sentence of *text*, capped at *max_chars* on a word boundary."""
    parts = re.split(r'(?<=[.!?])\s+', text, maxsplit=1)
    snippet = parts[0] if parts else text
    if len(snippet) > max_chars:
        cut = snippet[:max_chars].rsplit(" ", 1)[0]
        snippet = cut or snippet[:max_chars]
    return snippet


class Cancelled(Exception):
    """The cancel event was set; the run stopped without a result."""


@dataclass
class GenerationRequest:
    text: str
    eng_settings: dict     # EnginePanel.get_settings()
    cust_settings: dict    # CustomizationPanel.get_settings()
    fx_active: dict        # EffectsPanel.get_active_effects()
    preview: bool = False  # playback only: no subtitles, no MP3


@dataclass
class GenerationResult:
    playback_file: str
    save_file: str
    save_ext: str
    mp3_file: str = None
    srt_file: str = None
    notice: str = None     # a step was skipped (e.g. the cloned voice); shown instead of "success"


def run(req, synth, tmp_dir, cancel_event, progress=None, status=None, warn=None):
    """Generate speech for *req* into *tmp_dir*; returns a GenerationResult.

    synth: core.synth.Synthesizer. cancel_event: threading.Event.
    progress(fraction), status(message), warn(message): called on this thread.
    Raises Cancelled when cancel_event is set. Any other exception is a failure,
    unless cancel_event is set by then (an engine can raise on cancel).
    """
    progress = progress or (lambda f: None)
    status = status or (lambda m: None)
    warn = warn or (lambda m: None)

    def check_cancel():
        if cancel_event.is_set():
            raise Cancelled()

    import gc
    import torch
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    check_cancel()
    progress(0.05)

    eng_settings  = req.eng_settings
    cust_settings = req.cust_settings
    fx_active     = req.fx_active
    # Per-voice overrides from the Clone Voice group; empty means
    # "use the values saved in the voice's profile".
    clone_cfg     = cust_settings.get("clone_voice") or {}
    notice        = None

    progress(0.10)
    result = synth.synthesize(req.text, tmp_dir, eng_settings,
                              status_cb=status, check_cancel=cancel_event.is_set)
    check_cancel()

    playback_file = result["playback_file"]
    save_file     = result["save_file"]
    save_ext      = result["save_ext"]
    srt_file      = result.get("srt_file")

    progress(0.45)

    # ── Post-processing plan ───────────────────────────────
    # Everything below runs on ONE in-memory float32 array: the
    # engine output is decoded once, every enabled DSP stage runs
    # in RAM, and the result hits the disk exactly once at the end
    # (plus one MP3 encode from the same in-memory buffer).
    speed         = cust_settings["speed"]
    needs_silence = cust_settings["skip_silences"]
    needs_speed   = abs(speed - 1.0) > 0.01
    active_fx     = [e for e in EFFECTS_REGISTRY
                     if e["key"] != "normalize"
                     and fx_active.get(e["key"], False)]
    eq_enabled    = cust_settings.get("eq_enabled", False)
    env_choice    = cust_settings["env_choice"]
    needs_env     = env_choice != "None"
    needs_norm    = fx_active.get("normalize", False)

    dsp_stages = (int(needs_silence) + int(needs_speed) + len(active_fx)
                  + int(eq_enabled) + int(needs_env) + int(needs_norm))
    processed_seg = None   # in-memory AudioSegment for the final export

    if dsp_stages and playback_file:
        stages_done = 0

        def step():
            # Advance the bar proportionally through the 0.45–0.80 window
            nonlocal stages_done
            stages_done += 1
            progress(0.45 + 0.35 * stages_done / dsp_stages)

        try:
            seg = load_segment(playback_file)   # single decode

            # ── Skip silences (pydub, in memory) ───────────
            if needs_silence:
                check_cancel()
                status("Removing silences…")
                chunks = pydub_silence.split_on_silence(
                    seg, min_silence_len=200, silence_thresh=-40)
                if chunks:
                    joined = chunks[0]
                    for c in chunks[1:]:
                        joined += c
                    seg = joined
                step()

            # ── Decode once to float32 mono ────────────────
            seg  = seg.set_channels(1)
            sr   = seg.frame_rate
            peak = float(2 ** (8 * seg.sample_width - 1))
            samples = np.array(seg.get_array_of_samples(),
                               dtype=np.float32) / peak

            # ── Speed adjustment ───────────────────────────
            if needs_speed:
                check_cancel()
                status(f"Adjusting speed to {speed:.2f}x…")
                try:
                    samples = AudioEffects.change_speed(samples, speed)
                except Exception as e:
                    warn(f"⚠ Speed adjustment failed: {e}")
                step()

            # ── Voice effects ──────────────────────────────
            for effect in active_fx:
                check_cancel()
                status(f"Applying {effect['label'].split('  ')[-1]}…")
                fn        = effect["fn"]
                kwargs_fx = effect.get("kwargs", {})
                if is_clone_entry(effect):
                    # Cloned voice: runs in a worker process; can take
                    # seconds to a minute, and must honour Cancel.
                    from core.voice_cloning.errors import VCCancelled, VCError
                    kwargs_fx = dict(kwargs_fx, cancel_event=cancel_event,
                                     status_cb=status)
                    cfg = clone_cfg if clone_cfg.get("slug") == slug_of(effect["key"]) else {}
                    for name in ("pitch", "index_rate", "protect", "diffusion_steps"):
                        if cfg.get(name) is not None:
                            kwargs_fx[name] = cfg[name]
                    try:
                        samples = fn(samples, sr, **kwargs_fx)
                    except VCCancelled:
                        check_cancel()
                        raise
                    except VCError as vc_err:
                        # Keep going in the engine's own voice; say why.
                        notice = f"Cloned voice not applied: {vc_err.message}"
                        warn(f"⚠ {notice}")
                    step()
                    continue
                try:
                    samples = fn(samples, sr, **kwargs_fx)
                except TypeError:
                    samples = fn(samples, **kwargs_fx)
                step()

            # ── Equalizer ──────────────────────────────────
            if eq_enabled:
                check_cancel()
                status("Applying equalizer…")
                try:
                    low_factor  = 10 ** (cust_settings["eq_bass"] / 20.0)
                    mid_factor  = 10 ** (cust_settings["eq_mids"] / 20.0)
                    high_factor = 10 ** (cust_settings["eq_treble"] / 20.0)
                    samples = AudioEffects.three_band_eq(
                        samples, sr, low_factor, mid_factor, high_factor)
                except Exception as eq_err:
                    warn(f"⚠ EQ failed: {eq_err}")
                step()

            # ── Ambiance mix ───────────────────────────────
            if needs_env:
                check_cancel()
                status("Adding ambiance noise…")
                amb_path = (cust_settings["custom_amb_path"]
                            if env_choice == "Custom File..."
                            else env_choice)
                if amb_path:
                    samples = AudioEffects.add_environment_sound(
                        samples, sr, amb_path,
                        volume=cust_settings["env_volume"])
                step()

            # ── Post-mix normalization (always last) ───────
            if needs_norm:
                check_cancel()
                samples = AudioEffects.normalize(samples)
                step()

            # ── Single disk write ──────────────────────────
            int16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
            processed_seg = AudioSegment(
                data=int16.tobytes(), sample_width=2,
                frame_rate=sr, channels=1)
            out = os.path.join(tmp_dir, "processed.wav")
            processed_seg.export(out, format="wav")
            playback_file = save_file = out
            save_ext = ".wav"

        except Cancelled:
            raise
        except Exception as fx_err:
            if cancel_event.is_set():
                raise Cancelled() from fx_err
            # Fall back to the raw engine output
            processed_seg = None
            warn(f"⚠ Effects failed: {fx_err}")

    progress(0.80)

    # ── Subtitles generation ───────────────────────────────
    if not req.preview and cust_settings.get("auto_subtitle", False) and playback_file:
        check_cancel()
        from core.subtitles import generate_subtitles, SubtitleCancelled
        try:
            srt_out = os.path.join(tmp_dir, "final.srt")
            seg_mode = cust_settings.get("sub_segmentation", "word")
            generate_subtitles(playback_file, srt_out, status_cb=status,
                               segmentation=seg_mode,
                               check_cancel=cancel_event.is_set)
            srt_file = srt_out
        except SubtitleCancelled:
            raise Cancelled()
        except Exception as e:
            print(f"Subtitle generation failed: {e}")
            warn(f"⚠ Subtitles failed: {e}")

    progress(0.92)

    # ── MP3 export for save (from memory when possible) ────
    mp3_file = None
    if req.preview:
        pass   # previews are playback-only; skip the MP3 encode
    elif save_file and save_file.endswith(".mp3"):
        mp3_file = save_file
    elif save_file:
        try:
            mp3_out = os.path.join(tmp_dir, "final.mp3")
            src_seg = (processed_seg if processed_seg is not None
                       else load_segment(save_file))
            export_mp3(src_seg, mp3_out)
            mp3_file = mp3_out
        except Exception as mp3_err:
            # WAV is still saved; say why MP3 is missing from the dialog.
            print(f"MP3 export failed: {mp3_err}")
            warn(f"⚠ MP3 export failed, WAV only: {mp3_err}")

    progress(1.0)
    return GenerationResult(playback_file=playback_file, save_file=save_file,
                            save_ext=save_ext, mp3_file=mp3_file, srt_file=srt_file,
                            notice=notice)
