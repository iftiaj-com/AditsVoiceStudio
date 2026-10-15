"""
core/gpu.py
───────────
cuda_available(): torch.cuda.is_available() without importing torch.

Importing torch takes about 3 s, and the window only needs to know whether to
switch the GPU toggle on. Once torch is loaded, its own answer is used.
Otherwise this checks that the installed torch is a CUDA build (torch_cuda in
torch/lib) and asks the NVIDIA driver (nvcuda.dll) for at least one device.

use_torch_cudnn(): call before importing faster_whisper / ctranslate2. Both
ctranslate2 and torch ship a cudnn64_9.dll, and Windows loads a DLL name only
once per process. If Whisper's copy loads first, torch's next cuDNN call
(MeloTTS on the GPU, for example) dies with "Could not load symbol
cudnnGetLibConfig" and exit code 0xC0000409, which no try/except can catch.
Loading torch's copy first, by path and without importing torch, gives both
the one ctranslate2 used while torch was imported at start (before
2026-10-14).
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


def _torch_lib():
    spec = importlib.util.find_spec("torch")
    if spec is None or not spec.submodule_search_locations:
        return None
    return os.path.join(list(spec.submodule_search_locations)[0], "lib")


@lru_cache(maxsize=1)
def use_torch_cudnn() -> bool:
    """Load torch's cudnn64_9.dll before anything else can load one (see above)."""
    if sys.platform != "win32":
        return False
    try:
        lib = _torch_lib()
        dll = os.path.join(lib, "cudnn64_9.dll") if lib else None
        if not dll or not os.path.exists(dll):
            return False
        import ctypes
        os.add_dll_directory(lib)
        ctypes.WinDLL(dll)
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def _probe() -> bool:
    try:
        lib = _torch_lib()
        if lib is None:
            return False
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
