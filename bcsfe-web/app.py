"""Web front-end for BCSFE (Battle Cats Save File Editor) — Korean edition.

Run it with:  python app.py   then open http://localhost:8000

Routes:
  GET  /           – the editor page (static/index.html)
  POST /api/edit   – run an edit; form fields described in static/index.html
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------- config ----
HERE = Path(__file__).resolve().parent
DATA_DIR = os.path.abspath(os.environ.get("BCSFE_DATA_DIR", str(HERE / "bcsfe-data")))
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "60"))
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "300"))
# Set to 1 when running behind a reverse proxy (Render, Railway, nginx…) so the
# real visitor IP is used for rate limiting.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
WORKER = str(HERE / "worker.py")

COUNTRIES = {"en", "jp", "kr", "tw"}
NUMERIC = [
    "catfood", "xp", "np", "leadership", "normal_tickets", "rare_tickets",
    "platinum_tickets", "legend_tickets", "platinum_shards",
]
TOGGLES = [
    "unlock_cats", "true_form_cats", "max_battle_items", "max_catseyes",
    "max_catamins", "max_treasure_chests",
]
I32_MAX = 2_147_483_647
FIELD_NAMES = {
    "catfood": "고양이 통조림", "xp": "경험치", "np": "NP", "leadership": "통솔력",
    "normal_tickets": "냥코 티켓", "rare_tickets": "레어 티켓",
    "platinum_tickets": "플래티넘 티켓", "legend_tickets": "레전드 티켓",
    "platinum_shards": "플래티넘의 조각",
}

log = logging.getLogger("bcsfe-web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # save files are ~100 KB–1 MB

job_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
state_lock = threading.Lock()
active_ips: set[str] = set()
last_run: dict[str, float] = {}


def client_ip() -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    job_dir = tempfile.mkdtemp(prefix="bcsfe-job-")
    job = {**job, "data_dir": DATA_DIR, "job_dir": job_dir}
    try:
        if not job_slots.acquire(timeout=120):
            return {"ok": False, "error": "지금은 에디터가 바빠요. 1분 뒤에 다시 시도하세요."}
        try:
            proc = subprocess.run(
                [sys.executable, WORKER],
                input=json.dumps(job).encode(),
                capture_output=True,
                timeout=JOB_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "작업이 너무 오래 걸려 중단됐어요. 나중에 다시 시도하세요."}
        finally:
            job_slots.release()
        if proc.returncode != 0 or not proc.stdout:
            log.error("worker failed (rc=%s): %s", proc.returncode, proc.stderr.decode(errors="replace")[-2000:])
            return {"ok": False, "error": "에디터에 오류가 발생했어요. 서버 로그를 확인하세요."}
        return json.loads(proc.stdout)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def parse_edits(form) -> dict[str, Any] | str:
    edits: dict[str, Any] = {}
    for key in NUMERIC:
        raw = (form.get(key) or "").strip().replace(",", "")
        if not raw:
            continue
        if not raw.isdigit():
            return f"{FIELD_NAMES[key]}에는 정수만 입력할 수 있어요."
        edits[key] = min(int(raw), I32_MAX)
    for key in TOGGLES:
        if form.get(key) in ("1", "true", "on"):
            edits[key] = True
    return edits


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/edit")
def edit():
    form = request.form
    mode = form.get("mode")
    if mode not in ("codes", "file"):
        return fail("알 수 없는 모드예요.")

    country = (form.get("country") or "").strip().lower() or None
    if country is not None and country not in COUNTRIES:
        return fail("알 수 없는 국가예요.")

    edits = parse_edits(form)
    if isinstance(edits, str):
        return fail(edits)

    job: dict[str, Any] = {"mode": mode, "cc": country, "edits": edits}

    if mode == "codes":
        tc = (form.get("transfer_code") or "").strip()
        pin = (form.get("confirmation_code") or "").strip()
        if not tc or not pin:
            return fail("이어하기 코드와 인증번호를 모두 입력하세요.")
        if len(tc) > 64 or len(pin) > 16 or not tc.isalnum() or not pin.isalnum():
            return fail("코드 형식이 올바르지 않아요. 게임에 표시된 그대로 입력하세요.")
        if country is None:
            return fail("게임 국가를 선택하세요.")
        if not edits:
            return fail("편집할 항목을 하나 이상 고르세요. 이어하기 코드는 한 번만 쓸 수 있어서, "
                        "변경 사항과 함께 세이브를 다시 업로드해야 해요.")
        job.update(transfer_code=tc, confirmation_code=pin,
                   new_account=form.get("new_account") in ("1", "true", "on"))
    else:
        upload = request.files.get("save_file")
        if upload is None or not upload.filename:
            return fail("SAVE_DATA 파일을 선택하세요.")
        data = upload.read()
        if not data:
            return fail("빈 파일이에요.")
        job["file_b64"] = base64.b64encode(data).decode()

    ip = client_ip()
    with state_lock:
        if ip in active_ips:
            return fail("이미 진행 중인 편집이 있어요. 끝날 때까지 기다려 주세요.", 429)
        wait = COOLDOWN_SECONDS - (time.monotonic() - last_run.get(ip, -1e9))
        if wait > 0:
            return fail(f"잠시만요 — {int(wait) + 1}초 뒤에 다시 편집할 수 있어요.", 429)
        active_ips.add(ip)
    try:
        result = run_job(job)
    finally:
        with state_lock:
            active_ips.discard(ip)
            last_run[ip] = time.monotonic()

    if mode == "file" and not edits:
        result.pop("edited_b64", None)  # nothing changed; just show info
    return jsonify(result)


@app.errorhandler(413)
def too_large(_):
    return fail("세이브 파일이라기엔 너무 큰 파일이에요.", 413)


def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        from waitress import serve
    except ImportError:
        log.warning("waitress not installed; using Flask's development server")
        app.run(host=HOST, port=PORT)
        return
    log.info("BCSFE web editor running on http://localhost:%s", PORT)
    serve(app, host=HOST, port=PORT, threads=8)


if __name__ == "__main__":
    main()
