"""Full timeline review, clip limits, restart checkpoints and live triggers."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-full-scan-")
from studio import brain, creators, ctx, jobs, live, media, transcribe


class FullScan(unittest.TestCase):
    def test_late_winner_after_interruption(self):
        with ctx.as_user("viewer"), ExitStack() as stack:
            def mock(owner, attr, **opts):
                return stack.enter_context(patch.object(owner, attr, **opts))

            j = jobs.create({"type": "file", "path": "fixture", "name": "fixture"},
                            {"max_clips": 1, "range_start": 10, "range_end": 15,
                             "max_len": 10, "min_len": 5, "layout": "blur"}, enqueue=False)
            self.assertIsNone(j["options"]["range_end"])
            self.assertEqual(j["options"]["max_clips"], 0)
            self.assertEqual(j["options"]["min_len"], 15)
            # A saved job from before automatic counts retains its original selection on resume.
            j["options"]["max_clips"] = 1
            stack.enter_context(patch.object(jobs, "BLOCK", 20))
            audio = np.zeros(65 * 16000, dtype=np.int16)
            mock(media, "load_audio", return_value=audio)
            mock(media, "probe", return_value={"duration": 65, "width": 640, "height": 360})
            mock(media, "extract_audio")
            mock(media, "save_thumb", return_value=False)
            mock(transcribe, "model_ready", return_value=True)
            mock(transcribe, "transcribe", return_value=[{"w": "go", "s": 1, "e": 2}])
            mock(jobs.quality, "static_ranges", return_value=[])
            mock(jobs, "render_one", side_effect=lambda job, clip, words: clip.update(status="done"))
            seen = []
            def select(words, levels, length, *args, **kwargs):
                seen.append(length)
                if len(seen) == 2:
                    raise InterruptedError()
                if len(seen) == 5:
                    return [], {"model": None, "failed": [[0, length]]}
                score = 99 if len(seen) == 6 else 72
                return [{"start": 3, "end": 18, "score": score}], {"model": "test"}
            mock(brain, "find_clips", side_effect=select)
            with self.assertRaises(InterruptedError):
                jobs._run_job(j)
            self.assertEqual(j["clips"], [])
            job = json.loads((Path(j["dir"]) / "job.json").read_text())
            self.assertEqual(job["scan"]["through"], 20)
            job["resume"] = True
            jobs._run_job(job)
            self.assertEqual(seen, [20, 35, 35, 35, 20, 20])
            self.assertEqual(job["scan"]["checked"], 4)
            self.assertEqual(job["scan"]["through"], 65)
            self.assertEqual(job["scan"]["gaps"], [])
            self.assertEqual([(c["start"], c["end"], c["score"]) for c in job["clips"]], [(48, 63, 99)])
            self.assertEqual(len(json.loads((Path(j["dir"]) / "words.json").read_text())), 4)

    def test_sparse_speech_and_chat_without_speech(self):
        words = [{"w": "whoa", "s": 10, "e": 11}]
        with patch.object(brain, "ask_ai", return_value=([], "test", 1)) as judge:
            brain.find_clips(words, np.full(30, -35), 30)
            self.assertIn("whoa", judge.call_args.args[0])
            brain.find_clips([], np.full(30, -35), 30, chat=lambda s, e: s == 16)
            self.assertIn("(CHAT) (no speech)", judge.call_args.args[0])
            _, info = brain.find_clips([], np.full(30, -100), 30)
            self.assertTrue(info["quiet"])
            self.assertEqual(judge.call_count, 2)
        with patch.object(brain, "ask_ai", side_effect=RuntimeError("offline")):
            _, info = brain.find_clips(words, np.zeros(30), 30, log=lambda m: None)
            self.assertEqual(info["failed"], [[0, 30]])

    def test_vod_keeps_every_distinct_pick_and_accepts_an_empty_answer(self):
        with ctx.as_user("viewer"):
            job = jobs.create({"type": "file", "path": "fixture", "name": "fixture"}, {}, enqueue=False)
        self.assertEqual(job["options"]["max_clips"], 0)
        moments = [{"start": t, "end": t + 9, "score": 85} for t in range(10, 510, 20)]
        words = [{"w": "payoff!", "s": c["start"] + 2, "e": c["start"] + 3} for c in moments]
        levels = np.full(520, -32)
        with patch.object(brain, "ask_ai", return_value=(moments + [moments[2]], "test", 1)) as judge:
            picks, _ = brain.find_clips(words, levels, 520, min_len=5, max_len=12)
            self.assertIn("every distinct moment", judge.call_args.args[0])
        self.assertEqual(len(picks), 25)
        # The same payoff can be returned by two overlapping review windows.
        selected = jobs._best_candidates(picks + [dict(picks[0], score=84)], [], job["options"]["max_clips"])
        self.assertEqual(selected, picks)
        with patch.object(brain, "ask_ai", return_value=([], "test", 1)):
            self.assertEqual(brain.find_clips(words, levels, 520)[0], [])
        self.assertEqual(brain.fallback_by_loudness(levels, words, 0, 30, 520), [])

    def session(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        j = {"dir": directory.name, "user": "viewer", "creator": "", "title": "Stream",
             "uploader": "", "ai": {}, "clips": [], "id": "test", "source": {"name":"stream"}, "options":{}}
        return live.Session(j, dict(creators.DEFAULTS, ai_every_min=1))

    def test_video_gap_survives_segment_indexing_and_cannot_be_concatenated(self):
        s = self.session()
        s.job["part_offsets"] = {"1": 0, "2": 100}
        first, second = "p01_00005.ts", "p01_00006.ts"
        for name in (first, second, "p02_00000.ts"):
            (s.buf / name).touch()
        s._index_rows(1, [[first, "24.0", "30.334"], [second, "66.784", "86.0"]])
        s._index_rows(2, [["p02_00000.ts", "0.0", "20.0"]])
        self.assertEqual([(a, b) for _, a, b in s.segments], [(24.0,30.334), (66.784,86.0), (100.0,120.0)])
        self.assertEqual(s.seg_clock, 120.0)
        self.assertEqual(live._cut_segments(s.segments, 28.0, 70.0), [])
        picked = live._cut_segments(s.segments, 67.0, 84.0)
        self.assertEqual([p.name for p, _, _ in picked], [second])

    def idle(self, s):
        end = time.monotonic() + 5
        while s.ai_busy and time.monotonic() < end:
            time.sleep(.01)
        self.assertFalse(s.ai_busy)

    def test_busy_trigger_does_not_skip_regular_windows_or_final_tail(self):
        s = self.session()
        gate = threading.Event()
        calls = []
        def judge(window, why):
            calls.append(window)
            gate.wait(3)
        s._ai_check = judge
        s.t_done, s.spike = 50, {"t": 20, "why": "chat"}
        s._schedule_review()
        s.t_done = 186.25
        s._schedule_review()
        self.assertEqual(s.scan["through"], 0)
        gate.set()
        self.idle(s)
        for _ in range(4):
            s._schedule_review(final=True)
            self.idle(s)
        self.assertEqual(calls, [(0, 40), (0, 60), (0, 120), (30, 180), (90, 186.25)])
        self.assertEqual(s.scan, {"through": 186.25, "checked": 4, "gaps": []})

    def test_hourly_limit_only_affects_output(self):
        s = self.session()
        s.settings["live_per_hour"] = 1
        s.job["clips"] = [{"start": 0, "end": 10, "score": 99, "made_at": time.time()}]
        s.words = [{"s": 200, "e": 201, "w": "yes!"}]
        s.db = [-30] * 250
        c = {"start": 200, "end": 225, "score": 98}
        with patch.object(brain, "judge_transcript", return_value=([c], 5)) as words, \
                patch.object(brain, "judge_signals", return_value=([c], 4)) as signals:
            s._ai_check((190, 240))
        self.assertTrue(words.called and signals.called)
        self.assertEqual(len(s.job["clips"]), 1)
        self.assertIn("limit", s.verdicts[-1]["result"])

    def test_live_keeps_back_to_back_highlights_and_applies_both_judges(self):
        s = self.session()
        s.settings.update(min_len=15, max_len=19)
        moments = [{"start": t, "end": t + 16, "score": 90, "reason": "payoff"} for t in range(5, 170, 20)]
        moments[-2]["score"] = 30
        signals = moments[:-1]
        s.words = [{"w": "wow!", "s": c["start"], "e": c["start"] + 1} for c in moments]
        s.db = [-30] * 190
        s._add_clip = s.job["clips"].append
        with patch.object(brain, "ask_ai", side_effect=[(moments, "test", 5), (signals, "test", 4),
                                                       (moments, "test", 5), (signals, "test", 4)]) as judge:
            s._ai_check((0, 190))
            self.assertIn("every distinct moment", judge.call_args_list[0].args[0])
            self.assertIn("each distinct reaction", judge.call_args_list[1].args[0])
            self.assertEqual(len(s.job["clips"]), 7)
            s._ai_check((0, 190))
        self.assertEqual(len(s.job["clips"]), 7)

    def test_retries_unavailable_live_window_and_records_gap(self):
        s = self.session()
        s.t_done = 123.5
        def judge(window, why):
            return window[1] > 60
        s._ai_check = judge
        for _ in range(4):
            s._schedule_review(final=True)
            self.idle(s)
        self.assertEqual(s.scan["through"], 123.5)
        self.assertEqual(s.scan["gaps"], [[0, 60]])

    def test_stop_drains_last_fraction_and_restart_reads_unreviewed_audio(self):
        s = self.session()
        s.samples = 59 * live.SR
        s.scan["through"] = 59.
        s._keep_audio(bytes(2 * (live.SR + live.SR // 4)))
        s.ended = True
        s._ai_check = lambda *args: None
        with patch.object(transcribe, "transcribe", return_value=[]), patch.object(jobs, "submit_live", side_effect=lambda f:f()), \
                patch.object(live.alerts, "notify"):
            # Real recordings have complete minute files up to the cursor.
            part = s.buf / "audio-0000000000.pcm"
            part.write_bytes(bytes(2 * 59 * live.SR) + part.read_bytes())
            s._recover_audio()
            s.stop()
            s._analyze()
        self.assertEqual(s.scan["through"], 60.25)
        self.assertFalse(s.scan["gaps"])
        self.assertTrue(s.finished)


if __name__ == "__main__":
    unittest.main(verbosity=2)
