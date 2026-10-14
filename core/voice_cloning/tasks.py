"""
core/voice_cloning/tasks.py
───────────────────────────
The Clone Voice jobs and rules, as plain functions with no UI: creating a
Quick, trained or imported voice, clip preparation, previews, pitch
matching, the cleanup preview, the clip-length rules and the time
estimates.

Moved out of ui/panels/clone_voice_group.py (PySide6 migration, phase 4)
so the CustomTkinter and Qt Clone Voice groups run the same code. Job
functions run on a worker thread and report through a TaskContext; they
never touch a widget.
"""

import os
import tempfile
import time

from core import voice_selection
from core.voice_cloning import estimate, library, pitch, previews
from core.voice_cloning.errors import VCCancelled, VCError, VCWorkerError

MODES = ["Quick", "Train (RVC)", "Import model"]
SR_CHOICES = {"32 kHz": 32000, "40 kHz": 40000, "48 kHz": 48000}
# "Use part of each recording" presets: the first N seconds, or Custom
# ranges. Seed-VC (Quick) only reads 25 s of reference, so Quick stops at
# 25 s; training uses everything it is given.
TRIM_PRESETS = {
    "Quick": {"10 s": 10, "20 s": 20, "25 s": 25, "Custom": None},
    "Train (RVC)": {"1 min": 60, "3 min": 180, "5 min": 300, "Custom": None},
}


class TaskContext:
    """What a job needs from the UI that started it.

    synth: core.synth.Synthesizer. cancel: threading.Event (the Cancel button).
    progress(fraction or None, message or None): called on the worker thread;
    the UI forwards it to its own thread. tmp_dirs: a list the UI deletes on close.
    """

    def __init__(self, synth, cancel, progress=None, tmp_dirs=None):
        self.synth = synth
        self.cancel = cancel
        self._progress = progress or (lambda frac, msg: None)
        self.tmp_dirs = tmp_dirs if tmp_dirs is not None else []

    def progress(self, frac, msg=None):
        self._progress(frac, msg)

    def check_cancel(self):
        if self.cancel.is_set():
            raise VCCancelled()

    def new_tmp(self):
        d = tempfile.mkdtemp(prefix="adits_vs_clone_")
        self.tmp_dirs.append(d)
        return d


# ── rules and text (UI thread) ──────────────────────────────────────

def selection_for(p):
    """The profile's base selection; older voices only stored engine + voice."""
    if p is None:
        return None
    if p.base_selection:
        return p.base_selection
    if p.base_engine:
        return {"engine": p.base_engine, "kind": voice_selection.engine_kind(p.base_engine),
                "voice": p.base_voice or None}
    return None


def base_voice(s):
    """(engine name, voice id) stored on a new voice, from a get_settings() dict."""
    name = s["engine_name"]
    voice = (s["edge_voice_key"] if "Edge" in name else
             s["kokoro_voice_key"] if "Kokoro" in name else
             s["piper_voice_key"] if "Piper" in name else
             (s["melo_voice_cfg"] or {}).get("speaker", "") if "Melo" in name else
             s["pyttsx3_voice_id"] if "pyttsx3" in name else "")
    return name, str(voice)


def selections(clips, mode, trim_on, trim_choice):
    """[{"path", "start", "end"}] that cloning will use, given the trim options.

    clips: [{"path", "seconds" (full length), "start", "end"}].
    "First N s" counts recording time from the start of the first clip and
    continues into the next clips until N seconds are covered.
    """
    if not trim_on or mode not in TRIM_PRESETS:
        return [{"path": c["path"]} for c in clips]
    limit = TRIM_PRESETS[mode].get(trim_choice)
    if limit is None:                                       # Custom
        return [{"path": c["path"], "start": c.get("start"), "end": c.get("end")} for c in clips]
    out, left = [], float(limit)
    for c in clips:
        if left <= 0.05:
            break
        take = min(left, c["seconds"])
        out.append({"path": c["path"]} if take >= c["seconds"] - 0.01
                   else {"path": c["path"], "start": 0.0, "end": round(take, 3)})
        left -= take
    return out


def sel_seconds(sel, full):
    start = sel.get("start") or 0.0
    end = sel.get("end") if sel.get("end") is not None else full
    return max(0.0, end - start)


def clip_seconds(clips, sels):
    """Seconds of audio cloning will use (after the trim options)."""
    full = {c["path"]: c["seconds"] for c in clips}
    return sum(sel_seconds(s, full[s["path"]]) for s in sels)


def prep_options(mode, trim_on, trim_choice, clean, denoise, skip_silences):
    trim = None
    if trim_on and mode in TRIM_PRESETS:
        limit = TRIM_PRESETS[mode].get(trim_choice)
        trim = "custom" if limit is None else f"first{limit}"
    return {"trim": trim, "separate": bool(clean), "denoise": bool(denoise),
            "skip_silences": bool(skip_silences)}


def length_rule(mode, secs):
    """(ok, level, note): level is "ok", "warning" or "error"; ok False blocks Create."""
    if mode == "Quick":
        if secs < 3:
            return False, "error", "need at least 3 s"
        if secs < 8:
            return True, "warning", "10–25 s works best"
    elif mode == "Train (RVC)":
        if secs < 30:
            return False, "error", "need at least 30 s"
        if secs < 60:
            return True, "warning", "under 1 min: Quick mode is recommended"
        if secs < 180:
            return True, "warning", "under 3 min: expect rough results"
    return True, "ok", ""


def fmt_len(secs):
    m, s = divmod(int(round(secs)), 60)
    return f"{m} min {s:02d} s" if m else f"{s} s"


def clock(t):
    m, s = divmod(int(round(t)), 60)
    return f"{m}:{s:02d}"


def create_estimate(mode, n_clips, secs, epochs, batch, sr_label, has_pth, clean, rvc_warm):
    """The ⏱ line above Create Voice, or the hint shown when nothing is picked yet."""
    if mode == "Quick":
        est = f"⏱  Ready in {estimate.fmt(estimate.quick_create_seconds(secs))}" if n_clips else ""
    elif mode == "Train (RVC)":
        if n_clips:
            t = estimate.training_seconds(secs, int(epochs), int(batch), SR_CHOICES[sr_label])
            est = (f"⏱  {estimate.fmt(t['total'])[0].upper()}{estimate.fmt(t['total'])[1:]}: "
                   f"prepare {estimate.fmt(t['slice'] + t['features']).replace('about ', '')}, "
                   f"then {estimate.fmt(t['per_epoch']).replace('about ', '')} per epoch")
            if str(batch) == "4":
                est += " · batch 4 may not fit a 4 GB GPU"
        else:
            est = ""
    else:
        est = "⏱  Ready in about 15 s" if has_pth else ""
    if est and mode != "Import model" and n_clips and clean:
        extra = estimate.separation_seconds(secs, first_use=not rvc_warm)
        est += f" · music removal +{estimate.fmt(extra).replace('about ', '')}"
    if not est:
        est = ("Pick a model file to import." if mode == "Import model"
               else "Add or record clips of the voice to clone.")
    return est


def active_estimate(p, text, steps, warm):
    """The ⏱ line of the Active voice tab for the text in the box."""
    speech = estimate.speech_seconds(text)
    if speech < 0.5:
        per10 = estimate.conversion_seconds(p.mode, 10, steps)
        msg = f"⏱  Adds {estimate.fmt(per10)} per 10 s of speech"
    else:
        t = estimate.conversion_seconds(p.mode, speech, steps)
        msg = f"⏱  Adds {estimate.fmt(t)} to this text"
    if not warm:
        first = estimate.conversion_seconds(p.mode, 0, steps, first_use=True)
        msg += f", +{estimate.fmt(first).replace('about ', '')} the first time"
    return msg


def usable_match(match, eng_settings, clips, prep):
    """The voice match Result when it was made from these clips with this base voice, else None."""
    m = match
    if not m or not eng_settings or not os.path.isfile(m["wav"]):
        return None
    same_clean = bool(m["clean"]) == bool(prep.get("separate")) and (
        not m["clean"] or bool(m["denoise"]) == bool(prep.get("denoise")))
    if m["selections"] != clips or not same_clean:
        return None
    return m if m["synth_key"] == voice_selection.synthesis_key(eng_settings) else None


# ── jobs (worker thread) ────────────────────────────────────────────

def synthesize(synth, text, eng_settings, tmp, cancel):
    """Synthesize *text* with the selected engine; returns (float32 samples, sr)."""
    import numpy as np
    from core.audio_io import load_segment
    if eng_settings["engine_name"] not in synth.engine_map:
        raise VCError("The selected TTS engine is not available.")
    res = synth.synthesize(text, tmp, eng_settings, check_cancel=cancel.is_set)
    if cancel.is_set():
        raise VCCancelled()
    seg = load_segment(res["playback_file"]).set_channels(1)
    peak = float(2 ** (8 * seg.sample_width - 1))
    return np.array(seg.get_array_of_samples(), dtype=np.float32) / peak, seg.frame_rate


def make_preview(ctx, p, eng_settings, sound, cancel=None, say=None):
    """Speak *p*'s preview text with *eng_settings*, convert it into voice *p*; returns a WAV path.

    sound: previews.sound_of() values to convert with.
    """
    import soundfile as sf
    from effects.cloned_voices import convert_samples
    cancel = cancel or ctx.cancel
    say = say or (lambda m: None)
    tmp = ctx.new_tmp()
    say("Synthesizing the preview sentence…")
    y, sr = synthesize(ctx.synth, previews.text_of(p), eng_settings, tmp, cancel)
    say("Converting to the cloned voice…")
    params = {"pitch": p.pitch, "index_rate": p.index_rate, "protect": p.protect,
              "diffusion_steps": p.diffusion_steps}
    params.update(sound)
    out = convert_samples(p.slug, y, sr, cancel_event=cancel, status_cb=say, **params)
    path = os.path.join(tmp, "preview.wav")
    sf.write(path, out, sr)
    return path


def prepare(ctx, p, prep, lo=0.0, hi=0.3):
    """Trim and/or clean the clips before cloning (no-op when both are off)."""
    if not prep or not (prep.get("trim") or prep.get("separate") or prep.get("skip_silences")):
        return p
    separate_fn = None
    if prep.get("separate"):
        from core.voice_cloning import get_client
        client = get_client()

        def separate_fn(src, dst, denoise):
            client.separate(src, dst, denoise=denoise, cancel_event=ctx.cancel,
                            status_cb=lambda m: ctx.progress(None, m))
    t0 = time.monotonic()
    est = estimate.separation_seconds(p.total_seconds) if separate_fn else 2

    def progress(frac, msg):
        left = max(0.0, est - (time.monotonic() - t0))
        ctx.progress(lo + (hi - lo) * frac,
                     f"{msg} · {estimate.fmt(left)} left" if separate_fn and frac < 1 else msg)
    return library.prepare_clips(p, separate_fn=separate_fn, denoise=prep.get("denoise", False),
                                 progress_cb=progress, check_cancel=ctx.check_cancel,
                                 skip_silences=prep.get("skip_silences", False))


def save_first_preview(ctx, p, eng_settings, match=None):
    """Save the new voice's ▶ Preview.

    match (from usable_match): the voice match ▶ Result made from the same
    clips and base voice. Its first 10 s are transcribed once, and those
    words become the voice's preview_text, so later remakes say the same
    thing. A Quick voice keeps that audio as its preview: the analyzer
    cloned it the same way (Seed-VC, the clip as reference), so nothing is
    spoken again. A trained (RVC) voice speaks the words with its model.
    Without a match the preview says previews.TEXT.

    The voice is ready by now, so neither a failure nor Cancel undoes it;
    ▶ Preview makes the preview later instead.
    """
    if not eng_settings:
        return
    try:
        cut = None
        if match:
            ctx.progress(0.95, "Taking the preview from the voice match result…")
            cut = previews.cut_preview(match["wav"], os.path.join(ctx.new_tmp(), "preview.wav"))
            words = previews.transcribe(cut)
            if words:
                p = library.update_params(p.slug, preview_text=words)
            else:
                cut = None                  # nothing heard: fall back to the fixed sentence
        ctx.check_cancel()
        sound = previews.sound_of(p)
        if cut is None or p.mode != "seedvc":
            ctx.progress(0.95, "Saving a preview of the voice…")
            cut = make_preview(ctx, p, eng_settings, sound)
        previews.store(p.slug, cut, previews.fingerprint(p, voice_selection.synthesis_key(eng_settings), sound))
    except VCCancelled:
        print(f"Preview of '{p.name}' skipped: cancelled")
    except Exception as e:   # noqa: BLE001
        print(f"Preview of '{p.name}' not saved: {e}")


def create_quick(ctx, name, clips, eng, voice, base_sel=None, prep=None, eng_settings=None, match=None):
    """Make a Quick (Seed-VC) voice; returns its profile. A failure or Cancel deletes it."""
    p = library.create_voice(name, "seedvc", clips, eng, voice, base_selection=base_sel, prep=prep)
    try:
        p = prepare(ctx, p, prep, 0.0, 0.8)
        ctx.progress(0.3, "Trimming silence and building the reference…")
        library.build_seedvc_reference(p)
        ctx.check_cancel()
        p = library.update_params(p.slug, status="ready")
        save_first_preview(ctx, p, eng_settings, match)
    except BaseException:
        library.delete_voice(p.slug)
        raise
    return p


def import_model(ctx, name, pth, index):
    """Import an RVC .pth (and optional .index); returns (profile, model info)."""
    from core.voice_cloning import get_client
    info = get_client().inspect_rvc(pth)
    ctx.check_cancel()
    return library.import_rvc_model(name, pth, index, info), info


def train(ctx, name, clips, eng, voice, eng_settings, opts, base_sel=None, prep=None, match=None):
    """Train an RVC voice; returns (profile, seconds taken). A failure or Cancel deletes it.

    opts: {"epochs", "batch_size", "sample_rate", "match"}.
    """
    import shutil
    from core.voice_cloning import get_client
    p = library.create_voice(name, "rvc", clips, eng, voice, base_selection=base_sel, prep=prep)
    t0 = time.monotonic()
    est = estimate.training_seconds(p.total_seconds, opts["epochs"], opts["batch_size"], opts["sample_rate"])
    try:
        p = prepare(ctx, p, prep, 0.0, 0.02)
        if opts["match"] and eng_settings:
            ctx.progress(0.01, "Measuring pitch of the base and target voices…")
            tmp = ctx.new_tmp()
            y, sr = synthesize(ctx.synth, pitch.BASE_SENTENCE, eng_settings, tmp, ctx.cancel)
            base_wav = os.path.join(p.dir, "samples", "base.wav")
            os.makedirs(os.path.dirname(base_wav), exist_ok=True)
            import soundfile as sf
            sf.write(base_wav, y, sr)
            r = pitch.estimate_offset(base_wav, library.clip_audio_paths(p))
            ctx.check_cancel()
            p = library.update_params(p.slug, pitch=r["offset"], base_f0_hz=r["base_f0"],
                                      target_f0_hz=r["target_f0"])
        ctx.progress(0.02, "Converting clips for training…")
        ds = library.build_dataset(p)
        ctx.check_cancel()
        p = library.update_params(p.slug, status="training", batch_size=opts["batch_size"])

        def on_progress(ev):
            frac = ev.get("fraction") or 0
            stage = ev.get("stage")
            if stage == "train" and ev.get("epoch") is not None:
                left = ev.get("eta_seconds")
                if left is None:
                    left = (opts["epochs"] - ev["epoch"]) * est["per_epoch"] + 75
                text = (f"Training · epoch {ev['epoch']}/{ev['total_epoch']} · "
                        f"{estimate.fmt_clock(left)} left")
            else:
                elapsed = time.monotonic() - t0
                text = f"{ev.get('message') or stage} · {estimate.fmt(max(0, est['total'] - elapsed))} left"
            ctx.progress(frac, text)

        res = get_client().train_rvc(p, ds, epochs=opts["epochs"], batch_size=opts["batch_size"],
                                     sample_rate=opts["sample_rate"],
                                     save_every_epoch=max(10, opts["epochs"] // 10),
                                     progress_cb=on_progress, cancel_event=ctx.cancel)
        p = library.update_params(p.slug, status="ready", pth=f"model/{p.slug}.pth",
                                  index=f"model/{p.slug}.index" if res.get("index") else None,
                                  sample_rate=res["sample_rate"], epochs=res["epochs"])
        shutil.rmtree(p.dir / "dataset", ignore_errors=True)
        save_first_preview(ctx, p, eng_settings, match)
    except VCWorkerError as e:
        library.delete_voice(p.slug)
        if e.kind == "oom" and opts["batch_size"] > 1:
            e.hint = (e.hint or "") + f"\nTry again with batch size {opts['batch_size'] // 2}."
        raise
    except BaseException:
        library.delete_voice(p.slug)
        raise
    return p, time.monotonic() - t0


def measure_pitch(ctx, p, eng_settings, cancel=None, say=None):
    """Speak pitch.BASE_SENTENCE with *eng_settings* and compare it with *p*'s clips.

    Returns pitch.estimate_offset()'s dict; the profile is not changed.
    """
    import soundfile as sf
    cancel = cancel or ctx.cancel
    tmp = ctx.new_tmp()
    y, sr = synthesize(ctx.synth, pitch.BASE_SENTENCE, eng_settings, tmp, cancel)
    base_wav = os.path.join(tmp, "base.wav")
    sf.write(base_wav, y, sr)
    if say is None:
        ctx.progress(0.4, "Measuring pitch…")
        r = pitch.estimate_offset(base_wav, library.clip_audio_paths(p))
    else:
        r = pitch.estimate_offset(base_wav, library.clip_audio_paths(p), status_cb=say)
    if cancel.is_set():
        raise VCCancelled()
    return r


def match_pitch(ctx, p, eng_settings):
    """🎯 in the Clone Voice box: measure and save the pitch offset; returns the measurement."""
    r = measure_pitch(ctx, p, eng_settings)
    library.update_params(p.slug, pitch=r["offset"], base_f0_hz=r["base_f0"], target_f0_hz=r["target_f0"])
    return r


def cleanup_preview(ctx, first, full_seconds, denoise):
    """Remove music from up to 30 s of the first selection; returns (original, cleaned, seconds, elapsed)."""
    from core.audio_io import load_segment
    from core.voice_cloning import get_client
    start = first.get("start") or 0.0
    end = first.get("end") if first.get("end") is not None else full_seconds
    end = min(end, start + 30.0)                 # preview at most 30 s
    tmp = ctx.new_tmp()
    orig = os.path.join(tmp, "original.wav")
    load_segment(first["path"])[int(start * 1000):int(end * 1000)].export(orig, format="wav")
    ctx.check_cancel()
    res = get_client().separate(orig, os.path.join(tmp, "cleaned.wav"), denoise=denoise,
                                cancel_event=ctx.cancel,
                                status_cb=lambda m: ctx.progress(0.3, m))
    return orig, res["out"], end - start, res["elapsed"]
