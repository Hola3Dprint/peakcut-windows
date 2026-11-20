"""Run PeakCut's transcription worker with the bundled Windows CUDA runtime."""

import ctypes
import os
import sys
from pathlib import Path


def main(port):
    runtime = Path(__file__).resolve().with_name("asr-cuda")
    # ONNX Runtime and cuDNN also load libraries by name during inference.
    os.environ["PATH"] = str(runtime) + os.pathsep + os.environ.get("PATH", "")
    os.environ.update(
        PEAKCUT_ASR_PROVIDER=f"cuda:{runtime / 'provider.conf'}",
        PEAKCUT_ASR_MODEL="sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-fp16",
        PEAKCUT_ASR_HOST="127.0.0.1",
        PEAKCUT_ASR_PORT=port,
        PEAKCUT_ASR_COPIES="1",
        PEAKCUT_ASR_BATCH="1",
        PEAKCUT_ASR_CHUNK_SECONDS="6",
        CUDA_LAUNCH_BLOCKING="1",
    )
    with os.add_dll_directory(str(runtime)):
        # Load the CUDA build before Sherpa's Python extension resolves onnxruntime.dll.
        ctypes.WinDLL(str(runtime / "cudnn64_9.dll"))
        ctypes.WinDLL(str(runtime / "onnxruntime.dll"))
        from studio import asr_worker

        if not asr_worker.TOKEN:
            raise SystemExit("Set PEAKCUT_ASR_TOKEN before starting the CUDA worker.")
        asr_worker.transcribe._recognizer()
        get_module_handle = ctypes.windll.kernel32.GetModuleHandleW
        get_module_handle.argtypes = [ctypes.c_wchar_p]
        get_module_handle.restype = ctypes.c_void_p
        if not get_module_handle("onnxruntime_providers_cuda.dll"):
            raise SystemExit("The CUDA execution provider did not load.")
        asr_worker.main()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "9000")
