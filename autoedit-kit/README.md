# autoedit-kit

원본 영상 → **컷편집 · 자막 · 모션그래픽 · 효과음 · BGM 덕킹 · -14 LUFS · 쇼츠** 자동 완성.
필요: ffmpeg(libass 포함), Python 3.9+, numpy(효과음 재생성 시만), faster-whisper(선택, 자동 자막).

```bash
python autoedit.py analyze raw.mp4 --work W --style mystic          # 1) 분석 → W/plan.json, W/transcript.txt
# 2) W/plan.json 편집 (prompts/edit_prompt.md 규칙)
python autoedit.py render W/plan.json --preview                      # 3) 미리보기
python autoedit.py render W/plan.json                                # 4) 최종 → W/out/
python autoedit.py check W/out/long.mp4                              # A/V 싱크·라우드니스 검증
python autoedit.py ref ref.mp4 --work R                              # 레퍼런스 스타일 분석
```

| 파일 | 내용 |
|---|---|
| `autoedit.py` | 엔진: 무음/단어 기반 컷, 줌 교차·펀치 줌, ASS 자막(키워드 하이라이트·팝), 모션그래픽 11종, SFX 믹스, BGM 사이드체인 덕킹, 쇼츠(9:16), SRT·챕터 출력 |
| `styles.json` | 프리셋 `default`(토크) / `mystic`(사주·운세) / `news`(정보·투자) |
| `sfx/`, `sfx_gen.py` | 직접 합성한 효과음 13종 (저작권 무관) |
| `fonts/` | Pretendard, Black Han Sans, 나눔명조 (모두 OFL) |
| `prompts/edit_prompt.md` | plan.json 작성 규칙 = 편집자 판단 기준 |
| `prompts/reference_analysis.md` | 레퍼런스 영상 분석 절차와 현재 상태 |

모션그래픽: `intro` `lower_third` `chapter` `list` `keyword` `stat` `impact` `quote` `text` `cta` `image`
출력: `out/long.mp4`, `out/shorts_XX.mp4`, `out/long.srt`, `out/chapters.txt`
