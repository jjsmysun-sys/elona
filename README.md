# elona

## 영상 자동 편집
원본 영상 또는 **대본만** 주면 Claude가 TTS·스톡 영상 수집·컷편집·자막·모션그래픽·효과음·쇼츠까지 완성한다.
- 대본 모드 설정(최초 1회): 환경 변수 `PEXELS_API_KEY`, `PIXABAY_API_KEY` + 네트워크 허용 `speech.platform.bing.com`, `api.pexels.com`, `videos.pexels.com`, `images.pexels.com`, `pixabay.com`, `cdn.pixabay.com`
- 키트: [`autoedit-kit/`](autoedit-kit/README.md) (계정 스킬용 압축본: `autoedit-kit.zip`)
- Claude Code 스킬: `.claude/skills/youtube-autoedit/SKILL.md`
- 편집 규칙: [`autoedit-kit/prompts/edit_prompt.md`](autoedit-kit/prompts/edit_prompt.md)
