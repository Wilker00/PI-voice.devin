#!/usr/bin/env bash
# Start PI Voice with the real VC model (audio.cpp MeanVC2, utterance mode).
# Override any of these env vars before invoking.
set -euo pipefail
cd "$(dirname "$0")"

export MODEL_MODULE="${MODEL_MODULE:-server.vc_model}"
export AUDIOCPP_BIN="${AUDIOCPP_BIN:-$HOME/audiocpp/audiocpp_cli}"
export AUDIOCPP_MODEL="${AUDIOCPP_MODEL:-$HOME/audiocpp/model/meanvc2}"
export AUDIOCPP_THREADS="${AUDIOCPP_THREADS:-3}"
export VC_STATS_FILE="${VC_STATS_FILE:-/tmp/vc_stats.json}"

# fall back to the mock backend with a clear log line if the binary is absent
if [ ! -x "$AUDIOCPP_BIN" ]; then
  echo "WARNING: $AUDIOCPP_BIN not found — starting with mock passthrough" >&2
  export VC_BACKEND=mock
fi

exec ./start.sh
