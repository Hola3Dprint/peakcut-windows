"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import hashlib
import secrets
import tempfile
import threading
import time

import json
from pathlib import Path

from . import accounts, alerts, channel_queue, config, creators, ctx, devices, editorial, framing, jobs, language, live, publish_review, usage, voices

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "checking", "posting")
STOPPED = ("blocked", "deleted")
BLOCKED_MESSAGE = "This clip is blocked from posting."


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def forget(uid: str):
    """Drop everything queued or recorded for a deleted account, so nothing is posted or retried for it any more."""
    with _lock:
        for p in _posts:
            if p.get("user") == uid:
                p["status"] = "deleted"          # the posting loop may be holding this one already: it then skips it
        _posts[:] = [p for p in _posts if p.get("user") != uid]
        _save()


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url",
                                                                    "due_at", "error", "editorial", "window_closed")}
        jobs.save(job)


def _block_post(p):
    p.update(status="blocked", blocked_at=time.time(), error=None)
    p.pop("review_stage", None)


def set_clip_blocked(job, clip, blocked):
    """Save the clip restriction and close pending attempts on every account."""
    with _lock:
        group = [p for p in _mine() if (p["job"], p["clip"]) == (job["id"], clip["id"])]
        if blocked:
            clip["posting_blocked"] = True
            jobs.save(job)
        changed = [p for p in group if p["status"] in ("scheduled", "checking")] if clip.get("posting_blocked") else []
        for p in changed:
            _block_post(p)
        if changed:
            _save()
            for p in changed:
                _mirror(p)
        if not blocked:
            clip.pop("posting_blocked", None)
            jobs.save(job)
        return {"blocked": blocked, "uploading": any(p["status"] == "posting" for p in group)}


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def enqueue(job, clip, account_ids, now=False) -> list:
    if "start" in clip and "end" in clip and not config.long_enough(clip["start"], clip["end"]):
        raise publish_review.ShortClip(config.CLIP_TOO_SHORT)
    publish_review.local_check(clip)
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        if clip.get("posting_blocked"):
            raise publish_review.Hold(BLOCKED_MESSAGE)
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            previous = next((p for p in _mine() if p["job"] == job["id"] and p["clip"] == clip["id"]
                             and p["account"] == aid and p["status"] in OPEN + ("posted", "library")), None)
            if previous:
                if now and previous.get("ranked") and previous["status"] in ("library", "scheduled"):
                    previous.update(manual_now=True, status="scheduled")
                    previous.pop("window_closed", None)
                    made.append(previous)
                continue  # never post the same clip to the same account twice
            ranked = acc["platform"] == "youtube"
            due = time.time() if now or ranked else _next_slot(aid, c["post_gap_min"], c["daily_cap"])
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
            if ranked:
                p.update(ranked=True, manual_now=bool(now), window_plan=True)
                if not now and p["score"] < 80:
                    p["status"] = "library"
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    try:
        made = enqueue(job, clip, c["accounts"])
    except publish_review.Hold as error:
        creators.log(c["id"], f"Clip held from posting: {error}")
        return
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


_remote = {}


def _channel_group(entry):
    with _lock:
        local = channel_queue.account_posts(_posts, entry)
        known = {p.get("remote_id") for p in local if p.get("remote_id")}
        remote = _remote.get((entry.get("user"), entry["account"]), [])
        return local + [dict(r, status="posted") for r in remote if r["remote_id"] not in known]


def _sync_channel(acc):
    recent = accounts.youtube_uploads_today(acc)
    with _lock:
        _remote[(ctx.uid(), acc["id"])] = recent


def _type_for(p, now):
    if not p.get("ranked"):
        return "scheduled"
    winner, kind = channel_queue.pick(_channel_group(p), now, allow_early=editorial.enabled())
    return kind if winner is p else None


def _score_finished(p, job, clip):
    if not editorial.enabled():
        return
    original = dict(clip)
    path = Path(job["dir"]) / "clips" / clip["file"]
    title = clip.get("title") or clip.get("hook") or "Clip"
    key = [language.file_hash(path), title, editorial.EDIT_REVIEW_VERSION]
    previous = p.get("editorial") or {}
    digest = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
    if previous.get("digest") == digest:
        return
    usage.set_context(job["creator"], job.get("title") or title)
    stream_layout = clip.get("layout") in ("stream", "gameplay") or job.get("options", {}).get("layout") == "stream"
    old_stack = clip.get("layout") == "stream" and not clip.get("framing_verified") and job.get("width", 0) > job.get("height", 0)
    bad_stack = False
    try:
        if old_stack:
            framing.camera_box(clip.get("facecam"), job["width"], job["height"])
    except ValueError:
        bad_stack = True
    voice = voices.profile(job["creator"])
    try:
        result = editorial.score_cut(path, title, voice=voice, stream_layout=stream_layout, verify_media=True)
    except Exception as error:
        raise publish_review.ReviewerUnavailable("Waiting for the finished clip score. PeakCut will retry.") from error
    if bad_stack and result["decision"] != "skip":
        edit = result.get("edit")
        edit = dict(edit) if isinstance(edit, dict) else {}
        edit.setdefault("framing", "refresh")
        result.update(decision="rework", reason="The old upper panel repeats the stream. " + result["reason"], edit=edit)
    if (clip.get("status") != "done" or clip.get("version") != original.get("version")
            or language.file_hash(path) != key[0] or (clip.get("title") or clip.get("hook") or "Clip") != title):
        raise publish_review.ReviewerUnavailable("The edit changed. PeakCut will review the new cut.")
    if result["decision"] == "rework":
        changes = editorial.revision_changes(original, result.get("edit"))
        if changes:
            result, key = _recheck_revision(p, job, clip, original, result, changes, key, voice, stream_layout)
    if result.get("revision") != "passed" and (clip.get("status") != "done"
            or clip.get("version") != original.get("version") or clip.get("title") != original.get("title")):
        raise publish_review.ReviewerUnavailable("The edit changed. PeakCut will review the new cut.")
    if result["decision"] == "publish" and result["score"] < 80:
        result["decision"] = "skip"
    result.pop("edit", None)
    with _lock:
        if p["status"] in STOPPED:
            return
        p.setdefault("scout_score", p["score"])
        p.update(title=key[1], reviewed_at=time.time(),
                 editorial=dict(result, digest=hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest(),
                                file_digest=key[0], source_title=key[1],
                                version=editorial.EDIT_REVIEW_VERSION))
        _save()


def _recheck_revision(p, job, clip, original, first, changes, key, voice, stream_layout):
    reason = first["reason"]
    draft, result = original, first
    with tempfile.TemporaryDirectory(prefix="peakcut-final-edit-") as tmp:
        folder = Path(tmp)
        while changes:
            with _lock:
                if p["status"] in STOPPED:
                    return first, key
                attempt = jobs.reserve_revision(job, clip, original)
                if attempt is None:
                    raise publish_review.ReviewerUnavailable("The clip changed during the revision. PeakCut will check the new cut.")
                if not attempt:
                    break
                p["review_stage"] = f"Re-editing ({attempt} of {jobs.MAX_FINAL_REVISIONS})"
                _save()
            ctx.bump()
            try:
                draft = jobs.prepare_revision(job, draft, changes, folder)
                publish_review.local_check(draft)
            except (FileNotFoundError, ValueError, publish_review.Hold) as error:
                result = dict(result, reason=f"{reason} {error}")
                break
            except (OSError, RuntimeError):
                result = dict(result, reason=f"{reason} The source could not be exported with the revision.")
                break
            file = folder / draft["file"]
            title = draft.get("title") or draft.get("hook") or "Clip"
            with _lock:
                if p["status"] in STOPPED:
                    return first, key
                p["review_stage"] = f"Checking revision {attempt}"
            ctx.bump()
            try:
                result = editorial.score_cut(file, title, voice=voice, stream_layout=stream_layout, verify_media=True)
            except Exception as error:
                raise publish_review.ReviewerUnavailable("Waiting to verify the revised cut. PeakCut will retry.") from error
            if result["decision"] == "publish" and result["score"] >= 80:
                revised = [language.file_hash(file), title, editorial.EDIT_REVIEW_VERSION]
                with _lock:
                    if p["status"] in STOPPED:
                        return first, key
                    if not jobs.keep_revision(job, clip, original, draft, folder):
                        raise publish_review.ReviewerUnavailable("The clip changed during the revision. PeakCut will check the new cut.")
                return dict(result, revision="passed", revision_attempts=attempt, original_reason=reason), revised
            changes = editorial.revision_changes(draft, result.get("edit")) if result["decision"] == "rework" else {}
    return dict(result, revision="held", original_reason=reason,
                revision_attempts=(clip.get("revision_progress") or {}).get("attempts", 0)), key


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    version = (clip or {}).get("version")
    try:
        with _lock:
            if p["status"] in STOPPED:
                return
            if clip and clip.get("posting_blocked"):
                _block_post(p)
                _save()
                _mirror(p)
                return
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        if p.get("ranked"):
            try:
                _sync_channel(acc)
            except Exception as error:
                raise publish_review.ReviewerUnavailable("Waiting for YouTube to confirm the channel's daily count.") from error
        with _lock:
            if p["status"] in STOPPED:
                return
            now = time.time()
            kind = _type_for(p, now)
            if not devices.active() or accounts.settings()["paused"] or kind is None:
                p.update(status="scheduled", error=None)
                _save()
                _mirror(p)
                return
            if "start" in clip and "end" in clip and not config.long_enough(clip["start"], clip["end"]):
                raise publish_review.ShortClip(config.CLIP_TOO_SHORT)
            if kind == "standout_review":
                p["early_window"] = channel_queue.window_start(now)
            p.setdefault("selected_at", now)
            _save()
        _score_finished(p, job, clip)
        version = clip.get("version")
        with _lock:
            if p["status"] in STOPPED:
                return
            assessment = p.get("editorial") or {}
            if assessment.get("decision") in ("rework", "skip"):
                p.update(status="library", error=None)
                p.pop("review_stage", None)
                _save()
                _mirror(p)
                return
            if accounts.settings()["paused"] or _type_for(p, time.time()) in (None, "standout_review"):
                p.update(status="scheduled", error=None)
                p.pop("review_stage", None)
                _save()
                _mirror(p)
                return
        with publish_review.checked_upload(job, clip, creator) as (path, title, cap, tags):
            if editorial.enabled():
                approved = p.get("editorial") or {}
                if (approved.get("decision") != "publish" or language.file_hash(path) != approved.get("file_digest")
                        or (clip.get("title") or clip.get("hook") or "Clip") != approved.get("source_title")):
                    raise publish_review.ReviewerUnavailable("The edit changed. PeakCut will review the new cut.")
            if p.get("ranked"):
                try:
                    _sync_channel(acc)
                except Exception as error:
                    raise publish_review.ReviewerUnavailable("Waiting for YouTube to confirm the channel's daily count.") from error
            with _lock:
                if p["status"] in STOPPED:
                    return
                now = time.time()
                kind = _type_for(p, now)
                if not devices.active() or accounts.settings()["paused"] or kind in (None, "standout_review"):
                    p.update(status="scheduled", error=None)
                    _save()
                    _mirror(p)
                    return
                # Persist the point at which an external write may actually have happened.
                p.update(status="posting", transport_at=now, post_kind=kind)
                if kind == "regular":
                    p["release_window"] = channel_queue.window_start(now)
                _save()
            if acc["platform"] == "youtube":
                rid, url = accounts.youtube_upload(acc, path, title, cap + "\n\n#Shorts", tags, creator["privacy"])
            elif acc["platform"] == "instagram":
                rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
            elif acc["platform"] == "tiktok":
                rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
            else:
                raise RuntimeError("That kind of account is no longer supported.")
        if clip.get("status") == "done" and clip.get("version") == version:
            clip["language_review"].update(state="passed", reason="")
        p.update(status="posted", title=title, posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as error:  # noqa: BLE001
        with _lock:
            if p["status"] in STOPPED:
                return
            _post_error(p, clip, version, acc, error)
    p.pop("review_stage", None)
    _save()
    _mirror(p)


def _post_error(p, clip, version, acc, error):
    if isinstance(error, publish_review.ReviewerUnavailable):
        p.update(status="scheduled", not_before=time.time() + 300, due_at=time.time() + 300, error=str(error))
    elif isinstance(error, publish_review.ShortClip):
        p.update(status="library", error=str(error))
    elif isinstance(error, publish_review.Hold):
        if clip and clip.get("status") == "done" and clip.get("version") == version:
            report = clip.get("language_review") or {}
            if report.get("state") in (None, "pending", "passed"):
                report.update(state="blocked" if isinstance(error, publish_review.Block) else "uncertain", reason=str(error))
                clip["language_review"] = report
        p.update(status="held", error=str(error))
        creators.log(p["creator"], f"Post held: {error}")
    else:
        p["tries"] += 1
        p["error"] = str(error)[-300:]
        if p.get("transport_at"):
            p.update(status="failed", error="Upload outcome needs checking before retry. " + p["error"])
            creators.log(p["creator"], f"Check the upload on {p['account_name']} before retrying.")
        elif p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", not_before=time.time() + 600, due_at=time.time() + 600)
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")


def _due(now):
    with _lock:
        waiting = [p for p in _posts if p["status"] == "scheduled" and p.get("user")]
        due = [p for p in waiting if p["platform"] != "youtube" and p["due_at"] <= now]
        channels = {(p["user"], p["account"]): p for p in waiting if p["platform"] == "youtube"}
        for entry in channels.values():
            picked, _ = channel_queue.pick(_channel_group(entry), now, allow_early=editorial.enabled())
            if picked and picked["status"] == "scheduled":
                due.append(picked)
        return due


def _close_window(p):
    p.update(status="library", window_closed=True, error=None)


def _expire_windows(now):
    with _lock:
        channels = {(p["user"], p["account"]): p for p in _posts
                    if p.get("user") and p.get("ranked") and p["status"] == "scheduled"}
        expired = []
        for entry in channels.values():
            for p, reason, _ in channel_queue.standings(_channel_group(entry), now, allow_early=editorial.enabled()):
                if reason == "Window closed" and p["status"] == "scheduled":
                    _close_window(p)
                    expired.append(p)
        if expired:
            _save()
    for p in expired:
        with ctx.as_user(p["user"]):
            _mirror(p)


def _run():
    while True:
        time.sleep(15)
        now = time.time()
        try:
            _expire_windows(now)
        except (OSError, ValueError):
            pass  # A damaged clip record should not stop the queue.
        for p in _due(now):
            try:
                with ctx.as_user(p["user"]):
                    if not devices.active() or accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        if _type_for(p, time.time()) is None:
                            continue
                        p["status"] = "checking"
                        _save()
                        _mirror(p)
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for listing in accounts.list_accounts():
        if listing["platform"] == "youtube":
            try:
                _sync_channel(accounts.get(listing["id"]))
            except Exception:
                pass  # A fresh, successful check is required again before every real upload.
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled", "held"):
            raise ValueError("This post cannot be retried.")
        if any(q is not p and (q["job"],q["clip"],q["account"]) == (p["job"],p["clip"],p["account"])
               and q["status"] in OPEN + ("posted",) for q in _mine()):
            raise ValueError("This clip is already queued or posted on this account.")
        job = jobs.get(p["job"])
        clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
        if clip and clip.get("posting_blocked"):
            raise ValueError(BLOCKED_MESSAGE)
        if clip and (clip.get("language_review") or {}).get("state") == "uncertain":
            clip["language_review"].update(state="pending", reason="")
        if clip:
            publish_review.local_check(clip)
        p.pop("transport_at", None)
        p.pop("release_window", None)
        p.pop("post_kind", None)
        p.update(status="scheduled", due_at=time.time(), not_before=0, tries=0, error=None)
        if p.get("platform") == "youtube":
            p["ranked"] = True
        _save()
    _mirror(p)


def overview() -> dict:
    now = time.time()
    today = _day(now)
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    blocked_clips = {(j["id"], c["id"]) for j in all_js for c in j["clips"] if c.get("posting_blocked")}
    channels, annotations = [], {}
    for acc in accounts.list_accounts():
        if acc["platform"] != "youtube":
            continue
        group = _channel_group({"user": ctx.uid(), "account": acc["id"]})
        used, pending, _ = channel_queue.budget(group, now)
        channels.append(dict(name=acc["name"], used=used, pending=pending, limit=channel_queue.LIMIT,
                             next_window=channel_queue.window_boundary(now, 1),
                             next_regular=channel_queue.regular_ready_at(group),
                             next_standout=channel_queue.standout_ready_at(group), server_time=now))
        for rank, (p, reason, kind) in enumerate(channel_queue.standings(group, now, allow_early=editorial.enabled()), 1):
            label = {"standout": "92+ after review", "standout_review": "Early review candidate", "regular": "Window pick"}
            annotations[p["id"]] = dict(rank=rank, selection=reason or label.get(kind, "Selected manually"))
    queue = sorted((p for p in _posts if p["status"] in OPEN),
                   key=lambda p: (p["status"] != "posting", p["account"], annotations.get(p["id"], {}).get("rank", 0), p["due_at"]))
    done = sorted((p for p in _posts if
                   (p["status"] in ("posted", "failed", "library") and (not p.get("window_closed") or p.get("selected_at")))
                   or (p["status"] == "blocked" and (p["job"], p["clip"]) in blocked_clips)),
                  key=lambda p: -(p.get("posted_at") or p.get("blocked_at") or p.get("reviewed_at") or p["due_at"]))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, **annotations.get(p["id"], {}),
             "creator_name": names.get(p["creator"], ""), "posting_blocked": (p["job"], p["clip"]) in blocked_clips}
            for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily, "channels": channels,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def _restore_older_posts():
    for p in _posts:
        if (p.get("status") != "held" or not p.get("user")
                or p.get("error") != "Re-make this clip with the language filters before posting."):
            continue
        with ctx.as_user(p["user"]):
            job = jobs.get(p["job"])
            clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
            if not clip or clip.get("status") != "done":
                continue
            try:
                publish_review.local_check(clip)
            except publish_review.Hold as error:
                p["error"] = str(error)
                _save()
                _mirror(p)
                continue
            if any(q is not p and (q["job"], q["clip"], q["account"]) == (p["job"], p["clip"], p["account"])
                   and q["status"] in OPEN + ("posted",) for q in _mine()):
                p.update(status="cancelled", error=None)
                _save()
                _mirror(p)
                continue
            creator = creators.get(job["creator"]) or creators.DEFAULTS
            due = _next_slot(p["account"], creator["post_gap_min"], creator["daily_cap"])
            p.update(status="scheduled", due_at=max(due, p["due_at"]), error=None)
            _save()
            _mirror(p)


def start():
    _restore_older_posts()
    first_window = channel_queue.window_start(time.time())
    with _lock:
        for p in _posts:
            changed = False
            old_stage = p.pop("review_stage", None)
            old_review = p.get("editorial") or {}
            # An edit from the one-attempt rollout still counts against this clip's limit.
            earlier_attempt = (old_stage in ("Re-editing", "Checking revised cut") or
                               (old_review.get("version") == 4 and old_review.get("revision")
                                and "revision_attempts" not in old_review))
            if earlier_attempt and p.get("user"):
                with ctx.as_user(p["user"]):
                    job = jobs.get(p["job"])
                    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
                    if clip is not None and not clip.get("revision_progress"):
                        clip["revision_progress"] = {"attempts": 1}
                        jobs.save(job)
                        if p["status"] == "library" and old_review.get("decision") == "rework":
                            p.update(status="scheduled", not_before=0, error=None)
                            changed = True
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
                changed = True
            elif p["status"] == "checking":
                p.update(status="scheduled", error=None)
                changed = True
            if (p.get("ranked") and not p.get("window_closed") and not p.get("selected_at")
                    and old_review.get("version", 0) < editorial.EDIT_REVIEW_VERSION
                    and p.get("scout_score", 0) >= 80
                    and (p["status"] == "scheduled" or
                         (p["status"] == "library" and old_review.get("decision") == "rework"))):
                p.update(status="scheduled", score=p["scout_score"], not_before=0, error=None)
                changed = True
            if p["status"] == "scheduled" and p["platform"] == "youtube" and not p.get("ranked"):
                delay = min(p["due_at"], time.time() + 600) if p.get("error") else 0
                p.update(ranked=True, manual_now=False, not_before=delay, due_at=p["created"])
                if p.get("score", 0) < 80:
                    p["status"] = "library"
                changed = True
            if p.get("user") and p["status"] == "scheduled":
                with ctx.as_user(p["user"]):
                    job = jobs.get(p["job"])
                    if job and any(c["id"] == p["clip"] and c.get("posting_blocked") for c in job["clips"]):
                        _block_post(p)
                        changed = True
            if p.get("ranked") and not p.get("window_plan") and p["status"] == "scheduled":
                p["window_plan"] = True
                if not p.get("manual_now") and (p.get("created") or 0) < first_window:
                    _close_window(p)
                changed = True
            if changed and p.get("user"):
                with ctx.as_user(p["user"]):
                    _mirror(p)
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
