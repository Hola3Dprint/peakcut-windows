"""Turn an archived YouTube chat into crowd-reaction times on the original VOD clock."""
import json
import subprocess
import sys
import tempfile
import time
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from statistics import median

from .media import NO_WINDOW, is_youtube

BIN = 8
MAX_BYTES = 64 * 1024 * 1024


def bursts(path):
    viewers = defaultdict(set)
    with path.open("rb") as stream:
        for line in stream:
            if stream.tell() > MAX_BYTES:
                break
            if len(line) > 1_000_000:
                continue
            try:
                item = json.loads(line)["replayChatItemAction"]
                second = int(item["videoOffsetTimeMsec"]) // 1000
                if second < 0:
                    continue
                for action in item["actions"]:
                    message = action.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer", {})
                    author = message.get("authorExternalChannelId")
                    if isinstance(author, str) and author:
                        viewers[second // BIN].add(author)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    counts = {slot: len(people) for slot, people in viewers.items()}
    peaks = []
    for slot, count in sorted(counts.items()):
        if slot < 4:
            continue
        usual = median(counts.get(prev, 0) for prev in range(max(0, slot - 15), slot))
        if count >= max(4, usual * 2.5, usual + 3):
            peaks.append(slot * BIN)
    return peaks


def near(peaks, start, end):
    # The 8-second bin may straddle the line. A crowd can react several seconds after it.
    i = bisect_left(peaks, start - BIN + 1)
    return i < len(peaks) and peaks[i] <= end + 8


def download(url):
    if not is_youtube(url):
        return [], "unavailable"
    # Only compact timestamps leave the temporary folder. A huge or slow replay must not hold up a VOD.
    with tempfile.TemporaryDirectory(prefix="pkchat") as folder:
        root = Path(folder)
        cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--skip-download",
               "--write-subs", "--sub-langs", "live_chat", "--sub-format", "json", "--no-progress",
               "--socket-timeout", "10", "--retries", "1", "--fragment-retries", "1", "-o", "chat.%(ext)s", "--", url]
        with subprocess.Popen(cmd, cwd=folder, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=NO_WINDOW) as proc:
            deadline = time.monotonic() + 120
            while proc.poll() is None:
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    size = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and p.name.endswith((".part", ".json")))
                    if size > MAX_BYTES or time.monotonic() >= deadline:
                        proc.kill()
                        proc.wait()
                        break
        for name, state in (("chat.live_chat.json", "read"), ("chat.live_chat.json.part", "partial")):
            path = root / name
            if path.is_file() and path.stat().st_size:
                return bursts(path), ("partial" if path.stat().st_size > MAX_BYTES else state)
    return [], "unavailable"
