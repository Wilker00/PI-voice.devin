#!/usr/bin/env bash
# Hybrid inference wrapper for PI Voice — runs ON the Pi.
# Forwards MeanVC2 conversions over HTTP to the Starbook relay
# (10.104.0.61:8081, vc_relay.py). The Pi keeps capture, VAD segmentation,
# utterance buffering, profiles, playback, HUD and web serving.
#
# Called as: audiocpp_remote.sh [--task vc] [--family meanvc2] [--model X]
#               --audio in.wav --voice-ref ref.wav --out out.wav [...]
set -u

RELAY="${PIVC_RELAY:-http://10.104.0.61:8081}"

IN=""; REF=""; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --audio)     IN="$2";  shift 2 ;;
    --voice-ref) REF="$2"; shift 2 ;;
    --out)       OUT="$2"; shift 2 ;;
    *) shift $(( $# >= 2 ? 2 : 1 )) ;;   # ignore --task/--family/--model/etc
  esac
done
if [ -z "$IN" ] || [ -z "$REF" ] || [ -z "$OUT" ]; then
  echo "audiocpp_remote: --audio/--voice-ref/--out required" >&2
  exit 2
fi

code=$(curl -s -m 150 -o "$OUT.part" -w "%{http_code}" -X POST "$RELAY/convert" \
  -F "audio=@$IN" -F "ref=@$REF")
if [ "$code" = "200" ]; then
  mv "$OUT.part" "$OUT"
  exit 0
fi
echo "audiocpp_remote: relay returned HTTP $code" >&2
rm -f "$OUT.part"
exit 3
