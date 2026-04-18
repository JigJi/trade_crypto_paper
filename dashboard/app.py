"""Dashboard server - Flask + Flask-SocketIO."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import glob
import markdown
from flask import Flask, render_template, abort
from flask_socketio import SocketIO

app = Flask(__name__)
app.config["SECRET_KEY"] = "trade-crypto-dashboard-2026"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Register API routes
from dashboard.api import api_bp
app.register_blueprint(api_bp, url_prefix="/api")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/mission/<mission_id>")
def mission_report(mission_id):
    """Render mission .md as a styled HTML page.

    Supports both old format (mission_id=3 → mission_003_*.md)
    and new format (mission_id=mission_20260318_145817_a → exact match).
    """
    missions_dir = BASE_DIR / "missions"
    matches = []

    # Try old numeric format first (e.g. "3" or "003")
    try:
        num = int(mission_id)
        matches = glob.glob(str(missions_dir / f"mission_{num:03d}_*.md"))
    except ValueError:
        pass

    # Try exact mission_id match (new format: mission_20260318_145817_a)
    if not matches:
        mid = mission_id if mission_id.startswith("mission_") else f"mission_{mission_id}"
        matches = glob.glob(str(missions_dir / f"{mid}*.md"))

    if not matches:
        # Fallback: show insight from missions.json instead of 404
        try:
            from research.missions import MissionEngine
            me = MissionEngine()
            for m in me.get_recent(100):
                if m.get("mission_id") == mission_id or m.get("mission_id") == f"mission_{mission_id}":
                    # Render inline
                    import json
                    body_lines = [
                        f"# {m.get('title', mission_id)}",
                        f"**Date:** {m.get('date', 'N/A')}  ",
                        f"**Type:** {m.get('type', 'N/A')}  ",
                        f"**Status:** {m.get('status', 'N/A')}  ",
                        f"**XP:** +{m.get('xp_reward', 0)}  ",
                        "",
                        f"## Insight",
                        m.get("insight", "N/A"),
                        "",
                        f"## Result",
                        f"```json\n{json.dumps(m.get('result', {}), indent=2, ensure_ascii=False)}\n```",
                    ]
                    html_body = markdown.markdown(
                        "\n".join(body_lines),
                        extensions=["tables", "fenced_code", "nl2br"],
                    )
                    return render_template("mission_report.html", body=html_body, mission_id=mission_id)
        except Exception:
            pass
        abort(404)

    md_text = Path(matches[0]).read_text(encoding="utf-8")
    html_body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "nl2br"])
    return render_template("mission_report.html", body=html_body, mission_id=mission_id)


# ── WebSocket Events ──────────────────────────────────────────
@socketio.on("connect")
def handle_connect():
    from dashboard.api import _get_snapshot
    socketio.emit("snapshot", _get_snapshot())


@socketio.on("request_update")
def handle_request_update():
    from dashboard.api import _get_snapshot
    socketio.emit("snapshot", _get_snapshot())


def push_update(event: str, data: dict):
    """Push real-time update to all connected clients."""
    socketio.emit(event, data)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Enable debug/auto-reload")
    args = parser.parse_args()

    debug = args.debug or os.environ.get("FLASK_DEBUG", "").strip() == "1"
    print("=" * 60)
    print("  TRADING R&D DASHBOARD")
    print(f"  http://localhost:5000  (debug={debug})")
    print("=" * 60)
    socketio.run(app, host="0.0.0.0", port=5000, debug=debug, allow_unsafe_werkzeug=True)
