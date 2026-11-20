"""Choose YouTube posts across a channel's creators, using the PC's local calendar."""
from datetime import datetime, timedelta


LIMIT = 6
WINDOW_HOURS = 3
STANDOUT_SCORE = 92
STANDOUT_SCOUT = 90
KINDS = ("manual", "standout", "regular", "standout_review")


def window_start(t):
    local = datetime.fromtimestamp(t)
    return local.replace(hour=local.hour // WINDOW_HOURS * WINDOW_HOURS, minute=0, second=0,
                         microsecond=0, fold=0).timestamp()


def window_boundary(t, offset):
    start = datetime.fromtimestamp(window_start(t))
    return (start + timedelta(hours=offset * WINDOW_HOURS)).timestamp()


def account_posts(posts, entry):
    return [p for p in posts if p.get("platform") == "youtube"
            and (p.get("user"), p["account"]) == (entry.get("user"), entry["account"])]


def budget(posts, now):
    today = datetime.fromtimestamp(now).date()
    used = [p for p in posts if p["status"] == "posted" and p.get("posted_at")
            and datetime.fromtimestamp(p["posted_at"]).date() == today]
    # Reserve the slot for an interrupted transfer until someone checks the channel.
    pending = sum(p["status"] in ("posting", "failed") and bool(p.get("transport_at"))
                  and datetime.fromtimestamp(p["transport_at"]).date() == today for p in posts)
    return len(used), pending, max((p["posted_at"] for p in used), default=0)


def regular_filled(posts, current):
    for p in posts:
        if p["status"] not in ("posted", "posting", "failed"):
            continue
        if p.get("post_kind") == "regular" and p.get("release_window") == current:
            return True
        if not p.get("post_kind") and not p.get("manual_now"):
            stamp = p.get("posted_at") or p.get("transport_at")
            if stamp and window_start(stamp) == current:
                return True
    return False


def _approved_standout(p):
    review = p.get("editorial") or {}
    return review.get("decision") == "publish" and review.get("score", 0) >= STANDOUT_SCORE


def _ready_at(posts, kind):
    last = max((p.get("posted_at") or p.get("transport_at") or 0 for p in posts
                if p["status"] in ("posted", "posting", "failed") and
                (p.get("post_kind") or ("manual" if p.get("manual_now") else "regular")) == kind),
               default=0)
    return last + WINDOW_HOURS * 3600 if last else 0


def standout_ready_at(posts):
    return _ready_at(posts, "standout")


def regular_ready_at(posts):
    return _ready_at(posts, "regular")


def _decision(p, now, current, previous, filled, full, allow_early, early_open, regular_open):
    arrived = p.get("created") or 0
    early_carry = (early_open and p.get("early_window") == previous and
                   previous <= arrived < current and _approved_standout(p))
    if not p.get("manual_now") and (arrived < previous or (arrived < current and filled and not early_carry)):
        return None, "Window closed"
    if full:
        return None, "Daily limit reached"
    if p.get("not_before", 0) > now:
        return None, "Waiting to retry"
    if p.get("manual_now"):
        return "manual", ""
    if p.get("score", 0) < 80:
        return None, "Available for manual selection"
    if early_carry:
        return "standout", ""
    if arrived < current:
        return ("regular", "") if regular_open else (None, "Waiting for the next regular post")
    review = p.get("editorial") or {}
    if allow_early:
        approved = _approved_standout(p)
        nominated = p.get("score", 0) >= STANDOUT_SCOUT and not review.get("decision")
        if approved or nominated:
            if not early_open:
                return None, "Waiting for the next bonus post"
            return ("standout" if approved else "standout_review"), ""
    return None, "Competing in this window"


def standings(posts, now, *, allow_early=True):
    used, pending, _ = budget(posts, now)
    current = window_start(now)
    previous = window_boundary(now, -1)
    filled = regular_filled(posts, current)
    early_open = allow_early and now >= standout_ready_at(posts)
    regular_open = now >= regular_ready_at(posts)
    rows = []
    for p in posts:
        if p["status"] not in ("scheduled", "checking"):
            continue
        kind, reason = _decision(p, now, current, previous, filled, used + pending >= LIMIT,
                                 allow_early, early_open, regular_open)
        rows.append((p, reason, kind))
    rows.sort(key=lambda row: (KINDS.index(row[2]) if row[2] else len(KINDS),
                               -row[0].get("score", 0), -row[0].get("created", 0), row[0]["id"]))
    return rows


def pick(posts, now, *, allow_early=True):
    rows = standings(posts, now, allow_early=allow_early)
    return (rows[0][0], rows[0][2]) if rows and rows[0][2] else (None, None)
