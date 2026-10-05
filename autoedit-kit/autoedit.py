#!/usr/bin/env python3
"""autoedit — 원본 영상 → 컷편집 + 자막 + 모션그래픽 + 효과음 + 쇼츠.

  python autoedit.py analyze SRC --work W [--style mystic] [--srt x.srt] [--tight normal] [--keywords a,b]
  python autoedit.py render W/plan.json [--preview] [--only long|shorts]
  python autoedit.py ref REF.mp4 [REF2.mp4 ...] --work R
  python autoedit.py check W/out/long.mp4

시간은 plan.json 안에서 모두 '원본 기준 초'. 렌더 시 컷 반영 타임라인으로 자동 변환된다.
"""
import argparse, json, math, os, re, shutil, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

KIT = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(KIT, "fonts")
SFX = os.path.join(KIT, "sfx")

TEMPO = {  # silence noise dB, min silence, pad before, pad after, merge gap
    "tight": (-34, 0.30, 0.06, 0.10, 0.18),
    "normal": (-36, 0.45, 0.10, 0.16, 0.30),
    "loose": (-38, 0.75, 0.16, 0.26, 0.50),
}


# ------------------------------------------------------------------ utils
def run(cmd, quiet=True):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit("명령 실패: " + " ".join(cmd[:6]) + " ...")
    return r


def probe(path):
    r = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    j = json.loads(r.stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    info = {"duration": float(j["format"]["duration"]), "has_audio": a is not None}
    if v:
        w, h = int(v["width"]), int(v["height"])
        rot = int((v.get("tags") or {}).get("rotate", 0) or 0)
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        if abs(rot) in (90, 270):
            w, h = h, w
        n, d = (v.get("avg_frame_rate") or "30/1").split("/")
        fps = float(n) / float(d) if float(d) else 30.0
        info.update(w=w, h=h, fps=round(fps, 3) if 10 < fps < 121 else 30.0)
    return info


def load_styles():
    with open(os.path.join(KIT, "styles.json"), encoding="utf-8") as f:
        return json.load(f)


def get_style(name):
    st = load_styles()
    base = json.loads(json.dumps(st["default"]))
    if name not in st:
        raise SystemExit(f"스타일 '{name}' 없음. 사용 가능: {[k for k in st if not k.startswith('_')]}")

    def merge(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                merge(a[k], v)
            else:
                a[k] = v
    merge(base, st[name])
    return base


def fmt_ts(t, srt=False):
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    if srt:
        return f"{h:02d}:{m:02d}:{int(s):02d},{int(round((s - int(s)) * 1000)) % 1000:03d}"
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def parse_srt(path):
    txt = open(path, encoding="utf-8-sig").read().replace("\r", "")
    cues = []
    for block in re.split(r"\n\s*\n", txt):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 10 ** len(m.group(4))
        e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 10 ** len(m.group(8))
        text = " ".join(l.strip() for l in block[m.end():].strip().split("\n") if l.strip())
        text = re.sub(r"<[^>]+>", "", text)
        if text:
            cues.append({"s": round(s, 3), "e": round(e, 3), "text": text, "words": []})
    return cues


# ------------------------------------------------------------------ transcription
def transcribe(src, work, model_name="medium"):
    wav = os.path.join(work, "audio16k.wav")
    run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("! faster-whisper 미설치 → 자막 없이 진행 (SRT를 주면 --srt 로 사용)")
        return []
    try:
        model = WhisperModel(model_name, device="auto", compute_type="int8")
    except Exception as e:
        print(f"! Whisper 모델 로드 실패({e.__class__.__name__}) → 자막 없이 진행. huggingface.co 접근 필요")
        return []
    segs, _ = model.transcribe(wav, language="ko", word_timestamps=True, vad_filter=True,
                               condition_on_previous_text=False)
    out = []
    for s in segs:
        words = [{"s": round(w.start, 3), "e": round(w.end, 3), "w": w.word.strip()} for w in (s.words or []) if w.word.strip()]
        out.append({"s": round(s.start, 3), "e": round(s.end, 3), "text": s.text.strip(), "words": words})
    return out


# ------------------------------------------------------------------ cutting
def detect_speech(src, dur, tempo):
    noise, mind, pb, pa, gap = TEMPO[tempo]
    r = subprocess.run(["ffmpeg", "-i", src, "-vn", "-af", f"silencedetect=noise={noise}dB:d={mind}", "-f", "null", "-"],
                       stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    sil, cur = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            cur = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur is not None:
            sil.append((cur, float(m.group(1)))); cur = None
    if cur is not None:
        sil.append((cur, dur))
    speech, t = [], 0.0
    for s, e in sil:
        if s > t:
            speech.append([t, s])
        t = e
    if t < dur:
        speech.append([t, dur])
    return pad_merge(speech, dur, pb, pa, gap)


def speech_from_words(cues, dur, tempo):
    _, _, pb, pa, gap = TEMPO[tempo]
    iv = []
    for c in cues:
        if c.get("words"):
            iv += [[w["s"], w["e"]] for w in c["words"]]
        else:
            iv.append([c["s"], c["e"]])
    iv.sort()
    return pad_merge(iv, dur, pb, pa, gap)


def pad_merge(iv, dur, pb, pa, gap):
    out = []
    for s, e in sorted(iv):
        s, e = max(0, s - pb), min(dur, e + pa)
        if e - s < 0.12:
            continue
        if out and s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [[round(s, 3), round(e, 3)] for s, e in out]


def intersect_keep(segments, remove):
    keep = []
    for seg in segments:
        pieces = [[seg["s"], seg["e"]]]
        for rs, re_ in remove:
            nxt = []
            for s, e in pieces:
                if re_ <= s or rs >= e:
                    nxt.append([s, e]); continue
                if rs > s:
                    nxt.append([s, rs])
                if re_ < e:
                    nxt.append([re_, e])
            pieces = nxt
        for s, e in pieces:
            if e - s >= 0.15:
                keep.append({"s": s, "e": e, "zoom": seg.get("zoom", 1.0)})
    return keep


class Timeline:
    """원본 시각 ↔ 결과물 시각 변환."""

    def __init__(self, keep):
        self.keep = keep
        self.starts, acc = [], 0.0
        for k in keep:
            self.starts.append(acc); acc += k["e"] - k["s"]
        self.duration = acc

    def out(self, t):
        for k, o in zip(self.keep, self.starts):
            if t < k["s"]:
                return o
            if t <= k["e"]:
                return o + (t - k["s"])
        return self.duration

    def inside(self, t):
        return any(k["s"] <= t <= k["e"] for k in self.keep)


# ------------------------------------------------------------------ captions
def apply_fixes(text, fixes):
    for a, b in (fixes or {}).items():
        text = text.replace(a, b)
    return text


def caption_chunks(cues, fixes, max_chars):
    """큐를 화면용 한 줄 자막 조각으로 나눈다 (원본 시각)."""
    chunks = []
    for c in cues:
        if c.get("words"):
            buf, bs = [], None
            for i, w in enumerate(c["words"]):
                if bs is None:
                    bs = w["s"]
                buf.append(w)
                text = " ".join(x["w"] for x in buf)
                nxt = c["words"][i + 1] if i + 1 < len(c["words"]) else None
                end_punct = re.search(r"[.?!,]$", w["w"])
                too_long = nxt is not None and len(text) + 1 + len(nxt["w"]) > max_chars
                gap = nxt is not None and nxt["s"] - w["e"] > 0.6
                if nxt is None or too_long or gap or (end_punct and len(text) >= max_chars * 0.45):
                    chunks.append({"s": bs, "e": w["e"], "text": apply_fixes(text, fixes)})
                    buf, bs = [], None
        else:
            text = apply_fixes(c["text"], fixes)
            parts, cur = [], ""
            for tok in text.split():
                if cur and len(cur) + 1 + len(tok) > max_chars:
                    parts.append(cur); cur = tok
                else:
                    cur = (cur + " " + tok).strip()
            if cur:
                parts.append(cur)
            total = sum(len(p) for p in parts) or 1
            t = c["s"]
            for p in parts:
                d = (c["e"] - c["s"]) * len(p) / total
                chunks.append({"s": t, "e": t + d, "text": p}); t += d
    for ch in chunks:  # 문장부호 정리 (유튜브 자막 스타일: 마침표·쉼표 제거)
        ch["text"] = re.sub(r"[.,]+$", "", ch["text"]).strip()
    return [c for c in chunks if c["text"]]


# ------------------------------------------------------------------ ASS motion graphics
def ass_color(hexc, alpha=0.0):
    hexc = hexc.lstrip("#")
    r, g, b = hexc[0:2], hexc[2:4], hexc[4:6]
    a = int(round(max(0, min(1, alpha)) * 255))
    return f"&H{a:02X}{b}{g}{r}&".upper()


def c_only(hexc):
    hexc = hexc.lstrip("#")
    return f"&H{hexc[4:6]}{hexc[2:4]}{hexc[0:2]}&".upper()


def esc(text):
    return text.replace("{", "(").replace("}", ")").replace("\n", "\\N")


def text_w(text, size):
    w = 0
    for ch in text:
        if "가" <= ch <= "힣" or ord(ch) > 0x2E80:
            w += 0.95
        elif ch == " ":
            w += 0.28
        elif ch.isupper() or ch.isdigit():
            w += 0.62
        else:
            w += 0.52
    return w * size


def rect(w, h):
    return f"m 0 0 l {w:.0f} 0 {w:.0f} {h:.0f} 0 {h:.0f}"


def rrect(w, h, r):
    r = min(r, h / 2, w / 2); k = r * 0.4477
    return (f"m {r:.0f} 0 l {w - r:.0f} 0 b {w - k:.0f} 0 {w:.0f} {k:.0f} {w:.0f} {r:.0f} "
            f"l {w:.0f} {h - r:.0f} b {w:.0f} {h - k:.0f} {w - k:.0f} {h:.0f} {w - r:.0f} {h:.0f} "
            f"l {r:.0f} {h:.0f} b {k:.0f} {h:.0f} 0 {h - k:.0f} 0 {h - r:.0f} "
            f"l 0 {r:.0f} b 0 {k:.0f} {k:.0f} 0 {r:.0f} 0")


def wrap(text, n):
    if "\n" in text or len(text) <= n:
        return text
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    return "\n".join(lines)


class ASS:
    def __init__(self, W, H, style, scale=None):
        self.W, self.H, self.st = W, H, style
        self.s = scale or (H / 1080 if W >= H else W / 1080)
        self.lines = []
        self.sfx = []  # (time, name)

    # -- low level
    def dlg(self, t0, t1, text, layer=1, style="FX"):
        self.lines.append((layer, t0, t1, style, text))

    def header(self):
        st, s = self.st, self.s
        cap = st["caption"]
        size = cap["size"] * s
        out_c = cap["box_color"] if cap["box"] else cap["outline_color"]
        out_a = cap["box_alpha"] if cap["box"] else 0
        bs = 3 if cap["box"] else 1
        return "\n".join([
            "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {self.W}", f"PlayResY: {self.H}",
            "WrapStyle: 2", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Cap,{st['fonts']['caption']},{size:.0f},{ass_color(cap['color'])},&H000000FF&,{ass_color(out_c, out_a)},"
            f"&H96000000&,0,0,0,0,100,100,0,0,{bs},{cap['outline'] * s:.1f},{cap['shadow'] * s:.1f},2,{60 * s:.0f},{60 * s:.0f},{cap['margin_v'] * s:.0f},1",
            f"Style: FX,{st['fonts']['text']},{60 * s:.0f},&H00FFFFFF&,&H000000FF&,&H00000000&,&H96000000&,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
            "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ])

    def dump(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.header() + "\n")
            for layer, t0, t1, style, text in sorted(self.lines, key=lambda x: (x[1], x[0])):
                f.write(f"Dialogue: {layer},{fmt_ts(t0)},{fmt_ts(t1)},{style},,0,0,0,,{text}\n")

    # -- captions
    def caption(self, t0, t1, text, keywords, pos=None, size=None):
        cap = self.st["caption"]
        hl = c_only(cap["highlight"])
        base = c_only(cap["color"])
        out = esc(text)
        for kw in sorted(set(k for k in keywords if k), key=len, reverse=True):
            out = re.sub(re.escape(kw), lambda m: f"{{\\c{hl}\\fscx{cap['highlight_scale']}\\fscy{cap['highlight_scale']}}}{m.group(0)}{{\\c{base}\\fscx100\\fscy100}}", out)
        tag = ""
        if cap.get("pop"):
            tag += "\\fscx94\\fscy94\\t(0,80,\\fscx100\\fscy100)" if "\\fscx1" not in out[:12] else ""
        if pos:
            tag += f"\\an5\\pos({pos[0]:.0f},{pos[1]:.0f})"
        if size:
            tag += f"\\fs{size:.0f}"
        self.dlg(t0, t1, "{" + tag + "}" + out, layer=6, style="Cap")

    # -- fx templates: 모두 (t, d, **params). 효과음은 self.sfx 로 등록
    def fx(self, typ, t, d=None, **p):
        st, s, W, H = self.st, self.s, self.W, self.H
        acc, acc2, panel = st["accent"], st["accent2"], st["panel"]
        pa = 1 - st.get("panel_alpha", 0.85)
        F = st["fonts"]
        snd = p.get("sfx", st["sfx"].get(typ))
        if snd and snd != "none":
            self.sfx.append((t, snd))

        if typ == "lower_third":
            d = d or 4.5
            name, title = p.get("name", ""), p.get("title", "")
            x0, y0 = 90 * s, H - 290 * s
            w = max(text_w(name, 58 * s), text_w(title, 36 * s)) + 90 * s
            h = 140 * s
            self.dlg(t, t + d, f"{{\\an7\\pos({x0:.0f},{y0:.0f})\\p1\\c{c_only(panel)}\\alpha&H{int(pa * 255):02X}&"
                               f"\\clip({x0:.0f},{y0:.0f},{x0 + 1:.0f},{y0 + h:.0f})\\t(80,420,\\clip({x0:.0f},{y0:.0f},{x0 + w:.0f},{y0 + h:.0f}))\\fad(0,300)}}{rrect(w, h, 14 * s)}", 2)
            self.dlg(t, t + d, f"{{\\an7\\move({x0 - 40 * s:.0f},{y0:.0f},{x0:.0f},{y0:.0f},0,220)\\p1\\c{c_only(acc)}\\fad(120,300)}}{rect(12 * s, h)}", 3)
            self.dlg(t + 0.25, t + d, f"{{\\an7\\pos({x0 + 42 * s:.0f},{y0 + 16 * s:.0f})\\fn{F['text']}\\fs{58 * s:.0f}\\c&HFFFFFF&\\fad(250,300)}}{esc(name)}", 4)
            self.dlg(t + 0.4, t + d, f"{{\\an7\\pos({x0 + 44 * s:.0f},{y0 + 86 * s:.0f})\\fn{F['caption']}\\fs{36 * s:.0f}\\c{c_only(acc)}\\fad(250,300)}}{esc(title)}", 4)

        elif typ == "chapter":
            d = d or 3.0
            num, title = p.get("num", ""), p.get("title", "")
            cx, cy = W / 2, H / 2
            self.dlg(t, t + d, f"{{\\an7\\pos(0,0)\\p1\\c&H000000&\\alpha&HFF&\\t(0,250,\\alpha&H70&)\\t({int(d * 1000) - 350},{int(d * 1000)},\\alpha&HFF&)}}{rect(W, H)}", 5)
            if num != "":
                label = f"{int(num):02d}" if str(num).isdigit() else str(num)
                self.dlg(t, t + d, f"{{\\an5\\pos({cx:.0f},{cy - 70 * s:.0f})\\fn{F['title']}\\fs{150 * s:.0f}\\c{c_only(acc)}\\bord0\\shad0"
                                   f"\\fscx140\\fscy140\\t(0,220,\\fscx100\\fscy100)\\fad(120,300)}}{esc(label)}", 7)
            lw = max(text_w(title, 84 * s) + 120 * s, 520 * s)
            self.dlg(t + 0.15, t + d, f"{{\\an7\\pos({cx - lw / 2:.0f},{cy + 22 * s:.0f})\\p1\\c{c_only(acc)}"
                                      f"\\clip({cx:.0f},0,{cx + 1:.0f},{H})\\t(0,350,\\clip({cx - lw / 2:.0f},0,{cx + lw / 2:.0f},{H}))\\fad(0,300)}}{rect(lw, 4 * s)}", 7)
            self.dlg(t + 0.25, t + d, f"{{\\an8\\move({cx:.0f},{cy + 70 * s:.0f},{cx:.0f},{cy + 52 * s:.0f},0,300)\\fn{F['text']}\\fs{84 * s:.0f}"
                                      f"\\c&HFFFFFF&\\bord{3 * s:.1f}\\3c&H000000&\\fad(200,300)}}{esc(title)}", 7)

        elif typ == "list":
            items = p.get("items", [])
            title = p.get("title", "")
            step = p.get("step", 0.45)
            d = d or max(4.0, 1.2 + step * len(items) + 2.0)
            iw = 680 * s
            ih = (120 if title else 40) * s + len(items) * 92 * s
            x0, y0 = W - iw - 80 * s, (H - ih) / 2 - 40 * s
            self.dlg(t, t + d, f"{{\\an7\\move({x0 + 60 * s:.0f},{y0:.0f},{x0:.0f},{y0:.0f},0,250)\\p1\\c{c_only(panel)}\\alpha&H{int(pa * 255):02X}&\\fad(200,300)}}{rrect(iw, ih, 22 * s)}", 2)
            y = y0 + 30 * s
            if title:
                self.dlg(t + 0.15, t + d, f"{{\\an7\\pos({x0 + 40 * s:.0f},{y:.0f})\\fn{F['text']}\\fs{50 * s:.0f}\\c{c_only(acc)}\\fad(200,300)}}{esc(title)}", 3)
                y += 92 * s
            for i, it in enumerate(items):
                ti = t + 0.5 + i * step
                self.dlg(ti, t + d, f"{{\\an7\\pos({x0 + 44 * s:.0f},{y + 14 * s:.0f})\\p1\\c{c_only(acc)}\\fscx0\\fscy0\\t(0,150,\\fscx100\\fscy100)\\fad(0,300)}}{rrect(26 * s, 26 * s, 13 * s)}", 3)
                self.dlg(ti, t + d, f"{{\\an7\\move({x0 + 110 * s:.0f},{y:.0f},{x0 + 92 * s:.0f},{y:.0f},0,200)\\fn{F['caption']}\\fs{46 * s:.0f}\\c&HFFFFFF&\\fad(150,300)}}{esc(it)}", 3)
                if i and snd:
                    self.sfx.append((ti, snd))
                y += 92 * s

        elif typ == "keyword":
            d = d or 1.8
            text = p.get("text", "")
            fs = p.get("size", 118) * s
            cx, cy = W / 2, p.get("y", 0.24) * H
            bw, bh = text_w(text, fs) + 80 * s, fs * 1.45
            self.dlg(t, t + d, f"{{\\an5\\pos({cx:.0f},{cy:.0f})\\p1\\c{c_only(acc)}\\fscx130\\fscy130\\t(0,140,\\fscx100\\fscy100)\\fad(60,220)\\org({cx:.0f},{cy:.0f})\\frz-2}}{rrect(bw, bh, 18 * s)}", 6)
            self.dlg(t, t + d, f"{{\\an5\\pos({cx:.0f},{cy + 4 * s:.0f})\\fn{F['title']}\\fs{fs:.0f}\\c{c_only(st['on_accent'])}\\bord0\\shad0"
                               f"\\fscx130\\fscy130\\t(0,140,\\fscx100\\fscy100)\\t(140,{int(d * 1000)},\\fscx104\\fscy104)\\fad(60,220)\\frz-2}}{esc(text)}", 7)

        elif typ == "stat":
            d = d or 2.6
            val, label = p.get("value", ""), p.get("label", "")
            cx, cy = W / 2, H * 0.36
            self.dlg(t, t + d, f"{{\\an5\\pos({cx:.0f},{cy:.0f})\\fn{F['title']}\\fs{190 * s:.0f}\\c{c_only(acc)}\\bord{8 * s:.0f}\\3c&H000000&\\shad0"
                               f"\\fscx60\\fscy60\\t(0,200,\\fscx105\\fscy105)\\t(200,320,\\fscx100\\fscy100)\\fad(80,250)}}{esc(val)}", 7)
            if label:
                self.dlg(t + 0.2, t + d, f"{{\\an5\\pos({cx:.0f},{cy + 140 * s:.0f})\\fn{F['text']}\\fs{54 * s:.0f}\\c&HFFFFFF&\\bord{4 * s:.0f}\\3c&H000000&\\fad(200,250)}}{esc(label)}", 7)

        elif typ == "impact":
            d = d or 1.6
            text = p.get("text", "")
            cx, cy = W / 2, H * p.get("y", 0.42)
            self.dlg(t, t + 0.35, f"{{\\an7\\pos(0,0)\\p1\\c&HFFFFFF&\\alpha&H30&\\t(0,300,\\alpha&HFF&)}}{rect(W, H)}", 8)
            shake = "".join(f"\\t({i * 45},{i * 45 + 45},\\frz{(-4 if i % 2 else 4) * (1 - i / 7):.1f})" for i in range(7))
            self.dlg(t, t + d, f"{{\\an5\\pos({cx:.0f},{cy:.0f})\\org({cx:.0f},{cy:.0f})\\fn{F['title']}\\fs{p.get('size', 170) * s:.0f}\\c&HFFFFFF&"
                               f"\\bord{11 * s:.0f}\\3c{c_only(acc2)}\\shad{6 * s:.0f}\\4c&H000000&\\fscx170\\fscy170\\t(0,120,\\fscx100\\fscy100){shake}\\fad(0,250)}}{esc(wrap(text, 10))}", 9)

        elif typ == "quote":
            d = d or 4.5
            text, by = wrap(p.get("text", ""), p.get("wrap", 18)), p.get("by", "")
            cx, cy = W / 2, H * 0.45
            self.dlg(t, t + d, f"{{\\an7\\pos(0,0)\\p1\\c{c_only(panel)}\\alpha&HFF&\\t(0,400,\\alpha&H50&)\\t({int(d * 1000) - 400},{int(d * 1000)},\\alpha&HFF&)}}{rect(W, H)}", 5)
            self.dlg(t + 0.1, t + d, f"{{\\an5\\pos({cx:.0f},{cy - 170 * s:.0f})\\fn{F['quote']}\\fs{170 * s:.0f}\\c{c_only(acc)}\\bord0\\shad0\\fad(400,400)}}“", 6)
            self.dlg(t + 0.25, t + d, f"{{\\an5\\move({cx:.0f},{cy + 16 * s:.0f},{cx:.0f},{cy:.0f},0,600)\\fn{F['quote']}\\fs{70 * s:.0f}\\c&HFFFFFF&\\bord0\\shad{2 * s:.0f}"
                                      f"\\fsp{6 * s:.0f}\\t(0,900,\\fsp{1 * s:.0f})\\fad(500,400)}}{esc(text)}", 6)
            if by:
                n = text.count("\n") + 1
                self.dlg(t + 0.7, t + d, f"{{\\an5\\pos({cx:.0f},{cy + (60 + 45 * n) * s:.0f})\\fn{F['caption']}\\fs{38 * s:.0f}\\c{c_only(acc)}\\bord0\\fad(300,400)}}— {esc(by)}", 6)

        elif typ == "cta":
            d = d or 4.2
            bx, by_ = W - 520 * s, H - 250 * s
            bw, bh = 200 * s, 78 * s
            click = int(p.get("click", 1.1) * 1000)
            self.dlg(t, t + d, f"{{\\an7\\move({bx:.0f},{by_ + 40 * s:.0f},{bx:.0f},{by_:.0f},0,250)\\p1\\c&H3030E0&\\fad(200,300)"
                               f"\\t({click},{click + 90},\\c&H555555&)}}{rrect(bw, bh, bh / 2)}", 8)
            self.dlg(t, t + click / 1000, f"{{\\an5\\move({bx + bw / 2:.0f},{by_ + bh / 2 + 40 * s:.0f},{bx + bw / 2:.0f},{by_ + bh / 2:.0f},0,250)\\fn{F['text']}\\fs{40 * s:.0f}\\c&HFFFFFF&\\fad(200,0)}}구독", 9)
            self.dlg(t + click / 1000, t + d, f"{{\\an5\\pos({bx + bw / 2:.0f},{by_ + bh / 2:.0f})\\fn{F['text']}\\fs{36 * s:.0f}\\c&HFFFFFF&\\fad(0,300)}}구독중", 9)
            lx = bx + bw + 20 * s
            self.dlg(t + 0.15, t + d, f"{{\\an7\\move({lx:.0f},{by_ + 40 * s:.0f},{lx:.0f},{by_:.0f},0,250)\\p1\\c&HFFFFFF&\\alpha&H10&\\fad(200,300)}}{rrect(220 * s, bh, bh / 2)}", 8)
            self.dlg(t + 0.15, t + d, f"{{\\an5\\move({lx + 110 * s:.0f},{by_ + bh / 2 + 40 * s:.0f},{lx + 110 * s:.0f},{by_ + bh / 2:.0f},0,250)\\fn{F['text']}\\fs{36 * s:.0f}\\c&H111111&\\fad(200,300)"
                                      f"\\t({click + 500},{click + 600},\\fscx115\\fscy115)\\t({click + 600},{click + 700},\\fscx100\\fscy100)}}좋아요", 9)
            msg = p.get("text", "")
            if msg:
                self.dlg(t + 0.3, t + d, f"{{\\an3\\pos({bx + 420 * s:.0f},{by_ - 20 * s:.0f})\\fn{F['caption']}\\fs{36 * s:.0f}\\c&HFFFFFF&\\bord{3 * s:.0f}\\fad(200,300)}}{esc(msg)}", 9)
            self.sfx.append((t + click / 1000 + 0.55, "pop"))

        elif typ == "text":
            d = d or 3.2
            text = p.get("text", "")
            top = p.get("pos", "top") == "top"
            fs = 40 * s
            w, h = text_w(text, fs) + 70 * s, 76 * s
            x0, y0 = 70 * s, (70 * s if top else H - 330 * s)
            self.dlg(t, t + d, f"{{\\an7\\pos({x0:.0f},{y0:.0f})\\p1\\c{c_only(panel)}\\alpha&H{int(pa * 255):02X}&\\fad(150,250)}}{rrect(w, h, 12 * s)}", 2)
            self.dlg(t, t + d, f"{{\\an7\\pos({x0:.0f},{y0:.0f})\\p1\\c{c_only(acc)}\\fad(150,250)}}{rect(8 * s, h)}", 3)
            self.dlg(t, t + d, f"{{\\an4\\pos({x0 + 34 * s:.0f},{y0 + h / 2:.0f})\\fn{F['caption']}\\fs{fs:.0f}\\c&HFFFFFF&"
                               f"\\clip({x0:.0f},0,{x0 + 1:.0f},{H})\\t(0,{min(900, 45 * len(text))},\\clip({x0:.0f},0,{x0 + w:.0f},{H}))\\fad(0,250)}}{esc(text)}", 3)

        elif typ == "intro":
            d = d or 3.0
            title, sub = p.get("title", ""), p.get("sub", "")
            cx, cy = W / 2, H * 0.44
            self.dlg(t, t + d, f"{{\\an7\\pos(0,0)\\p1\\c{c_only(panel)}\\alpha&H40&\\t({int(d * 1000) - 400},{int(d * 1000)},\\alpha&HFF&)}}{rect(W, H)}", 5)
            self.dlg(t, t + d, f"{{\\an5\\pos({cx:.0f},{cy:.0f})\\fn{F['title']}\\fs{p.get('size', 128) * s:.0f}\\c&HFFFFFF&\\bord{6 * s:.0f}\\3c&H000000&\\shad0"
                               f"\\fsp{40 * s:.0f}\\t(0,500,\\fsp0)\\fad(200,350)}}{esc(wrap(title, 12))}", 7)
            n = wrap(title, 12).count("\n") + 1
            lw = 360 * s
            ly = cy + (60 + 62 * (n - 1)) * s + 30 * s
            self.dlg(t + 0.3, t + d, f"{{\\an7\\pos({cx - lw / 2:.0f},{ly:.0f})\\p1\\c{c_only(acc)}\\clip({cx:.0f},0,{cx + 1:.0f},{H})\\t(0,350,\\clip({cx - lw / 2:.0f},0,{cx + lw / 2:.0f},{H}))\\fad(0,350)}}{rect(lw, 5 * s)}", 7)
            if sub:
                self.dlg(t + 0.45, t + d, f"{{\\an8\\pos({cx:.0f},{ly + 28 * s:.0f})\\fn{F['text']}\\fs{52 * s:.0f}\\c{c_only(acc)}\\bord{3 * s:.0f}\\3c&H000000&\\fad(250,350)}}{esc(sub)}", 7)

        elif typ == "image":
            pass  # 비디오 합성 단계(ffmpeg overlay)에서 처리, 여기서는 효과음만
        else:
            print(f"! 알 수 없는 fx type: {typ}")

    def headline(self, t0, t1, text, highlight, y, size, color, hl_color):
        s = self.s
        out = esc(text)
        for kw in [h for h in (highlight if isinstance(highlight, list) else [highlight]) if h]:
            out = out.replace(esc(kw), f"{{\\c{c_only(hl_color)}}}{esc(kw)}{{\\c{c_only(color)}}}")
        self.dlg(t0, t1, f"{{\\an2\\pos({self.W / 2:.0f},{y:.0f})\\fn{self.st['fonts']['title']}\\fs{size * s:.0f}\\c{c_only(color)}"
                         f"\\bord{5 * s:.0f}\\3c&H000000&\\shad0\\q2}}{out}", 9)


# ------------------------------------------------------------------ analyze
CHAPTER_RE = re.compile(r"(첫\s?번째|두\s?번째|세\s?번째|네\s?번째|다섯\s?번째|마지막으로|다음으로|그럼 이제|본론)")
LIST_RE = re.compile(r"(두|세|네|다섯|여섯|일곱|[2-9])\s?가지")
CTA_RE = re.compile(r"(구독|좋아요|알림\s?설정)")
INTRO_RE = re.compile(r"(안녕하세요|반갑습니다).{0,30}(입니다|이에요|예요)")
NUM_KO = {"첫": 1, "두": 2, "세": 3, "네": 4, "다섯": 5}


def analyze(a):
    src = os.path.abspath(a.src)
    work = os.path.abspath(a.work)
    os.makedirs(work, exist_ok=True)
    info = probe(src)
    print(f"원본 {info['duration']:.1f}s  {info.get('w')}x{info.get('h')} @{info.get('fps')}fps")
    style = get_style(a.style)

    if a.srt:
        cues = parse_srt(a.srt)
        print(f"SRT {len(cues)}개 큐 사용")
    elif os.path.exists(os.path.join(work, "transcript.json")) and not a.retranscribe:
        cues = json.load(open(os.path.join(work, "transcript.json"), encoding="utf-8"))
        print("기존 transcript.json 재사용")
    else:
        cues = transcribe(src, work, a.model)
    json.dump(cues, open(os.path.join(work, "transcript.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(work, "transcript.txt"), "w", encoding="utf-8") as f:
        for c in cues:
            f.write(f"[{c['s']:8.2f} - {c['e']:8.2f}] {c['text']}\n")

    if info["has_audio"]:
        speech = speech_from_words(cues, info["duration"], a.tight) if any(c.get("words") for c in cues) \
            else detect_speech(src, info["duration"], a.tight)
    else:
        speech = [[0, info["duration"]]]

    # 긴 구간은 문장 경계에서 쪼개 줌을 교차 (점프컷 리듬)
    bounds = sorted({round(c["e"], 2) for c in cues})
    segs = []
    for s, e in speech:
        cur = s
        while e - cur > 9.0:
            cand = [b for b in bounds if cur + 3.5 < b < min(e - 2.0, cur + 9.0)]
            cut = cand[-1] if cand else cur + 6.5
            segs.append([cur, cut]); cur = cut
        segs.append([cur, e])
    zooms = style["zoom"]
    segments, zi = [], 0
    for s, e in segs:
        segments.append({"s": round(s, 3), "e": round(e, 3), "zoom": zooms[zi % len(zooms)]})
        zi += 1 if e - s > 1.2 else 0

    kept = sum(s["e"] - s["s"] for s in segments)

    # fx 자동 제안 (auto=true → 편집자가 검토)
    fx, last_t = [], -99
    for c in cues:
        txt = c["text"]
        if c["s"] < 40 and INTRO_RE.search(txt) and not any(f["type"] == "lower_third" for f in fx):
            fx.append({"type": "lower_third", "t": c["s"], "name": "", "title": "", "auto": True})
        m = CHAPTER_RE.search(txt)
        if m and c["s"] - last_t > 8:
            word = m.group(1).replace(" ", "")
            num = next((v for k, v in NUM_KO.items() if word.startswith(k) and "번째" in word), "")
            fx.append({"type": "chapter", "t": c["s"], "num": num, "title": "", "auto": True, "src": txt}); last_t = c["s"]
        m = LIST_RE.search(txt)
        if m:
            fx.append({"type": "list", "t": c["e"], "title": "", "items": [], "auto": True, "src": txt})
        if CTA_RE.search(txt):
            fx.append({"type": "cta", "t": c["s"], "auto": True, "src": txt})
    if not any(f["type"] == "cta" for f in fx) and info["duration"] > 60:
        fx.append({"type": "cta", "t": round(max(0, segments[-1]["e"] - 20), 2), "auto": True})

    plan = {
        "source": src, "work": work, "style": a.style, "tight": a.tight,
        "info": info, "segments": segments, "remove": [], "fixes": {},
        "keywords": [k for k in (a.keywords or "").split(",") if k],
        "intro": {"title": "", "sub": ""}, "fx": fx, "shorts": [], "bgm": None,
        "captions": True, "export": {"crf": 18, "preset": "medium"},
    }
    path = os.path.join(work, "plan.json")
    json.dump(plan, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"컷 후 예상 {kept:.1f}s ({kept / info['duration'] * 100:.0f}%), 세그먼트 {len(segments)}개, 자막 큐 {len(cues)}개, fx 제안 {len(fx)}개")
    print(f"→ {path}  /  {os.path.join(work, 'transcript.txt')}")


# ------------------------------------------------------------------ render
def build_keep(plan, style):
    keep = intersect_keep(plan["segments"], plan.get("remove", []))
    # impact 지점에 펀치 줌 (세그먼트 분할)
    pz = style.get("punch_zoom", 1.3)
    for f in plan.get("fx", []):
        if f.get("type") != "impact" or f.get("punch") is False:
            continue
        t0, t1 = f["t"], f["t"] + f.get("punch_dur", 1.4)
        new = []
        for k in keep:
            if k["e"] <= t0 or k["s"] >= t1:
                new.append(k); continue
            if k["s"] < t0:
                new.append({**k, "e": t0})
            new.append({**k, "s": max(k["s"], t0), "e": min(k["e"], t1), "zoom": pz})
            if k["e"] > t1:
                new.append({**k, "s": t1})
        keep = [k for k in new if k["e"] - k["s"] >= 0.05]
    return keep


def render_segments(src, keep, info, style, seg_dir, preview, W, H):
    os.makedirs(seg_dir, exist_ok=True)
    fps = info.get("fps", 30)
    ay = style.get("zoom_anchor_y", 0.42)
    enc = ["-c:v", "libx264", "-preset", "ultrafast" if preview else "veryfast", "-crf", "26" if preview else "14",
           "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]

    def one(i):
        k = keep[i]
        out = os.path.join(seg_dir, f"s{i:04d}.mkv")
        z = k.get("zoom", 1.0)
        vf = f"crop=iw/{z}:ih/{z}:(iw-iw/{z})/2:(ih-ih/{z})*{ay}," if z > 1.001 else ""
        vf += f"scale={W}:{H}:flags=lanczos,setsar=1,fps={fps}"
        d = k["e"] - k["s"]
        af = f"afade=t=in:d=0.012,afade=t=out:st={max(0, d - 0.015):.3f}:d=0.015"
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{k['s']:.3f}", "-t", f"{d:.3f}", "-i", src, "-vf", vf]
        if info["has_audio"]:
            cmd += ["-af", af]
        else:
            cmd += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a"]
        run(cmd + enc + ["-t", f"{d:.3f}", out])
        return out

    with ThreadPoolExecutor(max(2, (os.cpu_count() or 4) // 2)) as ex:
        files = list(ex.map(one, range(len(keep))))
    lst = os.path.join(seg_dir, "list.txt")
    with open(lst, "w") as f:
        for p in files:
            f.write(f"file '{p}'\n")
    base = os.path.join(seg_dir, "..", "base.mkv")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", base])
    return os.path.abspath(base)


def mix_and_encode(base, ass_path, sfx_events, bgm, images, style, out, preview, crf, preset, t_range=None, vf_pre=None, extra_v=None):
    """base.mkv 위에 ASS·이미지 합성, 보이스 정규화 + SFX + BGM(덕킹) + 최종 -14 LUFS."""
    inputs = []
    if t_range:
        inputs += ["-ss", f"{t_range[0]:.3f}", "-t", f"{t_range[1] - t_range[0]:.3f}"]
    inputs += ["-i", base]
    fc, idx = [], 1
    v = "[0:v]"
    if vf_pre:
        fc.append(f"{v}{vf_pre}[vp]"); v = "[vp]"
    grade = style.get("grade")
    if grade:
        fc.append(f"{v}{grade}[vg]"); v = "[vg]"
    if style.get("vignette"):
        fc.append(f"{v}vignette=angle=PI/5[vv]"); v = "[vv]"
    for im in images or []:
        inputs += ["-loop", "1", "-t", f"{im['t'] + im['dur'] + 0.5:.2f}", "-i", im["path"]]
        a, b = im["t"], im["t"] + im["dur"]
        if im.get("mode", "pip") == "full":
            sc = f"scale={im['W']}:{im['H']}:force_original_aspect_ratio=decrease,pad={im['W']}:{im['H']}:(ow-iw)/2:(oh-ih)/2:color=black"
            pos = "0:0"
        else:
            sc = f"scale={int(im['W'] * 0.42)}:-2"
            pos = f"W-w-{int(im['W'] * 0.05)}:{int(im['H'] * 0.12)}"
        fc.append(f"[{idx}:v]{sc},format=rgba,fade=t=in:st={a:.2f}:d=0.25:alpha=1,fade=t=out:st={b - 0.25:.2f}:d=0.25:alpha=1[im{idx}]")
        fc.append(f"{v}[im{idx}]overlay={pos}:enable='between(t,{a:.2f},{b:.2f})'[vo{idx}]"); v = f"[vo{idx}]"
        idx += 1
    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    fonts_esc = FONTS.replace(":", "\\:")
    fc.append(f"{v}ass='{ass_esc}':fontsdir='{fonts_esc}',format=yuv420p[vout]")

    fc.append("[0:a]loudnorm=I=-16:TP=-2:LRA=9,aresample=48000[voice]")
    mixes = ["[voice]"]
    vol = style.get("sfx_volume", 0.3)
    for i, (t, name, gain) in enumerate(sfx_events):
        p = os.path.join(SFX, name + ".wav")
        if not os.path.exists(p) or t < 0:
            continue
        inputs += ["-i", p]
        ms = int(t * 1000)
        fc.append(f"[{idx}:a]volume={vol * gain:.3f},adelay={ms}|{ms}[sx{i}]")
        mixes.append(f"[sx{i}]"); idx += 1
    if bgm and bgm.get("path"):
        inputs += ["-stream_loop", "-1", "-i", bgm["path"]]
        fc.append("[voice]asplit=2[voice1][sc]")
        mixes[0] = "[voice1]"
        fc.append(f"[{idx}:a]aresample=48000,volume={bgm.get('volume', 0.12)}[bg0]")
        fc.append("[bg0][sc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[bg]")
        mixes.append("[bg]"); idx += 1
    fc.append(f"{''.join(mixes)}amix=inputs={len(mixes)}:duration=first:normalize=0,"
              f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
    cmd = ["ffmpeg", "-y", "-v", "error"] + inputs + ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "ultrafast" if preview else preset, "-crf", str(28 if preview else crf),
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", out]
    run(cmd)


DEF_DUR = {"lower_third": 4.5, "chapter": 3.0, "keyword": 1.8, "stat": 2.6, "impact": 1.6, "quote": 4.5,
           "cta": 4.2, "text": 3.2, "intro": 3.0, "image": 3.0}
CENTER_FX = {"chapter", "list", "keyword", "stat", "impact", "quote"}  # 화면 중앙을 쓰는 fx끼리는 겹치면 앞의 것을 줄인다


def resolve_overlaps(fx_out, intro):
    fx_out.sort(key=lambda f: f["ot"])
    for f in fx_out:
        if "dur" not in f:
            f["dur"] = max(4.0, 1.2 + f.get("step", 0.45) * len(f.get("items", [])) + 2.0) if f["type"] == "list" \
                else DEF_DUR.get(f["type"], 3.0)
    intro_end = intro.get("dur", 3.0) if intro.get("title") else 0
    for f in fx_out:
        if f["type"] in CENTER_FX and f["ot"] < intro_end:
            print(f"! {f['type']}@{f['ot']:.1f}s 가 인트로와 겹침 → 인트로 뒤로 이동")
            f["ot"] = intro_end + 0.1
    fx_out.sort(key=lambda f: f["ot"])
    center = [f for f in fx_out if f["type"] in CENTER_FX]
    for a_, b in zip(center, center[1:]):
        if a_["ot"] + a_["dur"] > b["ot"] - 0.1:
            a_["dur"] = max(0.8, b["ot"] - a_["ot"] - 0.1)
            if b["ot"] - a_["ot"] < 0.9:
                print(f"! {a_['type']}@{a_['ot']:.1f}s 와 {b['type']}@{b['ot']:.1f}s 간격이 너무 좁음 (최소 1.5초 권장)")


def render(a):
    plan = json.load(open(a.plan, encoding="utf-8"))
    work = plan["work"]
    style = get_style(plan["style"])
    info = plan["info"]
    out_dir = os.path.join(work, "out")
    os.makedirs(out_dir, exist_ok=True)
    W, H = info["w"], info["h"]
    if a.preview and max(W, H) > 1280:
        W, H = (1280, int(round(1280 * info["h"] / info["w"] / 2) * 2)) if W >= H else (int(round(1280 * info["w"] / info["h"] / 2) * 2), 1280)
    keep = build_keep(plan, style)
    tl = Timeline(keep)
    print(f"컷 반영 길이 {tl.duration:.1f}s / 원본 {info['duration']:.1f}s, 구간 {len(keep)}개")

    cues = json.load(open(os.path.join(work, "transcript.json"), encoding="utf-8"))
    chunks = caption_chunks(cues, plan.get("fixes"), style["caption"]["max_chars"]) if plan.get("captions", True) else []
    out_chunks = []
    for c in chunks:
        if not (tl.inside(c["s"]) or tl.inside(c["e"]) or tl.inside((c["s"] + c["e"]) / 2)):
            continue
        s, e = tl.out(c["s"]), tl.out(c["e"])
        if e - s >= 0.2:
            out_chunks.append({"s": s, "e": e, "text": c["text"], "os": c["s"], "oe": c["e"]})
    for i in range(len(out_chunks) - 1):  # 겹침 제거 + 짧은 공백 메우기
        n = out_chunks[i + 1]
        if out_chunks[i]["e"] > n["s"] or n["s"] - out_chunks[i]["e"] < 0.25:
            out_chunks[i]["e"] = n["s"]
    kws = plan.get("keywords", [])

    base = os.path.join(work, "base.mkv")
    st_src = os.stat(plan["source"])
    stamp = json.dumps({"k": keep, "W": W, "H": H, "p": bool(a.preview), "src": plan["source"],
                        "m": st_src.st_mtime, "z": st_src.st_size}, sort_keys=True)
    stamp_path = os.path.join(work, "base.stamp")
    if not (os.path.exists(base) and os.path.exists(stamp_path) and open(stamp_path).read() == stamp):
        shutil.rmtree(os.path.join(work, "segs"), ignore_errors=True)
        print("세그먼트 렌더링...")
        base = render_segments(plan["source"], keep, info, style, os.path.join(work, "segs"), a.preview, W, H)
        open(stamp_path, "w").write(stamp)
    else:
        print("base.mkv 재사용 (컷 변경 없음)")

    fx_out = []
    for f in plan.get("fx", []):
        if f.get("skip"):
            continue
        if not tl.inside(f["t"]) and f["type"] not in ("cta",):
            print(f"! fx {f['type']}@{f['t']} 는 잘린 구간 → 다음 컷 시작으로 이동")
        fx_out.append({**f, "ot": tl.out(f["t"])})
    resolve_overlaps(fx_out, plan.get("intro") or {})

    if a.only in (None, "long"):
        ass = ASS(W, H, style)
        intro = plan.get("intro") or {}
        if intro.get("title"):
            ass.fx("intro", 0.0, intro.get("dur", 3.0), title=intro["title"], sub=intro.get("sub", ""))
        for c in out_chunks:
            if intro.get("title") and c["e"] < intro.get("dur", 3.0) - 0.2:
                continue
            mid = (c["s"] + c["e"]) / 2
            if not plan.get("quote_captions", True) and any(
                    f["type"] == "quote" and tl.out(f.get("cap_s", f["t"])) <= mid <= tl.out(f.get("cap_e", f["t"] + f["dur"]))
                    and f["ot"] <= mid <= f["ot"] + f["dur"] for f in fx_out):
                continue  # 인용 fx가 같은 문장을 크게 보여주는 동안 하단 자막 생략
            ass.caption(c["s"], c["e"], c["text"], kws)
        images = []
        for f in fx_out:
            p = {k: v for k, v in f.items() if k not in ("type", "t", "ot", "dur", "auto", "src", "cap_s", "cap_e")}
            ass.fx(f["type"], f["ot"], f.get("dur"), **p)
            if f["type"] == "image" and f.get("path"):
                images.append({"t": f["ot"], "dur": f.get("dur", 3.0), "path": f["path"], "mode": f.get("mode", "pip"), "W": W, "H": H})
        ass_path = os.path.join(work, "long.ass")
        ass.dump(ass_path)
        sfx = [(t, n, 1.0) for t, n in ass.sfx]
        # 컷 지점에 아주 작은 whoosh를 넣지 않는다(과함). 챕터·키워드만 효과음.
        out = os.path.join(out_dir, "long.mp4")
        print("롱폼 합성...")
        exp = plan.get("export", {})
        mix_and_encode(base, ass_path, sfx, plan.get("bgm"), images, style, out, a.preview, exp.get("crf", 18), exp.get("preset", "medium"))
        write_srt(out_chunks, os.path.join(out_dir, "long.srt"))
        write_chapters(fx_out, intro, os.path.join(out_dir, "chapters.txt"))
        print("→", out)

    if a.only in (None, "shorts"):
        for i, sh in enumerate(plan.get("shorts", []), 1):
            render_short(i, sh, plan, style, tl, base, out_chunks, fx_out, kws, out_dir, a.preview, W, H)


def render_short(i, sh, plan, style, tl, base, out_chunks, fx_out, kws, out_dir, preview, W, H):
    s0, s1 = tl.out(sh["s"]), tl.out(sh["e"])
    if s1 - s0 < 5:
        print(f"! shorts {i}: 컷 반영 후 {s1 - s0:.1f}s — 건너뜀"); return
    ss = style["shorts"]
    SW, SH = 1080, 1920
    layout = sh.get("layout", ss.get("layout", "square"))
    if layout == "full":
        vf = f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale={SW}:{SH}:flags=lanczos,setsar=1"
        cap_y, head_y = SH * 0.70, SH * 0.17
    else:
        vy = ss.get("video_y", 470)
        side = min(W, H)
        vf = (f"crop={side}:{side}:(iw-{side})/2:(ih-{side})*0.35,scale={SW}:{SW}:flags=lanczos,setsar=1,"
              f"pad={SW}:{SH}:0:{vy}:color={ss.get('bg', '#000000')}")
        cap_y, head_y = ss.get("caption_y", 1680), vy - 40
    ass = ASS(SW, SH, style, scale=1.0)
    dur = s1 - s0
    ass.headline(0, dur, sh.get("headline", ""), sh.get("highlight", ""), head_y, ss.get("headline_size", 88),
                 ss.get("headline_color", "#FFFFFF"), ss.get("headline_highlight", style["accent"]))
    for c in out_chunks:
        if c["e"] <= s0 or c["s"] >= s1:
            continue
        ass.caption(max(0, c["s"] - s0), min(dur, c["e"] - s0), c["text"], kws + sh.get("keywords", []),
                    pos=(SW / 2, cap_y), size=ss.get("caption_size", 72))
    for f in fx_out:
        if f["type"] in ("keyword", "impact", "quote", "stat") and s0 <= f["ot"] < s1 - 0.5:
            p = {k: v for k, v in f.items() if k not in ("type", "t", "ot", "dur", "auto", "src", "cap_s", "cap_e")}
            if layout != "full":
                p.setdefault("y", (ss.get("video_y", 470) + SW / 2) / SH)
            ass.fx(f["type"], f["ot"] - s0, f.get("dur"), **p)
    ass_path = os.path.join(out_dir, f"..{os.sep}shorts_{i:02d}.ass")
    ass.dump(ass_path)
    out = os.path.join(out_dir, f"shorts_{i:02d}.mp4")
    print(f"쇼츠 {i}: {dur:.1f}s")
    exp = plan.get("export", {})
    mix_and_encode(base, os.path.abspath(ass_path), [(t, n, 1.0) for t, n in ass.sfx], plan.get("bgm"), [], style, out,
                   preview, exp.get("crf", 18), exp.get("preset", "medium"), t_range=(s0, s1), vf_pre=vf)
    print("→", out)


def write_srt(chunks, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(chunks, 1):
            f.write(f"{i}\n{fmt_ts(c['s'], True)} --> {fmt_ts(c['e'], True)}\n{c['text']}\n\n")


def write_chapters(fx_out, intro, path):
    rows = [(0.0, intro.get("chapter", "인트로") if intro else "인트로")]
    for f in fx_out:
        if f["type"] == "chapter" and f.get("title") and f["ot"] - rows[-1][0] >= 10:
            rows.append((f["ot"], f["title"]))
    with open(path, "w", encoding="utf-8") as fh:
        for t, title in rows:
            m, s = int(t // 60), int(t % 60)
            fh.write(f"{m:02d}:{s:02d} {title}\n" if t < 3600 else f"{int(t // 3600)}:{m % 60:02d}:{s:02d} {title}\n")


# ------------------------------------------------------------------ reference analysis
def ref(a):
    work = os.path.abspath(a.work)
    os.makedirs(work, exist_ok=True)
    summary = []
    for path in a.files:
        name = os.path.splitext(os.path.basename(path))[0][:40]
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            summary.append({"file": path, "type": "image"}); continue
        info = probe(path)
        dur = info["duration"]
        r = subprocess.run(["ffmpeg", "-i", path, "-an", "-vf", "scale=320:-2,scdet=threshold=12", "-f", "null", "-"],
                           stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        cuts = [float(x) for x in re.findall(r"lavfi\.scd\.time: ([\d.]+)", r.stderr)]
        lufs = None
        if info["has_audio"]:
            r = subprocess.run(["ffmpeg", "-i", path, "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                               stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
            m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
            if m:
                lufs = float(json.loads(m.group(0))["input_i"])
        n = 24
        step = max(dur / n, 0.5)
        sheet = os.path.join(work, f"{name}_sheet.jpg")
        run(["ffmpeg", "-y", "-v", "error", "-i", path, "-vf", f"fps=1/{step:.3f},scale=480:-2,tile=6x4:padding=4", "-frames:v", "1", sheet])
        fdir = os.path.join(work, f"{name}_frames")
        os.makedirs(fdir, exist_ok=True)
        for k in range(10):
            t = dur * (k + 0.5) / 10
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", "-q:v", "3",
                 os.path.join(fdir, f"f{k:02d}_{t:06.1f}.jpg")])
        shots = [b - a_ for a_, b in zip([0] + cuts, cuts + [dur])]
        row = {"file": path, "duration": round(dur, 1), "size": f"{info.get('w')}x{info.get('h')}", "fps": info.get("fps"),
               "cuts": len(cuts), "cuts_per_min": round(len(cuts) / dur * 60, 1),
               "avg_shot": round(sum(shots) / len(shots), 2) if shots else dur, "lufs": lufs,
               "sheet": sheet, "frames": fdir,
               "suggest_tight": "tight" if len(cuts) / dur * 60 > 14 else "normal" if len(cuts) / dur * 60 > 6 else "loose"}
        summary.append(row)
        print(json.dumps(row, ensure_ascii=False))
    json.dump(summary, open(os.path.join(work, "ref.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("→", os.path.join(work, "ref.json"), "(컨택트시트·프레임을 직접 보고 styles.json 프리셋 조정)")


def check(a):
    r = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", a.file])
    st = json.loads(r.stdout)["streams"]
    durs = {s["codec_type"]: float(s.get("duration", 0)) for s in st}
    r = subprocess.run(["ffmpeg", "-i", a.file, "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    lufs = float(json.loads(m.group(0))["input_i"]) if m else None
    diff = abs(durs.get("video", 0) - durs.get("audio", 0))
    ok = diff < 0.1 and (lufs is None or abs(lufs + 14) < 1.5)
    print(json.dumps({"video": durs.get("video"), "audio": durs.get("audio"), "av_diff": round(diff, 3), "lufs": lufs, "ok": ok}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("analyze")
    p.add_argument("src"); p.add_argument("--work", required=True)
    p.add_argument("--style", default="default"); p.add_argument("--srt")
    p.add_argument("--tight", default="normal", choices=list(TEMPO))
    p.add_argument("--keywords"); p.add_argument("--model", default="medium")
    p.add_argument("--retranscribe", action="store_true")
    p.set_defaults(fn=analyze)
    p = sp.add_parser("render")
    p.add_argument("plan"); p.add_argument("--preview", action="store_true")
    p.add_argument("--only", choices=["long", "shorts"])
    p.set_defaults(fn=render)
    p = sp.add_parser("ref")
    p.add_argument("files", nargs="+"); p.add_argument("--work", required=True)
    p.set_defaults(fn=ref)
    p = sp.add_parser("check")
    p.add_argument("file")
    p.set_defaults(fn=check)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
