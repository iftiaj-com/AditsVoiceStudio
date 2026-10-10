"""
vc_runtime/seedvc_backend.py
─────────────────────────────
Zero-shot ("Quick") voice conversion with Seed-VC (pinned commit of an
archived repo, see core/voice_cloning/runtime.py). Runs inside venv_seedvc
with cwd = seed-vc/.

The conversion loop is ported from seed-vc/inference.py main() at that
commit (f0_condition=False path only), with two changes:
  * models are loaded once and reused across requests (upstream reloads
    every call);
  * the reference-voice features are cached per reference file, since the
    same voice converts many clips.

Defaults come from the Phase 0 measurements on a GTX 1650 (no tensor
cores): fp32 with 10 diffusion steps ran about 7x faster than the upstream
default of fp16 with 30 steps, with the same Whisper transcript.
"""

import contextlib
import os
import sys
import time

import numpy as np

from vc_common import TaskError

MAX_REF_SECONDS = 25
DEFAULT_STEPS = 10
DEFAULT_CFG = 0.7


def _crossfade(chunk1, chunk2, overlap):
    fade_out = np.cos(np.linspace(0, np.pi / 2, overlap)) ** 2
    fade_in = np.cos(np.linspace(np.pi / 2, 0, overlap)) ** 2
    if len(chunk2) < overlap:
        chunk2[:overlap] = chunk2[:overlap] * fade_in[:len(chunk2)] + (chunk1[-overlap:] * fade_out)[:len(chunk2)]
    else:
        chunk2[:overlap] = chunk2[:overlap] * fade_in + chunk1[-overlap:] * fade_out
    return chunk2


class SeedVcBackend:
    def __init__(self, repo):
        self.repo = repo
        self._models = None
        self._ref_key = None
        self._ref = None

    def _load(self):
        if self._models is not None:
            return
        # inference.py sets HF_HUB_CACHE relative to the cwd at import time,
        # and the worker has already chdir'd into the repo.
        import inference as svc
        from types import SimpleNamespace
        args = SimpleNamespace(fp16=False, f0_condition=False, checkpoint=None, config=None)
        (model, semantic_fn, _f0_fn, vocoder_fn, campplus, to_mel, mel_args) = svc.load_models(args)
        self.svc = svc
        self.device = svc.device
        self._models = dict(model=model, semantic_fn=semantic_fn, vocoder_fn=vocoder_fn,
                            campplus=campplus, to_mel=to_mel, sr=mel_args["sampling_rate"])

    def selfcheck(self):
        import torch
        self._load()
        return {
            "engine": "seedvc",
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "cuda": torch.cuda.is_available(),
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "sample_rate": self._models["sr"],
        }

    def _reference(self, ref_path):
        """Features of the reference voice, cached by path + mtime + size."""
        import librosa
        import torch
        import torchaudio
        st = os.stat(ref_path)
        key = (os.path.abspath(ref_path), st.st_mtime_ns, st.st_size)
        if key == self._ref_key:
            return self._ref
        m = self._models
        sr = m["sr"]
        ref_audio = librosa.load(ref_path, sr=sr)[0][: sr * MAX_REF_SECONDS]
        ref_audio = torch.tensor(ref_audio).unsqueeze(0).float().to(self.device)
        ori_16k = torchaudio.functional.resample(ref_audio, sr, 16000)
        S_ori = m["semantic_fn"](ori_16k)
        mel2 = m["to_mel"](ref_audio.float())
        target2_lengths = torch.LongTensor([mel2.size(2)]).to(mel2.device)
        feat2 = torchaudio.compliance.kaldi.fbank(ori_16k, num_mel_bins=80, dither=0,
                                                  sample_frequency=16000)
        feat2 = feat2 - feat2.mean(dim=0, keepdim=True)
        style2 = m["campplus"](feat2.unsqueeze(0))
        prompt_condition, *_ = m["model"].length_regulator(S_ori, ylens=target2_lengths,
                                                          n_quantizers=3, f0=None)
        self._ref_key = key
        self._ref = dict(mel2=mel2, style2=style2, prompt_condition=prompt_condition)
        return self._ref

    def convert(self, req, ctx):
        import librosa
        import soundfile as sf
        import torch
        import torchaudio

        src, ref, out = req["in"], req["ref"], req["out"]
        for p, what in ((src, "Input audio"), (ref, "Reference audio")):
            if not os.path.isfile(p):
                raise TaskError("not_found", f"{what} not found: {p}")
        steps = int(req.get("diffusion_steps", DEFAULT_STEPS))
        cfg_rate = float(req.get("cfg_rate", DEFAULT_CFG))
        length_adjust = float(req.get("length_adjust", 1.0))
        fp16 = bool(req.get("fp16", False))

        t0 = time.time()
        self._load()
        m = self._models
        sr = m["sr"]
        hop_length = 256
        max_context_window = sr // hop_length * 30
        overlap_frame_len = 16
        overlap_wave_len = overlap_frame_len * hop_length

        with torch.no_grad():
            ctx.progress("convert", 0.05, "Reading reference voice…")
            r = self._reference(ref)
            mel2, style2, prompt_condition = r["mel2"], r["style2"], r["prompt_condition"]

            source_audio = librosa.load(src, sr=sr)[0]
            source_audio = torch.tensor(source_audio).unsqueeze(0).float().to(self.device)
            waves_16k = torchaudio.functional.resample(source_audio, sr, 16000)

            # content features; Whisper handles 30 s per pass
            ctx.progress("convert", 0.15, "Extracting speech content…")
            if waves_16k.size(-1) <= 16000 * 30:
                S_alt = m["semantic_fn"](waves_16k)
            else:
                overlap_t = 5
                parts, buffer, traversed = [], None, 0
                while traversed < waves_16k.size(-1):
                    ctx.check_cancel()
                    if buffer is None:
                        chunk = waves_16k[:, traversed:traversed + 16000 * 30]
                    else:
                        chunk = torch.cat([buffer, waves_16k[:, traversed:traversed + 16000 * (30 - overlap_t)]], dim=-1)
                    S = m["semantic_fn"](chunk)
                    parts.append(S if traversed == 0 else S[:, 50 * overlap_t:])
                    buffer = chunk[:, -16000 * overlap_t:]
                    traversed += 30 * 16000 if traversed == 0 else chunk.size(-1) - 16000 * overlap_t
                S_alt = torch.cat(parts, dim=1)

            mel = m["to_mel"](source_audio.float())
            target_lengths = torch.LongTensor([int(mel.size(2) * length_adjust)]).to(mel.device)
            cond, *_ = m["model"].length_regulator(S_alt, ylens=target_lengths, n_quantizers=3, f0=None)

            max_source_window = max_context_window - mel2.size(2)
            processed, chunks_out, previous = 0, [], None
            total = max(1, cond.size(1))
            amp = (torch.autocast(device_type=self.device.type, dtype=torch.float16)
                   if fp16 else contextlib.nullcontext())
            while processed < cond.size(1):
                ctx.check_cancel()
                ctx.progress("convert", 0.2 + 0.75 * processed / total, "Converting voice…")
                chunk_cond = cond[:, processed:processed + max_source_window]
                is_last = processed + max_source_window >= cond.size(1)
                cat_condition = torch.cat([prompt_condition, chunk_cond], dim=1)
                with amp:
                    vc_target = m["model"].cfm.inference(
                        cat_condition, torch.LongTensor([cat_condition.size(1)]).to(mel2.device),
                        mel2, style2, None, steps, inference_cfg_rate=cfg_rate)
                    vc_target = vc_target[:, :, mel2.size(-1):]
                vc_wave = m["vocoder_fn"](vc_target.float()).squeeze()[None, :]
                if processed == 0:
                    if is_last:
                        chunks_out.append(vc_wave[0].cpu().numpy())
                        break
                    chunks_out.append(vc_wave[0, :-overlap_wave_len].cpu().numpy())
                    previous = vc_wave[0, -overlap_wave_len:]
                    processed += vc_target.size(2) - overlap_frame_len
                elif is_last:
                    chunks_out.append(_crossfade(previous.cpu().numpy(), vc_wave[0].cpu().numpy(), overlap_wave_len))
                    break
                else:
                    chunks_out.append(_crossfade(previous.cpu().numpy(),
                                                 vc_wave[0, :-overlap_wave_len].cpu().numpy(), overlap_wave_len))
                    previous = vc_wave[0, -overlap_wave_len:]
                    processed += vc_target.size(2) - overlap_frame_len

        wave = np.concatenate(chunks_out).astype(np.float32)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        sf.write(out, wave, sr, subtype="PCM_16")
        return {"out": out, "sample_rate": int(sr), "seconds": round(len(wave) / sr, 3),
                "elapsed": round(time.time() - t0, 2)}
