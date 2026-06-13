#!/usr/bin/env bash
#
# Luminara — run the whole WhatsApp stack with one command:
#   1. Flask webhook   (port $PORT, background — 5000 is taken by macOS AirPlay)
#   2. ngrok tunnel    (public URL for the Twilio sandbox webhook, background)
#   3. Streamlit app   (foreground — Ctrl-C stops everything)
#
# Usage:  ./run_with_whatsapp.sh
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Webhook port. NOTE: port 5000 is reserved by macOS AirPlay Receiver
# (Control Center → "AirTunes"), which returns HTTP 403, so we use 5050.
# Disable AirPlay Receiver in System Settings if you ever need 5000 back.
PORT=5050

# ── 0. Resolve the SYSTEM ngrok binary ────────────────────────────────────────
# Use the Homebrew install at /usr/local/bin/ngrok. Resolve this BEFORE activating
# the venv, since activation prepends venv/bin to PATH and could shadow it.
SYSTEM_NGROK="/usr/local/bin/ngrok"
if [ ! -x "$SYSTEM_NGROK" ]; then
  SYSTEM_NGROK="$(command -v ngrok || true)"  # fall back to PATH (e.g. Apple-silicon brew)
fi
if [ -z "$SYSTEM_NGROK" ] || [ ! -x "$SYSTEM_NGROK" ]; then
  echo "❌ ngrok not found at /usr/local/bin/ngrok or on PATH. Install it"
  echo "   ('brew install ngrok') and set your authtoken once:"
  echo "   ngrok config add-authtoken <YOUR_TOKEN>"
  exit 1
fi

# ── 1. Activate the virtualenv ────────────────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "❌ venv/ not found. Create it and install requirements first:"
  echo "   python3 -m venv venv && venv/bin/pip install -r requirements.txt"
  exit 1
fi
# shellcheck disable=SC1091
source venv/bin/activate

# ── 2. Load .env variables ────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  echo "❌ .env not found. Copy .env.template to .env and fill in your values."
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

# ── Clean up all background processes on exit ─────────────────────────────────
PIDS=()
cleanup() {
  echo ""
  echo "🛑 Shutting down webhook + ngrok..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  # Stop any lingering ngrok tunnels too.
  pkill -f "ngrok http $PORT" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── 3. Start the Flask webhook (background) ───────────────────────────────────
echo "🚀 Starting Flask webhook on http://localhost:$PORT ..."
# -u (unbuffered) so our print() log lines flush to flask.log immediately,
# instead of being block-buffered until the process exits.
python -u -m flask --app webhook run --port "$PORT" --host 127.0.0.1 > flask.log 2>&1 &
FLASK_PID=$!
PIDS+=("$FLASK_PID")

# Give Flask a moment, then confirm it didn't crash on startup.
sleep 3
if ! kill -0 "$FLASK_PID" 2>/dev/null; then
  echo ""
  echo "❌ Flask webhook crashed on startup. Last lines of flask.log:"
  echo "────────────────────────────────────────────────────────────────────"
  tail -n 30 flask.log
  echo "────────────────────────────────────────────────────────────────────"
  exit 1
fi

# Flask imports pandas, so it can take several seconds to serve — wait until the
# /health endpoint actually responds (and catch a late bind failure / crash).
echo "⏳ Waiting for the webhook to be ready..."
READY=""
for i in $(seq 1 30); do
  if [ -n "$(curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null)" ]; then
    READY="yes"; break
  fi
  if ! kill -0 "$FLASK_PID" 2>/dev/null; then
    echo ""
    echo "❌ Flask webhook exited while starting. Last lines of flask.log:"
    echo "────────────────────────────────────────────────────────────────────"
    tail -n 30 flask.log
    echo "────────────────────────────────────────────────────────────────────"
    exit 1
  fi
  sleep 1
done
if [ -z "$READY" ]; then
  echo "❌ Flask webhook did not become ready in time. See flask.log."
  tail -n 30 flask.log
  exit 1
fi
echo "✅ Flask webhook is running (pid $FLASK_PID). Logs → flask.log"

# ── 4. Start ngrok and print the public URL ───────────────────────────────────
echo "🌐 Opening ngrok tunnel to port $PORT (using $SYSTEM_NGROK) ..."
# Use the system ngrok binary directly (resolved in step 0, before venv shadowing).
"$SYSTEM_NGROK" http "$PORT" --log=stdout > ngrok.log 2>&1 &
PIDS+=($!)

# Give ngrok a moment to start, then fetch the public URL from its local API.
sleep 2
PUBLIC_URL=""
for i in $(seq 1 15); do
  PUBLIC_URL="$(curl -s http://localhost:4040/api/tunnels \
    | python -c 'import sys,json;
d=json.load(sys.stdin);
t=[x["public_url"] for x in d.get("tunnels",[]) if x["public_url"].startswith("https")];
print(t[0] if t else "")' 2>/dev/null)"
  [ -n "$PUBLIC_URL" ] && break
  sleep 1
done

if [ -z "${PUBLIC_URL:-}" ]; then
  echo "❌ Could not get an ngrok URL. See ngrok.log. If this is your first run,"
  echo "   set your free ngrok authtoken once:"
  echo "   ngrok config add-authtoken <YOUR_TOKEN>   (https://dashboard.ngrok.com)"
  exit 1
fi

echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  📋 Twilio Sandbox webhook URL — paste this into the Twilio console:"
echo ""
echo "     ${PUBLIC_URL}/webhook"
echo ""
echo "  (Twilio Console → Messaging → Try it out → WhatsApp sandbox settings"
echo "   → 'When a message comes in' → set to the URL above, method POST)"
echo "════════════════════════════════════════════════════════════════════"
echo ""

# ── 5. Start Streamlit (foreground) ───────────────────────────────────────────
echo "📊 Starting Streamlit dashboard (Ctrl-C to stop everything)..."
streamlit run app.py
