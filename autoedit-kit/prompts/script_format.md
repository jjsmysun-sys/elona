# 대본 → 영상 (script2video)

사용자는 **대본만** 보낸다. Claude가 TTS, 자료 수집, 모션그래픽, 편집, 쇼츠까지 만든다.
대본이 아래 문법을 안 지켜도 된다. Claude가 받아서 이 문법으로 **다듬은 사본**(W/script.txt)을 만들어 쓴다(내용·말투는 바꾸지 않음).

## 대본 문법
```
# 제목: 재물운 터지는 사주          ← 첫 3초 인트로 타이틀 (12자 내외)
# 부제: 3가지 특징

[화면: night sky stars]            ← 이후 장면의 스톡 영상 검색어 (영어가 결과가 좋다)
[사진: old calendar pages]         ← 영상 대신 사진(켄번스 줌)으로
안녕하세요. 오늘은 재물운이 들어오는 사주를 알려드릴게요.
[목록: 재물운 사주 3가지 | 식신생재 구조 | 대운의 재성 | 물을 만난 사주]

## 식신생재 구조                    ← 챕터 카드 + 유튜브 챕터
첫 번째는 **식신생재** 구조입니다.  ← **굵게** = 자막 하이라이트 키워드
[키워드: 식신생재]                  ← 큰 키워드 박스
[숫자: 40세 | 인생의 전환점]         ← 숫자 강조 카드
[자막: ※ 대운은 10년 주기로 바뀝니다] ← 상단 보충 설명 박스
[이미지: /path/chart.png | full]   ← 직접 준 이미지 (pip 또는 full)
!! 인생 역전                        ← 임팩트 (읽지 않음, 화면에만) + 펀치 줌
> 때를 아는 사람이 부를 얻는다.      ← 인용 카드 (읽음, 명조체 크게)
[쉼: 1.2]                           ← 쉼 (초)
// 메모                             ← 무시
```
- 지시어는 **바로 다음 문장이 시작될 때** 화면에 뜬다.
- 빈 줄 = 문단 구분(조금 더 쉼). 문장은 `. ? !` 기준으로 나눠서 읽는다.

## 실행
```bash
cd autoedit-kit
python script2video.py prepare W/script.txt --work W --style mystic --voice female   # TTS + 장면 분할
#   → W/scenes.json 의 scenes[].query 를 채운다 (아래 규칙)
python script2video.py build W                                                        # 자료 다운로드·합성·BGM·plan.json
python autoedit.py render W/plan.json --preview                                       # 미리보기 → 프레임 검수
python autoedit.py render W/plan.json                                                 # 최종
```
옵션: `--voice female|male|male2|<Edge 보이스명>`, `--rate +8%`(말 빠르기), `--pace fast|normal|slow`(문장 사이 쉼),
`--aspect 9:16`(세로 영상 전용), `--tts dummy`(네트워크 없이 레이아웃만 미리보기). build: `--no-bgm`, `--bgm-volume 0.1`

## scenes.json 검색어 작성 규칙 (편집자 판단)
장면마다 `query`(영어 2~4단어)를 쓴다. 이미 대본 지시어로 채워진 장면(`src: "directive"`)은 그대로 둔다.
- **문장의 '구체적 이미지'를 찍는다.** 추상어 그대로 검색하지 않는다.
  - "재물운" → `gold coins falling`, "기회가 찾아온다" → `door opening light`, "물이 부족한 사주" → `dry cracked earth`, "물을 만난다" → `rain drops water surface`
  - "대운이 바뀐다" → `seasons changing timelapse`, "인간관계" → `people silhouettes sunset`, "불안" → `storm clouds dark`
- 스타일 톤 맞추기: mystic은 `dark`, `night`, `candle`, `moon`, `mist`, `ink` 같은 어두운·신비로운 단어를 붙인다. news는 `business`, `chart`, `city`, `screen`.
- 같은 검색어를 3장면 넘게 연속으로 쓰지 않는다. 같은 검색어라도 클립은 자동으로 겹치지 않게 다른 것을 고른다.
- 사람 얼굴이 크게 나오는 클립은 자막·인물 혼동을 부르니 피한다 → `hands`, `silhouette`, `back view`를 붙인다.
- 장면 옵션: `"photo": true`(사진으로), `"pick": 2`(검색 결과 중 3번째, 결과가 마음에 안 들 때), `"gradient": true`(스타일 배경)
- 결과가 이상하면 `W/clips/cNNN.mp4`를 보고 query나 pick만 바꿔 `build`를 다시 한다(검색·다운로드는 캐시됨).

## build 이후 plan.json 다듬기
`prompts/edit_prompt.md` 규칙 그대로. script 모드에서는 컷(remove)이 필요 없고, fx·shorts·keywords만 손본다.
- 대본 지시어가 없는 구간에도 keyword/stat/impact를 15~25초에 1개꼴로 추가
- shorts 2~4개 지정(원본 기준 초 = 내레이션 기준 초)
- `credits.txt`(출처)는 영상 설명란용으로 함께 전달

## 필요 설정 (최초 1회)
| 항목 | 값 |
|---|---|
| 환경 변수 | `PEXELS_API_KEY`, `PIXABAY_API_KEY` (무료 발급: pexels.com/api, pixabay.com/api/docs) |
| 네트워크 허용 | `speech.platform.bing.com`, `api.pexels.com`, `videos.pexels.com`, `images.pexels.com`, `pixabay.com`, `cdn.pixabay.com` |
| 패키지 | `pip install edge-tts` |

키가 없으면 장면 배경은 스타일 색 그라데이션, TTS가 막히면 prepare가 멈추고 이유를 알려준다.
BGM은 스타일별 앰비언트 패드를 직접 합성한다(저작권 없음). 사용자가 BGM 파일을 주면 plan.json `bgm.path`를 교체.
