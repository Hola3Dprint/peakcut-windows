"""Live sessions: record a stream into a rolling buffer, transcribe as it goes, let the AI pick clips."""
import collections
import json
import logging
import re
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from . import alerts, auth, brain, config, creators, ctx, editorial, facecam, games, jobs, load, media, transcribe, usage, voices, buffer_budget

# ":username!user@host PRIVMSG #channel :the message"
_NICK = re.compile(r"(?m)^:([A-Za-z0-9_]{2,25})!")

SESSIONS: dict = {}
BUFFER_LIMIT_BYTES = max(0, int(float(config.env("PEAKCUT_LIVE_BUFFER_LIMIT_GIB", "0")) * 1024**3))
_buffer_paused = False
logger = logging.getLogger("uvicorn.error")
SEG_SEC = 6           # length of each buffer piece
PAD = 8               # seconds kept either side of a clip so it can be re-trimmed later
CHECK_EVERY = 180     # look at the stream every 3 minutes...
QUIET_MAX = 600       # ...but only ask the AI when something happened, or at most every 10 minutes when calm
WINDOW = 240          # seconds of transcript shown to the AI per check
CHAT_KEEP = 600       # seconds of chat names kept: only the last few minutes are ever read
SR = 16000
PCM_SECONDS = 60


class NotLive(Exception):
    pass


def _trim_names(users: dict, second: int, kept: int) -> int:
    """Drop the chat names older than CHAT_KEEP, and say when this was last done.

    Only the last few minutes are ever read, and a stream that runs for eight hours would otherwise hold
    a name for every second of it. Called under the session's lock, from the chat reader thread."""
    if second - kept < 60:
        return kept              # walking the whole dict on every message would cost more than it saves
    for t in [t for t in users if t < second - CHAT_KEEP]:
        del users[t]
    return second


def media_safe(e) -> str:
    from . import safe
    return safe.friendly(e, 300)


def normalize(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    m = re.match(r"^(https?://(?:www\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+))/?(?:[?#].*)?$", url)
    return m.group(1) + "/live" if m else url


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def resolve(url: str) -> dict:
    """Ask yt-dlp for the stream's playable address. Raises NotLive if the channel is offline."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": _Quiet(),
            "format": "b[height<=720]/bv*[height<=720]+ba/b"}
    if media.is_youtube(url) and media.youtube_paused():
        raise NotLive(media.YT_BLOCKED)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        if media.note_refusal(url, e):
            raise NotLive(media.YT_BLOCKED) from e
        raise NotLive(str(e)[-200:]) from e
    if not info or not info.get("is_live"):
        raise NotLive("This channel is not live right now.")
    fmts = info.get("requested_formats") or [info]
    title = info.get("title") or ""
    if "(live)" in title and info.get("description"):  # Twitch puts the real stream title here
        title = str(info["description"]).splitlines()[0][:120]
    return {"urls": [f["url"] for f in fmts], "title": title,
            "uploader": info.get("uploader") or info.get("channel") or ""}


def _check_buffer_budget():
    global _buffer_paused
    total, expired = buffer_budget.enforce(config.USERS_DIR, jobs.all_jobs(), set(SESSIONS), BUFFER_LIMIT_BYTES)
    cutoff = BUFFER_LIMIT_BYTES * (14 if _buffer_paused else 15) // 16
    _buffer_paused = total >= cutoff
    if _buffer_paused:
        for session in list(SESSIONS.values()):
            if not session.finished and not session.user_stopped and not session.ended:
                with ctx.as_user(session.job["user"]):
                    session.stop("Paused to stay within the recording buffer limit")
    for job in expired:
        details = job.setdefault("ai", {})
        if details.get("note") == "Highlight review was incomplete. The affected stream footage was kept.":
            details["note"] = "Highlight review was incomplete. Raw footage expired under the recording buffer limit."
            jobs.save(job)


def _buffer_budget_loop():
    while True:
        try:
            _check_buffer_budget()
        except OSError as exc:
            logger.warning("Could not reclaim recording buffer space: %s", exc)
        time.sleep(5)


def active():
    return [s for s in SESSIONS.values() if not s.finished and not s.user_stopped]


def active_mine():
    u = ctx.uid()
    return [s for s in active() if s.job["user"] == u]


def active_for(cid):
    return next((s for s in active_mine() if s.job["creator"] == cid), None)


def mine(jid):
    """The signed-in user's recording with this id, or None."""
    s = SESSIONS.get(jid)
    return s if s and s.job["user"] == ctx.uid() else None


class Session:
    def __init__(self, job, settings):
        self.job, self.settings = job, settings
        self.dir = Path(job["dir"])
        self.buf = self.dir / "buffer"
        self.buf.mkdir(exist_ok=True)
        (self.dir / "clips").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.stop_flag = threading.Event()
        self.ended = False            # recording has stopped (stream over, stopped, or failed)
        self.finished = False         # everything is wrapped up
        self.end_note = "Stream ended"
        self.user_stopped = False
        self.proc = None
        self.pending = bytearray()    # audio received but not yet transcribed
        self.clock_fix = 0.0          # how far the video-piece clock is behind the audio clock, last measured
        self.pending_base = 0         # sample index where `pending` starts
        self.samples = 0              # total audio samples received
        self.db = []                  # loudness per second
        self.words = []
        self.t_done = 0.0             # transcript is complete up to this second
        self.segments = []            # (path, start, end) on the session clock
        self.seg_clock = 0.0
        self.csv_seen = {}
        self.part = 0
        self.chat = collections.Counter()   # second -> messages
        self.chat_users = {}                 # second -> the people who talked, when it can be told
        self.chat_kept = 0                   # the second this was last pruned at
        self.chat_total = 0
        self.chat_on = False
        self.scan = job.setdefault("scan", {"through": 0.0, "checked": 0, "gaps": []})
        self.scan_attempts = 0
        self.asr_attempts = 0
        self.checks = 0
        self.rejected = 0
        self.verdicts = []
        self.last_trigger = -999.0
        self.started_at = time.time()
        self.skipped = 0
        self.last_ai = 0.0
        self.tokens = 0
        self.manual = []
        self.n = 0
        self.spike = None             # a jump in loudness or chat waiting for its transcript
        self.said = None              # someone said "clip": that moment, waiting for its transcript
        self.last_said = -999.0
        self.last_spike = -999.0      # when the last jump was sent to the AI (stream seconds)
        self.base = {}                # running averages the jumps are measured against, shown on the card
        self.spikes = 0
        self.game = ""                # which game this stream is, when it can be told
        self.facecam_box = None       # legacy picture-in-picture layout
        self.facecam_checked = False
        self.voice = voices.profile(job["creator"])
        self.ai_busy = 0.0            # when the AI check now running was started, 0 when none is

    def _ask_ai(self, window=None, why=None, scheduled=False):
        """Only a completed scheduled check advances coverage. A trigger is an additional look."""
        now = time.time()
        if self.ai_busy:
            return False

        def work():
            ok = False
            try:
                ok = self._ai_check(window, why) is not False
            except Exception as e:  # noqa: BLE001
                self.job["ai"]["last_error"] = str(e)[-200:]
            finally:
                try:
                    if scheduled:
                        with self.lock:
                            self.scan_attempts += 1
                            if ok or self.scan_attempts >= 2:
                                if not ok:
                                    self.scan["gaps"].append([self.scan["through"], window[1]])
                                self.scan.update(through=window[1], checked=self.scan["checked"] + 1)
                                self.scan_attempts = 0
                        jobs.save(self.job)
                finally:
                    self.ai_busy = 0.0
        self.ai_busy = now
        threading.Thread(target=ctx.bind(work), daemon=True, name="ai_check").start()
        return True

    def _schedule_review(self, final=False):
        if self.ai_busy:
            return
        every = (int(self.settings.get("ai_every_min", 3)) or 3) * 60
        cursor = self.scan["through"]
        overlap = max(90, int(self.settings["max_len"]))
        if self.t_done >= cursor + every or (final and self.t_done > cursor):
            end = min(self.t_done, cursor + every)
            if editorial.enabled() and not final and self.t_done < end + SEG_SEC * 2 and 0 < end - self.seg_clock <= SEG_SEC * 2:
                return  # the last few seconds of video are still being written
            self._ask_ai((max(0, cursor - overlap), end), "Scheduled review", scheduled=True)
            return
        for field, before, after in (("said", self.SAID_BEFORE, self.SAID_AFTER), ("spike", self.AUTO_BEFORE, self.AUTO_AFTER)):
            trigger = getattr(self, field)
            if trigger and (self.t_done >= trigger["t"] + after or final):
                window = (max(0, trigger["t"] - before), min(self.t_done, trigger["t"] + after))
                if window[1] <= cursor or self._ask_ai(window, trigger["why"]):
                    setattr(self, field, None)
                return

    def _find_facecam(self, src):
        """Where the webcam is in the stream, once. Only a sure answer is used.

        One frame goes to the AI as well, and whatever it says is measured again on the
        picture. A stream where the webcam is a square picture along the right edge, or
        one where there is no webcam at all and the game has panels of its own, is told
        apart from a real camera by what moves: a person sits still, the game does not."""
        try:
            visual_check = None if self.settings.get("layout") == "stream" else brain.look_for_facecam
            box = facecam.detect(src, ask=visual_check)
        except Exception:  # noqa: BLE001 - a missing facecam is not a failure
            return None
        if not box or box.get("conf") != "high":
            return None
        self.job["facecam"] = [box["x"], box["y"], box["w"], box["h"]]
        return self.job["facecam"]

    # ---------------------------------------------------------------- the game this stream is
    def _game(self):
        """The game, from what the creator set or from what the stream's title says.

        The stream title is the best place to look: on launch night it says the game."""
        if self.game:
            return self.game
        found = games.detect(self.settings.get("game"), self.job.get("title"), self.job.get("uploader"),
                             self.job.get("source", {}).get("name"))
        self.game = found[0] or ""
        if self.game:
            try:
                self.job["game"] = self.game
            except Exception:  # noqa: BLE001
                pass
        return self.game

    # ---------------------------------------------------------------- recording
    def start(self):
        SESSIONS[self.job["id"]] = self
        threading.Thread(target=ctx.bind(self._ingest), daemon=True).start()
        threading.Thread(target=ctx.bind(self._analyze), daemon=True).start()
        m = re.match(r"^https?://(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)", self.job["source"]["url"])
        if m:
            threading.Thread(target=ctx.bind(self._chat), args=(m.group(1).lower(),), daemon=True).start()

    def stop(self, note="Stopped by you"):
        """Stop capture, then finish reviewing audio already received."""
        self.end_note = note
        self.user_stopped = True
        self.stop_flag.set()
        with self.lock:
            self.job["capture_stopped"] = True
            self.job.update(status="running", stage="Finishing the recorded stream")
        jobs.save(self.job)
        proc = self.proc
        if proc and proc.poll() is None:
            def finish_segment():
                try:
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
                    proc.wait(timeout=8)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    if proc.poll() is None:
                        proc.terminate()
            threading.Thread(target=finish_segment, daemon=True).start()

    def _ingest(self):
        """Record the stream. However this ends, even when ffmpeg cannot start at all, the session is marked as
        ended, so it stops showing as recording and gives its place to the next one."""
        try:
            self._ingest_loop()
        except Exception as e:  # noqa: BLE001
            self.job["error"] = f"Recording stopped: {media_safe(e)}"
        finally:
            self.ended = True

    def _keep_audio(self, data):
        # The live pipe would otherwise lose its unwritten transcript if the app closed.
        pos = self.samples
        while data:
            first = pos // (PCM_SECONDS * SR) * PCM_SECONDS
            count = min(len(data), 2 * ((first + PCM_SECONDS) * SR - pos))
            with (self.buf / f"audio-{first:010d}.pcm").open("ab") as f:
                f.write(data[:count])
            pos += count // 2
            data = data[count:]

    def _recover_audio(self):
        cursor = round(self.scan["through"] * SR)
        self.words = [w for w in self.words if w["s"] < cursor / SR]
        self.pending_base = self.samples = cursor
        self.t_done = cursor / SR
        chat = self.buf / "chat.json"
        if chat.is_file():
            self.chat.update({int(t): n for t, n in json.loads(chat.read_text(encoding="utf-8")).items()})
            self.chat_total = sum(self.chat.values())
        for pcm in sorted(self.buf.glob("audio-*.pcm")):
            first = int(pcm.stem.split("-")[1])
            data = pcm.read_bytes()
            levels = media.loudness_per_second(np.frombuffer(data, dtype=np.int16))
            if len(self.db) < first:
                self.db.extend([-100.0] * (first - len(self.db)))
            self.db[first:first + len(levels)] = levels.tolist()
            offset = max(0, self.samples - first * SR)
            if offset * 2 >= len(data):
                continue
            if first * SR > self.samples:
                self.scan["gaps"].append([self.samples / SR, first])
                self.samples = self.pending_base = first * SR
            self.pending.extend(data[offset * 2:])
            self.samples += len(data) // 2 - offset
            while self.pending:
                try:
                    self._transcribe_step()
                except Exception:
                    if self.asr_attempts != 1:
                        raise
            self._schedule_review()
        recorded = max(self.job.get("duration", 0), self.seg_clock)
        if recorded - self.samples / SR > 1:
            self.scan["gaps"].append([self.samples / SR, recorded])
            self.samples = self.pending_base = round(recorded * SR)
            self.t_done = self.samples / SR

    def _ingest_loop(self):
        fails = 0
        url = self.job["source"]["url"]
        while not self.stop_flag.is_set():
            try:
                info = resolve(url)
            except NotLive as e:
                fails += 1
                if self.samples == 0 or fails >= 3:
                    if self.samples == 0:
                        self.job["error"] = f"Could not start recording: {e}"
                    break
                self.stop_flag.wait(10)
                continue
            if not self.job.get("uploader"):
                self.job.update(title=info["title"], uploader=info["uploader"])
            self.part += 1
            with self.lock:
                self._scan_segments()
                self.seg_clock = max(self.seg_clock, self.samples / SR)
                self.job.setdefault("part_offsets", {})[str(self.part)] = self.seg_clock
            jobs.save(self.job)
            p = f"p{self.part:02d}"
            cmd = ["ffmpeg", "-v", "error", "-reconnect", "1", "-reconnect_streamed", "1",
                   "-reconnect_delay_max", "10"]
            for u in info["urls"]:
                cmd += ["-i", u]
            a_in = "1:a:0" if len(info["urls"]) > 1 else "0:a:0"
            cmd += ["-map", "0:v:0", "-map", a_in, "-c", "copy", "-f", "segment", "-segment_time", str(SEG_SEC),
                    "-reset_timestamps", "1", "-segment_format", "mpegts", "-segment_list", f"{p}.csv",
                    "-segment_list_type", "csv", f"{p}_%05d.ts",
                    "-map", a_in, "-vn", "-af", "aresample=async=1:first_pts=0",
                    "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
            with open(self.dir / "ffmpeg.log", "ab") as errlog:
                if self.stop_flag.is_set():
                    break
                self.proc = subprocess.Popen(cmd, cwd=self.buf, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                                             creationflags=media.NO_WINDOW)
                got = 0
                check_video_at = time.monotonic() + 50
                while True:
                    data = self.proc.stdout.read(SR * 2)  # one second of 16-bit mono
                    if not data:
                        break
                    data = data[: len(data) // 2 * 2]
                    got += len(data)
                    x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    level = float(20 * np.log10(np.sqrt((x ** 2).mean() + 1e-10))) if len(x) else -100.0
                    with self.lock:
                        self._keep_audio(data)
                        self.pending += data
                        self.samples += len(data) // 2
                        self.db.append(level)
                    if time.monotonic() >= check_video_at:
                        check_video_at = time.monotonic() + 15
                        if self.samples / SR - self.seg_clock > 45:
                            self._scan_segments()
                            with self.lock:
                                newest = max((f.stat().st_mtime for f in self.buf.glob(f"{p}_*.ts")), default=0)
                            if self.samples / SR - self.seg_clock > 45 and time.time() - newest > 45:
                                creators.log(self.job["creator"], "The feed continued sending audio after the picture stopped. Reconnecting.")
                                self.proc.terminate()
                                break
                self.proc.wait()
            fails = 0 if got > SR * 2 * 20 else fails + 1
            if fails >= 3:
                break
            self.stop_flag.wait(3)
        self.ended = True

    def _scan_segments(self):
        """Pick up buffer pieces ffmpeg has finished writing."""
        with self.lock:
            for i in range(1, self.part + 1):
                f = self.buf / f"p{i:02d}.csv"
                if not f.exists():
                    continue
                rows = [r.split(",") for r in f.read_text(encoding="utf-8", errors="ignore").splitlines() if r.count(",") >= 2]
                self._index_rows(i, rows[self.csv_seen.get(i, 0):])
                self.csv_seen[i] = len(rows)
            overlap = max(90, int(self.settings["max_len"]))
            keep_from = min(self.seg_clock - self.settings["buffer_min"] * 60, self.scan["through"] - overlap - PAD)
            for clip in self.job["clips"]:
                if clip.get("status") in ("queued", "rendering") and not clip.get("src"):
                    keep_from = min(keep_from, clip["start"] - PAD)
            while self.segments and self.segments[0][2] < keep_from:
                path, a, b = self.segments.pop(0)
                if not any(lo - overlap - PAD < b and a < hi + PAD for lo, hi in self.scan["gaps"]):
                    path.unlink(missing_ok=True)
            for pcm in self.buf.glob("audio-*.pcm"):
                first = int(pcm.stem.split("-")[1])
                if first + PCM_SECONDS < keep_from and not any(lo - overlap < first + PCM_SECONDS and first < hi for lo, hi in self.scan["gaps"]):
                    pcm.unlink(missing_ok=True)

    def _index_rows(self, part, rows):
        # FFmpeg's CSV is relative to the start of this capture, including
        # missing video time. Adding durations erased those gaps.
        origin = self.job.setdefault("part_offsets", {}).setdefault(str(part), self.seg_clock)
        for name, a, b in (r[:3] for r in rows):
            try:
                start, end = origin + float(a), origin + float(b)
            except ValueError:
                continue
            if end > start:
                self.segments.append((self.buf / name, start, end))
                self.seg_clock = max(self.seg_clock, end)

    # ---------------------------------------------------------------- chat (Twitch only)
    def _chat(self, channel):
        while not self.stop_flag.is_set() and not self.ended:
            try:
                raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=20)
                s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
                s.sendall(f"NICK justinfan{int(time.time()) % 90000 + 10000}\r\nJOIN #{channel}\r\n".encode())
                s.settimeout(30)
                rest = b""
                self.chat_on = True
                while not self.stop_flag.is_set() and not self.ended:
                    try:
                        data = s.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    *lines, rest = (rest + data).split(b"\r\n")
                    for line in lines:
                        if line.startswith(b"PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif b" PRIVMSG #" in line:
                            with self.lock:
                                second = int(self.samples / SR)
                                self.chat[second] += 1
                                self.chat_total += 1
                                # how many different people, not how many messages: a wave of the same
                                # emotes is not a crowd getting excited
                                who = _NICK.findall(line.decode("utf-8", "replace"))
                                if who:
                                    self.chat_users.setdefault(second, set()).add(who[0].lower())
                                    self.chat_kept = _trim_names(self.chat_users, second, self.chat_kept)
                s.close()
            except Exception:  # noqa: BLE001
                pass
            self.chat_on = False
            self.stop_flag.wait(5)

    def _chat_spike(self, s, e):
        """True if chat was at least 3x busier than usual during (and just after) these seconds.

        The crowd is counted as people, not messages: ten people saying "GG" is a reaction, one
        person saying "GG" ten times is a bot."""
        elapsed = self.samples / SR
        if not self.chat_total or elapsed < 60:
            return False
        avg = self.chat_total / elapsed
        a, b = int(s), int(e) + 6
        rate = sum(self.chat.get(t, 0) for t in range(a, b + 1)) / (b - a + 1)
        if rate <= max(3 * avg, 1.0):
            return False
        people = self.chat_people()
        if not people:
            return True
        p_avg = sum(len(v) for v in people.values()) / max(1, len(people))
        p_rate = sum(len(people.get(t, ())) for t in range(a, b + 1)) / (b - a + 1)
        return p_rate >= max(2.0, 1.5 * p_avg)

    def chat_people(self):
        """Second -> how many different people were talking, where that is known.

        A copy is taken under the lock: the chat reader thread adds to it as it goes."""
        with self.lock:
            return {t: set(names) for t, names in self.chat_users.items()}

    # ---------------------------------------------------------------- analysis
    def _transcribe_step(self):
        with self.lock:
            n = len(self.pending) // 2
            hurry = self.ended or bool(self.manual)
            if n < (1 if self.ended else SR if hurry else SR * 7):
                return False
            audio = np.frombuffer(bytes(self.pending[: min(n, SR * 24) * 2]), dtype=np.int16)
            base = self.pending_base
        cut = len(audio)
        if not hurry or n > SR * 24:
            # cut at the quietest moment between 4 and 6.5 s, so no word is split; the rest waits for the next pass
            _, cut = next(transcribe._chunks(audio, 4.0, 6.5))
        off = base / SR
        try:
            words = transcribe.transcribe(audio[:cut])
            self.asr_attempts = 0
        except Exception as exc:
            self.asr_attempts += 1
            if self.asr_attempts < 2:
                raise
            self.asr_attempts = 0
            self.job["ai"]["last_error"] = media_safe(exc)
            self.scan["gaps"].append([off, (base + cut) / SR])
            words = []
        for w in words:
            w["s"], w["e"] = round(w["s"] + off, 2), round(w["e"] + off, 2)
        try:
            words = voices.tag(words, audio[:cut], self.voice, offset=off)
        except Exception as exc:
            self.job["ai"]["last_error"] = "Voice matching: " + media_safe(exc)
        with self.lock:
            del self.pending[: cut * 2]
            self.pending_base += cut
            self.words += words
            self.t_done = self.pending_base / SR
        self._triggers(words)
        tmp = self.dir / "words.live.tmp"
        tmp.write_text(json.dumps(self.words), encoding="utf-8")
        tmp.replace(self.dir / "words.json")
        return True

    AUTO_BEFORE, AUTO_AFTER, AUTO_GAP = 30, 20, 60   # seconds of stream sent around a jump; least time between jumps
    JUMP = 0.6                                        # 60% louder or busier than the last three minutes

    def _watch_spikes(self):
        """Always on. Keep a running average of loudness and chat over the last three minutes (leaving out the
        last few seconds), and mark a moment when the last five seconds jump a set percentage above it. Nothing is
        sent yet: the AI gets that moment once its transcript is ready, with the half minute before it."""
        with self.lock:
            n = len(self.db)
            if n < 60:
                self.base = {"warming": True}
                return
            db = np.array(self.db[max(0, n - 190):], dtype=np.float32)
            people = self.chat_people()
            if people:
                chat = [len(people.get(t, ())) for t in range(max(0, n - 190), n)]
            else:
                chat = [self.chat.get(t, 0) for t in range(max(0, n - 190), n)] if (self.chat_on or self.chat_total) else None
        amp = 10 ** (db / 20)                       # loudness as plain level, so a percentage means something
        base, cur = float(amp[:-10].mean()), float(amp[-5:].mean())
        loud_jump = cur / max(base, 1e-6) - 1
        pct = self.JUMP
        why = []
        if loud_jump >= pct:
            why.append(f"loudness +{round(loud_jump * 100)}%")
        chat_base = chat_now = None
        if chat is not None:
            chat_base = sum(chat[:-10]) / max(1, len(chat) - 10)
            chat_now = sum(chat[-8:]) / 8          # chat is bursty, so look at a slightly longer stretch
            if chat_now >= max(1.5, chat_base * (1 + pct)):
                why.append(f"chat +{round((chat_now / max(chat_base, 0.01) - 1) * 100)}%")
        self.base = {"loud_db": round(20 * np.log10(max(base, 1e-6)), 1), "loud_now": round(loud_jump * 100),
                     "chat": round(chat_base, 2) if chat_base is not None else None,
                     "chat_now": round(chat_now, 2) if chat_now is not None else None, "pct": round(pct * 100)}
        if why and not self.spike and n - self.last_spike >= self.AUTO_GAP:
            self.spike = {"t": float(n - 3), "why": ", ".join(why)}
            self.last_spike = n
            self.spikes += 1

    def _ai_check(self, window=None, why=None):
        """Judge 1 reads only the words. If it finds a moment, judge 2 reads only the loudness and chat activity
        for the same stretch, without seeing the words. A clip is made when both say yes (or as set in Setup)."""
        st = self.settings
        every = int(st.get("ai_every_min", 3)) or 3
        if window:                    # just the stretch around a jump, or around someone saying "clip"
            ws, now = max(0.0, window[0]), window[1]
        else:
            now = self.t_done
            ws = max(0.0, now - max(every, 3) * 60 - 90)  # each check overlaps the last by 90 s, so nothing falls between
        win = [w for w in self.words if ws <= w["s"] <= now]
        last_clip = max((c["end"] for c in self.job["clips"] if c["end"] <= now), default=0)
        must = st["must_clip_min"] > 0 and now - last_clip >= st["must_clip_min"] * 60
        verdict = {"t": round(now), "transcript": None, "reaction": None, "trigger": why}
        usage.set_context(self.job["creator"], self.job["title"] or "Live stream")
        title, up = self.job["title"], self.job["uploader"]
        try:
            cands, tok = brain.judge_transcript(win, ws, now, title, up, st["min_len"], st["max_len"], self.job["clips"],
                                                must=must, game=self._game(), timeout=config.LIVE_AI_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            self.job["ai"]["last_error"] = str(e)[-200:]
            self._verdict(verdict, "Words judge unavailable")
            return False
        self.checks += int(bool(win))
        self.tokens += tok
        keep = [c for c in cands if c["score"] >= (40 if must else st["min_score"])
                and (self.voice is None or voices.heard_creator(win, c["start"], c["end"]))]
        verdict["transcript"] = max((c["score"] for c in cands), default=0)
        visual = editorial.enabled()
        if win and not keep and not visual:
            return self._verdict(verdict, "Words judge: nothing clip-worthy")
        z = brain.loud_zscore(np.array(self.db, dtype=np.float32))
        lo, hi = int(ws), int(now) + 1
        people = self.chat_people()
        if people and lo >= max(0, int(self.samples / SR) - CHAT_KEEP):
            chat = [len(people.get(t, ())) for t in range(lo, hi)]
            unit = "people"
        else:
            chat = [self.chat.get(t, 0) for t in range(lo, hi)] if (self.chat_on or self.chat_total) else None
            unit = "messages"
        try:
            sig, tok2 = brain.judge_signals(z[lo:hi], chat, lo, now, unit=unit, timeout=config.LIVE_AI_TIMEOUT)
            self.checks += 1
            self.tokens += tok2
        except Exception as e:  # noqa: BLE001
            self.job["ai"]["last_error"] = str(e)[-200:]
            self._verdict(verdict, "Reaction judge unavailable")
            return False
        if visual:
            try:
                with self.lock:
                    segments = [s for s in self.segments if s[1] < now and s[2] > ws]
                peaks = [m["start"] for m in sig if m["score"] >= 50]
                if not segments:
                    raise ValueError("This interval has no saved video.")
                first, last = max(ws, segments[0][1]), min(now, segments[-1][2])
                seen, spent = editorial.scout(segments, first, last, win, peaks, st["min_len"], st["max_len"],
                                              self._game(), verify_speaker=st["layout"] == "stream")
                self.tokens += spent
                keep.extend(c for c in seen if c["score"] >= st["min_score"])
                keep.sort(key=lambda c: (-c["score"], not c.get("visual_evidence")))
                verdict["visual"] = max((c["score"] for c in seen), default=0)
                if first > ws + 1 or last < now - 1:
                    self.job["ai"]["last_error"] = "Only part of this window has saved video."
                    visual = False
            except Exception as error:
                self.job["ai"]["last_error"] = "Visual review: " + str(error)[-160:]
                self._verdict(verdict, "Visual review incomplete")
                # Retain valid speech finds even when a picture or the scorer is unavailable.
                visual = False
        if not win and not keep:
            verdict["reaction"] = max((m["score"] for m in sig), default=0)
            self._verdict(verdict, "No standalone moment found")
            return visual or not editorial.enabled()
        added = 0
        for c in keep:
            match = [m for m in sig if m["end"] >= c["start"] - 10 and m["start"] <= c["end"] + 10 and m["score"] >= 50]
            rs = max((m["score"] for m in match), default=0)
            verdict["reaction"] = rs
            both = bool(match)
            ok = c.get("visual_evidence") or both or (st["decide_mode"] == "either" and c["score"] >= 90) or (must and added == 0)
            if not ok:
                self.rejected += 1
                self._verdict(verdict, "Reaction judge said no, not clipped")
                continue
            # Apply the rolling cap on the stream clock, including when checking a backlog after capture.
            limit = st["live_per_hour"]
            if limit and not must:
                nearby = sorted([c["start"]] + [k["start"] for k in self.job["clips"]
                                if k.get("score", 0) > 0 and abs(k["start"] - c["start"]) < 3600])
                if any(b - a < 3600 for a, b in zip(nearby, nearby[limit:])):
                    self._verdict(verdict, "Reviewed; hourly clip limit reached")
                    continue
            if brain._overlaps(c["start"], c["end"], self.job["clips"]):
                self._verdict(verdict, "This moment already has a clip")
                continue
            if st["min_gap_min"] and any(abs(c["start"] - k["start"]) < st["min_gap_min"] * 60 for k in self.job["clips"]):
                self._verdict(verdict, "Too close to the last clip")
                continue
            why = "Both judges agreed" if both else ("Words judge was very sure" if c["score"] >= 90 and
                                                     st["decide_mode"] == "either" else "Forced: no clip for a while")
            if c.get("visual_evidence"):
                why = "Visible setup and payoff"
            c["judges"] = {"words": c["score"], "reaction": rs or None, "why": why}
            c["reason"] = f"{c['reason']} ({why.lower()}.)"
            self._add_clip(c)
            self._verdict(verdict, "Clipped: " + why.lower())
            added += 1
            must = False
        return visual or not editorial.enabled()

    def _verdict(self, v, result):
        self.verdicts.append(dict(v, result=result))
        del self.verdicts[:-6]

    CLIP_WORDS = re.compile(r"\bclip(\s+(it|that|this))?\b", re.I)
    SAID_BEFORE, SAID_AFTER, SAID_GAP = 60, 15, 45

    def _triggers(self, new_words):
        """Built in: when someone says "clip" ("clip it", "clip that"), the AI looks at that moment as soon as its
        transcript is ready; people say it right after something worth clipping. Then the creator's own trigger
        phrases from Setup, which clip straight away with no AI check."""
        for w in new_words or []:
            if self.voice is not None and w.get("speaker") != "creator":
                continue
            if self.CLIP_WORDS.search(re.sub(r"[^a-z' ]", "", w["w"].lower())) and w["s"] - self.last_said > self.SAID_GAP \
                    and not self.said:
                self.said = {"t": w["s"], "why": 'Someone said "clip"'}
                self.last_said = w["s"]
        phrases = [p.strip().lower() for p in self.settings.get("trigger_words", "").split(",") if p.strip()]
        if not phrases or not new_words:
            return
        key = [re.sub(r"[^a-z0-9']", "", w["w"].lower()) for w in new_words]
        for p in phrases:
            parts = [re.sub(r"[^a-z0-9']", "", x) for x in p.split()]
            for i in range(len(key) - len(parts) + 1):
                if key[i:i + len(parts)] == parts and (self.voice is None or
                        all(w.get("speaker") == "creator" for w in new_words[i:i + len(parts)])):
                    t = new_words[i]["s"]
                    if t - self.last_trigger > 45:
                        self.last_trigger = t
                        self.manual.append({"start": max(0.0, t - 30), "end": t + 2, "why": f'Someone said "{p}"'})

    def _manual_step(self):
        for req in [m for m in self.manual if m["end"] <= self.t_done or self.ended]:
            start, end = round(req["start"], 2), round(min(req["end"], self.samples / SR), 2)
            if not config.long_enough(start, end):
                self.manual.remove(req)
                continue
            text = " ".join(w["w"] for w in self.words if req["start"] <= w["s"] <= req["end"])
            usage.set_context(self.job["creator"], self.job["title"] or "Live stream")
            try:
                c = brain.describe(text, self.job["title"], self.job["uploader"], game=self._game(),
                                   timeout=config.LIVE_AI_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                # Someone asked for this clip by hand. A slow or broken AI costs it its hook, not the clip.
                self.job["ai"]["last_error"] = str(e)[-200:]
                c = {"score": 0, "category": "other", "hook": "", "title": self.job["title"],
                     "description": "", "hashtags": [], "highlight_words": []}
            self.manual.remove(req)
            c.update(start=start, end=end, score=0)
            if req.get("why"):
                c["reason"] = req["why"]
            self._add_clip(c)

    def _add_clip(self, c):
        self.n += 1
        c.update(id=f"clip{self.n:03d}", style=self.settings["style"], layout=self.settings["layout"],
                 captions=self.job["options"].get("captions", True), status="queued", version=0, made_at=time.time())
        with self.lock:
            self.job["clips"].append(c)
        jobs.save(self.job)
        jobs.submit_live(lambda: self._render(c))

    def _render(self, clip):
        """Runs on the render worker: copy the needed buffer pieces, then make the clip."""
        deadline = time.time() + 45
        while time.time() < deadline:
            self._scan_segments()
            if self.ended or (self.segments and self.segments[-1][2] >= clip["end"] + 1):
                break
            time.sleep(1)
        with self.lock:
            segs = _cut_segments(self.segments, clip["start"], clip["end"])
        if not segs:
            clip.update(status="error", error="Some video for this moment is missing from the recording.")
            jobs.save(self.job)
            return
        if "width" not in self.job:
            meta = media.probe(segs[0][0])
            self.job.update(width=meta["width"], height=meta["height"])
        lst = self.dir / "clips" / f"{clip['id']}.txt"
        lst.write_text("".join(f"file '{s[0].as_posix()}'\n" for s in segs), encoding="utf-8")
        src = self.dir / "clips" / f"{clip['id']}.src.ts"
        try:
            media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                       "-c", "copy", str(src)])
        except Exception as e:  # noqa: BLE001
            clip.update(status="error", error=media_safe(e))
            jobs.save(self.job)
            return
        finally:
            lst.unlink(missing_ok=True)
        if clip.get("layout") == "gameplay":
            if not self.facecam_checked:
                self.facecam_box = self._find_facecam(src)
                self.facecam_checked = True
            if self.facecam_box:
                clip["facecam"] = self.facecam_box
        dur = segs[-1][2] - segs[0][1]
        offset = segs[0][1]
        if not self.job.get("capture_timestamps"):
            offset = self._align(src, offset + self.clock_fix, dur)
            self.clock_fix = offset - segs[0][1]
        clip.update(src=str(src), src_offset=round(offset, 2), src_end=round(offset + dur, 2))
        clip["start"] = max(clip["start"], clip["src_offset"])
        clip["end"] = min(clip["end"], clip["src_end"])
        jobs.render_one(self.job, clip, list(self.words))

    def _align(self, src, guess, dur):
        """Where `src` really starts on the transcript clock. The video pieces and the audio feed keep separate
        clocks that drift apart, so transcribe the clip's own audio and line its words up with the live words.
        Local speech model only: no AI calls."""
        key = lambda w: re.sub(r"[^a-z0-9]", "", w.lower())
        try:
            wav = src.with_suffix(".wav")
            media.extract_audio(src, wav)
            heard = transcribe.transcribe(media.load_audio(wav))
            wav.unlink(missing_ok=True)
            with self.lock:
                near = [w for w in self.words if guess - 15 <= w["s"] <= guess + dur + 15]
            diffs = []
            for h in heard:
                k = key(h["w"])
                if len(k) < 3:
                    continue
                m = [w["s"] for w in near if key(w["w"]) == k and abs(w["s"] - (guess + h["s"])) < 8]
                if m:
                    diffs.append(min(m, key=lambda s: abs(s - (guess + h["s"]))) - h["s"])
            if len(diffs) >= 4:
                return float(np.median(diffs))
        except Exception:  # noqa: BLE001 - fall back to the piece clock
            pass
        return guess

    def clip_last(self, seconds):
        end = self.samples / SR
        if end < config.MIN_CLIP_SECONDS:
            raise ValueError(f"Wait until at least {config.MIN_CLIP_SECONDS} seconds are recorded.")
        self.manual.append({"start": max(0.0, end - max(config.MIN_CLIP_SECONDS, seconds)), "end": end})

    def _publish(self):
        elapsed = self.samples / SR
        lines = brain.make_lines([w for w in self.words if w["s"] >= self.t_done - 45])[-5:]
        with self.lock:
            sec = int(elapsed)
            self.job.update(
                duration=elapsed,
                live={"elapsed": round(elapsed), "buffer_sec": round(self.segments[-1][2] - self.segments[0][1]) if self.segments else 0,
                      "lines": [l["text"] for l in lines], "level": [round(x) for x in self.db[-60:]],
                      "chat": [self.chat.get(t, 0) for t in range(max(0, sec - 60), sec)] if self.chat_on or self.chat_total else None,
                      "checks": self.checks, "skipped": self.skipped, "tokens": self.tokens, "words": len(self.words),
                      "rejected": self.rejected, "verdicts": list(reversed(self.verdicts)),
                      "every_min": int(self.settings.get("ai_every_min", 3)) or 3,
                      "auto": None,
                      "pending_clips": len(self.manual)})
            if self.chat_total and int(elapsed) // 15 != getattr(self, "_chat_saved_at", -1):
                self._chat_saved_at = int(elapsed) // 15
                start = int(max(0, self.scan["through"] - max(90, self.settings["max_len"])))
                counts = {t: n for t, n in self.chat.items() if t >= start}
                tmp = self.buf / "chat.tmp"
                tmp.write_text(json.dumps(counts), encoding="utf-8")
                tmp.replace(self.buf / "chat.json")
        jobs.save(self.job)

    def _analyze(self):
        cid = self.job["creator"]
        started = False
        while True:
            busy = False
            try:
                if not self.user_stopped and not self.ended:      # a recording cannot run for ever
                    if time.time() - self.started_at > config.MAX_LIVE_HOURS * 3600:
                        self.stop(f"Stopped after {config.MAX_LIVE_HOURS:g} hours, the longest one recording can run")
                self._scan_segments()
                busy = self._transcribe_step()
                if not started and self.samples:
                    started = True
                    if not self.user_stopped and not self.ended:
                        self.job.update(status="live", stage="Recording")
                    creators.log(cid, f"Recording started: {self.job['title']}")
                self._manual_step()
                # Three ways the AI is asked: on the creator's schedule (every N minutes), and always, when the
                # stream suddenly gets louder or chat busier, or when someone says "clip".
                self._watch_spikes()
                if self.ended and self.job.get("status") == "live":
                    self.job.update(status="running", stage="Finishing the recorded stream")
                self._schedule_review(final=self.ended and not busy)
                self._publish()
            except Exception as e:  # noqa: BLE001
                self.job["ai"]["last_error"] = str(e)[-200:]
            # Leaving here must not happen while an AI check is still running: it may still be adding a
            # clip, and cleanup() deletes the buffer that clip is cut from. That is true after a stop as
            # well as at the end of a stream, so both wait for it. Stopping is still instant on the page:
            # stop() has already written the status.
            if (self.ended and not busy and not self.ai_busy and not self.pending and not self.manual
                    and self.scan["through"] >= self.t_done and not self.spike and not self.said):
                break
            if not busy:
                time.sleep(2)
        failed = bool(self.job.get("error")) and not started
        self.job.pop("recover", None)
        self.job.update(status="error" if failed else "done", stage="Could not record" if failed else self.end_note)
        if self.scan["gaps"]:
            self.job["ai"]["note"] = "Highlight review was incomplete. The affected stream footage was kept."
        jobs.save(self.job)
        creators.log(cid, f"Recording finished ({self.end_note.lower()}): {len(self.job['clips'])} clips, "
                          f"{round(self.samples / SR / 60)} min" if not failed else f"Recording failed: {self.job['error']}")

        if failed:
            alerts.notify(f"recording failed for {self.job['source']['name']}: {self.job['error']}")
        else:
            alerts.notify(f"recording finished for {self.job['source']['name']}: {len(self.job['clips'])} clips, "
                          f"{round(self.samples / SR / 60)} min.")

        def cleanup():  # runs after any queued renders, then frees the buffer
            if not self.scan["gaps"]:
                for f in self.buf.glob("*"):
                    f.unlink(missing_ok=True)
            self.finished = True
            SESSIONS.pop(self.job["id"], None)

        jobs.submit_live(cleanup)


def _cut_segments(segments, start, end):
    """Keep padding from one continuous stretch of saved video."""
    groups = []
    for seg in segments:
        if seg[2] <= start - PAD or seg[1] >= end + PAD or not seg[0].exists():
            continue
        if not groups or seg[1] - groups[-1][-1][2] > 0.5:
            groups.append([])
        groups[-1].append(seg)
    return next((g for g in groups if g[0][1] <= start + .25 and g[-1][2] >= end - .25), [])


def recover(job):
    """After a restart: finish the clips of an interrupted recording from the stream pieces still on disk."""
    c = creators.get(job["creator"]) or dict(creators.DEFAULTS)
    resume_scan = bool(job.get("scan") and job.get("status") == "running")
    s = Session(job, c)
    s.ended, s.end_note = True, "Recording stopped when the studio closed"
    s.part = max((int(m[1]) for f in s.buf.glob("p*") if (m := re.match(r"p(\d+)[_.]", f.name))), default=0)
    s.seg_clock = 0.0
    s.csv_seen = {}
    with s.lock:
        for i in range(1, s.part + 1):
            f = s.buf / f"p{i:02d}.csv"
            s.seg_clock = max(s.seg_clock, job.get("part_offsets", {}).get(str(i), 0))
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines() if f.exists() else []
            rows = [r.split(",") for r in lines if r.count(",") >= 2]
            s._index_rows(i, rows)
            s.csv_seen[i] = len(rows)
            # A forced shutdown can leave a readable final video segment that ffmpeg never indexed.
            indexed = {row[0] for row in rows}
            for piece in sorted(s.buf.glob(f"p{i:02d}_*.ts")):
                if piece.name in indexed:
                    continue
                try:
                    length = media.probe(piece)["duration"]
                except (OSError, RuntimeError, ValueError):
                    continue
                s.segments.append((piece, s.seg_clock, s.seg_clock + length))
                s.seg_clock += length
    try:
        s.words = json.loads((s.dir / "words.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        s.words = []
    s.n = max((int(k["id"][4:]) for k in job["clips"] if k["id"][4:].isdigit()), default=0)
    todo = [k for k in job["clips"] if k.get("status") == "queued"]
    if resume_scan:
        s.user_stopped = True  # this session only drains disk; the channel can reconnect separately
        SESSIONS[job["id"]] = s
        def finish_scan():
            try:
                s._recover_audio()
                for k in todo:
                    jobs.submit_live(lambda k=k: s._render(k))
                s._analyze()
            except Exception as exc:
                job.update(status="error", stage="Recorded stream review interrupted", error=media_safe(exc))
                jobs.save(job)
                SESSIONS.pop(job["id"], None)
        threading.Thread(target=ctx.bind(finish_scan), daemon=True, name="recover_review").start()
        return
    for k in todo:
        jobs.submit_live(lambda k=k: s._render(k))

    def cleanup():
        for f in s.buf.glob("*"):
            f.unlink(missing_ok=True)
        job.pop("recover", None)
        jobs.save(job)
        creators.log(job["creator"], f"Finished {len(todo)} clip(s) from a recording interrupted by a restart")

    jobs.submit_live(cleanup)


def start_session(creator: dict, url=None) -> dict:
    if _buffer_paused:
        raise ValueError("The recording buffer is full. PeakCut is finishing existing recordings.")
    url = normalize(url or creator.get("channel") or "")
    if not re.match(r"^https?://[^/]+\.[^/]+/", url + "/"):
        raise ValueError("Add this creator's channel link first.")
    if active_for(creator["id"]):
        raise ValueError("This creator is already being recorded.")
    if not media.site_allowed(url):
        raise ValueError("Channel links must be from YouTube, Twitch or Kick.")
    if media.is_youtube(url) and media.youtube_paused():
        raise ValueError(media.YT_BLOCKED.replace("download videos", "record YouTube streams")
                         .replace("Upload the video file instead, or use a Twitch or Kick link. YouTube links",
                                  "Twitch and Kick streams still work. YouTube"))
    if len(active_mine()) >= config.MAX_LIVE_PER_USER:
        raise ValueError(f"You're already recording {len(active_mine())} streams, the most this server allows at once. Stop one first.")
    if len(active()) >= config.MAX_LIVE_TOTAL or (active() and load.busy()):
        raise ValueError("PeakCut is recording as many streams as it can right now. Try again in a little while.")
    from . import tools
    if tools.missing():
        raise ValueError(tools.HELP)
    job = jobs.create({"type": "live", "url": url, "name": f"{creator['name']} live"}, {}, enqueue=False,
                      creator=creator["id"], kind="live")
    job.update(status="live", stage="Connecting to the stream", capture_timestamps=True)
    jobs.save(job)
    Session(job, creator).start()
    return job


def _watch_user(uid):
    """Start recording any of this user's auto-record creators who have gone live."""
    from . import profiles
    with ctx.as_user(uid):
        try:
            todo = [c for c in creators.all_creators() if c["auto_watch"] and c["channel"]]
            if not todo:
                return
        except Exception:  # noqa: BLE001
            return
        for c in todo:
            _watch_one(c)


def check_now(cid):
    """Auto-record was just turned on: look now, on a thread, instead of waiting for the next round."""
    uid = ctx.uid()

    def run():
        from . import profiles
        with ctx.as_user(uid):
            c = creators.get(cid)
            if not c or not c["auto_watch"] or not c["channel"]:
                return
            for url in c.get("channels", {}).values():
                profiles._live.pop(url.rstrip("/").lower(), None)     # a fresh answer, not one from two minutes ago
            _watch_one(c)
            profiles.refresh(cid, force=True)
    threading.Thread(target=run, daemon=True).start()


def _watch_one(c):
    """Start recording this auto-record creator if they are live on any of their channels."""
    from . import profiles
    try:
        if _buffer_paused or active_for(c["id"]) or len(active_mine()) >= config.MAX_LIVE_PER_USER or len(active()) >= config.MAX_LIVE_TOTAL or (active() and load.busy()):
            return
        # the chosen channel first, then their other platforms
        urls = [c["channel"]] + [u for u in c.get("channels", {}).values() if u != c["channel"]]
        for url in urls:
            # one shared "are they live?" answer per channel, however many people follow it
            if not profiles.live_shared(profiles.platform_of(url), url).get("live"):
                continue
            if media.is_youtube(url):
                # check YouTube will hand over the stream before making a recording that could only fail
                if media.youtube_paused():
                    continue
                try:
                    resolve(normalize(url))
                except NotLive:
                    continue
            start_session(c, url)
            where = creators.platform_key(url).title()
            creators.log(c["id"], f"Went live on {where}: recording started automatically")
            alerts.notify(f"{c['name']} went live on {where}, recording started.")
            break
    except NotLive:
        pass
    except Exception as e:  # noqa: BLE001
        creators.log(c["id"], f"Auto-record check failed: {str(e)[-150:]}")


def watch_loop():
    """Every two minutes, start recording any creator set to auto-record who has gone live. Covers every user,
    several at a time, so the round stays short however many accounts there are."""
    from concurrent.futures import ThreadPoolExecutor
    time.sleep(20)
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="watch")
    while True:
        t0 = time.time()
        try:
            list(pool.map(_watch_user, auth.user_ids()))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(max(15, 120 - (time.time() - t0)))


def _resume_recordings(paused):
    """Recordings that were running when the server stopped: start recording the same channel again, if the
    stream is still on. Tried for a few minutes, since the server may come back a little before the network."""
    time.sleep(10)
    for _ in range(6):
        left = []
        for uid, cid, url in paused:
            with ctx.as_user(uid):
                try:
                    c = creators.get(cid)
                    if not c or active_for(cid):
                        continue
                    resolve(normalize(url))      # raises NotLive if the stream has ended: no empty recording is made
                    start_session(c, url)
                    creators.log(cid, "Recording picked up again after a server update")
                except NotLive:
                    continue                  # the stream ended while the server was away
                except Exception:  # noqa: BLE001 - try again shortly
                    left.append((uid, cid, url))
        if not left:
            return
        paused = left
        time.sleep(30)


def start():
    paused = []
    for j in jobs.all_jobs():
        if j.get("recover"):
            with ctx.as_user(j["user"]):
                recover(j)
        if j.get("was_live") and j.get("creator"):
            paused.append((j["user"], j["creator"], j["source"]["url"]))
            j.pop("was_live", None)
            jobs.save(j)
    if BUFFER_LIMIT_BYTES:
        threading.Thread(target=_buffer_budget_loop, daemon=True, name="live-buffer-budget").start()
    if paused:
        threading.Thread(target=_resume_recordings, args=(paused,), daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
