"""
يحوّل JSON (سكربت + مشاهد) إلى فيديو ريلز 9:16 كامل:
صوت (edge-tts) + صور (Pollinations) + ترجمة عربية + FFmpeg
الاستخدام: SCRIPT_JSON='{...}' python generate.py
"""
import os, json, asyncio, subprocess, urllib.parse, time, random
from pathlib import Path

import requests
import edge_tts

VOICE = os.getenv("TTS_VOICE", "ar-EG-ShakirNeural")  # بدائل: ar-EG-SalmaNeural, ar-SA-HamedNeural
W, H, FPS = 1080, 1920, 30
PAD = 0.45          # فاصل صغير بعد كل مشهد (ثانية)
WORDS_PER_SUB = 4   # عدد كلمات الترجمة في الشاشة الواحدة
FONT = "Noto Sans Arabic"

OUT = Path("output")
TMP = OUT / "tmp"
TMP.mkdir(parents=True, exist_ok=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        raise RuntimeError("command failed: " + " ".join(cmd[:4]))
    return r.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)])
    return float(out.strip())


async def tts(text, path):
    for attempt in range(4):
        try:
            await edge_tts.Communicate(text, VOICE, rate="-5%").save(str(path))
            if Path(path).stat().st_size > 1000:
                return
        except Exception as e:
            print("tts retry", attempt, e)
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError("TTS failed")


def fallback_image(path):
    colors = ["0x1b1b2f", "0x162447", "0x1f4068", "0x2d132c"]
    run(["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"color=c={random.choice(colors)}:s={W}x{H}", "-frames:v", "1", str(path)])


def fetch_image(prompt, seed, path):
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
           f"?width={W}&height={H}&nologo=true&model=flux&seed={seed}")
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=120)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and ctype.startswith("image") and len(r.content) > 10_000:
                Path(path).write_bytes(r.content)
                return
            print("image retry", attempt, r.status_code, ctype)
        except Exception as e:
            print("image retry", attempt, e)
        time.sleep(5 * (attempt + 1))
    print("!! using fallback image")
    fallback_image(path)


def ass_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_subs(text, start, dur):
    """يقسّم النص لمجموعات كلمات ويوزّع الوقت حسب عدد الحروف."""
    words = text.split()
    groups = [" ".join(words[i:i + WORDS_PER_SUB]) for i in range(0, len(words), WORDS_PER_SUB)]
    total = sum(len(g) for g in groups) or 1
    speech = max(dur - PAD, 0.5)
    t = start
    lines = []
    for g in groups:
        d = speech * len(g) / total
        lines.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + d)},Default,,0,0,0,,{g}")
        t += d
    return lines


ASS_HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT},74,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,7,2,2,80,80,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def make_clip(img, audio, dur, out):
    frames = int(dur * FPS) + 1
    vf = (f"scale=1296:2304:force_original_aspect_ratio=increase,crop=1296:2304,"
          f"zoompan=z='min(zoom+0.0007,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          f"d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
    run(["ffmpeg", "-y", "-i", str(img), "-i", str(audio),
         "-vf", vf, "-af", "apad", "-t", f"{dur:.2f}", "-r", str(FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k", str(out)])


def main():
    data = json.loads(os.environ["SCRIPT_JSON"])
    scenes = data["scenes"]
    style = data.get("style", "cinematic, dramatic lighting, highly detailed, vertical 9:16")
    base_seed = random.randint(1, 10**6)

    clips, subs, offset = [], [], 0.0
    for i, sc in enumerate(scenes, 1):
        print(f"--- scene {i}/{len(scenes)}")
        audio = TMP / f"a{i}.mp3"
        img = TMP / f"i{i}.jpg"
        clip = TMP / f"c{i}.mp4"

        asyncio.run(tts(sc["narration"], audio))
        dur = duration(audio) + PAD
        fetch_image(f"{sc['image_prompt']}, {style}", base_seed + i, img)
        make_clip(img, audio, dur, clip)

        subs += build_subs(sc["narration"], offset, dur)
        offset += dur
        clips.append(clip)

    (TMP / "list.txt").write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    (TMP / "subs.ass").write_text(ASS_HEADER + "\n".join(subs) + "\n", encoding="utf-8")

    joined = TMP / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(TMP / "list.txt"),
         "-c", "copy", str(joined)])

    final = OUT / "video.mp4"
    run(["ffmpeg", "-y", "-i", str(joined), "-vf", f"ass={TMP / 'subs.ass'}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", str(final)])

    meta = {
        "title": data.get("title", ""),
        "caption": data.get("caption", ""),
        "hashtags": data.get("hashtags", []),
        "duration": round(offset, 1),
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print("DONE", final, meta)


if __name__ == "__main__":
    main()
