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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

import extras

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


jobs: dict[str, dict[str, Any]] = {}   # job id -> running edit, so /api/cancel can find it
JOB_ID = re.compile(r"[0-9a-f]{16,64}")
CANCEL_FLAG, EDITING_MARK = "cancel", "editing"   # shared with worker.py
CANCEL_TEXT = {'early': '세이브를 내려받기 전에 편집을 취소했어요. 아무것도 바뀌지 않았고 이어하기 코드도 그대로 쓸 수 있어요.', 'file': '편집을 취소했어요. 아무것도 바뀌지 않았어요.', 'late': '취소하기엔 늦었어요. 이미 편집을 적용해서 저장하는 중이에요. 새 코드가 나올 때까지 기다려 주세요.', 'nothing': '취소할 편집이 없어요.'}


def cancelled_result(mode: str) -> dict[str, Any]:
    return {"ok": False, "cancelled": True, "error": CANCEL_TEXT["early" if mode == "codes" else "file"]}


def run_job(job: dict[str, Any], job_id: str = "", ip: str = "") -> dict[str, Any]:
    job_dir = tempfile.mkdtemp(prefix="bcsfe-job-")
    job = {**job, "data_dir": DATA_DIR, "job_dir": job_dir}
    entry: dict[str, Any] = {"ip": ip, "dir": job_dir, "mode": job["mode"], "proc": None,
                              "cancelled": False, "t": time.monotonic()}
    if job_id:
        with state_lock:
            early = jobs.get(job_id)  # a cancel can arrive before the edit request is registered
            entry["cancelled"] = bool(early and early["ip"] == ip and early["cancelled"])
            jobs[job_id] = entry
    try:
        deadline = time.monotonic() + 120
        while not job_slots.acquire(timeout=1):
            if entry["cancelled"]:
                return cancelled_result(job["mode"])
            if time.monotonic() > deadline:
                return {"ok": False, "error": "지금은 에디터가 바빠요. 1분 뒤에 다시 시도하세요."}
        try:
            with state_lock:
                if entry["cancelled"]:
                    return cancelled_result(job["mode"])
                proc = subprocess.Popen([sys.executable, WORKER], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                entry["proc"] = proc
            try:
                out, err = proc.communicate(json.dumps(job).encode(), timeout=JOB_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return {"ok": False, "error": "작업이 너무 오래 걸려 중단됐어요. 나중에 다시 시도하세요."}
        finally:
            job_slots.release()
        if entry["cancelled"] and job["mode"] == "file":
            return cancelled_result("file")  # killed; nothing was uploaded anywhere
        if proc.returncode != 0 or not out:
            log.error("worker failed (rc=%s): %s", proc.returncode, err.decode(errors="replace")[-2000:])
            return {"ok": False, "error": "에디터에 오류가 발생했어요. 서버 로그를 확인하세요."}
        return json.loads(out)
    finally:
        if job_id:
            with state_lock:
                if jobs.get(job_id) is entry:
                    del jobs[job_id]
        shutil.rmtree(job_dir, ignore_errors=True)


def parse_ids(raw: str, limit: int = 5000) -> list[int] | None:
    """Parse "1, 5, 10-12" into [1, 5, 10, 11, 12]; None if malformed."""
    ids: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        lo, sep, hi = part.partition("-")
        if not lo.isdigit() or (sep and not hi.isdigit()):
            return None
        a, b = int(lo), int(hi) if sep else int(lo)
        if a > b or b >= limit:
            return None
        ids.update(range(a, b + 1))
    return sorted(ids)


LEVEL_MAX_WORDS = ("max", "최대")


def parse_level(text: str) -> tuple[Any, Any] | None:
    """Parse "50+10", "50", "+10", "max+max" into (base, plus); None = keep, "max" = cap."""
    base_s, sep, plus_s = text.replace(" ", "").lower().partition("+")

    def part(s: str, low: int) -> Any:
        if s in LEVEL_MAX_WORDS:
            return "max"
        if s.isdigit() and low <= int(s) <= 9999:
            return int(s)
        raise ValueError(s)

    try:
        base = part(base_s, 1) if base_s else None
        plus = part(plus_s, 0) if sep and plus_s else None
    except ValueError:
        return None
    if base is None and plus is None:
        return None
    return base, plus


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

    # Story stages: which chapters (0-8), whether to clear them, and a treasure level.
    raw = (form.get("story_chapters") or "").strip()
    chapters: list[int] = []
    if raw:
        parts = raw.split(",")
        if not all(p.strip().isdigit() and int(p) <= 8 for p in parts):
            return '알 수 없는 스토리 챕터예요.'
        chapters = sorted({int(p) for p in parts})
    treasure = (form.get("treasure_level") or "").strip()
    if treasure and treasure not in ("0", "1", "2", "3"):
        return '알 수 없는 보물 등급이에요.'
    clear_story = form.get("clear_story") in ("1", "true", "on")
    if (clear_story or treasure) and not chapters:
        return '스토리 스테이지 편집에 쓸 챕터를 하나 이상 고르세요.'
    if clear_story:
        edits["clear_story"] = True
    if treasure:
        edits["treasure_level"] = int(treasure)
    if clear_story or treasure:
        edits["story_chapters"] = chapters

    # Individual characters: "25, 100-110" style ID lists; rarity groups 0-5.
    for key in ("add_cats", "remove_cats"):
        ids = parse_ids(form.get(key) or "")
        if ids is None:
            return '캐릭터 ID는 25나 100-110처럼 숫자로 입력하세요.'
        if ids:
            edits[key] = ids
    rarities = parse_ids(form.get("add_rarities") or "")
    if rarities is None or any(r > 5 for r in rarities):
        return '알 수 없는 등급이에요.'
    if rarities:
        edits["add_rarities"] = rarities

    # Level up: "50+10" style text for all owned or picked characters.
    level = (form.get("upgrade_level") or "").strip()
    if level:
        parsed = parse_level(level)
        if parsed is None:
            return '레벨은 50+10, 30, +20 또는 최대처럼 입력하세요.'
        target = form.get("upgrade_target") or "all"
        ids = parse_ids(form.get("upgrade_ids") or "") if target == "picked" else []
        if target not in ("all", "picked") or ids is None:
            return 'Lv 버튼으로 레벨업할 캐릭터를 고르거나 보유한 모든 캐릭터를 선택하세요.'
        if target == "picked" and not ids:
            return 'Lv 버튼으로 레벨업할 캐릭터를 고르거나 보유한 모든 캐릭터를 선택하세요.'
        edits["upgrade"] = {"base": parsed[0], "plus": parsed[1], "target": target, "ids": ids}
    elif form.get("upgrade_ids"):
        return '레벨업할 레벨을 입력하세요.'

    # Catfruit/seeds, behemoth stones/gems, catseyes: "index:amount,index:amount".
    for group, limit in (("fruit", 998), ("stone", 998), ("eye", 9999), ("battle", 9999), ("drink", 9999), ("chest", 9999),
                         ("material", 9999), ("medal", 9999)):
        raw = (form.get(f"items_{group}") or "").replace(" ", "")
        if not raw:
            continue
        values: dict[int, int] = {}
        for pair in raw.split(","):
            index, _, amount = pair.partition(":")
            if not (index.isdigit() and amount.isdigit()) or int(index) >= 500:
                return '아이템 개수는 정수로 입력하세요.'
            values[int(index)] = min(int(amount), limit)
        edits[f"items_{group}"] = values

    # Talent orbs: an amount for all orb types, or for those matching grade/trait/effect filters.
    orb_mode = form.get("orb_mode") or ""
    if orb_mode:
        amount = (form.get("orb_count") or "").strip().replace(",", "")
        if not amount:
            return '구슬 개수를 입력하세요.'
        if not amount.isdigit() or int(amount) > 998:
            return '구슬 개수는 0부터 998까지 숫자로 입력하세요.'
        spec: dict[str, Any] = {"count": int(amount), "all": orb_mode == "all"}
        if orb_mode == "filter":
            for key, limit in (("grades", 10), ("effects", 100)):
                ids = parse_ids(form.get(f"orb_{key}") or "", limit)
                if ids is None:
                    return '알 수 없는 구슬 카테고리예요.'
                spec[key] = ids
            traits = [t for t in (form.get("orb_traits") or "").split(",") if t]
            if not all(t == "-1" or (t.isdigit() and int(t) < 100) for t in traits):
                return '알 수 없는 구슬 카테고리예요.'
            spec["traits"] = [int(t) for t in traits]
            if not (spec["grades"] or spec["traits"] or spec["effects"]):
                return '등급, 속성, 효과 중 하나 이상을 고르거나 모든 구슬을 선택하세요.'
        elif orb_mode != "all":
            return '알 수 없는 구슬 카테고리예요.'
        edits["orbs"] = spec

    # Legend/event maps: which groups to clear and how many crowns (0 = all).
    maps = [k for k in ['legend', 'uncanny', 'zero', 'event', 'collab', 'gauntlet', 'collab_gauntlet', 'behemoth', 'enigma', 'tower', 'legend_quest', 'catamin_stage', 'catclaw'] if form.get(f"clear_{k}") in ("1", "true", "on")]
    crowns = (form.get("map_crowns") or "0").strip()
    if crowns not in ("0", "1", "2", "3", "4"):
        return '알 수 없는 크라운 수예요.'
    if maps:
        edits["clear_maps"] = maps
        edits["map_crowns"] = int(crowns)

    # Forms & talents, special skills, Ototo, Gamatoto, endless items, more stages, progress, fixes.
    extra = extras.parse(form)
    if isinstance(extra, str):
        return extra
    edits.update(extra)
    return edits


@app.after_request
def no_index(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"  # keep the site out of search results
    return response


@app.get("/robots.txt")
def robots():
    return app.response_class("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


catalog_lock = threading.Lock()
catalog_cache: dict[str, tuple[float, dict[str, Any]]] = {}
CATALOG_MAX_AGE = 6 * 3600


@app.get("/api/cats")
def cats():
    """Character list (id, name, rarity, obtainable) for the search box, cached per region."""
    return game_data_list("catalog")


@app.get("/api/items")
def items():
    """Catfruit/seed, behemoth stone/gem and catseye names, cached per region."""
    return game_data_list("items")


@app.get("/api/extras")
def extras_list():
    """Names and limits for special skills, Gamatoto, the cat shrine and Ototo, cached per region."""
    return game_data_list("extras")


@app.get("/api/orbs")
def orbs():
    """Talent orb types and their grade/trait/effect names, cached per region."""
    return game_data_list("orbs")


def game_data_list(mode: str):
    cc = (request.args.get("cc") or 'kr').lower()
    if cc not in COUNTRIES:
        return fail("Unknown country.")
    key = f"{mode}:{cc}"
    cached = catalog_cache.get(key)
    if cached and time.time() - cached[0] < CATALOG_MAX_AGE:
        return jsonify(cached[1])
    if not catalog_lock.acquire(timeout=90):
        return fail('캐릭터 목록을 준비 중이에요. 잠시 뒤 다시 시도하세요.', 503)
    try:
        cached = catalog_cache.get(key)
        if cached and time.time() - cached[0] < CATALOG_MAX_AGE:
            return jsonify(cached[1])
        result = run_job({"mode": mode, "cc": cc})
        if result.get("ok"):
            catalog_cache[key] = (time.time(), result)
        return jsonify(result), (200 if result.get("ok") else 502)
    finally:
        catalog_lock.release()


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
    job_id = form.get("job_id") or ""
    if not JOB_ID.fullmatch(job_id):
        job_id = ""

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
                   new_account=False)  # the new-account upload option was removed from the site
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
    result: dict[str, Any] | None = None
    try:
        result = run_job(job, job_id, ip)
    finally:
        with state_lock:
            active_ips.discard(ip)
            # a cancel before anything was downloaded doesn't count toward the cooldown
            if not (result and result.get("cancelled") and "before" not in result):
                last_run[ip] = time.monotonic()

    if mode == "file" and not edits:
        result.pop("edited_b64", None)  # nothing changed; just show info
    return jsonify(result)


@app.post("/api/cancel")
def cancel():
    job_id = request.form.get("job_id") or ""
    if not JOB_ID.fullmatch(job_id):
        return fail(CANCEL_TEXT["nothing"], 404)
    ip = client_ip()
    with state_lock:
        now = time.monotonic()
        for k in [k for k, e in jobs.items() if e["dir"] is None and now - e["t"] > 300]:
            del jobs[k]
        entry = jobs.get(job_id)
        if entry is None:
            if len(jobs) < 500:  # the edit request may not have reached us yet
                jobs[job_id] = {"ip": ip, "dir": None, "mode": None, "proc": None, "cancelled": True, "t": now}
            return jsonify({"ok": True})
        if entry["ip"] != ip:
            return fail(CANCEL_TEXT["nothing"], 404)
        if entry["mode"] == "codes" and os.path.exists(os.path.join(entry["dir"], EDITING_MARK)):
            return jsonify({"ok": False, "late": True, "error": CANCEL_TEXT["late"]})
        entry["cancelled"] = True
        if entry["dir"]:
            try:
                open(os.path.join(entry["dir"], CANCEL_FLAG), "w").close()
            except OSError:
                pass  # the job just finished
        if entry["mode"] == "file" and entry["proc"] is not None:
            entry["proc"].kill()
    return jsonify({"ok": True})


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
