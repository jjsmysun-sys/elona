#!/usr/bin/env python3
"""효과음 합성기 — 저작권 걱정 없는 SFX를 numpy로 직접 생성한다.

python sfx_gen.py            # sfx/*.wav 전부 생성
python sfx_gen.py whoosh pop # 일부만

새 효과음은 함수 하나 만들고 ALL 에 등록하면 된다.
"""
import sys, os, wave
import numpy as np

SR = 48000
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sfx")


def t(d):
    return np.arange(int(SR * d)) / SR


def env(n, a=0.005, r=0.2, curve=4.0):
    """attack 선형 + 지수 감쇠 엔벨로프."""
    x = np.ones(n)
    na = max(1, int(SR * a))
    x[:na] = np.linspace(0, 1, na)
    rest = n - na
    if rest > 0:
        x[na:] = np.exp(-curve * np.linspace(0, 1, rest) * (len(x) / SR) / max(r, 1e-3))
    return x


def lowpass(x, cutoff):
    """1-pole lowpass. cutoff는 스칼라 또는 샘플별 배열."""
    cutoff = np.broadcast_to(np.asarray(cutoff, float), x.shape)
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc = (1 - a[i]) * x[i] + a[i] * acc
        y[i] = acc
    return y


def highpass(x, cutoff):
    return x - lowpass(x, cutoff)


def norm(x, peak=0.89):
    m = np.max(np.abs(x)) or 1.0
    return x / m * peak


def noise(d, seed=0):
    return np.random.default_rng(seed).uniform(-1, 1, int(SR * d))


# ---------------------------------------------------------------- sounds
def whoosh():  # 화면 전환/텍스트 슬라이드
    d = 0.55
    n = noise(d, 1)
    tt = t(d)
    sweep = 300 + 5200 * np.sin(np.pi * tt / d) ** 2
    y = lowpass(n, sweep) - lowpass(n, sweep * 0.25)
    e = np.sin(np.pi * np.clip(tt / d, 0, 1)) ** 1.6
    return norm(y * e, 0.7)


def swoosh():  # 짧고 날카로운 휙 (키워드 등장)
    d = 0.28
    n = noise(d, 2)
    tt = t(d)
    y = highpass(lowpass(n, 1500 + 9000 * tt / d), 800)
    e = (tt / d) ** 1.5 * np.exp(-((tt - d * 0.75) ** 2) / 0.004)
    return norm(y * e, 0.6)


def pop():  # 자막 키워드/리스트 항목
    d = 0.12
    tt = t(d)
    f = 900 * np.exp(-tt * 40) + 380
    y = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(len(tt), 0.001, 0.03)
    return norm(y, 0.8)


def click():  # UI 클릭 (CTA 버튼)
    d = 0.05
    y = noise(d, 3) * env(int(SR * d), 0.0005, 0.006)
    y = highpass(y, 2500)
    return norm(y, 0.7)


def ding():  # 정보/포인트 강조
    d = 1.2
    tt = t(d)
    y = sum(a * np.sin(2 * np.pi * f * tt) for f, a in [(1318.5, 1), (2637, 0.35), (3955, 0.12)])
    return norm(y * env(len(tt), 0.002, 0.35), 0.55)


def bell():  # 신비로운 종 (mystic 챕터/인용)
    d = 3.0
    tt = t(d)
    parts = [(1.0, 1.0, 1.2), (2.76, 0.5, 0.7), (5.40, 0.25, 0.4), (8.93, 0.12, 0.25), (0.5, 0.3, 1.6)]
    base = 660
    y = sum(a * np.sin(2 * np.pi * base * r * tt + 0.3 * np.sin(2 * np.pi * 3 * tt)) * np.exp(-tt / dec)
            for r, a, dec in parts)
    att = np.clip(tt / 0.004, 0, 1)
    return norm(y * att, 0.55)


def shimmer():  # 반짝임 (운세 결과, 좋은 소식)
    d = 1.6
    tt = t(d)
    rng = np.random.default_rng(5)
    y = np.zeros_like(tt)
    for i in range(18):
        st = rng.uniform(0, d * 0.6)
        f = rng.choice([2093, 2349, 2637, 3136, 3520, 4186])
        m = tt >= st
        tl = tt[m] - st
        y[m] += np.sin(2 * np.pi * f * tl) * np.exp(-tl / 0.18) * rng.uniform(0.3, 1)
    return norm(y * np.minimum(1, tt / 0.05), 0.45)


def impact():  # 펀치라인/반전 — 붐
    d = 1.4
    tt = t(d)
    f = 30 + 90 * np.exp(-tt * 9)
    sub = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt / 0.45)
    hit = lowpass(noise(d, 4), 2500) * np.exp(-tt / 0.05)
    y = np.tanh(2.2 * (sub + 0.6 * hit))
    return norm(y, 0.9)


def riser():  # 긴장 상승 (반전 직전, 인트로)
    d = 1.8
    tt = t(d)
    f = 200 * (2 ** (tt / d * 3))
    tone = np.sin(2 * np.pi * np.cumsum(f) / SR)
    n = highpass(noise(d, 6), 1000) * 0.4
    e = (tt / d) ** 2.2
    return norm((tone * 0.5 + n) * e, 0.6)


def typing():  # 텍스트 박스/타자 효과
    d = 0.6
    y = np.zeros(int(SR * d))
    rng = np.random.default_rng(7)
    for k in range(7):
        st = int(SR * (k * 0.08 + rng.uniform(0, 0.02)))
        c = highpass(noise(0.03, 10 + k), 1800) * env(int(SR * 0.03), 0.0005, 0.008)
        y[st:st + len(c)] += c[: len(y) - st]
    return norm(y, 0.5)


def tick():  # 리스트 항목 틱
    d = 0.06
    tt = t(d)
    y = np.sin(2 * np.pi * 2200 * tt) * env(len(tt), 0.0005, 0.012)
    return norm(y, 0.6)


def boing():  # 웃음 포인트(default 스타일)
    d = 0.5
    tt = t(d)
    f = 220 + 160 * np.sin(2 * np.pi * 9 * tt) * np.exp(-tt * 5) + 200 * tt
    y = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(len(tt), 0.003, 0.18)
    return norm(y, 0.6)


def news_sting():  # 정보형(news) 챕터 스팅
    d = 0.9
    tt = t(d)
    y = np.zeros_like(tt)
    for st, f in [(0.0, 523.25), (0.09, 659.25), (0.18, 987.77)]:
        m = tt >= st
        tl = tt[m] - st
        y[m] += (np.sign(np.sin(2 * np.pi * f * tl)) * 0.25 + np.sin(2 * np.pi * f * tl)) * np.exp(-tl / 0.18)
    return norm(lowpass(y, 5000), 0.55)


ALL = {
    "whoosh": whoosh, "swoosh": swoosh, "pop": pop, "click": click, "ding": ding,
    "bell": bell, "shimmer": shimmer, "impact": impact, "riser": riser,
    "typing": typing, "tick": tick, "boing": boing, "news_sting": news_sting,
}


def write(name, y):
    os.makedirs(OUT, exist_ok=True)
    y = np.clip(y, -1, 1)
    st = np.stack([y, y], 1)
    pcm = (st * 32767).astype("<i2").tobytes()
    with wave.open(os.path.join(OUT, name + ".wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


def main(names=None):
    for n in names or ALL:
        write(n, ALL[n]())
        print("sfx/" + n + ".wav")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
