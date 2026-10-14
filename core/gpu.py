"""
core/gpu.py
───────────
cuda_available(): torch.cuda.is_available() without importing torch.

Importing torch takes about 3 s, and the window only needs to know whether to
switch the GPU toggle on. Once torch is loaded, its own answer is used.
Otherwise this checks that the installed torch is a CUDA build (torch_cuda in
torch/lib) and asks the NVIDIA driver (nvcuda.dll) for at least one device.
"""
import importlib.util
import os
import sys
from functools import lru_cache


def cuda_available() -> bool:
    if "torch" in sys.modules:
        try:
            return bool(sys.modules["torch"].cuda.is_available())
        except Exception:
            return False
    return _probe()


@lru_cache(maxsize=1)
def _probe() -> bool:
    try:
        spec = importlib.util.find_spec("torch")
        if spec is None or not spec.submodule_search_locations:
            return False
        lib = os.path.join(list(spec.submodule_search_locations)[0], "lib")
        if not any(os.path.exists(os.path.join(lib, n))
                   for n in ("torch_cuda.dll", "libtorch_cuda.so")):
            return False                    # CPU-only torch
        if sys.platform != "win32":
            return True
        import ctypes
        cuda = ctypes.WinDLL("nvcuda.dll")
        if cuda.cuInit(0) != 0:
            return False
        count = ctypes.c_int(0)
        return cuda.cuDeviceGetCount(ctypes.byref(count)) == 0 and count.value > 0
    except Exception:
        return False
