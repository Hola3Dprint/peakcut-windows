"""Portrait composition for streams. Boxes use source pixels; shot times use the VOD clock."""
import math

FACE_HEIGHT = 768
REACTION_HEIGHT = 912


def source_box(value, width, height):
    if (not isinstance(value, (tuple, list)) or len(value) != 4
            or any(type(n) is not int for n in value)):
        raise ValueError("A framing box needs four whole pixel coordinates.")
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) < 12 or x + w > width or y + h > height:
        raise ValueError("A framing box must fit inside the source picture.")
    return list(value)


def camera_box(value, width, height):
    x, y, w, h = source_box(value, width, height)
    if w > .62 * width or h > .82 * height or w * h > .34 * width * height:
        raise ValueError("The facecam crop has to be smaller than the stream view.")
    return [x, y, w, h]


def shot_list(value, width, height):
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("Choose up to eight framing shots.")
    shots = []
    for shot in value:
        if not isinstance(shot, dict):
            raise ValueError("Each framing shot needs a time and a source box.")
        at = shot.get("at")
        if (type(at) not in (int, float) or not math.isfinite(at) or at < 0
                or (shots and at <= shots[-1]["at"])):
            raise ValueError("Framing shots need increasing times on the source clock.")
        if type(shot.get("reaction", False)) is not bool:
            raise ValueError("Reaction emphasis must be on or off.")
        shots.append({"at": at, "box": source_box(shot.get("box"), width, height),
                      "reaction": shot.get("reaction", False)})
    return shots


def _crop(box):
    x, y, w, h = box
    return f"crop={w}:{h}:{x}:{y}"


def filtergraph(width, height, face, shots, start, end, fps, single=False, gameplay=None):
    backdrop = ("scale=108:192:force_original_aspect_ratio=increase,crop=108:192,"
                "boxblur=6:2,eq=brightness=-0.20:saturation=0.66,scale=1080:1920,setsar=1")
    fit = "force_original_aspect_ratio=decrease:force_divisible_by=2"
    if height >= width:
        return (f"[0:v]fps={fps},split[bg][fg];[bg]{backdrop}[b];"
                f"[fg]scale=1080:1920:{fit},setsar=1[p];[b][p]overlay=(W-w)/2:(H-h)/2[v0];")

    full = [0, 0, width, height]
    # A stream without a verified camera has one gameplay view.
    single = single or not face
    default = {"box": gameplay or full, "reaction": False}
    cuts = {0: default}
    frames = max(1, math.ceil((end - start) * fps - 1e-6))
    for shot in shots:
        frame = max(0, round((shot["at"] - start) * fps))
        if frame < frames:
            cuts[frame] = shot
    sections = sorted(cuts.items())
    labels = "".join(f"[s{i}]" for i in range(len(sections)))
    lines = [f"[0:v]fps={fps},split={len(sections)}{labels}"]
    for i, (frame, shot) in enumerate(sections):
        until = f":end_frame={sections[i+1][0]}" if i+1 < len(sections) else ""
        split = f"split=2[b{i}][g{i}]" if single else f"split=3[b{i}][u{i}][g{i}]"
        lines.append(f"[s{i}]trim=start_frame={frame}{until},setpts=PTS-STARTPTS,{split}")
        lines.append(f"[b{i}]{backdrop}[bg{i}]")
        if single:
            lines.append(f"[g{i}]{_crop(shot['box'])},scale=1080:1420:{fit},setsar=1[game{i}]")
            lines.append(f"[bg{i}][game{i}]overlay=(W-w)/2:890-h/2,setsar=1[out{i}]")
            continue
        upper_h = REACTION_HEIGHT if shot["reaction"] else FACE_HEIGHT
        lines.append(f"[u{i}]{_crop(face)},scale=1080:{upper_h}:force_original_aspect_ratio=increase,"
                     f"crop=1080:{upper_h}:(iw-ow)/2:(ih-oh)/5,setsar=1[upper{i}]")
        lines.append(f"[g{i}]{_crop(shot['box'])},scale=1080:{1920-upper_h}:{fit},setsar=1[game{i}]")
        lines.append(f"[bg{i}][upper{i}]overlay=0:0[stack{i}]")
        lines.append(f"[stack{i}][game{i}]overlay=(W-w)/2:{upper_h},setsar=1[out{i}]")
    outputs = "".join(f"[out{i}]" for i in range(len(sections)))
    lines.append(f"{outputs}concat=n={len(sections)}:v=1:a=0[v0]")
    return ";".join(lines) + ";"
