#!/usr/bin/env bash
# Ask the llama-server a question. Usage:  ./ask.sh "Who are you?"
#
# Exists because the working curl is one long line, and a long line pasted into
# a wrapped terminal picks up a newline inside the Authorization header, which
# corrupts the request and returns nothing at all.
set -uo pipefail

B="${QWEN_BASE_URL:-${B:-http://10.11.245.41:8091}}"
K="${QWEN_API_KEY:-${K:-pick-a-long-secret-string}}"
Q="${1:-Who are you?}"
MAX="${MAX_TOKENS:-4000}"

# Build the JSON with python so quotes/newlines in the question cannot break it.
BODY=$(python3 -c 'import json,sys; print(json.dumps({
  "messages":[{"role":"user","content":sys.argv[1]}],
  "max_tokens":int(sys.argv[2]), "temperature":0.1}))' "$Q" "$MAX")

RESP=$(curl -sS --max-time 300 "$B/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $K" \
  --data-binary "$BODY") || { echo "curl failed (is $B reachable?)" >&2; exit 1; }

[ -z "$RESP" ] && { echo "empty response from $B" >&2; exit 1; }

python3 - "$RESP" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print("not JSON:", sys.argv[1][:400]); raise SystemExit(1)
if "error" in d:
    print("server error:", d["error"]); raise SystemExit(1)
c = d["choices"][0]
print(f"[finish={c.get('finish_reason')}  {d.get('usage')}]\n")
txt = (c["message"].get("content") or "").strip()
print(txt or "(empty - the model spent the whole budget reasoning; raise MAX_TOKENS)")
PY
