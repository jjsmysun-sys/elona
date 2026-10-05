#!/usr/bin/env python3
"""script2video — 대본(텍스트) → TTS 내레이션 + 스톡 영상/사진 + 모션그래픽 → autoedit 렌더.

  python script2video.py prepare script.txt --work W --style mystic [--voice female|male|<edge 보이스>] [--rate +5%] [--aspect 16:9|9:16]
      → W/narration.wav, W/transcript.json(단어 타이밍), W/scenes.json (장면별 검색어 = 편집자가 채움)
  python script2video.py build W
      → 장면마다 Pexels/Pixabay 영상·사진 다운로드 → W/src.mp4 + W/plan.json + W/credits.txt
  python autoedit.py render W/plan.json [--preview]

키: 환경 변수 PEXELS_API_KEY, PIXABAY_API_KEY (둘 중 하나만 있어도 됨, 없으면 그라데이션 배경)
네트워크 허용: speech.platform.bing.com, api.pexels.com, videos.pexels.com, images.pexels.com, pixabay.com, cdn.pixabay.com
대본 문법은 prompts/script_format.md
"""
import argparse, asyncio, json, os, re, ssl, subprocess, sys, urllib.parse, urllib.request
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autoedit as AE  # noqa: E402

VOICES = {"female": "ko-KR-SunHiNeural", "male": "ko-KR-InJoonNeural", "male2": "ko-KR-HyunsuMultilingualNeural"}
SCENE_LEN = {"mystic": 6.5, "news": 4.2, "default": 5.0}
DEFAULT_QUERIES = {
    "mystic": ["night sky stars", "candle flame dark", "ink in water", "full moon clouds", "galaxy nebula",
               "smoke dark background", "old book pages", "zen stones water", "aurora night", "lantern night",
               "temple incense", "constellation"],
    "news": ["stock market chart", "city skyline business", "money cash", "data analysis laptop",
             "trading screen", "graph growth", "office meeting", "coins", "global economy", "server room"],
    "default": ["abstract background", "city timelapse", "nature landscape", "people walking city",
                "sunrise", "coffee desk", "ocean waves", "forest light"],
}
UA = {"User-Agent": "Mozilla/5.0 autoedit-kit"}


# ------------------------------------------------------------------ net
def _ssl_ctx():
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=ca) if ca and os.path.exists(ca) else ssl.create_default_context()


def http_json(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as r:
        return json.loads(r.read().decode())


def http_download(url, path, headers=None):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    tmp = path + ".part"
    with urllib.request.urlopen(req, timeout=120, context=_ssl_ctx()) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ script parsing
def parse_script(text):
    """→ meta(title/sub), items: [{kind: say|pause|chapter|query|keyword|stat|list|impact|quote|text|image|para, ...}]"""
    meta, items = {"title": "", "sub": ""}, []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            items.append({"kind": "para"}); continue
        m = re.match(r"^#\s*(제목|title)\s*[:：]\s*(.+)$", line, re.I)
        if m:
            meta["title"] = m.group(2).strip(); continue
        m = re.match(r"^#\s*(부제|sub)\s*[:：]\s*(.+)$", line, re.I)
        if m:
            meta["sub"] = m.group(2).strip(); continue
        if line.startswith("## "):
            items.append({"kind": "chapter", "title": line[3:].strip()}); continue
        if line.startswith("//"):
            continue  # 메모
        m = re.match(r"^\[(.+?)\s*[:：]\s*(.*)\]$", line)
        if m:
            tag, val = m.group(1).strip(), m.group(2).strip()
            parts = [p.strip() for p in val.split("|")]
            if tag in ("화면", "검색", "영상", "broll"):
                items.append({"kind": "query", "q": val})
            elif tag in ("사진",):
                items.append({"kind": "query", "q": val, "photo": True})
            elif tag in ("키워드", "keyword"):
                items.append({"kind": "keyword", "text": val})
            elif tag in ("숫자", "stat"):
                items.append({"kind": "stat", "value": parts[0], "label": parts[1] if len(parts) > 1 else ""})
            elif tag in ("목록", "list"):
                items.append({"kind": "list", "title": parts[0], "items": parts[1:]})
            elif tag in ("자막", "설명", "text"):
                items.append({"kind": "text", "text": val})
            elif tag in ("이미지", "image"):
                items.append({"kind": "image", "path": parts[0], "mode": parts[1] if len(parts) > 1 else "pip"})
            elif tag in ("쉼", "pause"):
                items.append({"kind": "pause", "d": float(val or 0.8)})
            else:
                print(f"! 모르는 지시어 [{tag}: ...] — 무시")
            continue
        if re.match(r"^\[(쉼|pause)\]$", line):
            items.append({"kind": "pause", "d": 0.8}); continue
        if line.startswith("!!"):
            items.append({"kind": "impact", "text": line[2:].strip()}); continue
        quote = line.startswith(">")
        if quote:
            line = line[1:].strip()
        for sent in split_sentences(line):
            items.append({"kind": "say", "text": sent, "quote": quote})
    return meta, items


def split_sentences(line):
    parts = re.split(r"(?<=[.?!。…])\s+", line)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(clean(p)) > 70:  # 너무 긴 문장은 쉼표에서 나눈다
            sub, cur = re.split(r"(?<=,)\s+", p), ""
            for s in sub:
                if cur and len(clean(cur)) + len(clean(s)) > 60:
                    out.append(cur); cur = s
                else:
                    cur = (cur + " " + s).strip()
            if cur:
                out.append(cur)
        else:
            out.append(p)
    return out


def clean(t):
    return re.sub(r"\*\*(.+?)\*\*", r"\1", t)


def bold_words(t):
    return re.findall(r"\*\*(.+?)\*\*", t)


# ------------------------------------------------------------------ TTS
async def _edge_one(text, voice, rate, pitch, out_mp3):
    import edge_tts, aiohttp
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    conn = aiohttp.TCPConnector(ssl=_ssl_ctx())
    com = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary", connector=conn, proxy=proxy)
    words = []
    with open(out_mp3, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                s = ch["offset"] / 1e7
                words.append({"s": round(s, 3), "e": round(s + ch["duration"] / 1e7, 3), "w": ch["text"]})
    return words


async def _edge_all(jobs, voice, rate, pitch, conc=4):
    sem = asyncio.Semaphore(conc)

    async def one(j):
        async with sem:
            for attempt in range(3):
                try:
                    return await _edge_one(j["text"], voice, rate, pitch, j["mp3"])
                except Exception as e:  # noqa
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1.5 * (attempt + 1))
    return await asyncio.gather(*[one(j) for j in jobs])


def dummy_tts(text, wav):
    """네트워크 없을 때 레이아웃 미리보기용: 글자 수 기반 길이의 낮은 톤 + 가짜 단어 타이밍."""
    toks = text.split()
    d = max(1.0, 0.16 * len(text.replace(" ", "")) + 0.1 * len(toks))
    sr = 48000
    t = np.arange(int(sr * d)) / sr
    y = 0.15 * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    import wave
    with wave.open(wav, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((y * 32767).astype("<i2").tobytes())
    words, total = [], sum(len(x) for x in toks) or 1
    cur = 0.05
    for tok in toks:
        dd = (d - 0.1) * len(tok) / total
        words.append({"s": round(cur, 3), "e": round(cur + dd * 0.92, 3), "w": tok}); cur += dd
    return words


# ------------------------------------------------------------------ prepare
def prepare(a):
    work = os.path.abspath(a.work)
    tdir = os.path.join(work, "tts")
    os.makedirs(tdir, exist_ok=True)
    style = a.style
    meta, items = parse_script(open(a.script, encoding="utf-8").read())
    says = [it for it in items if it["kind"] == "say"]
    if not says:
        raise SystemExit("대본에서 읽을 문장을 찾지 못했습니다.")
    voice = VOICES.get(a.voice, a.voice)
    print(f"문장 {len(says)}개 → TTS ({'dummy' if a.tts == 'dummy' else voice}, rate {a.rate})")
    jobs = [{"text": clean(it["text"]), "mp3": os.path.join(tdir, f"{i:04d}.mp3"), "wav": os.path.join(tdir, f"{i:04d}.wav")}
            for i, it in enumerate(says)]
    if a.tts == "dummy":
        word_lists = [dummy_tts(j["text"], j["wav"]) for j in jobs]
    else:
        try:
            word_lists = asyncio.run(_edge_all(jobs, voice, a.rate, a.pitch))
        except Exception as e:
            raise SystemExit(f"Edge TTS 실패: {e.__class__.__name__}: {e}\n"
                             "→ speech.platform.bing.com 네트워크 허용 필요. 레이아웃만 미리 보려면 --tts dummy")
        for j in jobs:
            AE.run(["ffmpeg", "-y", "-v", "error", "-i", j["mp3"], "-ac", "1", "-ar", "48000", j["wav"]])

    # 이어 붙이기: 문장 사이 gap, 문단/챕터 사이 더 긴 gap
    gap_s, gap_p = (0.16, 0.38) if a.pace == "fast" else (0.28, 0.6) if a.pace == "slow" else (0.22, 0.48)
    t, si, cues, timeline = 0.35, 0, [], []
    pending_gap, fx_items = 0.0, []
    concat = []
    for it in items:
        k = it["kind"]
        if k == "say":
            if cues:
                t += max(gap_s, pending_gap)
            pending_gap = 0.0
            j = jobs[si]
            d = AE.probe(j["wav"])["duration"]
            words = [{"s": round(t + w["s"], 3), "e": round(t + min(w["e"], d), 3), "w": w["w"]} for w in word_lists[si]]
            cues.append({"s": round(t, 3), "e": round(t + d, 3), "text": clean(it["text"]), "words": words,
                         "quote": it.get("quote", False), "bold": bold_words(it["text"])})
            concat.append((t, j["wav"]))
            for f in fx_items:
                f["t"] = round(t, 3)
            timeline += fx_items; fx_items = []
            t += d; si += 1
        elif k == "para":
            pending_gap = max(pending_gap, gap_p)
        elif k == "pause":
            pending_gap = max(pending_gap, it["d"])
        elif k == "chapter":
            pending_gap = max(pending_gap, 0.9)
            fx_items.append(dict(it))
        else:
            fx_items.append(dict(it))
    total = t + 0.6
    for f in fx_items:  # 대본 끝 지시어
        f["t"] = round(max(0, total - 2.5), 3); timeline.append(f)

    # 내레이션 합성 (샘플 정확도로 배치)
    sr = 48000
    buf = np.zeros(int(total * sr) + sr, dtype=np.float32)
    import wave
    for st, wav in concat:
        with wave.open(wav) as w:
            y = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        i0 = int(st * sr)
        buf[i0:i0 + len(y)] += y
    buf = buf[: int(total * sr)]
    nar = os.path.join(work, "narration.wav")
    with wave.open(nar, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(buf, -1, 1) * 32767).astype("<i2").tobytes())

    json.dump([{k: v for k, v in c.items() if k in ("s", "e", "text", "words")} for c in cues],
              open(os.path.join(work, "transcript.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(work, "transcript.txt"), "w", encoding="utf-8") as f:
        for c in cues:
            f.write(f"[{c['s']:8.2f} - {c['e']:8.2f}] {c['text']}\n")

    scenes = make_scenes(cues, timeline, total, SCENE_LEN.get(style, 5.0))
    state = {"style": style, "aspect": a.aspect, "voice": voice if a.tts != "dummy" else "dummy", "meta": meta,
             "duration": round(total, 3), "fx_items": timeline,
             "keywords": sorted({w for c in cues for w in c["bold"]} | {f["text"] for f in timeline if f["kind"] == "keyword"}),
             "quotes": [{"t": c["s"], "e": c["e"], "text": c["text"]} for c in cues if c["quote"]],
             "scenes": scenes}
    json.dump(state, open(os.path.join(work, "scenes.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"내레이션 {total:.1f}s, 장면 {len(scenes)}개 → {os.path.join(work, 'scenes.json')}")
    print("다음: scenes.json 의 각 장면 query(영어 검색어)를 채운 뒤 build")


def make_scenes(cues, timeline, total, target):
    forced = {round(f["t"], 3): f for f in timeline if f["kind"] in ("chapter", "query")}
    scenes, cur = [], None
    q, photo = "", False
    for c in cues:
        f = forced.get(round(c["s"], 3))
        for g in [x for x in timeline if x["kind"] == "query" and abs(x["t"] - c["s"]) < 1e-3]:
            q, photo = g["q"], g.get("photo", False)
        new = cur is None or f is not None or (c["e"] - cur["s"] > target * 1.35 and c["s"] - cur["s"] >= target * 0.6)
        if new:
            if cur:
                cur["e"] = c["s"]
            cur = {"s": c["s"] if scenes else 0.0, "e": c["e"], "text": "", "query": q, "photo": photo}
            scenes.append(cur)
        cur["text"] = (cur["text"] + " " + c["text"]).strip()
        cur["e"] = c["e"]
    if scenes:
        scenes[-1]["e"] = total
    for i, s in enumerate(scenes):
        s["i"] = i
        s["s"], s["e"] = round(s["s"], 3), round(s["e"], 3)
        s["src"] = "directive" if s["query"] else ""
    return scenes


# ------------------------------------------------------------------ asset search
def search_assets(query, kind, orient, keys):
    """→ [{'type': video|photo, 'url', 'w', 'h', 'dur', 'credit', 'page', 'id'}]"""
    res = []
    q = urllib.parse.quote(query)
    o_px = "landscape" if orient == "landscape" else "portrait"
    o_pb = "horizontal" if orient == "landscape" else "vertical"
    if kind == "video" and keys.get("pexels"):
        try:
            j = http_json(f"https://api.pexels.com/videos/search?query={q}&per_page=20&orientation={o_px}", {"Authorization": keys["pexels"]})
            for v in j.get("videos", []):
                files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("width")]
                if not files:
                    continue
                best = sorted(files, key=lambda f: (abs(max(f["width"], f["height"]) - 1920), -f["width"]))[0]
                res.append({"type": "video", "url": best["link"], "w": best["width"], "h": best["height"], "dur": v.get("duration", 0),
                            "credit": f"{v.get('user', {}).get('name', '')} (Pexels)", "page": v.get("url", ""), "id": f"pexels_v{v['id']}"})
        except Exception as e:
            print(f"  ! Pexels 영상 검색 실패: {e}")
    if kind == "video" and keys.get("pixabay"):
        try:
            j = http_json(f"https://pixabay.com/api/videos/?key={keys['pixabay']}&q={q}&per_page=20&safesearch=true")
            for h in j.get("hits", []):
                vs = h.get("videos", {})
                cand = [vs[k] for k in ("large", "medium") if vs.get(k, {}).get("url")]
                if not cand:
                    continue
                if orient == "portrait" and cand[0].get("width", 1) > cand[0].get("height", 1):
                    continue
                res.append({"type": "video", "url": cand[0]["url"], "w": cand[0].get("width", 0), "h": cand[0].get("height", 0),
                            "dur": h.get("duration", 0), "credit": f"{h.get('user', '')} (Pixabay)", "page": h.get("pageURL", ""), "id": f"pixabay_v{h['id']}"})
        except Exception as e:
            print(f"  ! Pixabay 영상 검색 실패: {e}")
    if kind == "photo" and keys.get("pexels"):
        try:
            j = http_json(f"https://api.pexels.com/v1/search?query={q}&per_page=20&orientation={o_px}", {"Authorization": keys["pexels"]})
            for p in j.get("photos", []):
                res.append({"type": "photo", "url": p["src"].get("large2x") or p["src"]["original"], "w": p.get("width"), "h": p.get("height"),
                            "credit": f"{p.get('photographer', '')} (Pexels)", "page": p.get("url", ""), "id": f"pexels_p{p['id']}"})
        except Exception as e:
            print(f"  ! Pexels 사진 검색 실패: {e}")
    if kind == "photo" and keys.get("pixabay"):
        try:
            j = http_json(f"https://pixabay.com/api/?key={keys['pixabay']}&q={q}&image_type=photo&orientation={o_pb}&per_page=20&safesearch=true")
            for h in j.get("hits", []):
                res.append({"type": "photo", "url": h.get("largeImageURL"), "w": h.get("imageWidth"), "h": h.get("imageHeight"),
                            "credit": f"{h.get('user', '')} (Pixabay)", "page": h.get("pageURL", ""), "id": f"pixabay_p{h['id']}"})
        except Exception as e:
            print(f"  ! Pixabay 사진 검색 실패: {e}")
    return res


# ------------------------------------------------------------------ compose
def scene_clip(sc, asset, path, out, W, H, fps, dim, style):
    d = sc["e"] - sc["s"]
    grade = f"eq=brightness={-dim:.3f}:saturation=0.96"
    if asset is None:  # 스타일 색 그라데이션 배경
        c0, c1 = style["panel"], style.get("accent2", style["accent"])
        vf = f"gradients=s={W}x{H}:c0={c0}:c1={c1}:x0=0:y0=0:x1={W}:y1={H}:speed=0.008:d={d:.3f}:r={fps},format=yuv420p,eq=brightness=-0.12"
        AE.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", vf, "-t", f"{d:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", out])
        return
    if asset["type"] == "video":
        src_d = asset.get("real_dur") or asset.get("dur") or d
        start = max(0.0, min((src_d - d) * 0.3, 2.0)) if src_d > d + 0.5 else 0.0
        loop = ["-stream_loop", "-1"] if src_d < d + 0.2 else []
        slow = ""
        if src_d < d * 0.75:  # 너무 짧으면 루프 대신 살짝 느리게
            loop, slow = ["-stream_loop", "-1"], f"setpts={min(1.5, d / max(src_d, 0.1)):.3f}*PTS,"
        vf = (f"{slow}scale={int(W * 1.08)}:{int(H * 1.08)}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H}:x='(iw-{W})*t/{d:.3f}':y='(ih-{H})/2',fps={fps},setsar=1,{grade},format=yuv420p")
        AE.run(["ffmpeg", "-y", "-v", "error"] + loop + ["-ss", f"{start:.2f}", "-i", path, "-t", f"{d:.3f}", "-vf", vf,
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", out])
    else:  # 사진: Ken Burns
        n = int(round(d * fps))
        zin = sc["i"] % 2 == 0
        z = f"'min(1+0.12*on/{n},1.12)'" if zin else f"'max(1.12-0.12*on/{n},1.0)'"
        vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z={z}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={n}:s={W}x{H}:fps={fps},setsar=1,{grade},format=yuv420p")
        AE.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", path, "-t", f"{d:.3f}", "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", out])


def gen_bgm(path, dur, style_name):
    """저작권 없는 앰비언트 패드 BGM 합성 (스타일별 코드 진행)."""
    sr = 48000
    prog = {"mystic": [[57, 60, 64, 69], [53, 57, 60, 65], [48, 55, 60, 64], [55, 59, 62, 67]],
            "news": [[52, 59, 64, 68], [49, 56, 61, 64], [45, 52, 57, 61], [47, 54, 59, 63]],
            "default": [[48, 55, 60, 64], [45, 52, 57, 60], [41, 48, 53, 57], [43, 50, 55, 59]]}[style_name if style_name in ("mystic", "news") else "default"]
    seg = 8.0
    n = int(sr * (dur + 1))
    y = np.zeros((n, 2), np.float32)
    t_seg = np.arange(int(sr * (seg + 2))) / sr
    env = np.minimum(1, t_seg / 2.0) * np.minimum(1, np.maximum(0, (seg + 2 - t_seg) / 2.0))
    k = 0
    while k * seg < dur + 1:
        chord = prog[k % len(prog)]
        part = np.zeros((len(t_seg), 2), np.float32)
        for j, m in enumerate(chord):
            f = 440 * 2 ** ((m - 69) / 12)
            for det, pan in ((-0.12, 0.3), (0.12, 0.7)):
                ph = np.random.default_rng(k * 10 + j).uniform(0, 6.28)
                s = np.sin(2 * np.pi * (f + det) * t_seg + ph) + 0.25 * np.sin(2 * np.pi * 2 * (f + det) * t_seg)
                part[:, 0] += s * (1 - pan); part[:, 1] += s * pan
        part *= env[:, None] / 8
        i0 = int(k * seg * sr)
        m = min(len(part), n - i0)
        if m > 0:
            y[i0:i0 + m] += part[:m]
        k += 1
    # 간단한 리버브: 감쇠 노이즈 임펄스와 FFT 컨볼루션
    ir_n = int(sr * 2.5)
    ir = np.random.default_rng(1).uniform(-1, 1, ir_n) * np.exp(-np.arange(ir_n) / (sr * 0.7))
    ir /= np.sum(np.abs(ir)) / 6
    L = 1 << int(np.ceil(np.log2(n + ir_n)))
    IR = np.fft.rfft(ir, L)
    for ch in range(2):
        wet = np.fft.irfft(np.fft.rfft(y[:, ch], L) * IR, L)[:n]
        y[:, ch] = 0.55 * y[:, ch] + 0.45 * wet
    y = y[: int(sr * dur)]
    y /= (np.max(np.abs(y)) or 1) / 0.5
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((y * 32767).astype("<i2").tobytes())


def build(a):
    work = os.path.abspath(a.work)
    st = json.load(open(os.path.join(work, "scenes.json"), encoding="utf-8"))
    style = AE.get_style(st["style"])
    W, H = (1920, 1080) if st.get("aspect", "16:9") == "16:9" else (1080, 1920)
    orient = "landscape" if W > H else "portrait"
    fps = 30
    keys = {"pexels": os.environ.get("PEXELS_API_KEY", ""), "pixabay": os.environ.get("PIXABAY_API_KEY", "")}
    if not any(keys.values()):
        print("! PEXELS_API_KEY / PIXABAY_API_KEY 없음 → 모든 장면을 그라데이션 배경으로 만듭니다")
    adir, cdir = os.path.join(work, "assets"), os.path.join(work, "clips")
    os.makedirs(adir, exist_ok=True); os.makedirs(cdir, exist_ok=True)
    pool = DEFAULT_QUERIES.get(st["style"], DEFAULT_QUERIES["default"])
    cache_p = os.path.join(adir, "search_cache.json")
    cache = json.load(open(cache_p)) if os.path.exists(cache_p) else {}
    used, credits, clips = set(), [], []
    for sc in st["scenes"]:
        q = sc.get("query") or pool[sc["i"] % len(pool)]
        kinds = ["photo", "video"] if sc.get("photo") else ["video", "photo"]
        asset, path = None, None
        if any(keys.values()) and not sc.get("gradient"):
            for kind in kinds:
                ck = f"{kind}|{orient}|{q}"
                if ck not in cache:
                    cache[ck] = search_assets(q, kind, orient, keys)
                    json.dump(cache, open(cache_p, "w"), ensure_ascii=False)
                need = sc["e"] - sc["s"]
                cands = [c for c in cache[ck] if c["id"] not in used]
                if kind == "video":
                    cands.sort(key=lambda c: (c.get("dur", 0) < need * 0.75, 0))
                pick = sc.get("pick", 0)
                if cands:
                    asset = cands[min(pick, len(cands) - 1)]
                    ext = ".mp4" if asset["type"] == "video" else ".jpg"
                    path = os.path.join(adir, asset["id"] + ext)
                    try:
                        http_download(asset["url"], path)
                        if asset["type"] == "video":
                            asset["real_dur"] = AE.probe(path)["duration"]
                        break
                    except Exception as e:
                        print(f"  ! 다운로드 실패 {asset['id']}: {e}")
                        asset = None
        out = os.path.join(cdir, f"c{sc['i']:03d}.mp4")
        scene_clip(sc, asset, path, out, W, H, fps, style.get("broll_dim", 0.07), style)
        clips.append(out)
        label = f"{asset['id']} — {asset['credit']} {asset['page']}" if asset else "gradient"
        print(f"장면 {sc['i']:02d} [{sc['s']:6.1f}-{sc['e']:6.1f}] '{q}' → {label}")
        if asset:
            used.add(asset["id"]); credits.append(f"{asset['credit']} — {asset['page']}")
    lst = os.path.join(cdir, "list.txt")
    open(lst, "w").write("".join(f"file '{c}'\n" for c in clips))
    src = os.path.join(work, "src.mp4")
    AE.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-i", os.path.join(work, "narration.wav"),
            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest", src])
    with open(os.path.join(work, "credits.txt"), "w", encoding="utf-8") as f:
        f.write("영상/사진 출처 (설명란에 붙여넣기)\n" + "\n".join(dict.fromkeys(credits)) + "\n")

    bgm = None
    if not a.no_bgm:
        bp = os.path.join(work, "bgm.wav")
        gen_bgm(bp, st["duration"] + 1, st["style"])
        bgm = {"path": bp, "volume": a.bgm_volume}
    write_plan(work, st, src, bgm)


def write_plan(work, st, src, bgm):
    info = AE.probe(src)
    fx, chap = [], 0
    for it in st["fx_items"]:
        k, t = it["kind"], it["t"]
        if k == "chapter":
            chap += 1; fx.append({"type": "chapter", "t": t, "num": chap, "title": it["title"]})
        elif k == "keyword":
            fx.append({"type": "keyword", "t": t + 0.3, "text": it["text"]})
        elif k == "stat":
            fx.append({"type": "stat", "t": t + 0.2, "value": it["value"], "label": it["label"]})
        elif k == "list":
            fx.append({"type": "list", "t": t, "title": it["title"], "items": it["items"]})
        elif k == "impact":
            fx.append({"type": "impact", "t": t, "text": it["text"]})
        elif k == "text":
            fx.append({"type": "text", "t": t, "text": it["text"]})
        elif k == "image":
            fx.append({"type": "image", "t": t, "path": it["path"], "mode": it["mode"], "dur": 3.5})
    chap_ts = [f["t"] for f in fx if f["type"] == "chapter"]
    for q in st["quotes"]:
        t0 = q["t"]
        for ct in chap_ts:  # 챕터 카드 직후 인용이면 카드가 끝난 뒤에 띄운다
            if ct - 0.05 <= t0 < ct + 2.6:
                t0 = ct + 2.6
        fx.append({"type": "quote", "t": round(t0, 3), "dur": round(max(3.0, q["e"] + 0.6 - t0), 2), "text": re.sub(r"[.]+$", "", q["text"]),
                   "cap_s": q["t"], "cap_e": q["e"]})
    cues = json.load(open(os.path.join(work, "transcript.json"), encoding="utf-8"))
    for c in cues:
        if AE.CTA_RE.search(c["text"]) and not any(f["type"] == "cta" for f in fx):
            fx.append({"type": "cta", "t": c["s"]})
    fx.sort(key=lambda f: f["t"])
    plan = {
        "source": src, "work": work, "style": st["style"], "tight": "none", "info": info,
        "segments": [{"s": 0.0, "e": round(info["duration"], 3), "zoom": 1.0}],
        "remove": [], "fixes": {}, "keywords": st["keywords"],
        "intro": {"title": st["meta"].get("title", ""), "sub": st["meta"].get("sub", "")},
        "fx": fx, "shorts": [], "bgm": bgm, "captions": True,
        "quote_captions": False, "export": {"crf": 18, "preset": "medium"}, "mode": "script",
    }
    path = os.path.join(work, "plan.json")
    json.dump(plan, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"→ {path}  (다음: python autoedit.py render {path} --preview)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("prepare")
    p.add_argument("script"); p.add_argument("--work", required=True)
    p.add_argument("--style", default="default")
    p.add_argument("--voice", default="female", help="female|male|male2 또는 edge 보이스 이름")
    p.add_argument("--rate", default="+8%"); p.add_argument("--pitch", default="+0Hz")
    p.add_argument("--pace", default="normal", choices=["fast", "normal", "slow"])
    p.add_argument("--aspect", default="16:9", choices=["16:9", "9:16"])
    p.add_argument("--tts", default="edge", choices=["edge", "dummy"])
    p.set_defaults(fn=prepare)
    p = sp.add_parser("build")
    p.add_argument("work"); p.add_argument("--no-bgm", action="store_true")
    p.add_argument("--bgm-volume", type=float, default=0.10)
    p.set_defaults(fn=build)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
