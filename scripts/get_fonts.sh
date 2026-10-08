#!/usr/bin/env sh
# Child-friendly rounded "Baloo" fonts (SIL OFL) covering Latin + Indian scripts, plus Arabic/Urdu and
# Japanese fallbacks. Used for the animated on-screen lyrics. Run once (the Dockerfile does it).
set -e
DEST="${1:-fonts}"
mkdir -p "$DEST"
BASE="https://raw.githubusercontent.com/google/fonts/main/ofl"
for f in baloo2/Baloo2 balooda2/BalooDa2 baloobhai2/BalooBhai2 baloochettan2/BalooChettan2 \
         baloopaaji2/BalooPaaji2 balootamma2/BalooTamma2 balootammudu2/BalooTammudu2 \
         baloothambi2/BalooThambi2 notonaskharabic/NotoNaskhArabic notosansjp/NotoSansJP; do
  name=$(basename "$f")
  [ -s "$DEST/$name.ttf" ] || curl -fsSL "$BASE/${f}%5Bwght%5D.ttf" -o "$DEST/$name.ttf"
done
echo "fonts in $DEST:"; ls "$DEST"
