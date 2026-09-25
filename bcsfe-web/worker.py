"""Runs one BCSFE job in an isolated process.

The web server (or Discord bot) starts this script once per request, sends the job as JSON on stdin and
reads the result as JSON from stdout. Running each job in its own process keeps
BCSFE's global state (config, cached game data, country code) from leaking
between users, and means one crashed or hung edit can't take the bot down.

Job format (stdin):
{
  "mode": "codes" | "file",
  "transfer_code": "...", "confirmation_code": "...",   # mode == codes
  "file_b64": "...",                                     # mode == file
  "cc": "en" | "jp" | "kr" | "tw" | null,
  "edits": {"catfood": 45000, "xp": 99999999, "unlock_cats": true, ...},
  "new_account": false,
  "data_dir": "/path/to/shared/bcsfe-data",
  "job_dir": "/path/to/private/tmp/dir"
}
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import traceback
from typing import Any, Callable

# BCSFE prints to stdout (progress messages, colour codes). Keep the real
# stdout for our JSON result and send everything else to stderr.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

from importlib import resources  # noqa: E402

import bcsfe  # noqa: E402
from bcsfe import core  # noqa: E402

# Numeric fields: (save attribute, max-value key, managed item type or None).
# Managed items are how the game server tracks premium currency; BCSFE reports
# changes to them so the server's records match the save (ban prevention).
NUMERIC_FIELDS: dict[str, tuple[str, str, Any]] = {
    "catfood": ("catfood", "catfood", core.ManagedItemType.CATFOOD),
    "xp": ("xp", "xp", None),
    "np": ("np", "np", None),
    "leadership": ("leadership", "leadership", None),
    "normal_tickets": ("normal_tickets", "normal_tickets", None),
    "rare_tickets": ("rare_tickets", "rare_tickets", core.ManagedItemType.RARE_TICKET),
    "platinum_tickets": (
        "platinum_tickets",
        "platinum_tickets",
        core.ManagedItemType.PLATINUM_TICKET,
    ),
    "legend_tickets": (
        "legend_tickets",
        "legend_tickets",
        core.ManagedItemType.LEGEND_TICKET,
    ),
    "platinum_shards": ("platinum_shards", "platinum_tickets", None),
}

LABELS = {
    "catfood": "고양이 통조림",
    "xp": "경험치",
    "np": "NP",
    "leadership": "통솔력",
    "normal_tickets": "냥코 티켓",
    "rare_tickets": "레어 티켓",
    "platinum_tickets": "플래티넘 티켓",
    "legend_tickets": "레전드 티켓",
    "platinum_shards": "플래티넘의 조각",
}


def accept_backup_game_data_repo() -> None:
    """Let BCSFE fall back to its backup game-data repo without asking.

    When the main repo is unreachable BCSFE asks on the terminal whether to
    switch to its GitLab mirror. A web request can't answer, so cat edits would
    just fail; answer yes instead. Every other question still raises EOFError.
    """
    from bcsfe.core.server import game_data_getter

    ask = game_data_getter.dialog_creator.yes_no_key
    if getattr(ask, "_accepts_backup_repo", False):
        return

    def yes_no_key(key: str, *args: Any, **kwargs: Any) -> bool:
        if key == "use_alternative_repo":
            return True
        return ask(key, *args, **kwargs)

    yes_no_key._accepts_backup_repo = True  # type: ignore[attr-defined]
    game_data_getter.dialog_creator.yes_no_key = yes_no_key


# Main story chapters, in BCSFE's get_real_chapters() order.
STORY_CHAPTERS = ['세계편 제1장', '세계편 제2장', '세계편 제3장', '미래편 제1장', '미래편 제2장', '미래편 제3장', '우주편 제1장', '우주편 제2장', '우주편 제3장']
# Chapters the game needs cleared first (from BCSFE's clear_previous_chapters).
STORY_REQUIRES = {1: [0], 2: [0, 1], 3: [0], 4: [0, 3], 5: [0, 3, 4], 6: [0, 3], 7: [0, 3, 6], 8: [0, 3, 6, 7]}
TREASURE_LEVELS = ['없음', '조잡한', '평범한', '최고급']
STORY_STAGES = 48


def clear_story_chapter(chapter: Any) -> None:
    """Clear all 48 stages without lowering existing clear counts."""
    for stage in chapter.stages[:STORY_STAGES]:
        stage.clear_times = max(stage.clear_times, 1)
    chapter.progress = max(chapter.progress, STORY_STAGES)


def migrate_data(data_dir: str) -> None:
    """Copy BCSFE's bundled files (locales, themes, max values) into data_dir."""
    core.set_data_dir_path(core.Path(data_dir))
    version_path = core.Path.get_data_folder().add("version.txt")
    if version_path.exists() and version_path.read().to_str().strip() == bcsfe.__version__:
        return
    src = resources.files(bcsfe.__app_name__).joinpath("files")
    bcsfe.copy_to_data_dir(src, src)
    version_path.write(core.Data(bcsfe.__version__))


@contextlib.contextmanager
def game_data_lock(data_dir: str):
    """Serialise game-data downloads so concurrent jobs don't corrupt the cache."""
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, ".game_data.lock"), "a+") as fh:
        if os.name == "nt":  # Windows
            import msvcrt
            import time

            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def reads_back_exactly(original: bytes, cc: core.CountryCode) -> bool:
    """True if BCSFE writes the save back byte-for-byte as it came in.

    BCSFE's own tests require this for every save it supports. A mismatch means
    the save uses a newer format than this BCSFE version understands (new game
    versions sometimes add fields mid-save), so editing it could corrupt it.

    This parses a fresh copy: after a transfer-code download BCSFE stores the
    server's login details in the save, so that copy never matches the original.
    """
    try:
        return core.SaveFile(core.Data(original), cc).to_data().to_bytes() == original
    except Exception:
        traceback.print_exc()
        return False


def attach_backup(result: dict[str, Any], path: str) -> None:
    if os.path.exists(path):
        with open(path, "rb") as fh:
            result["original_b64"] = base64.b64encode(fh.read()).decode()


def snapshot(save: core.SaveFile) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, (attr, _, _) in NUMERIC_FIELDS.items():
        try:
            out[key] = int(getattr(save, attr))
        except Exception:
            pass
    try:
        out["cats_unlocked"] = len(save.cats.get_unlocked_cats())
    except Exception:
        pass
    try:
        chapters = save.story.get_real_chapters()
        out["story_cleared"] = sum(s.clear_times > 0 for c in chapters for s in c.stages[:STORY_STAGES])
        out["story_treasures"] = sum(s.treasure > 0 for c in chapters for s in c.stages[:STORY_STAGES])
    except Exception:
        pass
    return out


def apply_edits(save: core.SaveFile, edits: dict[str, Any], data_dir: str) -> tuple[list[str], list[str]]:
    done: list[str] = []
    failed: list[str] = []
    maxes = core.core_data.max_value_manager

    def attempt(label: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            done.append(label)
        except EOFError:
            # BCSFE asked an interactive question (usually: game data repo unreachable)
            failed.append(f"{label}: 게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요")
            traceback.print_exc()
        except Exception as e:  # keep going; report what failed
            failed.append(f"{label}: {e}")
            traceback.print_exc()

    for key, (attr, max_key, managed_type) in NUMERIC_FIELDS.items():
        if edits.get(key) is None:
            continue
        cap = int(getattr(maxes, max_key))
        if key == "platinum_shards":
            cap *= 10
        value = max(0, min(int(edits[key]), cap))

        def set_value(attr=attr, value=value, managed_type=managed_type):
            before = int(getattr(save, attr))
            setattr(save, attr, value)
            if managed_type is not None and value != before:
                core.BackupMetaData(save).add_managed_item(
                    core.ManagedItem.from_change(value - before, managed_type)
                )

        label = LABELS[key] + (f" (최대 {cap:,}(으)로 제한)" if value != int(edits[key]) else "")
        attempt(label, set_value)

    if edits.get("max_battle_items"):
        def f():
            for item in save.battle_items.items:
                item.amount = maxes.battle_items
        attempt("배틀 아이템 최대", f)

    if edits.get("max_catseyes"):
        def f():
            for i in range(len(save.catseyes)):
                save.catseyes[i] = maxes.catseyes
        attempt("캣츠아이 최대", f)

    if edits.get("max_catamins"):
        def f():
            for i in range(len(save.catamins)):
                save.catamins[i] = maxes.catamins
        attempt("고양이 드링크 최대", f)

    if edits.get("max_treasure_chests"):
        def f():
            for i in range(len(save.treasure_chests)):
                save.treasure_chests[i] = maxes.treasure_chests
        attempt("보물 상자 최대", f)

    chapter_ids = edits.get("story_chapters") or []
    names = ", ".join(STORY_CHAPTERS[i] for i in chapter_ids)

    if edits.get("clear_story") and chapter_ids:
        required = sorted({r for i in chapter_ids for r in STORY_REQUIRES.get(i, [])} - set(chapter_ids))
        label = '{names} 클리어'.format(names=names)
        if required:
            label += ' (필요한 챕터도 클리어: {names})'.format(names=", ".join(STORY_CHAPTERS[i] for i in required))

        def f():
            core.StoryChapters.clear_tutorial(save)
            chapters = save.story.get_real_chapters()
            for i in required + list(chapter_ids):
                clear_story_chapter(chapters[i])
        attempt(label, f)

    if edits.get("treasure_level") is not None and chapter_ids:
        level = int(edits["treasure_level"])

        def f():
            chapters = save.story.get_real_chapters()
            for i in chapter_ids:
                for stage in chapters[i].get_valid_treasure_stages():
                    stage.set_treasure(level)
        attempt('보물을 {level}(으)로 설정: {names}'.format(level=TREASURE_LEVELS[level], names=names), f)

    # Cat edits need game data (downloaded and cached in data_dir).
    if edits.get("unlock_cats") or edits.get("true_form_cats"):
        accept_backup_game_data_repo()
        with game_data_lock(data_dir):
            if edits.get("unlock_cats"):
                def f():
                    cats = save.cats.get_cats_obtainable(save)
                    if cats is None:
                        raise RuntimeError("획득 가능한 캐릭터를 찾기 위한 게임 데이터를 내려받지 못했어요")
                    for cat in cats:
                        cat.unlock(save)
                attempt("획득 가능한 모든 캐릭터 획득", f)

            if edits.get("true_form_cats"):
                def f():
                    cats = save.cats.get_unlocked_cats()
                    set_forms = core.core_data.config.get_bool(core.ConfigKey.SET_CAT_CURRENT_FORMS)
                    save.cats.true_form_cats(save, cats, False, set_forms)
                attempt("보유 캐릭터 제3형태 진화", f)

    return done, failed


def run(job: dict[str, Any]) -> dict[str, Any]:
    data_dir = job["data_dir"]
    job_dir = job["job_dir"]
    os.makedirs(job_dir, exist_ok=True)

    migrate_data(data_dir)
    # Per-job config/log so nothing user-specific lands in the shared folder.
    core.set_log_path(core.Path(os.path.join(job_dir, "bcsfe.log")))
    core.set_transfer_backup_path(core.Path(os.path.join(job_dir, "original_SAVE_DATA")))
    core.core_data.init_data()

    cc = core.CountryCode.from_code(job["cc"]) if job.get("cc") else None
    result: dict[str, Any] = {"ok": False}

    # ---- load the save ----------------------------------------------------
    if job["mode"] == "codes":
        backup = os.path.join(job_dir, "original_SAVE_DATA")
        if cc is None:
            return {"ok": False, "error": "국가를 선택해야 해요."}
        try:
            handler, req = core.ServerHandler.from_codes(
                job["transfer_code"].strip(),
                job["confirmation_code"].strip(),
                cc,
                core.GameVersion(120200),
                print=False,
                save_backup=True,
            )
        except Exception:
            # The save may already be downloaded (and the code used up) when
            # parsing fails, so always hand back the backup if there is one.
            traceback.print_exc()
            if not os.path.exists(backup):
                raise
            out = {"ok": False, "error": (
                "세이브를 내려받았지만 에디터가 읽지 못했어요 — 에디터가 아직 지원하지 않는 새 게임 버전일 수 "
                "있어요. 이어하기 코드는 이미 사용됐으니 아래에서 원본 백업을 내려받아 꼭 보관하세요."
            )}
            attach_backup(out, backup)
            return out
        if handler is None:
            if req is None:
                return {"ok": False, "error": "게임 서버에 연결할 수 없어요. 나중에 다시 시도하세요."}
            hint = " (일본판과 대만판 코드는 헷갈리기 쉬워요 — 국가를 확인하세요.)" if job["cc"] in ("jp", "tw") else ""
            return {"ok": False, "error": "이어하기 코드, 인증번호 또는 국가가 올바르지 않아요." + hint}
        save = handler.save_file
        attach_backup(result, backup)
        with open(backup, "rb") as fh:
            original = fh.read()
    else:
        raw = core.Data(base64.b64decode(job["file_b64"]))
        try:
            save = core.SaveFile(raw, cc)
        except core.CantDetectSaveCCError:
            return {"ok": False, "error": "세이브의 국가를 감지하지 못했어요. 게임 국가를 선택하고 다시 시도하세요."}
        except Exception as e:
            return {"ok": False, "error": f"세이브 파일을 읽을 수 없어요: {e}"}
        original = raw.to_bytes()

    result["country"] = save.cc.get_code()
    result["game_version"] = save.game_version.to_string()
    result["before"] = snapshot(save)

    # ---- make sure this BCSFE version fully understands the save ------------
    if not reads_back_exactly(original, save.cc):
        version = result["game_version"]
        print(f"save does not round-trip (game version {version})", file=sys.stderr)
        if job["mode"] != "codes":
            return {"ok": False, "error": (
                f"이 세이브는 게임 버전 {version}이라 에디터가 아직 정확히 읽을 수 없어요. "
                "아무것도 바꾸지 않았어요."
            )}
        # The transfer code is already used up, so re-upload the untouched
        # original to give the player working codes again.
        save.to_data = lambda: core.Data(original)
        codes = core.ServerHandler(save, print=False).get_codes()
        result["after"] = result["before"]
        result["done"], result["failed"] = [], []
        if codes is None:
            result["error"] = (
                "세이브를 내려받았지만 에디터가 읽지 못했어요 — 에디터가 아직 지원하지 않는 새 게임 버전일 수 "
                "있어요. 이어하기 코드는 이미 사용됐으니 아래에서 원본 백업을 내려받아 꼭 보관하세요."
            )
            return result
        result["transfer_code"], result["confirmation_code"] = codes
        result["error"] = (
            f"이 세이브는 게임 버전 {version}이라 에디터가 아직 정확히 읽을 수 없어서 아무것도 "
            "편집하지 않았어요. 세이브는 바뀌지 않은 그대로 다시 업로드했어요 — 아래 새 코드를 "
            "입력하면 게임에서 되찾을 수 있어요."
        )
        result["ok"] = True
        return result

    # ---- edit ---------------------------------------------------------------
    done, failed = apply_edits(save, job.get("edits") or {}, data_dir)
    result["done"] = done
    result["failed"] = failed
    result["after"] = snapshot(save)

    # ---- output -------------------------------------------------------------
    if job["mode"] == "codes":
        if job.get("new_account"):
            if not core.ServerHandler(save, print=False).create_new_account():
                result["failed"].append("새 계정: 서버가 거부해서 기존 계정에 업로드했어요")
            else:
                result["done"].append("새 계정(문의코드)으로 이동")
        codes = core.ServerHandler(save, print=False).get_codes()
        if codes is None:
            result["error"] = (
                "편집은 적용됐지만 업로드에 실패했어요. 아래에서 원본 세이브를 내려받으세요 — "
                "세이브 관리 도구로 복원하거나 다시 시도할 수 있어요."
            )
            result["edited_b64"] = base64.b64encode(save.to_data().to_bytes()).decode()
            return result
        result["transfer_code"], result["confirmation_code"] = codes
    else:
        result["edited_b64"] = base64.b64encode(save.to_data().to_bytes()).decode()

    result["ok"] = True
    return result


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        out = run(job)
    except Exception as e:
        traceback.print_exc()
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _REAL_STDOUT.write(json.dumps(out))
    _REAL_STDOUT.flush()


if __name__ == "__main__":
    main()
