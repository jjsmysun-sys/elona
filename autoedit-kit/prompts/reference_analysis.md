# 레퍼런스 영상 분석

## 대상 (2026-10-05 요청)
| # | 링크 | 상태 |
|---|---|---|
| 1 | https://youtu.be/Rso_ysW0ozk | 포스텔러 X 게시물에서 공유된 사주(팔자) 영상으로 확인. 내용 미시청 |
| 2 | https://youtu.be/fdceKyw-QX8 | 미확인 |
| 3 | https://youtu.be/0OOcCIHMd2k | 미확인 |
| 4 | https://youtu.be/-AfMXUo93oI | 미확인 |
| 5 | https://youtu.be/7CmGaeKamdc | 미확인 |

**클라우드 세션의 네트워크 정책이 youtube.com을 막고 있어서 영상을 직접 보지 못했다.**
그래서 지금 들어 있는 프리셋(`mystic`/`news`/`default`)은 한국 사주·정보형 유튜브의 일반적인 문법을 기준으로 만들었고,
아래 방법 중 하나로 레퍼런스를 받으면 수치로 보정한다.

## 레퍼런스로 보정하는 방법
1. **영상 파일**(가장 정확): 레퍼런스 mp4를 업로드 → 
   ```
   python autoedit.py ref ref1.mp4 ref2.mp4 ... --work R
   ```
   → 컷/분, 평균 샷 길이, LUFS, 컨택트시트(`R/*_sheet.jpg`), 프레임 10장(`R/*_frames/`)
2. **스크린샷**: 자막·모션그래픽이 보이는 장면 캡처 5~10장
3. **네트워크 허용**: 환경 설정 → Network access → Custom → `www.youtube.com`, `*.googlevideo.com` 추가 → yt-dlp로 직접 다운로드해서 1번 진행

## 프레임을 보고 기록할 항목 → styles.json 반영
| 항목 | 보는 곳 | styles.json 키 |
|---|---|---|
| 자막 폰트(고딕/명조/손글씨, 굵기) | 하단 자막 | `fonts.caption` |
| 자막 크기(화면 높이 대비 %) | 하단 자막 | `caption.size` (1080p 기준 px = % × 10.8) |
| 자막 색/외곽선/박스 여부 | 하단 자막 | `caption.color/outline/outline_color/box` |
| 자막 위치(하단에서 거리) | 하단 자막 | `caption.margin_v` |
| 한 줄 글자 수 | 하단 자막 | `caption.max_chars` |
| 강조 단어 색 | 자막 속 컬러 단어 | `caption.highlight` |
| 메인 컬러/포인트 컬러 | 타이틀·박스 | `accent`, `accent2`, `panel` |
| 줌인 빈도/배율 | 컨택트시트 | `zoom`, `punch_zoom` |
| 컷 빈도 | ref.json cuts_per_min | `--tight` |
| 색보정(따뜻함/채도/비네팅) | 전체 톤 | `grade`, `vignette` |
| 쇼츠 레이아웃(상단 헤드라인/풀화면) | 쇼츠 | `shorts.layout`, `video_y` |
| 효과음 성격 | 영상 소리 | `sfx` 매핑 |

새 프리셋 이름은 `ref_<채널약칭>`처럼 짓고, 이후 해당 장르의 기본값으로 쓴다.

## 현재 기본 가정 (보정 전)
- 사주·운세 롱폼: 정면 토크 + 하단 1줄 자막(흰 글씨, 진한 외곽선, 핵심어 금색), 문장 단위 점프컷, 1.0↔1.1↔1.2 줌 교차,
  주제 전환마다 번호 챕터 카드, 핵심 개념은 박스 키워드, 격언은 명조체 인용, 종소리/반짝임 효과음, 남보라 톤 + 비네팅
- 정보형(ELONA): 노랑 강조·빨강 포인트, 더 빠른 컷(tight), 숫자 stat 카드, 스팅 효과음
- 쇼츠: 상단 2줄 헤드라인(강조어 컬러), 중앙 정사각 영상, 하단 큰 자막
