"""Match speakers in a stream's mixed soundtrack to a local reference for its creator."""
import functools
import json
import re
import subprocess
import threading

import numpy as np

from . import config, ctx, media, transcribe

SR = 16000
MODEL = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
SEGMENTATION = "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"
_lock = threading.Lock()


@functools.lru_cache(maxsize=1)
def _models():
    s = transcribe._sherpa()
    embedding = s.SpeakerEmbeddingExtractorConfig(model=str(config.MODELS_DIR / MODEL), num_threads=1)
    segmentation = s.OfflineSpeakerSegmentationModelConfig(
        pyannote=s.OfflineSpeakerSegmentationPyannoteModelConfig(
            model=str(config.MODELS_DIR / SEGMENTATION), window_shift_ratio=0.25),
        num_threads=1)
    diarization = s.OfflineSpeakerDiarizationConfig(
        segmentation=segmentation, embedding=embedding,
        clustering=s.FastClusteringConfig(num_clusters=-1, threshold=0.55),
        min_duration_on=0.35, min_duration_off=0.25)
    if not diarization.validate():
        raise ValueError("The speaker models are unavailable.")
    return s.SpeakerEmbeddingExtractor(embedding), s.OfflineSpeakerDiarization(diarization)


def audio(path, start=None, end=None):
    """Read mono PCM using the same sample rate as the transcript and the voice models."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:
        cmd.extend(["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}"])
    cmd.extend(["-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"])
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=90, creationflags=media.NO_WINDOW).stdout
    return np.frombuffer(raw, dtype="<i2")


def _embedding(extractor, samples):
    stream = extractor.create_stream()
    stream.accept_waveform(SR, samples)
    stream.input_finished()
    if not extractor.is_ready(stream):
        return None
    v = np.asarray(extractor.compute(stream), dtype=np.float32)
    norm = np.linalg.norm(v)
    return v / norm if norm > 0 and np.isfinite(norm) else None


def _path(creator):
    if ctx.uid_or_none() and re.fullmatch(r"[a-zA-Z0-9_-]+", creator or ""):
        return config.user_dir(ctx.uid()) / "voices" / f"{creator}.json"
    return None


def profile(creator):
    path = _path(creator)
    if not path or not path.is_file() or not (config.MODELS_DIR / MODEL).is_file() or not (
            config.MODELS_DIR / SEGMENTATION).is_file():
        return None
    try:
        refs = np.array(json.loads(path.read_text(encoding="utf-8"))["references"], dtype=np.float32)
        if refs.ndim == 2 and refs.shape[1] == 512 and 2 <= len(refs) <= 12 and np.isfinite(refs).all():
            return refs
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def enroll(creator, samples):
    """Use clean reference excerpts of the streamer's mic. Reject an inconsistent set."""
    with _lock:
        extractor, _ = _models()
        refs = [_embedding(extractor, a.astype(np.float32) / 32768) for a in samples if len(a) >= SR * 2]
    refs = [r for r in refs if r is not None]
    if not 2 <= len(refs) <= 12:
        raise ValueError("Use at least two clear voice samples of the same streamer.")
    refs = np.stack(refs)
    if (refs @ refs.T).min() < .39:
        raise ValueError("Use at least two clear voice samples of the same streamer.")
    path = _path(creator)
    if path is None:
        raise ValueError("Choose a creator in the local PeakCut account.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"references": refs.tolist()}), encoding="utf-8")
    tmp.replace(path)


def _roles(samples, refs):
    """Compare clean turns to the reference. Overlap and brief or borderline speech stay unknown."""
    with _lock:
        extractor, diarizer = _models()
        pcm = samples.astype(np.float32) / 32768
        turns = diarizer.process(pcm).sort_by_start_time()
        clean = [t for i, t in enumerate(turns) if t.end - t.start >= 1.15
                 and not any(i != k and min(t.end, u.end) - max(t.start, u.start) > .18
                             for k, u in enumerate(turns))]
        matching, different = [], []
        for t in clean:
            v = _embedding(extractor, pcm[round(t.start * SR):round(t.end * SR)])
            if v is None:
                continue
            scores = np.sort(refs @ v)
            if scores[-2] >= .40 and scores[-1] >= .47:
                matching.append((t.start, t.end, "creator"))
            elif scores[-1] < .23:
                different.append((t.start, t.end, "other"))
        return sorted(matching + different)


def tag(words, samples, refs, offset=0.0):
    """Preserve every word and its timing. Add a role only where there is a voice match or mismatch."""
    if not words or refs is None:
        return words
    roles = []
    for a in range(0, len(samples), 45 * SR):
        first = max(0, a - 2 * SR)
        end = min(len(samples), a + 47 * SR)
        if end - first >= SR:
            for x, y, role in _roles(samples[first:end], refs):
                s = offset + first / SR + x
                e = offset + first / SR + y
                if offset + a / SR <= (s + e) / 2 < offset + (a + 45 * SR) / SR:
                    roles.append((s, e, role))
    result = []
    for w in words:
        mid = w["s"] + min(max(0, (w["e"] - w["s"]) / 2), .20)
        matches = {role for s, e, role in roles if s <= mid < e}
        role = next(iter(matches)) if len(matches) == 1 else "unknown"
        result.append(dict(w, speaker=role))
    return result


def heard_creator(words, start, end):
    return any(w.get("speaker") == "creator" and start <= w["s"] < end for w in words)
