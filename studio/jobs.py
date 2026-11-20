"""Job pipeline: get video -> audio -> transcript -> AI picks -> rendered clips. One heavy task at a time."""
import collections
import json
import os
import math
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from . import language

from . import brain, config, creators, ctx, editorial, facecam, framing, games, load, media, quality, render, replay_chat, roman, safe, transcribe, usage, voices

class _FairQueue:
    """Whose video is next. Each person has their own line and the lines take turns, so someone who adds ten
    videos does not make everyone else wait behind all ten. Small tasks (re-making one clip) go first."""

    def __init__(self):
        self.cv = threading.Condition()
        self.quick = collections.deque()
        self.lines = {}                       # user -> their waiting videos, oldest first
        self.turn = collections.deque()       # users with something waiting, in the order they will be served
        self.epoch = 0                        # goes up whenever the line moves
        self._pos = (0.0, {})
        self.running = 0                      # videos being worked on right now
        self.last_start = 0.0
        self.held = False                     # videos are waiting only because the machine is near full

    def _room(self) -> bool:
        """May one more video start? One always may. More only while the machine has room, and a few seconds
        after the last one started, so its weight is already showing in the measurements."""
        if self.running == 0:
            return True
        return self.running < config.JOB_WORKERS and time.time() - self.last_start > 6 and not load.busy()

    def done(self):
        with self.cv:
            self.running = max(0, self.running - 1)
            self.cv.notify_all()

    def put(self, item, user=None):
        with self.cv:
            if user is None:
                self.quick.append(item)
            else:
                if user not in self.lines:
                    self.lines[user] = collections.deque()
                    self.turn.append(user)
                self.lines[user].append(item)
            self.epoch += 1
            self.cv.notify()

    def get(self, quick_only=False):
        with self.cv:
            while True:
                if self.quick:
                    self.epoch += 1
                    return self.quick.popleft()
                if not quick_only and self.turn:
                    if self._room():
                        break
                    if not self.held:
                        self.held = True
                        self.epoch += 1
                elif self.held:
                    self.held = False
                    self.epoch += 1
                self.cv.wait(1.0)             # looked at again every second: the line moves the moment there is room
            self.epoch += 1
            self.held = False
            self.running += 1
            self.last_start = time.time()
            user = self.turn.popleft()
            item = self.lines[user].popleft()
            if self.lines[user]:
                self.turn.append(user)
            else:
                del self.lines[user]
            return item

    def ahead(self) -> dict:
        """job id -> how many videos will start before it. Worked out at most once a second."""
        with self.cv:
            if time.time() - self._pos[0] < 1:
                return self._pos[1]
            out, lines, order, n = {}, {u: list(q) for u, q in self.lines.items()}, list(self.turn), 0
            depth = 0
            while order:
                nxt = []
                for u in order:
                    if depth < len(lines[u]):
                        out[lines[u][depth][1]] = n
                        n += 1
                        if depth + 1 < len(lines[u]):
                            nxt.append(u)
                order, depth = nxt, depth + 1
            self._pos = (time.time(), out)
            return out


_q = _FairQueue()
_live_q: "queue.Queue" = queue.Queue()   # live clips never wait behind a long video
_jobs: dict = {}
_by_user: dict = {}                       # user -> {job id: job}: listing one person's videos never scans everyone's
_lock = threading.RLock()
on_clip_ready: list = []   # callbacks (job, clip), run once when a clip first finishes
MAX_FINAL_REVISIONS = 2

DEFAULTS = {"max_clips": 0, "min_len": config.MIN_CLIP_SECONDS, "max_len": 60, "style": "bounce", "layout": "stream", "quality": 720,
            "range_start": None, "range_end": None, "captions": True}


LIMITS = {"max_clips": (0, 20), "min_len": (config.MIN_CLIP_SECONDS, 120),
          "max_len": (config.MIN_CLIP_SECONDS, 180), "quality": (360, 1080)}


def clean_options(options) -> dict:
    """Only known settings, each inside its allowed range. Raises ValueError on nonsense."""
    out = {}
    for k, v in (options if isinstance(options, dict) else {}).items():
        if k == "captions":
            if not isinstance(v, bool):
                raise ValueError("Captions must be on or off.")
            out[k] = v
            continue
        if k not in DEFAULTS or v in ("", None):
            continue
        if k in LIMITS:
            lo, hi = LIMITS[k]
            v = max(lo, min(hi, int(float(v))))
        elif k == "style":
            v = v if v in render.STYLES else DEFAULTS["style"]
        elif k == "layout":
            v = v if v in render.LAYOUTS else DEFAULTS["layout"]
        else:                                    # range_start, range_end: seconds into the video
            v = float(v)
            if not math.isfinite(v) or v < 0:
                raise ValueError("That part of the video isn't valid.")
        out[k] = v
    if out.get("range_end") is not None and out["range_end"] <= out.get("range_start", 0):
        raise ValueError("The end of the chosen part must be after its start.")
    return out


def job_dir(jid) -> Path:
    return Path(_jobs[jid]["dir"])


HIDDEN = ("dir", "user", "source_path", "recover", "scan_candidates", "part_offsets")


def public(job) -> dict:
    """What the browser may see: no server paths, no owner id."""
    out = {k: v for k, v in job.items() if k not in HIDDEN}
    out["source"] = {k: v for k, v in job["source"].items() if k != "path"}
    out["clips"] = [{k: v for k, v in c.items() if k not in ("src", "fixed_words", "fixed_for", "review_speech", "revision_progress")} for c in job["clips"]]
    ai = job.get("ai") or {}
    out["ai"] = {"tokens": ai.get("tokens", 0), "note": ai.get("note", ""), "used": bool(ai.get("model"))}   # never the model, never raw errors
    if job.get("status") == "queued":
        n = _q.ahead().get(job["id"])
        if n:
            out["stage"] = (f"High demand right now: {n} video{'s' if n != 1 else ''} ahead of yours" if _q.held else
                            f"Waiting in line: {n} video{'s' if n != 1 else ''} ahead of yours")
        elif n == 0 and _q.held:
            out["stage"] = "High demand right now: yours is next"
    return out


def save(job):
    ctx.bump(job.get("user"))
    with _lock:
        d = Path(job["dir"])
        for _ in range(5):
            try:
                text = json.dumps(job, indent=1)
                break
            except RuntimeError:  # changed mid-dump by another thread
                time.sleep(0.05)
        else:
            return
        tmp = d / "job.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(d / "job.json")


def _set(job, **kw):
    with _lock:
        job.update(kw)
    save(job)


def _remap(path, folder: Path):
    """Paths saved before data moved into per-user folders (or on another machine): find the file by name."""
    if path and not Path(path).exists():
        cand = folder / Path(str(path).replace("\\", "/")).name
        if cand.exists():
            return str(cand)
    return path


def _load_file(f: Path, uid: str):
    try:
        job = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    job.update(dir=str(f.parent), user=uid)
    job.setdefault("kind", "video")
    job.setdefault("creator", None)
    job.setdefault("options", {}).setdefault("captions", True)
    if job.get("source_path"):
        job["source_path"] = _remap(job["source_path"], f.parent)
    if job.get("source", {}).get("path"):
        job["source"]["path"] = _remap(job["source"]["path"], f.parent)
    if job.get("status") in ("queued", "running") and job.get("kind") != "live":
        # Picked up again as soon as the server is back (jobs.start), from the last finished part of the transcript.
        job.update(status="queued", stage="Picking up where it stopped", error=None, resume=True)
    interrupted_live = job.get("kind") == "live" and job.get("status") in ("running", "live")
    if job.get("status") == "live":
        job.update(status="done", stage="Recording paused by a server update", was_live=True)
    if interrupted_live and job.get("scan"):
        job.update(status="running", stage="Finishing the recorded stream", recover=True)
    buffered = job.get("kind") == "live" and any((f.parent / "buffer").glob("*.ts"))
    for c in job.get("clips", []):
        c.setdefault("captions", job["options"]["captions"])
        if c.get("src"):
            c["src"] = _remap(c["src"], f.parent / "clips")
        if c.get("status") in ("rendering", "queued"):
            if buffered and not c.get("src"):
                c["status"] = "queued"  # the stream is still on disk: live.recover() finishes these
                job["recover"] = True
            elif job.get("resume"):
                c["status"] = "queued"  # the video is being picked up again: this clip is made then
            elif job.get("kind") != "live" and (f.parent / "words.json").is_file():
                c["status"] = "queued"  # a single clip being re-made: jobs.start() makes it again
                job.setdefault("remake", []).append(c["id"])
            else:
                c.update(status="error", error="Interrupted by a restart. Open it and press Save and re-make.")
    _jobs[job["id"]] = job
    _by_user.setdefault(uid, {})[job["id"]] = job


def load_user(uid: str):
    d = config.USERS_DIR / uid
    for f in list((d / "jobs").glob("*/job.json")) + list((d / "creators").glob("*/*/*/job.json")):
        _load_file(f, uid)


def load_all():
    for d in config.USERS_DIR.iterdir():
        if d.is_dir():
            load_user(d.name)


def all_jobs() -> list:
    """Every user's jobs. Only for background work; never send this to a browser."""
    with _lock:
        return list(_jobs.values())


def list_jobs():
    u = ctx.uid()
    with _lock:
        return sorted((dict(j) for j in _by_user.get(u, {}).values()), key=lambda j: -j["created"])


def get(jid):
    """The job, only if it belongs to whoever this code is running for."""
    job = _jobs.get(jid)
    return job if job and job["user"] == ctx.uid() else None


def busy_count() -> int:
    u = ctx.uid()
    return sum(1 for j in list(_by_user.get(u, {}).values()) if j["kind"] == "video" and j["status"] in ("queued", "running"))


def waiting_total() -> int:
    return sum(1 for j in list(_jobs.values()) if j["kind"] == "video" and j["status"] == "queued")


def queue_epoch() -> int:
    return _q.epoch


def running_now() -> int:
    return _q.running


def create(source: dict, options: dict, enqueue=True, creator=None, kind="video") -> dict:
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
    c = creators.get(creator) if creator else None
    if c:
        d = creators.root(c["id"]) / ("live" if kind == "live" else "videos") / jid
        base = {**DEFAULTS, **{k: c[k] for k in ("max_clips", "min_len", "max_len", "style", "layout", "captions")}}
    else:
        d, base, creator = config.user_dir(ctx.uid()) / "jobs" / jid, dict(DEFAULTS), None
    opts = {**base, **clean_options(options)}
    if kind == "video":
        # Older open dashboard tabs can still send a numeric clip count.
        opts.update(range_start=None, range_end=None, max_clips=0)
    opts["min_len"] = min(opts["min_len"], opts["max_len"])
    job = {"id": jid, "user": ctx.uid(), "kind": kind, "creator": creator, "dir": str(d), "created": time.time(), "source": source,
           "options": opts, "status": "queued", "stage": "Waiting", "progress": 0.0,
           "title": source.get("name", ""), "uploader": "", "duration": 0, "clips": [], "error": None, "ai": {}}
    d.mkdir(parents=True)
    with _lock:
        _jobs[jid] = job
        _by_user.setdefault(job["user"], {})[jid] = job
    save(job)
    if enqueue:
        _q.put(("job", jid, None), user=job["user"])
    return job


def enqueue(job):
    save(job)
    _q.put(("job", job["id"], None), user=job["user"])


def submit(fn):
    """Run any heavy task (a render, a cleanup) on the single worker, in order."""
    _q.put(("fn", ctx.bind(fn), None))


def submit_live(fn):
    """Live-stream renders and cleanups: their own worker, so a long video can never hold them up."""
    _live_q.put(ctx.bind(fn))


def _live_worker():
    while True:
        fn = _live_q.get()
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def delete(jid):
    with _lock:
        job = _jobs.pop(jid, None)
        if job:
            _by_user.get(job["user"], {}).pop(jid, None)
    if job:
        ctx.bump(job["user"])
        shutil.rmtree(job["dir"], ignore_errors=True)
    return bool(job)


def request_render(jid, cid, changes: dict):
    job = _jobs[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    new = dict(clip)
    new.pop("posting_blocked", None)
    if "title" in changes and changes["title"] != clip.get("title"):
        new["custom_title"] = True
    if "captions" in changes:
        new.update(clean_options({"captions": changes["captions"]}))
    for k in ("start", "end"):
        if k in changes:
            new[k] = round(float(changes[k]), 2)
    if "src_offset" in clip:  # live clip: can only move inside the saved piece of stream
        new["start"] = max(new["start"], clip["src_offset"])
        new["end"] = min(new["end"], clip["src_end"])
    elif job.get("duration"):
        new["end"] = min(new["end"], job["duration"])
    if not (math.isfinite(new["start"]) and math.isfinite(new["end"])) or new["start"] < 0:
        raise ValueError("Those times aren't valid.")
    if not config.long_enough(new["start"], new["end"]):
        raise ValueError(config.CLIP_TOO_SHORT)
    if new["end"] - new["start"] > 180:
        raise ValueError("A clip can be up to 180 seconds long.")
    for k in ("hook", "title", "description", "style", "layout"):
        if k in changes:
            new[k] = str(changes[k])[:{"hook": 60, "title": 100, "description": 2000}.get(k, 20)]
    if new.get("style") not in render.STYLES:
        new["style"] = DEFAULTS["style"]
    if new.get("layout") not in render.LAYOUTS:
        new["layout"] = "blur"  # Older saved clips without a layout used the full-frame blurred fill.
    if new["layout"] in ("stream", "subject"):
        if "facecam" in changes:
            face = changes["facecam"]
            new["facecam"] = None if face is None else framing.camera_box(face, job["width"], job["height"])
            new["facecam_manual"] = True
        elif "facecam" not in new:
            new["facecam"] = job.get("facecam")
        if "framing_shots" in changes:
            new["framing_shots"] = framing.shot_list(changes["framing_shots"], job["width"], job["height"])
    with _lock:
        clip.update(new, status="queued")
        clip.pop("language_review", None)
        clip.pop("revision_progress", None)
        save(job)
    _q.put(("clip", jid, cid))


def _render_clip(job, clip, words, folder, *, name=None):
    """Render the supplied clip and generate its language review record."""
    if not clip.get("src") and not job.get("source_path"):
        _piece_again(job, clip)
    off = clip.get("src_offset", 0.0)
    src = Path(clip["src"]) if clip.get("src") else Path(job["source_path"])
    if not clip.get("src"):
        clip["end"] = min(clip["end"], job["duration"])
    start, end = clip["start"], clip["end"]
    if not config.long_enough(start, end):
        raise ValueError(config.CLIP_TOO_SHORT)
    if job.get("kind") == "live" and src.name.endswith(".src.ts"):
        with tempfile.TemporaryDirectory(prefix="peakcut-live-cut-") as tmp:
            source = render.prepare_live_source(Path(tmp), src, start - off, end - off, job["width"], job["height"])
            return _render_live_cut(job, clip, source, start, folder, name)
    if job.get("kind") == "live":
        return _render_live_cut(job, clip, src, off, folder, name)
    return _render_prepared(job, clip, src, off, words, folder, name)


def _render_live_cut(job, clip, src, off, folder, name):
    # Use the same audio that enters the renderer. The stream transcript may
    # cover different footage after a Twitch ad, dropped frames or a reconnect.
    start, end = clip["start"], clip["end"]
    samples = voices.audio(src, start - off, end - off)
    words = [dict(w, s=w["s"] + start, e=w["e"] + start) for w in transcribe.transcribe(samples)]
    clip.pop("fixed_for", None)
    return _render_prepared(job, clip, src, off, words, folder, name)


def _render_prepared(job, clip, src, off, words, folder, name):
    start, end = clip["start"], clip["end"]
    speech = [w for w in words if w["e"] > start and w["s"] < end]
    _, _, slur, unclear = language.inspect_words(speech, start, end)
    clip["review_speech"] = " ".join(w["w"] for w in speech)
    fields = [clip.get(k, "") for k in ("title", "hook", "description")] + list(clip.get("hashtags", []))
    slur |= any(kind == "slur" for value in fields for _,_,kind in language.hits(language.tag_terms(value)))
    context = " ".join(str(job.get(k) or "") for k in ("creator", "uploader", "title", "game")).replace("-", " ")
    title = (clip.get("title") or "").strip()
    generic = {"", "clip", "loud moment", "live clip", "untitled"}
    legacy = clip.get("version", 0) > 0 and not clip.get("hook_title_checked")
    if (legacy or title.casefold() in generic) and not clip.get("custom_title") and not slur and len(speech) >= 4:
        usage.set_context(job.get("creator"), job.get("title"))
        words_for_title = language.caption_words(speech, context)
        fresh = brain.describe(" ".join(w["w"] for w in words_for_title), job.get("title",""),
                               job.get("uploader",""), game=job.get("game"))
        if fresh.get("title"):
            clip["title"] = fresh["title"]
            slur |= any(kind == "slur" for _, _, kind in language.hits(clip["title"]))
    clip["hook_title_checked"] = True
    for k in ("title", "hook", "description", "reason"):
        if k in clip:
            clip[k] = language.mask(clip[k])
    clip["hashtags"] = [t for t in clip.get("hashtags", []) if not language.hits(language.tag_terms(t))]
    clip["highlight_words"] = [w for w in clip.get("highlight_words", []) if not language.hits(w)]
    local = dict(clip, start=start - off, end=end - off, src_offset=off, hook=roman.to_roman(clip.get("hook") or ""))
    near = _caption_words(job, clip, words, off) if clip.get("captions", True) else []
    near, masked_count, cap_slur, cap_unclear = language.inspect_words(near, start-off, end-off)
    out = render.render_clip(src, folder, name or clip["id"], local, near, job["width"], job["height"])
    path = folder / out["file"]
    lang = str(job.get("lang") or "").lower().split("-")[0]
    issue = ("Slur or racist language detected." if slur or cap_slur else
             "The finished audio reviewer supports English speech only." if lang and lang not in ("en", "english") else
             "Speech is missing, censored at source, or unclear." if not speech or unclear or cap_unclear else
             "Add a specific hook title and re-make this clip before posting." if clip.get("title","").strip().casefold() in generic else "")
    if path.is_file():
        info = media.probe(path)
        if info.get("video_duration", info["duration"]) < config.MIN_CLIP_SECONDS:
            raise ValueError(config.CLIP_TOO_SHORT)
        clip["language_review"] = {"policy": language.VERSION, "digest": language.file_hash(path),
                                   "state": "held" if issue else "pending", "reason": issue,
                                   "masked_words": masked_count}
    clip.update(out, status="done", version=clip.get("version", 0) + 1, error=None)


def prepare_revision(job, clip, changes, folder):
    """Render the proposed edit to private staging while the original stays playable."""
    draft = dict(clip, **changes)
    src = draft.get("src") or job.get("source_path")
    if not src or not Path(src).is_file():
        raise FileNotFoundError("The original footage for this edit is unavailable.")
    if "title" in changes:
        draft["custom_title"] = True
    if "layout" in changes:
        draft.update(facecam=None, facecam_manual=False, framing_shots=[])
    saved = Path(job["dir"]) / "words.json"
    words = json.loads(saved.read_text(encoding="utf-8")) if saved.is_file() else []
    a, b = draft["start"], draft["end"]
    if job.get("kind") != "live" and not any(w["s"] < b and w["e"] > a for w in words):
        off = draft.get("src_offset", 0)
        samples = voices.audio(src, a - off, b - off)
        words = [dict(w, s=w["s"] + a, e=w["e"] + a) for w in transcribe.transcribe(samples)]
    _render_clip(job, draft, words, folder, name=clip["id"] + "-edit-" + uuid.uuid4().hex[:10])
    return draft


def _same_revision_source(job, clip, original):
    return (_jobs.get(job["id"]) is job and any(c is clip for c in job["clips"])
            and all(clip.get(k) == original.get(k)
                    for k in ("status", "file", "version", "start", "end", "title", "style", "layout")))


def reserve_revision(job, clip, original):
    """Reserve a render before starting. Count is shared by all accounts and survives a restart."""
    with _lock:
        if not _same_revision_source(job, clip, original):
            return None
        state = clip.get("revision_progress") or {}
        count = state.get("attempts", 0)
        if count >= MAX_FINAL_REVISIONS:
            return 0
        clip["revision_progress"] = {"attempts": count + 1}
        save(job)
        return count + 1


def keep_revision(job, clip, original, draft, folder):
    """Commit a reviewed replacement only if nobody edited or removed the original meanwhile."""
    with _lock:
        if not _same_revision_source(job, clip, original):
            return False
        clips = Path(job["dir"]) / "clips"
        for name in (draft["file"], draft["cover"], Path(draft["file"]).with_suffix(".ass").name):
            shutil.move(folder / name, clips / name)
        original_posts = clip.get("posts", {})
        progress = clip["revision_progress"]
        draft.pop("posting_blocked", None)
        clip.update(draft, posts=original_posts, revision_progress=progress)
        data = {k: clip[k] for k in ("title", "description", "hashtags", "hook", "start", "end", "score")}
        (clips / f"{clip['id']}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        save(job)
        return True


def render_one(job, clip, words):
    d = Path(job["dir"])
    clip["status"] = "rendering"
    clip.pop("language_review", None)
    save(job)
    try:
        _render_clip(job, clip, words, d / "clips")
        post = {k: clip[k] for k in ("title", "description", "hashtags", "hook", "start", "end", "score")}
        (d / "clips" / f"{clip['id']}.json").write_text(json.dumps(post, indent=1), encoding="utf-8")
        if clip["version"] == 1:
            clip.setdefault("made_at", time.time())
            creators.log(job["creator"], f"Clip ready (score {clip['score']}): {clip['title'] or clip['hook']}")
            save(job)
            for fn in on_clip_ready:
                try:
                    fn(job, clip)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
    except Exception as e:  # noqa: BLE001
        clip.update(status="error", error=safe.friendly(e, 400))
        creators.log(job["creator"], f"Clip failed: {safe.friendly(e, 200)}")
    save(job)


def _caption_words(job, clip, words, off):
    span = [round(clip["start"], 2), round(clip["end"], 2), "en2"]      # "en2": captions made always-English
    if clip.get("fixed_for") == span and clip.get("fixed_words") and not any(
            roman.needs_roman(w.get("w", "")) for w in clip["fixed_words"]):
        raw = clip["fixed_words"]          # already tidied for this exact span: no second AI call
    else:
        raw = [w for w in words if clip["start"] - 1 <= w["s"] <= clip["end"] + 1]
        try:
            usage.set_context(job.get("creator"), job.get("title") or "")
            raw = brain.english_captions(raw, job.get("lang") or "", job.get("title") or "",
                                         youtube_english=lambda: _english_part(job, clip))
            clip.update(fixed_words=raw, fixed_for=span)
        except Exception:  # noqa: BLE001 - captions still render with the words as heard
            pass
    # the last word on it: whatever was said, nothing is drawn in Hindi or Urdu script
    context = " ".join(str(job.get(k) or "") for k in ("game", "title", "uploader", "creator")).replace("-", " ")
    return [dict(w, s=w["s"] - off, e=w["e"] - off) for w in language.caption_words(roman.words(raw), context)]


CLIP_MINUTES = float(config.env("PEAKCUT_CLIP_MINUTES", "15"))     # the longest one clip may take to make


def _wait_for_clips(job, futures, began, workers):
    """Wait for a video's clips, but not for ever. A clip that hangs (a download that stalls, an encoder that
    freezes) used to keep the whole video "in progress", and no other clip could be re-made. Now it is marked as
    failed after CLIP_MINUTES and the video finishes with the clips that did work."""
    left, given_up = list(futures), 0
    late = "This clip took too long and was stopped. Open it and press Save and re-make to try again."
    while left:
        still = []
        for f, clip in left:
            if f.done():
                f.result()
                continue
            t = began.get(clip["id"])
            if (t and time.time() - t > CLIP_MINUTES * 60) or (not t and given_up >= workers):
                given_up += 1
                clip.update(status="error", error=late)
                creators.log(job["creator"], f"Clip stopped after {CLIP_MINUTES:g} minutes: {clip.get('title') or clip.get('hook') or clip['id']}")
                save(job)
                continue
            still.append((f, clip))
        left = still
        if left:
            time.sleep(1)


def _english_part(job, clip):
    """YouTube's English captions for just this clip, for a video in another language. Fetched once per video."""
    if (job.get("source") or {}).get("type") != "url":
        return None
    f = Path(job["dir"]) / "words_en.json"
    if f.is_file():
        en = json.loads(f.read_text(encoding="utf-8"))
    else:
        try:
            en = media.youtube_english_words(job["source"]["url"]) or []
        except Exception:  # noqa: BLE001 - YouTube often refuses its translations: the AI translates instead
            en = []
        f.write_text(json.dumps(en), encoding="utf-8")       # asked once per video, refused or not
    return [w for w in en if clip["start"] - 0.5 <= w["s"] <= clip["end"] + 0.5]


LONG_VIDEO = 40 * 60   # above this, fetch only the pieces that become clips
PAD = 8                # seconds kept either side of a fetched piece, so clips can be re-trimmed


def _piece_again(job, clip):
    """Remaking a clip whose own piece of the video was never fetched (its first try failed) and whose whole video
    is not here: fetch the piece now, cut exactly at the asked time, so no sound matching is needed."""
    src = job.get("source") or {}
    if src.get("type") != "url" or not src.get("url"):
        raise RuntimeError("The original video is no longer here, so this clip can't be made again. Add the video again.")
    d = Path(job["dir"])
    want = max(0.0, clip["start"] - PAD)
    until = clip["end"] + PAD if not job.get("duration") else min(job["duration"], clip["end"] + PAD)
    try:
        piece = media.download_section(src["url"], d / "clips", f"{clip['id']}.src", want, until,
                                       int(job["options"]["quality"]), exact=True)
        meta = media.probe(piece)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Could not fetch this part of the video: {safe.friendly(e, 250)}") from e
    clip.update(src=str(piece), src_offset=round(want, 2), src_end=round(want + meta["duration"], 2))
    clip["end"] = min(clip["end"], clip["src_end"])
    job.setdefault("width", meta["width"])
    job.setdefault("height", meta["height"])


def _fetch_piece(job, clip, full_audio):
    """Long videos: download just this clip's stretch and pin down exactly where it starts."""
    d = Path(job["dir"])
    o = job["options"]
    try:
        want = max(0.0, clip["start"] - PAD)
        piece = media.download_section(job["source"]["url"], d / "clips", f"{clip['id']}.src", want,
                                       min(job["duration"], clip["end"] + PAD), int(o["quality"]))
        wav = d / "clips" / f"{clip['id']}.wav"
        media.extract_audio(piece, wav)
        part = media.load_audio(wav)
        wav.unlink(missing_ok=True)
        meta = media.probe(piece)
        off = media.find_offset(full_audio, part, want)
        clip.update(src=str(piece), src_offset=round(off, 2), src_end=round(off + meta["duration"], 2))
        clip["start"], clip["end"] = max(clip["start"], clip["src_offset"]), min(clip["end"], clip["src_end"])
        job.setdefault("width", meta["width"])
        job.setdefault("height", meta["height"])
    except Exception as e:  # noqa: BLE001
        clip.update(status="error", error=f"Could not fetch this part of the video: {safe.friendly(e, 250)}")


BLOCK = 10 * 60         # scan in bounded sections and rank across them before rendering


def _best_candidates(candidates, existing, limit):
    selected = []
    for c in sorted(candidates, key=lambda c: (-c["score"], c["start"])):
        if limit and len(existing) + len(selected) >= limit:
            break
        if not brain._overlaps(c["start"], c["end"], existing + selected):
            selected.append(c)
    return sorted(selected, key=lambda c: c["start"])


def _covers(words: list, start: float, end: float) -> bool:
    """Is a borrowed transcript good enough to find clips with? It has to reach the end of the part being clipped
    and have a normal amount of talking in it; otherwise our own model writes the transcript."""
    inside = [w for w in words if start <= w["s"] <= end]
    minutes = max(0.5, (end - start) / 60)
    return len(inside) >= 25 * minutes and inside[-1]["s"] >= start + 0.85 * (end - start)


def _facecam(job, piece=None) -> list | None:
    """Where the webcam is in the source: once for the whole video, for every clip of it.

    Only a sure answer is used. A box that is a scoreboard drawn over the game would
    put a picture of a scoreboard in the corner of the clip. One frame is also shown to
    the AI, which tells a person from a game character better than any measurement can;
    what it says is measured again on the picture, so it cannot do any harm."""
    path = piece or job.get("source_path")
    if not path or not Path(path).is_file():
        return None
    try:
        visual_check = None if job.get("options", {}).get("layout") == "stream" else brain.look_for_facecam
        box = facecam.detect(path, ask=visual_check)
    except Exception:  # noqa: BLE001 - a missing facecam is not a failure
        return None
    if not box or box.get("conf") != "high":
        return None
    found = [box["x"], box["y"], box["w"], box["h"]]
    if not piece:
        job["facecam"] = found
    return found


def _run_job(job):
    from concurrent.futures import ThreadPoolExecutor
    d = Path(job["dir"])
    o = job["options"]
    o["max_len"] = max(config.MIN_CLIP_SECONDS, o["max_len"])
    o["min_len"] = max(config.MIN_CLIP_SECONDS, min(o["min_len"], o["max_len"]))
    src = job["source"]
    # Which game this is: what the creator set, or what the title says. It decides the extra rules the AI
    # reads and the tags every post carries.
    game = job.get("game") or ""
    if not game and job.get("creator"):
        try:
            game = (creators.get(job["creator"]) or {}).get("game") or ""
        except Exception:  # noqa: BLE001
            game = ""
    if not game:
        game = games.detect(job["title"], src.get("name"))[0] or ""
    if game and job.get("game") != game:
        job["game"] = game
    t0 = time.time()
    notes = []
    face = False       # where the webcam is in the source, found once; False means not looked for yet
    usage.set_context(job["creator"], job["title"] or src.get("name"))
    resuming = bool(job.pop("resume", False))
    if resuming:   # after a restart: keep the transcript parts, clips and AI answers already made, and carry on
        _set(job, status="running", stage="Picking up where it stopped", error=None)
    else:
        _set(job, status="running", stage="Getting the audio", progress=0.03, error=None, clips=[], ai={}, done_blocks=0)
    if not transcribe.model_ready() and not transcribe.REMOTE:
        from . import fetch_model
        _set(job, stage="Getting the speech model (one time, about 480 MB)")
        fetch_model.ensure()
        _set(job, stage="Getting the audio")
    fetch = ThreadPoolExecutor(1)
    video = None
    has_replay = False
    if src["type"] == "url":
        meta = media.info(src["url"])
        has_replay = meta.get("has_chat_replay", False)
        if meta["kind"] != "video":
            raise RuntimeError("That link is a channel or playlist, not a single video. Paste the link of one video.")
        if ((o["range_end"] or meta["duration"]) - (o["range_start"] or 0)) > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That video exceeds the server limit of {config.MAX_VIDEO_HOURS:g} hours.")
        if meta["is_live"]:
            raise RuntimeError("This link is a live stream, so it would never finish downloading. Open the creator, "
                               "put this link under Setup, and press Start recording instead.")
        _set(job, title=meta["title"], uploader=meta["uploader"], duration=meta["duration"])
        if not job.get("thumb") and media.save_thumb(d, url=src["url"]):
            _set(job, thumb=True)
        usage.set_context(job["creator"], meta["title"])
        span = (o["range_end"] or meta["duration"]) - (o["range_start"] or 0)
        pieces = span > LONG_VIDEO or meta["duration"] <= 0
        if not pieces:  # shorter video: fetch all of it in the background while the audio is worked on
            video = fetch.submit(media.download, src["url"], d, int(o["quality"]))
        audio_file = media.download_audio(src["url"], d, lambda p: _set(job, progress=0.03 + 0.1 * p))
    else:
        pieces, audio_file = False, Path(src["path"])
        info = media.probe(audio_file)
        if info["duration"] > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That video is longer than {config.MAX_VIDEO_HOURS:g} hours, which is the limit.")
        _set(job, source_path=str(audio_file), width=info["width"], height=info["height"], duration=info["duration"])
        if not job.get("thumb") and media.save_thumb(d, video=audio_file):
            _set(job, thumb=True)
    wav = d / "audio.wav"
    media.extract_audio(audio_file, wav)
    audio = media.load_audio(wav)
    wav.unlink(missing_ok=True)
    if src["type"] == "url":
        audio_file.unlink(missing_ok=True)
    if not job["duration"]:
        job["duration"] = len(audio) / 16000
    lo = int((o["range_start"] or 0) * 16000)
    hi = min(len(audio), int(o["range_end"] * 16000)) if o["range_end"] else len(audio)
    span = (hi - lo) / 16000
    if span <= 0:
        raise RuntimeError("No audio was decoded for this video.")
    db = media.loudness_per_second(audio[lo:hi])

    vlock, vstate = threading.Lock(), {}

    def whole_video():
        """Wait for the background download once; retry it, or give up and use pieces."""
        with vlock:
            if "path" not in vstate:
                path = None
                try:
                    path = video.result()
                except Exception as e:  # noqa: BLE001 - sites sometimes refuse a download
                    notes.append(f"Video download refused once ({str(e)[-80:]}), retrying")
                    try:
                        path = media.download(src["url"], d, int(o["quality"]))
                    except Exception as e2:  # noqa: BLE001
                        notes.append(f"Whole-video download failed ({str(e2)[-80:]}), fetching clip parts instead")
                if path:
                    info = media.probe(path)
                    job.update(source_path=str(path), width=info["width"], height=info["height"])
                vstate["path"] = path
            return vstate["path"]

    exact = {}      # clip id -> (from, to, words): our own model's words for just that clip

    began = {}      # clip id -> when work on it started

    def make(clip, words_now):
        nonlocal face
        began[clip["id"]] = time.time()
        if src["type"] == "url" and (pieces or not whole_video()):
            _fetch_piece(job, clip, audio)
        if clip["status"] != "error" and clip.get("captions", True) and yt and yt[1].lower().startswith("en"):
            # The clip was found with YouTube's transcript. What is shown on screen comes from our own model,
            # run on just this clip: full punctuation, nothing blanked out, timings to the word.
            a0, b0 = max(lo, int((clip["start"] - 1.5) * 16000)), min(hi, int((clip["end"] + 1.5) * 16000))
            try:
                tw = [dict(w, s=round(w["s"] + a0 / 16000, 2), e=round(w["e"] + a0 / 16000, 2)) for w in transcribe.transcribe(audio[a0:b0])]
            except Exception:  # noqa: BLE001 - the clip is still made, with YouTube's words
                tw = []
            if len(tw) >= 3:
                exact[clip["id"]] = (a0 / 16000, b0 / 16000, tw)
                words_now = tw
        if clip["status"] != "error":
            render_one(job, clip, words_now)

    cpus = os.cpu_count() or 4
    renders = ThreadPoolExecutor(2 if cpus < 8 else min(6, cpus // 6))  # more clips made at once on a bigger machine
    futures = []
    step = BLOCK * 16000
    blocks = [(x, min(hi, x + step)) for x in range(lo, hi, step)] or [(lo, hi)]
    words, tokens, model = [], 0, None
    done_blocks = 0
    yt = None
    if src["type"] == "url" and config.YT_TRANSCRIPT and media.is_youtube(src["url"]):
        try:
            yt = media.youtube_words(src["url"])
        except Exception:  # noqa: BLE001 - no transcript to borrow: ours is written as usual
            yt = None
        if yt and not _covers(yt[0], lo / 16000, hi / 16000):
            yt = None
        job["transcript_from"] = "youtube" if yt else "own"
        job["lang"] = yt[1] if yt else ""          # the language spoken, when YouTube says (decides the captions)
    if resuming:
        try:
            words = json.loads((d / "words.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            words = []
            job.pop("scan", None)
            job.pop("scan_candidates", None)
        # Older versions counted transcribed blocks even after they stopped looking for clips.
        done_blocks = min(int(job.get("done_blocks", 0)), len(blocks)) if job.get("scan") else 0
        tokens, model = int((job.get("ai") or {}).get("tokens", 0)), (job.get("ai") or {}).get("model")
        if done_blocks < len(blocks):     # words past the last finished part are transcribed again
            cut = blocks[done_blocks][0] / 16000
            words = [w for w in words if w["s"] < cut]
        for c in job["clips"]:            # clips found before the restart that were not finished yet
            if c.get("status") not in ("done", "error"):
                c["status"] = "queued"
                futures.append((renders.submit(ctx.bind(make), c, list(words)), c))
        save(job)
    scan = job.setdefault("scan", {"total": len(blocks), "checked": 0, "through": 0, "quiet": 0, "gaps": []})
    candidates = job.setdefault("scan_candidates", [])
    # The transcript of the next part is written while the AI is still reading this one (the AI is a wait on the
    # network, the transcript is work on this machine), so neither waits for the other.
    ahead = ThreadPoolExecutor(2, thread_name_prefix="ahead")
    heard, now_on = {}, [done_blocks]

    def hear(i):
        if i < len(blocks) and i >= done_blocks and i not in heard:
            a_, b_ = blocks[i]
            last = [0.0]

            def tp(p, i=i, a_=a_, b_=b_):
                if now_on[0] == i and p - last[0] > 0.05:      # only the part being shown moves the bar
                    last[0] = p
                    _set(job, progress=max(job["progress"], 0.14 + 0.78 * ((a_ - lo) + p * (b_ - a_)) / max(1, hi - lo)))
            heard[i] = ahead.submit(ctx.bind(transcribe.transcribe), audio[a_:b_], tp)

    # The ad breaks and static screens of the whole video, found once. It reads the video end to end,
    # so it is done here rather than for each clip, and only when asked for.
    statics = quality.static_ranges(job.get("source_path") or "") if config.SKIP_STATIC else []
    peaks = []
    if has_replay:
        _set(job, stage="Checking the chat replay")
        try:
            peaks, state = replay_chat.download(src["url"])
        except (OSError, subprocess.SubprocessError):
            state = "unavailable"
        # Several busy eight-second bins in a row describe one sustained crowd reaction.
        count = sum(i == 0 or second - peaks[i - 1] > replay_chat.BIN for i, second in enumerate(peaks))
        job["chat_replay"] = {"status": state, "bursts": count}
    elif src["type"] == "url" and media.is_youtube(src["url"]):
        job["chat_replay"] = {"status": "unavailable", "bursts": 0}
    voice = voices.profile(job["creator"])

    def review(start, end, context, final_attempt=False):
        nonlocal tokens, model
        if voice is not None:
            a, b = round(start * voices.SR), round(end * voices.SR)
            try:
                context = voices.tag(context, audio[a:b], voice, offset=a / voices.SR)
            except Exception as error:
                notes.append("Voice matching incomplete: " + safe.friendly(error, 110))
        local = [dict(w, s=round(w["s"] - start, 2), e=round(w["e"] - start, 2)) for w in context]
        offset = lo / 16000
        levels = db[int(start - offset):math.ceil(end - offset)]
        found, ai = brain.find_clips(local, levels, end - start, job["title"], job["uploader"],
                                     int(o["max_clips"]), int(o["min_len"]), int(o["max_len"]), notes.append,
                                     game=game, chat=(lambda s, e: replay_chat.near(peaks, start + s, start + e)) if peaks else None)
        tokens += ai.get("tokens", 0)
        model = ai.get("model") or model
        gaps = [[start + s, start + e] for s, e in ai.get("failed", [])]
        if gaps and not ai.get("model") and (not final_attempt or config.env("PEAKCUT_AI_CLIPS_ONLY") == "1"):
            found = []
        for c in found:
            a_s, b_s = round(c["start"] + start, 2), round(c["end"] + start, 2)
            if voice is not None and not voices.heard_creator(context, a_s, b_s):
                continue
            if statics and quality.static_overlap(a_s, b_s, statics) > 0.5 * max(1, b_s - a_s):
                continue
            candidates.append(dict(c, start=a_s, end=b_s))
        job["ai"] = {"model": model, "tokens": tokens, "note": "", "log": notes[-5:]}
        if editorial.enabled() and end - start >= int(o["min_len"]):
            try:
                with tempfile.TemporaryDirectory(dir=d, prefix="scout-") as tmp:
                    video_path = job.get("source_path")
                    if not video_path and not pieces and video:
                        video_path = whole_video()
                    if video_path:
                        segments = [(video_path, 0, job["duration"])]
                    else:
                        piece = media.download_section(src["url"], Path(tmp), "preview", start, end, 360, exact=True)
                        segments = [(piece, start, end)]
                    visible, spent = editorial.scout(segments, start, end, context, peaks, int(o["min_len"]),
                                                      int(o["max_len"]), game, verify_speaker=o["layout"] == "stream")
                candidates.extend(visible)
                tokens += spent
                model = model or "openclaw:peakcut"
            except Exception as error:
                notes.append("Visual review incomplete: " + str(error)[-140:])
                if [start, end] not in gaps:
                    gaps.append([start, end])
        return dict(ai, gaps=gaps)

    for bi, (a, b) in enumerate(blocks):
        if bi < done_blocks:
            continue                      # transcribed and checked by the AI before the restart
        shift = a / 16000
        where = f" (minute {round((a - lo) / 16000 / 60)} of {round(span / 60)})" if len(blocks) > 1 else ""
        section_words = [w for w in yt[0] if shift <= w["s"] < b / 16000] if yt else []
        # Downloaded captions may look complete overall and still omit a scene in the middle.
        use_captions = bool(section_words) and _covers(section_words, shift, b / 16000)
        if use_captions:
            edges = [shift] + [w["s"] for w in section_words] + [b / 16000]
            use_captions = all(y - x < 30 for x, y in zip(edges, edges[1:]))
        if use_captions:
            _set(job, stage="Reading the transcript" + where)
            bw = [dict(w, s=round(w["s"] - shift, 2), e=round(w["e"] - shift, 2)) for w in section_words]
        else:
            if yt:
                job["transcript_from"] = "youtube and own"
            _set(job, stage="Writing the transcript" + where)
            now_on[0] = bi
            hear(bi)
            if not yt:
                hear(bi + 1)
            bw = heard.pop(bi).result()
        _set(job, progress=max(job["progress"], 0.14 + 0.78 * (b - lo) / max(1, hi - lo)))
        # Include the end of the preceding section so a setup, payoff and reaction can cross the cut.
        window_start = max(lo / 16000, shift - int(o["max_len"]))
        context = [w for w in words if w["s"] >= window_start]
        new_words = [dict(w, s=round(w["s"] + shift, 2), e=round(w["e"] + shift, 2)) for w in bw]
        context.extend(new_words)
        words.extend(new_words)
        tmp_words = d / "words.scan.tmp"
        tmp_words.write_text(json.dumps(words), encoding="utf-8")
        tmp_words.replace(d / "words.json")
        _set(job, stage="Finding the best moments" + where, word_count=len(words))
        ai = review(window_start, b / 16000, context)
        scan["gaps"].extend(ai["gaps"])
        scan.update(checked=bi + 1, through=round(b / 16000, 2), quiet=scan["quiet"] + int(bool(ai.get("quiet"))))
        job["done_blocks"] = bi + 1       # a restart from here on carries on with the next part
        save(job)
    if not scan.get("ranked"):
        # A temporary subscription outage need not cost an entire section. Reuse its saved transcript.
        for gap in list(scan["gaps"]):
            start, end = gap
            _set(job, stage=f"Retrying highlight review at minute {int(start // 60)}")
            ai = review(start, end, [w for w in words if start <= w["s"] < end], final_attempt=True)
            scan["gaps"].remove(gap)
            scan["gaps"].extend(ai["gaps"])
            save(job)
    selected = _best_candidates(
        [c for c in candidates if config.long_enough(c["start"], c["end"])],
        job["clips"], int(o["max_clips"]))
    for c in selected:
        c.update(id=f"clip{len(job['clips']) + 1:02d}", style=o["style"], layout=o["layout"],
                 captions=o.get("captions", True), status="queued", version=0)
        if o["layout"] == "gameplay":
            if face is False:
                face = _facecam(job)
            if face:
                c["facecam"] = face
        job["clips"].append(c)
    scan["ranked"] = True
    job.pop("scan_candidates", None)
    save(job)  # persist the entire ranked selection before the first render starts
    for c in selected:
        futures.append((renders.submit(ctx.bind(make), c, words), c))
    if futures:
        _set(job, stage="Finishing the clips", progress=max(job["progress"], 0.92))
    _wait_for_clips(job, futures, began, renders._max_workers)
    if exact:           # keep our own words for the clips in the saved transcript, so editing a clip later uses them
        for a_s, b_s, tw in exact.values():
            words = [w for w in words if not a_s <= w["s"] < b_s] + tw
        words.sort(key=lambda w: w["s"])
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
    renders.shutdown(wait=False)
    ahead.shutdown(wait=False)
    fetch.shutdown(wait=False)
    del audio
    clips = job["clips"]
    job["ai"] = {"model": model, "tokens": tokens, "log": notes[-5:],
                 "note": "The AI was unavailable, so clips were picked by loudness only." if clips and not model else ""}
    expected_end = o.get("range_end") or job["duration"]
    if expected_end - hi / 16000 > 3:
        scan["gaps"].append([round(hi / 16000, 2), round(expected_end, 2)])
    if scan["gaps"]:
        job["ai"]["note"] = "Highlight review was incomplete. See the time ranges below."
    stage = "Done" if clips else "Done, but nothing clip-worthy was found"
    _set(job, status="done", stage=stage, progress=1.0, took=round(time.time() - t0))
    try:
        from . import notices
        name = job.get("title") or job["source"].get("name") or "your video"
        notices.add(job.get("user"), "clips", f"{len(clips)} clip{'s' if len(clips) != 1 else ''} ready" if clips else "Nothing clip-worthy found",
                    name, f"/app/creators/{job['creator']}" if job.get("creator") else "/app/videos")
    except Exception:  # noqa: BLE001
        pass
    creators.log(job["creator"], f"Video finished with {len(clips)} clips in {round(time.time() - t0)}s: {job['title']}")


def _worker(quick_only=False):
    while True:
        kind, a, cid = _q.get(quick_only)
        if kind == "fn":
            try:
                a()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            continue
        job = _jobs.get(a)
        if not job:
            if kind == "job":
                _q.done()
            continue
        token = ctx._uid.set(job["user"])  # everything this job does runs as its owner
        try:
            if kind == "job":
                _run_job(job)
            else:
                clip = next(c for c in job["clips"] if c["id"] == cid)
                words = []
                if (Path(job["dir"]) / "words.json").is_file():
                    words = json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
                render_one(job, clip, words)
        except Exception as e:  # noqa: BLE001
            (Path(job["dir"]) / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            if kind == "job":
                for c in job["clips"]:
                    if c.get("status") in ("queued", "rendering"):
                        c.update(status="error", error="The video stopped before this clip was made.")
                _set(job, status="error", error=safe.friendly(e), stage="Stopped")
                creators.log(job["creator"], f"Video failed: {safe.friendly(e)[:200]}")
        finally:
            ctx._uid.reset(token)
            if kind == "job":
                _q.done()


def _pick_up_after_restart():
    """Videos and single clips that were being made when the server stopped: queued again, oldest first."""
    waiting = sorted((j for j in all_jobs() if j.get("status") == "queued" and j.get("resume")), key=lambda j: j["created"])
    for j in waiting:
        _q.put(("job", j["id"], None), user=j["user"])
    for j in all_jobs():
        for cid in j.pop("remake", []):
            _q.put(("clip", j["id"], cid))
        if j.get("status") == "done":
            save(j)


def start():
    load.start()
    load_all()
    _pick_up_after_restart()
    for _ in range(max(1, config.JOB_WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_worker, args=(True,), daemon=True).start()   # re-making a single clip never waits behind whole videos
    threading.Thread(target=_live_worker, daemon=True).start()
