"""
vc_runtime/rvc_backend.py
──────────────────────────
RVC conversion and training on top of Applio (pinned commit, see
core/voice_cloning/runtime.py). Runs inside venv_rvc with cwd = Applio/.

Conversion is in-process through Applio's VoiceConverter, which keeps the
last model loaded. Training runs Applio's own CLI (core.py) as child
processes, because train.py starts its own worker processes and exits with
os._exit().

Applio quirks handled here (found in the Phase 0 spike):
  * convert_audio() returns silently when the model file is missing, so the
    model is checked up front and the output file afterwards.
  * convert_audio() replaces "trained" with "added" anywhere in the index
    path, so an index whose path contains "trained" is passed via a copy.
  * Without assets/config.json, training "succeeds" but writes no final
    model. The installer creates it; it is re-created here if missing.
  * train.py exits 2333333 on success and on "not enough data", so success
    is decided by the model file, not the exit code.
"""

import glob
import os
import re
import shutil
import sys
import tempfile
import time

from vc_common import Cancelled, TaskError, run_streaming

_EPOCH_RE = re.compile(r"\|\s*epoch=(\d+)\s*\|")
_OOM_RE = re.compile(r"(?i)out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED|"
                     r"CUDNN_STATUS_ALLOC_FAILED")
_SMALL_RE = re.compile(r"Not enough data present in the training set")
_RAM_RE = re.compile(r"MemoryError|Unable to allocate|paging file is too small")

REQUIRED_MODELS = [
    "rvc/models/embedders/contentvec/pytorch_model.bin",
    "rvc/models/embedders/contentvec/config.json",
    "rvc/models/predictors/rmvpe.pt",
]


def ensure_applio_config(repo):
    cfg = os.path.join(repo, "assets", "config.json")
    if not os.path.exists(cfg):
        shutil.copyfile(os.path.join(repo, "assets", "config_template.json"), cfg)


class RvcBackend:
    def __init__(self, repo):
        self.repo = repo
        self._vc = None
        self._tmp = tempfile.mkdtemp(prefix="vc_rvc_")
        ensure_applio_config(repo)

    # ── helpers ────────────────────────────────────────────────────
    def _converter(self):
        if self._vc is None:
            from rvc.infer.infer import VoiceConverter
            self._vc = VoiceConverter()
        return self._vc

    def _safe_index(self, index_path):
        """A path to *index_path* that does not contain the word "trained"."""
        if not index_path:
            return ""
        if not os.path.isfile(index_path):
            raise TaskError("not_found", f"Index file not found: {index_path}")
        if "trained" not in index_path:
            return index_path
        st = os.stat(index_path)
        dest = os.path.join(self._tmp, f"idx_{abs(hash(index_path))}_{int(st.st_mtime)}.index")
        if not os.path.exists(dest):
            shutil.copyfile(index_path, dest)
        return dest

    # ── commands ───────────────────────────────────────────────────
    def selfcheck(self):
        import torch
        missing = [p for p in REQUIRED_MODELS if not os.path.isfile(os.path.join(self.repo, p))]
        if missing:
            raise TaskError("not_found", "Missing Applio models: " + ", ".join(missing),
                            hint="Re-run the voice cloning install.")
        self._converter()
        return {
            "engine": "rvc",
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "cuda": torch.cuda.is_available(),
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        }

    def inspect(self, pth):
        import torch
        if not os.path.isfile(pth):
            raise TaskError("not_found", f"Model file not found: {pth}")
        try:
            # weights_only: a downloaded model cannot run code while loading.
            cpt = torch.load(pth, map_location="cpu", weights_only=True)
        except Exception as e:
            raise TaskError("bad_model", f"Not a loadable RVC model: {e}")
        if not isinstance(cpt, dict) or "weight" not in cpt or "config" not in cpt:
            raise TaskError("bad_model", "This .pth file is not an RVC voice model "
                                         "(it may be a training checkpoint like G_*.pth).")
        return {
            "sample_rate": int(cpt["config"][-1]),
            "version": cpt.get("version", "v1"),
            "f0": int(cpt.get("f0", 1)),
            "vocoder": cpt.get("vocoder", "HiFi-GAN"),
            "epochs": cpt.get("epoch"),
            "steps": cpt.get("step"),
        }

    def convert(self, req, ctx):
        import soundfile as sf
        src, out, pth = req["in"], req["out"], req["pth"]
        if not os.path.isfile(src):
            raise TaskError("not_found", f"Input audio not found: {src}")
        if not os.path.isfile(pth):
            raise TaskError("not_found", f"Voice model not found: {pth}")
        index = self._safe_index(req.get("index"))
        if os.path.exists(out):
            os.remove(out)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

        ctx.progress("convert", 0.1, "Converting voice…")
        t0 = time.time()
        self._converter().convert_audio(
            audio_input_path=src,
            audio_output_path=out,
            model_path=pth,
            index_path=index,
            pitch=int(req.get("pitch", 0)),
            f0_method=req.get("f0_method", "rmvpe"),
            index_rate=float(req.get("index_rate", 0.3)),
            volume_envelope=float(req.get("volume_envelope", 1.0)),
            protect=float(req.get("protect", 0.33)),
            split_audio=bool(req.get("split_audio", False)),
            embedder_model="contentvec",
            export_format="WAV",
            clean_audio=False,
        )
        ctx.check_cancel()
        if not os.path.isfile(out) or os.path.getsize(out) <= 44:
            raise TaskError("generic", "RVC produced no audio (see the worker log).")
        info = sf.info(out)
        return {"out": out, "sample_rate": int(info.samplerate),
                "seconds": round(info.duration, 3), "elapsed": round(time.time() - t0, 2)}

    def train(self, req, ctx):
        """preprocess → extract → train (+index) → copy model out → clean up."""
        py = sys.executable
        name = req["model_name"]
        dataset = req["dataset_dir"]
        out_dir = req["out_dir"]
        sr = int(req.get("sample_rate", 40000))
        epochs = int(req.get("epochs", 150))
        batch = int(req.get("batch_size", 2))
        save_every = max(1, min(int(req.get("save_every_epoch", 10)), epochs))
        cores = str(int(req.get("cpu_cores", max(1, min(4, (os.cpu_count() or 2) - 1)))))
        f0 = req.get("f0_method", "rmvpe")
        fresh = bool(req.get("fresh", True))
        log_dir = os.path.join(self.repo, "logs", name)
        ensure_applio_config(self.repo)

        if not os.path.isdir(dataset) or not os.listdir(dataset):
            raise TaskError("not_found", f"Dataset folder is empty: {dataset}")
        if fresh and os.path.isdir(log_dir):
            shutil.rmtree(log_dir, ignore_errors=True)

        flags = {"oom": False, "small": False, "ram": False}

        def watch(line):
            if _OOM_RE.search(line):
                flags["oom"] = True
                ctx.request_cancel()   # a crashed DDP child can hang; stop it now
            if _SMALL_RE.search(line):
                flags["small"] = True
            if _RAM_RE.search(line) and not flags["ram"]:
                flags["ram"] = True
                ctx.request_cancel()

        def fail_from_flags(stage):
            if flags["oom"]:
                raise TaskError("oom", f"Out of GPU memory during {stage}.",
                                hint="Retry with batch size 1, or close other GPU apps.")
            if flags["ram"]:
                raise TaskError("ram", f"The computer ran out of memory (RAM) during {stage}.",
                                hint="Close other programs (browsers, IDEs) and retry.")
            if flags["small"]:
                raise TaskError("dataset_too_small",
                                "Not enough audio to train on.",
                                hint="Add more clips (3+ minutes), or use Quick mode.")
            ctx.check_cancel()

        def has_files(sub):
            d = os.path.join(log_dir, sub)
            return os.path.isdir(d) and any(os.scandir(d))

        # 1. preprocess
        if fresh or not has_files("sliced_audios"):
            ctx.progress("preprocess", 0.0, "Slicing audio…")
            rc = run_streaming([py, "core.py", "preprocess", "--model-name", name,
                                "--dataset-path", dataset, "--sample-rate", str(sr),
                                "--cpu-cores", cores, "--cut-preprocess", "Automatic",
                                "--chunk-len", "3.0", "--overlap-len", "0.3",
                                "--normalization-mode", "none"],
                               self.repo, ctx, watch)
            fail_from_flags("preprocess")
            if rc != 0 or not has_files("sliced_audios"):
                raise TaskError("generic", f"Preprocess failed (exit {rc}). See the worker log.")

        # 2. extract pitch + content features
        if fresh or not (has_files("extracted") and has_files("f0")):
            ctx.progress("extract", 0.10, "Extracting pitch and features…")
            rc = run_streaming([py, "core.py", "extract", "--model-name", name,
                                "--sample-rate", str(sr), "--f0-method", f0, "--gpu", "0",
                                "--embedder-model", "contentvec", "--include-mutes", "2",
                                "--cpu-cores", cores],
                               self.repo, ctx, watch)
            fail_from_flags("feature extraction")
            if rc != 0 or not has_files("extracted"):
                raise TaskError("generic", f"Feature extraction failed (exit {rc}). See the worker log.")

        # 3. train (core.py also builds the index afterwards)
        ctx.progress("train", 0.25, f"Training… epoch 0/{epochs}", epoch=0, total_epoch=epochs)
        times = []

        def on_train_line(line):
            watch(line)
            m = _EPOCH_RE.search(line)
            if m:
                ep = int(m.group(1))
                times.append(time.monotonic())
                eta = None
                if len(times) >= 2:
                    recent = times[-6:]
                    per = (recent[-1] - recent[0]) / (len(recent) - 1)
                    eta = int(per * max(0, epochs - ep))
                ctx.progress("train", 0.25 + 0.70 * min(ep, epochs) / epochs,
                             f"Training… epoch {ep}/{epochs}",
                             epoch=ep, total_epoch=epochs, eta_seconds=eta)

        # train_site/sitecustomize.py caps Applio's DataLoader workers (RAM, see there)
        train_env = dict(os.environ)
        site_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_site")
        train_env["PYTHONPATH"] = site_dir + os.pathsep + train_env.get("PYTHONPATH", "")
        train_env["VC_MAX_LOADER_WORKERS"] = str(int(req.get("loader_workers", 0)))
        run_streaming([py, "core.py", "train", "--model-name", name,
                       "--sample-rate", str(sr), "--total-epoch", str(epochs),
                       "--save-every-epoch", str(save_every), "--batch-size", str(batch),
                       "--gpu", "0", "--vocoder", "HiFi-GAN", "--pretrained",
                       "--save-only-latest", "--index-algorithm", "Auto"],
                      self.repo, ctx, on_train_line, env=train_env)

        models = []
        for p in glob.glob(os.path.join(log_dir, f"{glob.escape(name)}_*e_*s.pth")):
            m = re.search(r"_(\d+)e_(\d+)s\.pth$", p)
            if m:
                models.append((int(m.group(1)), int(m.group(2)), p))
        if not models:
            fail_from_flags("training")
            raise TaskError("generic", "Training ended without producing a model. See the worker log.")
        if ctx.cancel.is_set() and not (flags["oom"] or flags["ram"]):
            raise Cancelled()
        ep_done, steps, pth = max(models)
        if ep_done < epochs:
            fail_from_flags("training")

        # 4. index
        ctx.progress("index", 0.95, "Building search index…")
        indexes = sorted(glob.glob(os.path.join(log_dir, "*.index")), key=os.path.getmtime)
        if not indexes:
            run_streaming([py, "core.py", "index", "--model-name", name,
                           "--index-algorithm", "Auto"], self.repo, ctx, watch)
            indexes = sorted(glob.glob(os.path.join(log_dir, "*.index")), key=os.path.getmtime)
        ctx.check_cancel()

        # 5. copy out, drop the 1.3 GB of training checkpoints
        ctx.progress("copy", 0.98, "Saving voice…")
        os.makedirs(out_dir, exist_ok=True)
        out_pth = os.path.join(out_dir, f"{name}.pth")
        shutil.copyfile(pth, out_pth)
        out_index = None
        if indexes:
            out_index = os.path.join(out_dir, f"{name}.index")
            shutil.copyfile(indexes[-1], out_index)
        info = self.inspect(out_pth)
        if not req.get("keep_checkpoints", False):
            shutil.rmtree(log_dir, ignore_errors=True)
        ctx.progress("done", 1.0, "Training complete")
        return {"pth": out_pth, "index": out_index, "sample_rate": info["sample_rate"],
                "epochs": ep_done, "steps": steps}
