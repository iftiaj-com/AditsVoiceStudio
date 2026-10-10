"""
vc_runtime/train_site/sitecustomize.py
───────────────────────────────────────
Loaded automatically by every Python process of an RVC training run,
because rvc_backend.py puts this folder on PYTHONPATH for that run only.

Applio's train.py hardcodes DataLoader(num_workers=4, prefetch_factor=8,
persistent_workers=True). On Windows each loader worker is a fresh Python
process that loads all of torch, about 2 GB of commit charge each. On the
14 GB dev machine a 2-epoch run pushed commit to 34.5 GB and free RAM to
0.5 GB, and one run died with MemoryError in a loader worker.

Measured with a 2-3 epoch run: 4 workers peaked at 34.5 GB commit, 1 worker
at 29.9 GB, 0 workers at 28.8 GB with no slower epochs (RVC loads small
precomputed feature files, so loading in-process is cheap).

This caps num_workers at VC_MAX_LOADER_WORKERS (default 0) and the
prefetch depth at 2, without editing Applio's files. The patch is applied
lazily, when torch.utils.data.dataloader is first imported, so processes
that never import torch pay nothing.
"""

import importlib.abc
import importlib.util
import os
import sys

_CAP = max(0, int(os.environ.get("VC_MAX_LOADER_WORKERS", "0")))
_TARGET = "torch.utils.data.dataloader"


def _patch(module):
    DataLoader = module.DataLoader
    if getattr(DataLoader, "_vc_capped", False):
        return
    orig_init = DataLoader.__init__

    def __init__(self, *args, **kwargs):
        nw = kwargs.get("num_workers", 0) or 0
        if nw > _CAP:
            kwargs["num_workers"] = _CAP
            if _CAP == 0:
                kwargs["persistent_workers"] = False
                kwargs["prefetch_factor"] = None
            else:
                kwargs["prefetch_factor"] = min(kwargs.get("prefetch_factor") or 2, 2)
            print(f"[voice cloning] DataLoader workers capped {nw} -> {_CAP} to save RAM", flush=True)
        orig_init(self, *args, **kwargs)

    DataLoader.__init__ = __init__
    DataLoader._vc_capped = True


class _Hook(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != _TARGET:
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        orig_exec = loader.exec_module

        def exec_module(module):
            orig_exec(module)
            _patch(module)

        loader.exec_module = exec_module
        return spec


if _TARGET in sys.modules:
    _patch(sys.modules[_TARGET])
else:
    sys.meta_path.insert(0, _Hook())
