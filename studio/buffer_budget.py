"""Keep raw recording buffers within a shared storage budget."""

from dataclasses import dataclass
from pathlib import Path


RAW_SUFFIXES = {".ts", ".pcm"}


@dataclass
class Buffer:
    job_id: str
    files: list[tuple[Path, int]]
    newest: float


def inventory(root: Path) -> list[Buffer]:
    root = root.resolve()
    buffers = []
    for folder in root.glob("*/creators/*/live/*/buffer"):
        if folder.is_symlink() or not folder.resolve().is_relative_to(root):
            continue
        files = []
        newest = 0.0
        try:
            for file in folder.iterdir():
                if file.suffix not in RAW_SUFFIXES or file.is_symlink():
                    continue
                try:
                    stat = file.stat()
                except FileNotFoundError:
                    continue
                if file.is_file():
                    files.append((file, stat.st_size))
                    newest = max(newest, stat.st_mtime)
        except FileNotFoundError:
            continue
        if files:
            buffers.append(Buffer(folder.parent.name, files, newest))
    return buffers


def available_for_cleanup(job: dict, in_use: set[str]) -> bool:
    return (
        job["id"] not in in_use
        and job.get("status") in {"done", "error", "cancelled"}
        and not job.get("recover")
        and not any(clip.get("status") in {"queued", "rendering", "running"} for clip in job.get("clips", []))
    )


def enforce(root: Path, jobs: list[dict], in_use: set[str], limit: int) -> tuple[int, list[dict]]:
    """Remove old closed buffers ahead of the limit. Active, recovering and queued clips keep their source."""
    buffers = inventory(root)
    total = sum(size for buffer in buffers for _, size in buffer.files)
    if total < limit * 7 // 8:
        return total, []

    known = {job["id"]: job for job in jobs if job.get("kind") == "live"}
    expired = []
    for buffer in sorted(buffers, key=lambda b: b.newest):
        if total <= limit * 13 // 16:
            break
        job = known.get(buffer.job_id)
        if not job or not available_for_cleanup(job, in_use):
            continue
        removed = False
        for path, size in buffer.files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            removed = True
        if removed:
            expired.append(job)
    return total, expired
