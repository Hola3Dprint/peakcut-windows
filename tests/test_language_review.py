"""Publishing must only see a checked copy, regardless of how the post was queued."""
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, ".")


def main(root):
    os.environ["PEAKCUT_DATA"] = str(root)
    from PIL import Image
    from studio import language, posting, publish_review as review

    check = TestCase()
    def words(text):
        return [dict(w=w, s=i*.25, e=(i+1)*.25) for i, w in enumerate(text.split())]

    names = language.caption_words(words("Jinxy's spawn peak!"), "Jynxzi Rainbow Six Siege")
    check.assertEqual(names, [dict(w="Jynxzi's", s=0., e=.25), dict(w="spawn peek!", s=.25, e=.75)])
    check.assertEqual(language.caption_words(words("jinxy Jinx assassin"), "League of Legends"), words("jinxy Jinx assassin"))
    check.assertEqual(language.mask("class assassin Nigeria"), "class assassin Nigeria")
    check.assertEqual(language.mask("He’ll whiff and he'll blame lag."), "He’ll whiff and he'll blame lag.")
    check.assertFalse(language.inspect_words(words("Do not use the N-word."), 0, 4)[3])
    check.assertEqual(language.mask("Oh sh1t! What a fuckup."), "Oh ****! What a ****.")
    check.assertEqual(language.mask("f**k"), "****")
    masked, masked_count, banned, unclear = language.inspect_words(words("okay f u c k then"), 0, 1.5)
    check.assertEqual([w["w"] for w in masked], ["okay", "****", "****", "****", "****", "then"])
    check.assertTrue(masked_count == 4 and not banned and not unclear)
    check.assertTrue(language.inspect_words(words("quoted n1gg3r"), 0, 1)[2])
    check.assertTrue(language.inspect_words(words("voice [ __ ]"), 0, 2)[3])
    print("PASS dictionary spelling, word timing, four stars, slurs and unclear source words")

    with (patch.object(review.subprocess, "run") as decode,
          patch.object(review.transcribe, "transcribe") as hear):
        decode.return_value.stdout = bytes(32000)
        hear.return_value = words("Oh shit")
        check.assertEqual(review._audio_text(root / "sample.mp4"), "Oh shit")
        hear.return_value = words("A n1gg3r")
        with check.assertRaises(review.Block):
            review._audio_text(root / "sample.mp4")
        hear.return_value = words("[inaudible]")
        with check.assertRaises(review.Hold):
            review._audio_text(root / "sample.mp4")
    print("PASS ordinary spoken profanity is allowed and slurs and unclear speech are held")

    folder = root / "clips"
    folder.mkdir()
    video = folder / "clip1.mp4"
    video.write_bytes(b"rendered video")
    clip = dict(id="clip1", file=video.name, version=1, status="done", review_speech="We got lucky!",
                title="Who saw that shit?", description="Wow", hashtags=["Gaming", "HolyShit"])
    job = dict(id="job12345678", dir=str(root), creator="test", clips=[clip])
    creator = {"post_footer": "Damn.", "privacy": "private"}
    check.assertIsNone(review.local_check(clip))
    with patch.object(review, "_screen") as screen:
        with review.checked_upload(job, clip, creator) as (file, *_):
            check.assertEqual(file.read_bytes(), video.read_bytes())
        check.assertEqual(screen.call_args.args[2], clip["review_speech"])
        check.assertEqual(clip["language_review"]["state"], "pending")
        check.assertEqual(clip["language_review"]["digest"], language.file_hash(video))
    old = {**clip, "language_review":{"policy":1,"state":"blocked","reason":"Earlier content decision"}}
    with check.assertRaisesRegex(review.Hold, "Earlier content decision"):
        review.local_check(old)
    old = dict(clip, start=.25, end=.75)
    del old["language_review"]
    del old["review_speech"]
    (root/"words.json").write_text(json.dumps(words("before nice shot after")))
    with patch.object(review, "_screen") as screen, review.checked_upload(job, old, creator):
        check.assertEqual(screen.call_args.args[2], "nice shot")
    (root/"words.json").unlink()
    old.pop("language_review")
    old.pop("review_speech")
    with (patch.object(review, "_audio_text", return_value="Sound from the saved video"),
          patch.object(review, "_screen") as screen, review.checked_upload(job, old, creator)):
        check.assertEqual(screen.call_args.args[2], "Sound from the saved video")
    ass = video.with_suffix(".ass")
    ass.write_text(r"Dialogue: 0,0,1,Cap,,0,0,0,,{\an5}Holy shit!")
    old.pop("language_review")
    with patch.object(review, "_screen") as screen, check.assertRaisesRegex(review.Block, "uncensored swear"):
        with review.checked_upload(job, old, creator):
            pass
    screen.assert_not_called()
    ass.unlink()
    print("PASS older finished clips enter review with original or recovered speech and actual caption checks")
    age_hold = dict(id="was-queued", user="test", job=job["id"], clip=clip["id"], account="test", status="held",
                    error="Re-make this clip with the language filters before posting.", due_at=0)
    real_hold = dict(age_hold, id="keep-held", clip="another", error="Unverified speech")
    with (patch.object(posting,"_posts",[age_hold, real_hold]), patch.object(posting.jobs,"get",return_value=job),
          patch.object(posting.creators,"get",return_value={"post_gap_min":0,"daily_cap":5}),
          patch.object(posting,"_save"), patch.object(posting,"_mirror") as mirror):
        posting._restore_older_posts()
        posting._restore_older_posts()
        check.assertEqual((age_hold["status"], real_hold["status"]), ("scheduled","held"))
        mirror.assert_called_once()
        age_hold.update(status="held", error="Re-make this clip with the language filters before posting.")
        posting._posts.append(dict(age_hold, id="published", status="posted"))
        posting._restore_older_posts()
        check.assertEqual(age_hold["status"], "cancelled")
    print("PASS only age-based holds return to their existing queue and published clips are never duplicated")
    clip["language_review"] = dict(policy=language.VERSION, state="pending", digest=language.file_hash(video))
    with patch.object(review, "_screen") as screen:
        with review.checked_upload(job, clip, creator) as (file, title, caption, tags):
            check.assertEqual(file.read_bytes(), video.read_bytes())
            check.assertNotEqual(file, video)
            check.assertEqual((title, tags), ("Who saw that ****?", ["Gaming"]))
            check.assertIn("****.", caption)
            check.assertNotIn("HolyShit", caption)
        check.assertFalse(file.exists())
        screen.assert_called_once()
        video.write_bytes(b"replacement")
        with check.assertRaises(review.Hold), review.checked_upload(job, clip, creator):
            pass
        screen.assert_called_once()
        video.write_bytes(b"rendered video")
    print("PASS legacy renders, changed files, post footer, tags and exact approved upload copy")

    def sample(_cmd, **_opts):
        image = Image.new("RGB", (1080,1920))
        for index in range(1, 7):
            image.save(root / f"frames-{index:04d}.jpg")
    with (patch.object(review, "_audio_text", return_value="I heard that."),
          patch.object(review.media, "probe", return_value={"duration":16}),
          patch.object(review.media, "run", side_effect=sample),
          patch.object(review.secrets, "token_hex", return_value="abcd1234"),
          patch.object(review.usage, "record"), patch.object(review.ai, "see") as see):
        for decision, ticket, nudity, expected in [
            ("block", "ABCD1234", "clear", review.Block), ("hold", "ABCD1234", "clear", review.Hold),
            ("allow", "BAD", "clear", review.Hold), ("other", "ABCD1234", "clear", review.Hold),
            ("allow", "ABCD1234", "block", review.Block), ("allow", "ABCD1234", "uncertain", review.Hold),
            ("allow", "ABCD1234", None, review.Hold),
        ]:
            see.return_value = json.dumps(dict(decision=decision, ticket=ticket, nudity=nudity)), 0, 0
            with check.assertRaises(expected):
                review._screen(video, root, "source speech", {})
            check.assertTrue(see.call_args.kwargs["clip_review"])
        see.reset_mock()
        see.return_value = json.dumps(dict(decision="allow", ticket="ABCD1234", nudity="clear")), 0, 0
        review._screen(video, root, "source speech", {})
        check.assertEqual(see.call_count, 6)
        check.assertIn("game characters", see.call_args.args[0])
    print("PASS uncertain, blocked and unverified visual responses cannot approve a clip")
    print("PASS visible nudity blocks, uncertainty holds, and each sheet needs an explicit clearance")

    for platform in ("youtube","instagram","tiktok"):
        acc = dict(name="Test", platform=platform)
        p = dict(job=job["id"], clip=clip["id"], creator="test", account="test", account_name="Test", tries=0, status="posting")
        with ExitStack() as stack:
            for obj, name, result in [(posting.jobs,"get",job), (posting.accounts,"get",acc),
                    (posting.creators,"get",creator), (posting.creators,"log",None),
                    (posting.accounts,"settings",{"paused":False}), (posting,"_save",None), (posting,"_mirror",None)]:
                stack.enter_context(patch.object(obj, name, return_value=result))
            upload = stack.enter_context(patch.object(posting.accounts, platform+"_upload", return_value=("id","test")))
            screen = stack.enter_context(patch.object(review, "_screen", side_effect=TimeoutError()))
            clip["language_review"]["state"] = "pending"
            posting._post(p)
            check.assertEqual((p["status"], clip["language_review"]["state"]), ("held","uncertain"))
            upload.assert_not_called()
            clip["language_review"]["state"] = "pending"
            screen.side_effect = review.ReviewerUnavailable("Waiting for ChatGPT")
            posting._post(p)
            check.assertEqual((p["status"], clip["language_review"]["state"]), ("scheduled","pending"))
            upload.assert_not_called()
            screen.side_effect = review.Block("Content was flagged")
            posting._post(p)
            check.assertEqual((p["status"], clip["language_review"]["state"]), ("held","blocked"))
            upload.assert_not_called()
            clip["language_review"]["state"] = "pending"
            screen.side_effect = None
            posting._post(p)
            check.assertEqual(p["status"], "posted")
            check.assertTrue(upload.call_args.args[1].name.startswith("reviewed-"))
            upload.assert_called_once()
        print("PASS " + platform + " holds failed checks and sends only approved clips")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp:
        main(Path(temp))
