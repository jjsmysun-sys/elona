---
name: youtube-autoedit
description: 원본 영상 또는 대본(텍스트)을 받으면 컷편집·TTS·스톡 영상 수집·자막·모션그래픽·효과음·BGM·쇼츠까지 완성 영상을 만든다 (이 레포의 autoedit-kit 사용). 레퍼런스 영상 스타일 분석·프리셋 보정에도 사용.
---

# YouTube 자동 편집 (레포 키트 버전)

입력이 두 종류다:
- **대본(텍스트)만 받은 경우 → 아래 'A. 대본 모드'** (TTS + 스톡 자료 + 편집)
- **촬영 원본 영상을 받은 경우 → '1. 분석'부터** (컷편집 모드)

## A. 대본 모드 (script2video)
`autoedit-kit/prompts/script_format.md`를 먼저 읽는다.
1. 대본을 문법에 맞게 다듬은 사본 `W/script.txt` 작성: `# 제목:`/`# 부제:`, `## 챕터`, `**키워드**`, `[화면: 영어 검색어]`, `[키워드:]`, `[숫자:]`, `[목록:]`, `!! 임팩트`, `> 인용`. 사용자의 문장·말투는 바꾸지 않는다.
2. `pip install edge-tts --break-system-packages` 후 `python script2video.py prepare W/script.txt --work W --style <스타일> --voice female`
3. `W/scenes.json`의 모든 장면 `query`를 문장 내용에 맞는 구체적 영어 검색어로 채운다(규칙: script_format.md)
4. `python script2video.py build W` → `W/clips/` 몇 개를 프레임으로 확인, 이상하면 query/pick 수정 후 재실행
5. plan.json에 fx·shorts 보강 → 아래 3번(렌더 & 검수)과 동일. 전달 시 `W/credits.txt`도 함께
- 막히는 경우: TTS 실패 → `speech.platform.bing.com` 허용 안내. `PEXELS_API_KEY`/`PIXABAY_API_KEY` 없음 → 그라데이션 배경으로 진행하되 키 설정 안내(키는 채팅으로 받지 않고 환경 설정에 넣게 한다).

사용자가 원본 영상을 주면 **묻지 말고 끝까지 편집해서 완성본을 전달**한다. 확인 질문은 스타일이 정말 모호할 때 1회만.
키트: 레포의 `autoedit-kit/` (zip 불필요). 상세 규칙은 `autoedit-kit/prompts/edit_prompt.md` — **plan.json 작성 전에 반드시 읽는다.**

## 0. 준비
```
cd autoedit-kit
pip install faster-whisper --break-system-packages   # 실패해도 진행
```
- Whisper 모델은 huggingface.co 접근이 필요하다. 막혀 있으면 사용자에게 SRT(Vrew/CapCut/클로바노트/유튜브 자동자막)를 받아 `--srt`로 쓰거나, 환경 Network access에 huggingface.co 허용을 안내한다.
- 원본이 레포 밖(업로드)이면 경로 그대로 쓴다. 작업 폴더 W는 스크래치패드에 만든다(대용량 영상은 커밋하지 않는다).

## 1. 분석
```
python autoedit.py analyze <원본> --work W --style <default|mystic|news> [--srt x.srt] [--tight tight|normal|loose] [--keywords a,b]
```
- 스타일: 사주·명리·타로·운세 → `mystic`, 정보/분석/투자(ELONA) → `news`, 그 외 → `default`. 레퍼런스로 만든 `ref_*` 프리셋이 있으면 그것 우선.
- 템포: 정보형/쇼츠 `tight`, 일반 `normal`, 상담·감성 `loose`

## 2. plan.json 다듬기 = 편집자의 판단 (핵심)
`W/transcript.txt` 전부 읽고 `prompts/edit_prompt.md` 규칙대로 remove / fixes / keywords / intro / fx / shorts / bgm 작성.
analyze가 넣은 `"auto": true` fx는 내용을 채우거나 `"skip": true`.

## 3. 렌더 & 검수
```
python autoedit.py render W/plan.json --preview
```
- fx 시점마다 `ffmpeg -ss T -i W/out/long.mp4 -frames:v 1 x.png` 로 프레임을 뽑아 Read로 직접 확인 (자막 겹침, 텍스트 잘림, 얼굴 가림, 쇼츠 헤드라인 겹침)
- 컷이 안 바뀌면 base.mkv를 재사용하므로 fx/자막 수정은 빠르다
- 최종: `python autoedit.py render W/plan.json` (`--only long|shorts`)
- `python autoedit.py check W/out/long.mp4` → av_diff < 0.1, lufs ≈ -14

## 4. 전달
`W/out/`의 long.mp4, shorts_XX.mp4, long.srt, chapters.txt를 SendUserFile로 전달.
답변은 1~2문장 + 원본→편집 길이, 쇼츠 개수, 제목 제안 1줄.

## 레퍼런스 스타일 반영
`prompts/reference_analysis.md` 참고. 레퍼런스 파일을 받으면 `python autoedit.py ref <파일들> --work R` → 컨택트시트/프레임을 보고
`styles.json`에 `ref_<이름>` 프리셋 추가(기존 키 구조 유지), 결과를 reference_analysis.md에 기록하고 커밋.
유튜브 링크는 기본 네트워크 정책에서 열리지 않는다 → 파일/스크린샷 요청 또는 네트워크 허용 안내.

## 키트 수정 시
- 모션그래픽 추가: `autoedit.py`의 `ASS.fx()`에 `elif typ == ...` 분기 + `DEF_DUR`/`CENTER_FX` 등록 + edit_prompt.md 표에 추가
- 효과음 추가: `sfx_gen.py`의 `ALL`에 등록 후 `python sfx_gen.py <이름>`
- 수정 후 `bash make_kit.sh`로 `autoedit-kit.zip`도 갱신 (다른 대화의 계정 스킬용)
