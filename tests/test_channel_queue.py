"""No external writes. Exercise the selector, its transport guard and silent visual discovery."""
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="peakcut-ranked-")
os.environ["PEAKCUT_EDITORIAL_REVIEW"] = "0"
from studio import channel_queue as queue, ctx, editorial, posting


def when(day=19, hour=10, minute=0):
    return datetime(2026, 11, day, hour, minute).timestamp()


def entry(name, score, **fields):
    return dict({"id":name, "user":"owner", "account":"shared", "platform":"youtube", "creator":name,
                 "title":name, "score":score, "created":when(), "status":"scheduled", "due_at":when(29),
                 "job":name, "clip":name, "ranked":True}, **fields)


class Selection(unittest.TestCase):
    def test_manual_block_closes_all_pending_attempts_and_stops_in_progress_review(self):
        current = entry("clip", 96, status="checking", tries=0, ranked=False)
        scheduled = dict(current, id="scheduled", account="second", status="scheduled")
        failed = dict(current, id="failed", account="third", status="failed")
        clip = dict(id="clip", status="done", version=1, language_review={}, start=10, end=28)
        job = dict(id="clip", creator="clip", clips=[clip])

        def review(*_):
            self.assertEqual(posting.set_clip_blocked(job, clip, True), {"blocked":True, "uploading":False})
            self.assertEqual([current["status"], scheduled["status"]], ["blocked", "blocked"])
            with self.assertRaisesRegex(ValueError, "blocked from posting"):
                posting.enqueue(job, clip, ["shared"], now=True)
            with self.assertRaisesRegex(ValueError, "blocked from posting"):
                posting.retry("failed")
            posting.set_clip_blocked(job, clip, False)
            self.assertNotIn("posting_blocked", clip)
            raise posting.publish_review.ReviewerUnavailable("Example interruption during review.")

        with (ctx.as_user("owner"), patch.object(posting, "_posts", [current, scheduled, failed]),
              patch.object(posting, "_save"), patch.object(posting, "_mirror"),
              patch.object(posting.jobs, "save"), patch.object(posting.jobs, "get", return_value=job),
              patch.object(posting.creators, "get", return_value={"privacy":"public"}),
              patch.object(posting.accounts, "get", return_value={"platform":"youtube", "name":"Test"}),
              patch.object(posting.accounts, "settings", return_value={"paused":False}),
              patch.object(posting.publish_review, "checked_upload", review),
              patch.object(posting.accounts, "youtube_upload") as upload):
            posting._post(current)
            self.assertEqual(current["status"], "blocked")
            self.assertEqual(scheduled["status"], "blocked")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(posting._due(when()), [])
            upload.assert_not_called()

    def test_duration_survives_final_review_and_upload(self):
        clip = dict(start=1060.9, end=1090.9, title="Crop Duster")
        self.assertEqual(editorial.revision_changes(clip, {"end":7}), {})
        self.assertEqual(editorial.revision_changes(clip, {"end":7, "audio":"normalize"}), {"level_audio":True})
        self.assertEqual(editorial.revision_changes(clip, {"end":15}), {"start":1060.9, "end":1075.9})
        # A long audio track cannot make a short video qualify.
        with (patch.object(editorial.media, "probe", return_value={"duration":15.01, "video_duration":14.97}),
              patch.object(editorial, "finished_speech") as speech,
              patch.object(posting.publish_review, "_audio_text") as content):
            result = editorial.score_cut("export.mp4", "Crop Duster", verify_media=True)
            self.assertEqual(result["decision"], "skip")
            with self.assertRaisesRegex(posting.publish_review.ShortClip, "at least 15 seconds"):
                posting.publish_review._screen("export.mp4", None, "", {})
            speech.assert_not_called()
            content.assert_not_called()

    def test_best_from_just_closed_window_and_reset(self):
        old = entry("old", 100, created=when(hour=8))
        first = entry("first", 81)
        better = entry("better", 89, created=when(hour=11, minute=59))
        new = entry("new", 88, created=when(hour=12))
        self.assertEqual(queue.pick([first, better], when(hour=11, minute=59)), (None, None))
        self.assertEqual(queue.pick([old, first, better, new], when(hour=12)), (better, "regular"))
        posted = dict(better, status="posted", posted_at=when(hour=12, minute=4),
                      post_kind="regular", release_window=when(hour=12))
        rows = queue.standings([posted, first, new], when(hour=12, minute=5))
        self.assertIn((first, "Window closed", None), rows)
        self.assertEqual(queue.pick([posted, first, new], when(hour=15)), (None, None))
        self.assertEqual(queue.pick([posted, first, new], when(hour=15, minute=4)), (new, "regular"))

    def test_standout_needs_92_after_review_and_keeps_regular_slot(self):
        now = when(hour=12, minute=30)
        regular = entry("regular", 87, status="posted", posted_at=when(hour=12, minute=8),
                        post_kind="regular", release_window=when(hour=12))
        future = entry("candidate", 94, created=when(hour=12, minute=15))
        self.assertEqual(queue.pick([regular, future], now), (future, "standout_review"))
        future["editorial"] = {"decision":"publish", "score":91}
        self.assertEqual(queue.pick([regular, future], now), (None, None))
        future["editorial"]["score"] = 92
        self.assertEqual(queue.pick([regular, future], now), (future, "standout"))
        bonus = dict(future, status="posted", posted_at=now, post_kind="standout")
        next_regular = entry("next pick", 87, created=when(hour=12, minute=12))
        second_bonus = entry("second bonus", 93, created=when(hour=12, minute=40),
                             editorial={"decision":"publish", "score":93})
        self.assertEqual(queue.pick([regular, bonus, second_bonus], when(hour=12, minute=50)), (None, None))
        another = dict(second_bonus, created=when(hour=15, minute=5))
        group = [regular, bonus, next_regular, another]
        self.assertEqual(queue.pick(group, when(hour=15, minute=8)), (next_regular, "regular"))
        next_regular.update(status="posted", posted_at=when(hour=15, minute=8),
                            post_kind="regular", release_window=when(hour=15))
        self.assertEqual(queue.pick(group, when(hour=15, minute=29)), (None, None))
        self.assertEqual(queue.pick(group, when(hour=15, minute=30)), (another, "standout"))

    def test_uncertain_bonus_transport_keeps_its_slot(self):
        first = entry("first", 98, status="failed", post_kind="standout", transport_at=when(hour=11, minute=58))
        another = entry("second", 95, created=when(hour=12, minute=2),
                        editorial={"decision":"publish", "score":96})
        self.assertEqual(queue.pick([first, another], when(hour=14, minute=57)), (None, None))
        self.assertEqual(queue.pick([first, another], when(hour=14, minute=58)), (another, "standout"))

    def test_losers_go_to_library_without_removing_clips(self):
        old = entry("old", 93, created=when(hour=8))
        fresh = entry("fresh", 88, created=when(hour=11))
        chosen = entry("manual", 82, created=when(hour=8), manual_now=True)
        with (patch.object(posting, "_posts", [old, fresh, chosen]), patch.object(posting, "_remote", {}),
              patch.object(posting, "_save"), patch.object(posting, "_mirror") as mirror):
            posting._expire_windows(when(hour=12))
            self.assertEqual(old["status"], "library")
            self.assertTrue(old["window_closed"])
            self.assertEqual(fresh["status"], "scheduled")
            self.assertEqual(chosen["status"], "scheduled")
            mirror.assert_called_once_with(old)

    def test_cap_rollover_isolation_retries_and_pacing(self):
        pool = [entry(str(i), 90, status="posted", posted_at=when()) for i in range(5)]
        pending = entry("ambiguous", 88, status="failed", transport_at=when())
        manual = entry("manual", 60, manual_now=True)
        early = entry("early", 95, editorial={"decision":"publish", "score":93})
        self.assertEqual(queue.pick(pool + [pending, manual, early], when()), (None, None))
        self.assertEqual(queue.pick(pool + [pending, manual], when(20, 0)), (manual, "manual"))
        late = entry("late", 94, not_before=when(hour=14))
        other = entry("alt", 90, account="other", created=when(hour=8))
        with patch.object(posting, "_posts", pool + [late, other, manual]):
            self.assertEqual(set(p["id"] for p in posting._due(when())), {"alt", "manual"})

    def test_another_winner_or_external_upload_during_review(self):
        current = entry("first", 91, status="checking", tries=0, created=when(hour=8))
        group = [current]
        job = dict(creator="first", clips=[dict(id="first", status="done", version=1, language_review={})])
        @contextmanager
        def review(*_):
            yield "test.mp4", "title", "", []
        with (ctx.as_user("owner"), patch.object(posting, "_posts", group), patch.object(posting, "_remote", {}),
              patch.object(posting.jobs, "get", return_value=job), patch.object(posting, "_score_finished") as score,
              patch.object(posting.creators, "get", return_value={"privacy":"public"}),
              patch.object(posting.accounts, "get", return_value=dict(id="shared",platform="youtube")),
              patch.object(posting.accounts, "settings", return_value={"paused":False}),
              patch.object(posting.publish_review, "checked_upload", review), patch.object(posting, "_mirror"),
              patch.object(posting, "_save"), patch.object(posting.accounts, "youtube_upload") as upload,
              patch.object(posting.accounts, "youtube_uploads_today") as recent,
              patch.object(posting.time, "time", return_value=when())):
            recent.side_effect = lambda _: group.append(entry("better", 96, created=when(hour=8, minute=30))) or []
            posting._post(current)
            self.assertEqual(current["status"], "scheduled")
            score.assert_not_called()
            group.pop()
            recent.side_effect = None
            recent.return_value = [dict(remote_id=str(i), posted_at=when()) for i in range(6)]
            current["status"] = "checking"
            posting._post(current)
            self.assertEqual(current["status"], "scheduled")
            upload.assert_not_called()
            score.assert_not_called()

    def test_silent_visual_payoff_with_delayed_chat(self):
        item = dict(start=10, end=32, score=92, title="Who let him drive?", reason="The car lands on its roof.")
        with patch.object(editorial, "_ask", return_value=({"clips":[item, dict(item, end=95)]}, 24)) as judge:
            picks, spent = editorial.scout([("file", 0, 60)], 0, 60, [], [27], 12, 40, None)
            self.assertEqual([p["title"] for p in picks], [item["title"]])
            self.assertTrue(picks[0]["visual_evidence"])
            self.assertIn(25, judge.call_args.args[1])
            self.assertEqual(spent, 24)

    def test_second_reviewer_vetoes_strong_score_and_requires_evidence(self):
        first = dict(decision="publish", score=93, reason="A well-timed crash.", payoff_at=16)
        second = dict(decision="rework", reason="The steering wheel blocks the payoff in the crop.")
        with (patch.object(editorial.media, "probe", return_value={"duration":30}),
              patch.object(editorial, "finished_speech", return_value=[{"s":2, "e":3, "w":"Watch this."}]),
              patch.object(editorial, "_ask", side_effect=[(first, 12), (second, 10)]) as ask):
            out = editorial.score_cut("edit.mp4", "Can he make it?")
            self.assertEqual((out["score"], out["decision"], out["reason"]), (93, "rework", second["reason"]))
            self.assertIn(16.5, ask.call_args.args[1])
        with (patch.object(editorial.media, "probe", return_value={"duration":30}),
              patch.object(editorial, "finished_speech", return_value=[]),
              patch.object(editorial, "_ask", return_value=(dict(first, payoff_at=70), 15))):
            with self.assertRaises(ValueError):
                editorial.score_cut("edit.mp4", "Test")

    def test_game_dialogue_cannot_pass_as_the_streamers_payoff(self):
        report = dict(decision="publish", score=95, basis="creator_voice", reason="Funny line.", payoff_at=10)
        words = [{"s":10, "e":11, "w":"Watch this.", "speaker":"other"}]
        with (patch.object(editorial.media, "probe", return_value={"duration":30}),
              patch.object(editorial, "finished_speech", return_value=words),
              patch.object(editorial, "_ask", return_value=(report, 15)) as ask):
            result = editorial.score_cut("edit.mp4", "Test", stream_layout=True)
            self.assertEqual(result["decision"], "rework")
            ask.assert_called_once()
        self.assertTrue(editorial.supports_payoff("independent_play", words, 10))
        words[0]["speaker"] = "creator"
        self.assertTrue(editorial.supports_payoff("creator_voice", words, 10))

    def test_finalist_revision_is_reviewed_and_only_passed_bytes_ship(self):
        with tempfile.TemporaryDirectory() as td, ctx.as_user("owner"):
            root = Path(td)
            clips = root / "clips"
            clips.mkdir()
            original = clips / "test.mp4"
            original.write_bytes(b"original")
            clip = dict(id="finalist", status="done", file=original.name, start=60, end=90,
                        title="Original title", version=1, language_review={})
            job = dict(id="finalist", dir=td, creator="one", clips=[clip])
            p = entry("finalist", 93, status="checking", tries=0)
            regular = entry("regular", 85, status="posted", posted_at=when(hour=9, minute=5),
                            post_kind="regular", release_window=when(hour=9))
            first = dict(decision="rework", score=83, reason="End after the reaction.",
                         edit={"end":23.5, "title":"A better hook"})

            def repair(job, original, changes, folder):
                if original["file"] == "test.mp4":
                    self.assertEqual(changes, dict(start=60, end=83.5, title="A better hook"))
                    name = "first-revision.mp4"
                else:
                    self.assertEqual(changes, dict(start=60, end=81))
                    name = "second-revision.mp4"
                (folder / name).write_bytes(name.encode())
                return dict(original, **changes, file=name)

            def keep(job, clip, original, draft, folder):
                (folder / draft["file"]).replace(clips / draft["file"])
                clip.update(draft, version=2)
                return True

            @contextmanager
            def check_upload(job, clip, creator):
                self.assertEqual(clip["version"], 2)
                yield clips / clip["file"], clip["title"], "", []

            with ExitStack() as mocks:
                for mock in (
                    patch.object(posting, "_posts", [regular, p]), patch.object(posting, "_remote", {}),
                    patch.object(posting.time, "time", return_value=when(hour=10, minute=15)),
                    patch.object(posting, "_save"), patch.object(posting, "_mirror"),
                    patch.dict(posting.jobs._jobs, {"finalist": job}), patch.object(posting.jobs, "save"),
                    patch.object(posting.jobs, "get", return_value=job),
                    patch.object(posting.jobs, "prepare_revision", side_effect=repair),
                    patch.object(posting.jobs, "keep_revision", side_effect=keep),
                    patch.object(posting.creators, "get", return_value={"privacy":"public"}),
                    patch.object(posting.creators, "log"),
                    patch.object(posting.accounts, "get", return_value=dict(id="shared", platform="youtube", name="test")),
                    patch.object(posting.accounts, "settings", return_value={"paused":False}),
                    patch.object(posting.accounts, "youtube_uploads_today", return_value=[]),
                    patch.object(posting.publish_review, "local_check"),
                    patch.object(posting.publish_review, "checked_upload", check_upload),
                    patch.object(posting.voices, "profile", return_value=None),
                    patch.object(editorial, "enabled", return_value=True),
                ):
                    mocks.enter_context(mock)
                upload = mocks.enter_context(patch.object(posting.accounts, "youtube_upload", return_value=("id", "url")))
                score = mocks.enter_context(patch.object(editorial, "score_cut", side_effect=[first,
                    dict(decision="rework", score=84, reason="Trim the last pause.", edit={"end":21}),
                    dict(decision="publish", score=92, reason="The ending lands.")]))
                posting._post(p)
                self.assertEqual(p["status"], "posted", p.get("error"))
                self.assertEqual(p["post_kind"], "standout")
                self.assertEqual(p["editorial"]["revision"], "passed")
                self.assertEqual(p["editorial"]["revision_attempts"], 2)
                self.assertEqual(p["score"], 93)
                self.assertEqual(score.call_count, 3)
                self.assertEqual(upload.call_args.args[1].read_bytes(), b"second-revision.mp4")
                self.assertEqual(original.read_bytes(), b"original")
                self.assertEqual(posting.jobs.reserve_revision(job, clip, dict(clip)), 0)


if __name__ == "__main__":
    unittest.main()
