#!/usr/bin/env bash
# 계정 스킬(youtube-autoedit)이 쓰는 autoedit-kit.zip 생성 → 레포 루트에 저장
set -e
cd "$(dirname "$0")/.."
rm -f autoedit-kit.zip
zip -qr autoedit-kit.zip autoedit-kit -x "autoedit-kit/__pycache__/*" "autoedit-kit/make_kit.sh"
ls -lh autoedit-kit.zip
