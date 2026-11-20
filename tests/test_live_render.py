"""A live clip keeps both halves of a stream that changes resolution."""
import subprocess
import tempfile
from pathlib import Path

from studio import framing, render


def ffmpeg(directory, *args):
    return subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args], cwd=directory,
                          check=True, capture_output=True).stdout


with tempfile.TemporaryDirectory(prefix="peakcut-live-render-") as directory:
    work = Path(directory)
    for name, color, size in (("first.ts", "red", "320x180"), ("second.ts", "blue", "160x90")):
        ffmpeg(work, "-f", "lavfi", "-i", f"color={color}:s={size}:r=30",
               "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "2",
               "-c:v", "libx264", "-c:a", "aac", name)
    (work / "parts.txt").write_text("file 'first.ts'\nfile 'second.ts'\n", encoding="ascii")
    ffmpeg(work, "-f", "concat", "-safe", "1", "-i", "parts.txt", "-c", "copy", "mixed.src.ts")

    result = render.render_clip(work / "mixed.src.ts", work, "mixed",
                                {"start": 0.5, "end": 3.5, "layout": "stream"}, [], 320, 180)

    def gameplay_color(at):
        return ffmpeg(work, "-ss", str(at), "-i", result["file"], "-frames:v", "1",
                      "-vf", "crop=2:2:500:1300,scale=1:1,format=rgb24", "-f", "rawvideo", "pipe:1")

    before = gameplay_color(0.75)
    after = gameplay_color(2.75)
    assert len(before) == len(after) == 3, "Both halves need a decodable video frame."
    assert before[0] > before[2] and after[2] > after[0], "Show the second segment after the resolution switch."
    print("PASS a live render keeps the frames on both sides of a resolution change")

    graph = framing.filtergraph(320, 180, None, [], 0, 3, 30)
    assert "[upper" not in graph
    try:
        framing.camera_box([0, 0, 320, 180], 320, 180)
    except ValueError:
        pass
    else:
        raise AssertionError("A full gameplay frame cannot be the facecam crop.")
    cam = framing.camera_box([12, 24, 68, 60], 320, 180)
    graph = framing.filtergraph(320, 180, cam, [], 0, 3, 30, gameplay=[72, 30, 130, 124])
    assert "crop=68:60:12:24" in graph
    assert "crop=130:124:72:30" in graph
    assert "overlay=0:0" in graph and f"overlay=(W-w)/2:{framing.FACE_HEIGHT}" in graph
    print("PASS the webcam panel uses only the confirmed source crop")
