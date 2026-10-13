# auth_layer.py
# -*- coding: utf-8 -*-
"""
SHAPPNO LV UP — Dashboard + Owner Lock Panel + Auto Delete + Concurrent Limit
Single aiohttp app (Railway-ready)
"""

import os
import json
import time
import asyncio
import contextvars
import httpx
from typing import Dict, Any, Optional, List
from aiohttp import web

import dashboard_server as ds

bot_state = ds.bot_state

# ==================== TEMPLATE LOADER ====================
_TEMPLATE_CACHE: Dict[str, str] = {}


def _load_template(name: str) -> str:
    if name in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[name]
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base_dir, "templates", name)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        _TEMPLATE_CACHE[name] = content
        return content
    except Exception as e:
        print(f"[auth] Template load error {name}: {e}", flush=True)
        return f"<h1>Template {name} not found</h1>"


DASHBOARD_HTML = _load_template("dashboard.html")

# ==================== CONFIG ====================
LOCK_PATH = "/shappno_004x-lock"
OWNER_PASSWORD = "8325703815"

DATA_DIR = "data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
LOCK_FILE = os.path.join(DATA_DIR, "lock.json")
AUTO_SETTINGS_FILE = os.path.join(DATA_DIR, "auto_settings.json")

# ==================== HELPERS ====================
_CURRENT_OWNER = contextvars.ContextVar("current_owner", default="global")


def _ensure():
    os.makedirs(DATA_DIR, exist_ok=True)


def _read(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, data):
    _ensure()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ==================== LOCK ====================
def load_lock() -> Dict[str, Any]:
    d = _read(LOCK_FILE, {"locked": False})
    if not isinstance(d, dict):
        d = {"locked": False}
    d.setdefault("locked", False)
    return d


def save_lock(d: Dict[str, Any]):
    _write(LOCK_FILE, d)


def is_locked() -> bool:
    return bool(load_lock().get("locked", False))


# ==================== MODE REGISTRY ====================
_mode_registry: Dict[str, str] = {}
_mode_lock = asyncio.Lock()


async def _set_mode(uid: str, mode: str):
    async with _mode_lock:
        _mode_registry[str(uid)] = mode


async def _get_mode(uid: str) -> Optional[str]:
    async with _mode_lock:
        return _mode_registry.get(str(uid))


async def _clear_mode(uid: str):
    async with _mode_lock:
        _mode_registry.pop(str(uid), None)


# ==================== USER STATE ====================
USER_WORKERS: Dict[str, asyncio.Task] = {}
USER_PAUSED: set = set()
USER_DELETED: set = set()
_PENDING: Dict[str, Dict[str, str]] = {}   # uid -> {password, mode}
_LAST_SYNC: float = 0.0

# ==================== USER POOL ====================
def load_users() -> Dict[str, Any]:
    d = _read(USERS_FILE, {"accounts": []})
    if not isinstance(d, dict) or "accounts" not in d:
        d = {"accounts": []}
    if not isinstance(d.get("accounts"), list):
        d["accounts"] = []
    return d


def save_users(u: Dict[str, Any]):
    _write(USERS_FILE, u)


def _add_account_to_pool(entry: Dict[str, Any]):
    try:
        users = load_users()
        users.setdefault("accounts", []).append(entry)
        save_users(users)
    except Exception as e:
        print(f"[pool-add] {e}", flush=True)


def _remove_account_from_pool(uid: str):
    try:
        users = load_users()
        accs = users.get("accounts", [])
        real_uid = None
        cred = bot_state.account_credentials.get(str(uid))
        if cred:
            real_uid = str(cred.get("auth_uid", ""))
        new_accs = []
        for a in accs:
            a_uid = str(a.get("uid", ""))
            if a_uid == str(uid):
                continue
            if real_uid and a_uid == real_uid:
                continue
            new_accs.append(a)
        users["accounts"] = new_accs
        save_users(users)
    except Exception as e:
        print(f"[pool-remove] {e}", flush=True)


def _find_account_in_pool(uid: str) -> Optional[Dict[str, Any]]:
    users = load_users()
    for a in users.get("accounts", []):
        if str(a.get("uid")) == str(uid):
            return a
        if str(a.get("account_id")) == str(uid):
            return a
    return None


# ==================== AUTO SETTINGS ====================
def _load_auto_settings() -> Dict[str, Any]:
    d = _read(AUTO_SETTINGS_FILE, {})
    return d if isinstance(d, dict) else {}


def _save_auto_settings(d: Dict[str, Any]):
    _write(AUTO_SETTINGS_FILE, d)


def _get_auto() -> Dict[str, Any]:
    u = _load_auto_settings().get("global", {})
    if not isinstance(u, dict):
        u = {}
    return {
        "delete_enabled": bool(u.get("delete_enabled", False)),
        "target_level": int(u.get("target_level", 22) or 22),
        "limit_enabled": bool(u.get("limit_enabled", False)),
        "lw_limit": int(u.get("lw_limit", 5) or 5),
        "br_limit": int(u.get("br_limit", 5) or 5),
    }


def _set_auto(cfg: Dict[str, Any]):
    all_s = _load_auto_settings()
    all_s["global"] = cfg
    _save_auto_settings(all_s)


# ==================== REGISTER_ACCOUNT PATCH ====================
_orig_register = ds.BotState.register_account


def _patched_register(self, uid, nickname, region, level, exp, likes=0):
    uid_str = str(uid)
    _orig_register(self, uid, nickname, region, level, exp, likes)

    acc = self.accounts.get(uid_str)
    if not acc:
        return

    try:
        users = load_users()
        accs = users.get("accounts", [])
        cred = bot_state.account_credentials.get(uid_str)
        real_uid = str(cred.get("auth_uid", "")) if cred else ""

        matched = None
        for a in accs:
            if a.get("type") != "guest":
                continue
            if real_uid and str(a.get("uid")) == real_uid:
                matched = a
                break
            if str(a.get("uid")) == uid_str:
                matched = a
                break

        now = time.time()
        cur_exp = int(acc.get("current_exp", 0) or 0)
        gained = int(acc.get("gained_exp", 0) or 0)

        if matched:
            matched["account_id"] = uid_str
            matched["nickname"] = acc.get("nickname", "") or matched.get("nickname", "")
            matched["region"] = acc.get("region", "") or matched.get("region", "")
            matched["level"] = int(acc.get("level", 1) or 1)
            matched["last_exp"] = cur_exp
            matched["gained_exp"] = gained
            matched["matches_played"] = int(acc.get("matches_played", 0) or 0)
            matched["last_update"] = now
            if matched.get("initial_exp") is None:
                matched["initial_exp"] = cur_exp

        users["accounts"] = accs
        save_users(users)
    except Exception as e:
        print(f"[register-patch] {type(e).__name__}: {e}", flush=True)


ds.BotState.register_account = _patched_register


# ==================== GUEST WORKER ====================
async def _guest_worker(uid: str, password: str):
    """একটা account এর infinite loop — login → run → repeat"""
    try:
        import main as m
    except Exception as e:
        print(f"[worker] main import failed: {e}", flush=True)
        return

    uid_str = str(uid)

    try:
        while True:
            try:
                if uid_str in USER_DELETED:
                    print(f"[worker] {uid_str} deleted -> exit", flush=True)
                    break
                if uid_str in USER_PAUSED:
                    await asyncio.sleep(2)
                    continue
                if is_locked():
                    await asyncio.sleep(5)
                    continue

                cv = _CURRENT_OWNER.set("global")
                try:
                    ad = await m.process_account_uid_pass(uid_str, password)
                    if not ad:
                        await asyncio.sleep(15)
                        continue

                    acc_id = str(ad.get("account_id", ""))
                    if acc_id in USER_DELETED or uid_str in USER_DELETED:
                        break
                    if acc_id in USER_PAUSED or uid_str in USER_PAUSED:
                        await asyncio.sleep(3)
                        continue

                    await _set_mode(acc_id, "auto")
                    print(f"[worker] run uid={uid_str} acc_id={acc_id}", flush=True)

                    # AUTO MODE — level ≥ 3 → LW, < 3 → BR (main.py handle করে)
                    await m.run_account_worker(ad, uid_str, mode="auto")

                    if acc_id in USER_DELETED or uid_str in USER_DELETED:
                        break

                    await asyncio.sleep(3)
                finally:
                    _CURRENT_OWNER.reset(cv)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[worker] {uid_str} err: {type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(10)
    except asyncio.CancelledError:
        return


# ==================== WORKER START/STOP ====================
async def start_worker_for(uid: str, password: str):
    uid_str = str(uid)
    old = USER_WORKERS.get(uid_str)
    if old and not old.done():
        old.cancel()
        try:
            await asyncio.wait_for(old, timeout=2)
        except Exception:
            pass
    t = asyncio.create_task(_guest_worker(uid_str, password))
    USER_WORKERS[uid_str] = t


async def stop_worker_for(uid: str):
    uid_str = str(uid)
    t = USER_WORKERS.pop(uid_str, None)
    if t and not t.done():
        t.cancel()
        try:
            await asyncio.wait_for(t, timeout=3)
        except Exception:
            pass


async def stop_all_workers():
    tasks = []
    for uid, t in list(USER_WORKERS.items()):
        if t and not t.done():
            t.cancel()
            tasks.append(t)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    USER_WORKERS.clear()
    _PENDING.clear()


# ==================== CONCURRENT LIMITER ====================
def _classify_level(lvl: int) -> str:
    """Level ≥ 3 → LW | Level < 3 → BR"""
    try:
        lvl = int(lvl or 1)
    except Exception:
        lvl = 1
    return "LW" if lvl >= 3 else "BR"


async def _enforce_concurrent_limits():
    """
    Limit ON থাকলে:
      - pool এর প্রতিটা account কে LW/BR এ classify করি
      - যতটা limit, ততটা worker চালু রাখি
      - বাকিগুলো _PENDING এ রাখি
    """
    try:
        cfg = _get_auto()

        if not cfg.get("limit_enabled"):
            if _PENDING:
                pending_copy = dict(_PENDING)
                _PENDING.clear()
                for uid, item in pending_copy.items():
                    await start_worker_for(uid, item["password"])
            return

        lw_limit = max(1, int(cfg.get("lw_limit", 5) or 5))
        br_limit = max(1, int(cfg.get("br_limit", 5) or 5))

        users = load_users()
        accs = users.get("accounts", [])

        lw_all: List[Dict[str, str]] = []
        br_all: List[Dict[str, str]] = []

        for a in accs:
            uid = str(a.get("uid", "")).strip()
            pw = str(a.get("password", "")).strip()
            if not uid or not pw:
                continue
            if uid in USER_DELETED or uid in USER_PAUSED:
                continue

            lvl = 1
            live = bot_state.accounts.get(uid)
            if live:
                lvl = int(live.get("level", 1) or 1)
            else:
                lvl = int(a.get("level", 1) or 1)

            mode = _classify_level(lvl)
            if mode == "LW":
                lw_all.append({"uid": uid, "password": pw, "mode": "LW"})
            else:
                br_all.append({"uid": uid, "password": pw, "mode": "BR"})

        running_uids = set()
        for uid, t in list(USER_WORKERS.items()):
            if t and not t.done():
                running_uids.add(str(uid))

        running_lw = []
        running_br = []
        for uid in list(running_uids):
            pool_acc = next((x for x in accs if str(x.get("uid")) == uid), None)
            lvl = 1
            live = bot_state.accounts.get(uid)
            if live:
                lvl = int(live.get("level", 1) or 1)
            elif pool_acc:
                lvl = int(pool_acc.get("level", 1) or 1)
            if _classify_level(lvl) == "LW":
                running_lw.append(uid)
            else:
                running_br.append(uid)

        if len(running_lw) > lw_limit:
            extra = running_lw[lw_limit:]
            for uid in extra:
                await stop_worker_for(uid)
                pool_acc = next((x for x in accs if str(x.get("uid")) == uid), None)
                if pool_acc:
                    _PENDING[uid] = {
                        "password": str(pool_acc.get("password", "")),
                        "mode": "LW",
                    }
                running_uids.discard(uid)

        if len(running_br) > br_limit:
            extra = running_br[br_limit:]
            for uid in extra:
                await stop_worker_for(uid)
                pool_acc = next((x for x in accs if str(x.get("uid")) == uid), None)
                if pool_acc:
                    _PENDING[uid] = {
                        "password": str(pool_acc.get("password", "")),
                        "mode": "BR",
                    }
                running_uids.discard(uid)

        running_lw = [u for u in running_uids if _classify_level(
            int((bot_state.accounts.get(u) or {}).get("level", 1) or 1)
        ) == "LW"]
        running_br = [u for u in running_uids if _classify_level(
            int((bot_state.accounts.get(u) or {}).get("level", 1) or 1)
        ) == "BR"]

        slots_lw = max(0, lw_limit - len(running_lw))
        for item in lw_all:
            if slots_lw <= 0:
                break
            uid = item["uid"]
            if uid in running_uids:
                continue
            _PENDING.pop(uid, None)
            await start_worker_for(uid, item["password"])
            running_uids.add(uid)
            slots_lw -= 1

        slots_br = max(0, br_limit - len(running_br))
        for item in br_all:
            if slots_br <= 0:
                break
            uid = item["uid"]
            if uid in running_uids:
                continue
            _PENDING.pop(uid, None)
            await start_worker_for(uid, item["password"])
            running_uids.add(uid)
            slots_br -= 1

        for item in lw_all + br_all:
            uid = item["uid"]
            if uid in running_uids:
                continue
            if uid in USER_DELETED or uid in USER_PAUSED:
                continue
            _PENDING[uid] = {
                "password": item["password"],
                "mode": item["mode"],
            }

    except Exception as e:
        print(f"[limiter] error: {type(e).__name__}: {e}", flush=True)


async def _limiter_watcher():
    while True:
        try:
            await asyncio.sleep(5)
            if is_locked():
                continue
            await _enforce_concurrent_limits()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[limiter-watcher] {type(e).__name__}: {e}", flush=True)


async def start_all_workers():
    if is_locked():
        return

    users = load_users()
    cfg = _get_auto()

    if not cfg.get("limit_enabled"):
        for acc in users.get("accounts", []):
            if acc.get("type") != "guest":
                continue
            uid = str(acc.get("uid", "")).strip()
            pw = str(acc.get("password", "")).strip()
            if not uid or not pw:
                continue
            if uid in USER_PAUSED or uid in USER_DELETED:
                continue
            old = USER_WORKERS.get(uid)
            if old and not old.done():
                continue
            t = asyncio.create_task(_guest_worker(uid, pw))
            USER_WORKERS[uid] = t
    else:
        await _enforce_concurrent_limits()
        
# ==================== DELETE HELPER (delete button এর মতো same logic) ====================
async def _delete_account_logic(uid: str):
    """
    Delete button চাপলে যা হয়, ঠিক সেটাই।
    worker cancel + pool remove + cache invalidation + bot_state cleanup
    """
    uid_str = str(uid)

    real_uid = None
    real_acc_id = None
    cred = bot_state.account_credentials.get(uid_str)
    if cred:
        real_uid = str(cred.get("auth_uid", ""))
        real_acc_id = str(cred.get("account_id", ""))

    keys_to_kill = {uid_str}
    if real_uid:
        keys_to_kill.add(real_uid)
    if real_acc_id:
        keys_to_kill.add(real_acc_id)

    print(f"[DELETE] {uid_str} -> keys={keys_to_kill}", flush=True)

    for k in keys_to_kill:
        USER_DELETED.add(k)
        USER_PAUSED.discard(k)
        _PENDING.pop(k, None)

    for k in list(keys_to_kill):
        await stop_worker_for(k)

    for k in list(keys_to_kill):
        bot_state.accounts.pop(k, None)

    try:
        import main as m
        for k in keys_to_kill:
            try:
                m.cache_invalidate(k)
            except Exception:
                pass
    except Exception:
        pass

    for k in keys_to_kill:
        await _clear_mode(k)

    users = load_users()
    accs = users.get("accounts", [])
    new_accs = []
    for a in accs:
        a_uid = str(a.get("uid", ""))
        if a_uid in keys_to_kill:
            continue
        new_accs.append(a)
    users["accounts"] = new_accs
    save_users(users)

    for k in list(keys_to_kill):
        USER_DELETED.discard(k)

    _PENDING.pop(uid_str, None)

    # limiter enforce → pending থেকে পরের account start
    await _enforce_concurrent_limits()


# ==================== AUTO DELETE WATCHER (60s interval) ====================
async def _auto_delete_watcher():
    """
    প্রতি ৬০ সেকেন্ডে check:
      - delete_enabled?
      - কোনো account এর level >= target_level?
      - হলে delete button এর মতো same logic চালাই
    """
    while True:
        try:
            await asyncio.sleep(60)

            if is_locked():
                continue

            cfg = _get_auto()
            if not cfg.get("delete_enabled"):
                continue

            tgt = int(cfg.get("target_level", 0) or 0)
            if tgt <= 0:
                continue

            to_delete: List[str] = []

            # Live accounts (memory তে)
            for uid, acc in list(bot_state.accounts.items()):
                try:
                    lvl = int(acc.get("level", 1) or 1)
                except Exception:
                    lvl = 1
                if lvl >= tgt:
                    to_delete.append(str(uid))

            # Pending accounts (pool এ)
            users = load_users()
            for a in users.get("accounts", []):
                uid = str(a.get("uid", ""))
                if not uid or uid in to_delete:
                    continue
                try:
                    lvl = int(a.get("level", 1) or 1)
                except Exception:
                    lvl = 1
                if lvl >= tgt:
                    to_delete.append(uid)

            if not to_delete:
                continue

            to_delete = list(dict.fromkeys(to_delete))

            for uid in to_delete:
                try:
                    print(f"[AUTO-DELETE] uid={uid} reached target Lv{tgt} -> deleting", flush=True)
                    await _delete_account_logic(uid)
                except Exception as e:
                    print(f"[AUTO-DELETE] error {uid}: {e}", flush=True)

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[AUTO-DELETE] watcher error: {type(e).__name__}: {e}", flush=True)


# ==================== PERIODIC REFRESH ====================
async def _periodic_refresh():
    """প্রতি ৫ মিনিটে accounts এর exp/level refresh করি।"""
    while True:
        try:
            await asyncio.sleep(300)
            if is_locked():
                continue
            try:
                import main as m
                creds = list(bot_state.account_credentials.values())
                seen_ids = set()
                for cred in creds:
                    acc_id = str(cred.get("account_id", ""))
                    if not acc_id or acc_id in seen_ids:
                        continue
                    seen_ids.add(acc_id)
                    try:
                        await m.refresh_account_profile(cred)
                    except Exception:
                        pass
            except Exception:
                pass
        except asyncio.CancelledError:
            break
        except Exception:
            pass
            
# ==================== PAGE ROUTES ====================
async def h_root(req):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html", charset="utf-8")


async def h_dashboard(req):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html", charset="utf-8")


async def h_lock_page(req):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html", charset="utf-8")


async def h_favicon(req):
    raise web.HTTPNotFound()


# ==================== STATS API ====================
async def h_stats(req):
    if is_locked():
        return web.json_response({"error": "locked"}, status=403)

    users = load_users()
    accs = users.get("accounts", [])
    out: List[Dict[str, Any]] = []
    seen: set = set()
    total_exp = 0
    total_uploaded = len(accs)

    # Live accounts (memory)
    for uid, a in list(bot_state.accounts.items()):
        if uid in USER_DELETED:
            continue
        seen.add(str(uid))
        mode = await _get_mode(uid) or "auto"
        status = "PAUSED" if uid in USER_PAUSED else a.get("status", "LIVE")
        out.append({
            "uid": uid,
            "nickname": a.get("nickname", "?"),
            "region": a.get("region", "IND"),
            "level": a.get("level", 1),
            "initial_exp": a.get("initial_exp", 0),
            "current_exp": a.get("current_exp", 0),
            "gained_exp": a.get("gained_exp", 0),
            "matches_played": a.get("matches_played", 0),
            "status": status,
            "active_matches": a.get("active_matches", 0),
            "created_at": a.get("created_at"),
            "last_update": a.get("last_update"),
            "mode": mode,
        })
        total_exp += int(a.get("gained_exp", 0) or 0)

    # Offline / pending / connecting (from users.json)
    for acc in accs:
        key = str(acc.get("uid", ""))
        if not key or key in seen or key in USER_DELETED:
            continue
        seen.add(key)

        if key in _PENDING:
            status = "PENDING"
        elif key in USER_PAUSED:
            status = "PAUSED"
        else:
            status = "CONNECTING"

        out.append({
            "uid": key,
            "nickname": acc.get("nickname", "Loading..."),
            "region": acc.get("region", "IND"),
            "level": acc.get("level", 1),
            "initial_exp": acc.get("initial_exp", 0),
            "current_exp": acc.get("last_exp", 0),
            "gained_exp": acc.get("gained_exp", 0),
            "matches_played": acc.get("matches_played", 0),
            "status": status,
            "active_matches": 0,
            "created_at": acc.get("created_at"),
            "last_update": acc.get("last_update"),
            "mode": "auto",
        })
        total_exp += int(acc.get("gained_exp", 0) or 0)

    out.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)

    return web.json_response({
        "total_accounts": len(out),
        "total_uploaded": total_uploaded,
        "total_gained_exp": total_exp,
        "accounts": out,
    })


# ==================== ACCOUNT APIs ====================
async def h_acc_add_job(req):
    if is_locked():
        return web.json_response({"status": "error", "error": "Site Locked"}, status=403)

    try:
        d = await req.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid JSON"})

    uid = str(d.get("uid", "")).strip()
    pw = str(d.get("password", "")).strip()
    if not uid or not pw:
        return web.json_response({"status": "error", "error": "UID & Password required"})

    users = load_users()
    for a in users.get("accounts", []):
        if str(a.get("uid")) == uid:
            return web.json_response({"status": "error", "error": "UID already added"})

    try:
        import main as m
    except Exception:
        return web.json_response({"status": "error", "error": "main module unavailable"})

    cached = m.cache_get(uid)
    if cached and cached.get("level"):
        ad = cached
    else:
        try:
            ad = await m.process_account_uid_pass(uid, pw)
        except Exception:
            ad = None

    if not ad:
        return web.json_response({"status": "error", "error": "Login failed - invalid UID/password"})

    lvl = int(ad.get("level", 1) or 1)
    acc_id = str(ad.get("account_id", uid))

    _add_account_to_pool({
        "type": "guest",
        "uid": uid,
        "password": pw,
        "account_id": acc_id,
        "nickname": ad.get("nickname", ""),
        "region": ad.get("region", ""),
        "level": lvl,
        "initial_exp": int(ad.get("exp", 0) or 0),
        "last_exp": int(ad.get("exp", 0) or 0),
        "gained_exp": 0,
        "matches_played": 0,
        "created_at": time.time(),
        "last_update": time.time(),
    })

    USER_DELETED.discard(uid)
    USER_DELETED.discard(acc_id)
    USER_PAUSED.discard(uid)
    USER_PAUSED.discard(acc_id)

    cfg = _get_auto()
    if not cfg.get("limit_enabled"):
        await start_worker_for(uid, pw)
    else:
        await _enforce_concurrent_limits()

    mode_hint = "LW" if lvl >= 3 else "BR"
    return web.json_response({
        "status": "ok",
        "uid": uid,
        "nickname": ad.get("nickname", ""),
        "level": lvl,
        "region": str(ad.get("region", "")).upper(),
        "account_id": acc_id,
        "mode_hint": mode_hint,
    })


async def h_acc_bulk_add(req):
    if is_locked():
        return web.json_response({"status": "error", "error": "Site Locked"}, status=403)

    try:
        d = await req.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid JSON"})

    lst = d.get("accounts", [])
    if not isinstance(lst, list) or not lst:
        return web.json_response({"status": "error", "error": "accounts list required"})

    try:
        import main as m
    except Exception:
        return web.json_response({"status": "error", "error": "main module unavailable"})

    added = 0
    failed = 0
    errors: List[str] = []

    users = load_users()
    existing = {str(a.get("uid")) for a in users.get("accounts", [])}

    for it in lst:
        try:
            u = str(it.get("uid", "")).strip()
            p = str(it.get("password", "")).strip()
            if not u or not p or u in existing:
                failed += 1
                continue

            cached = m.cache_get(u)
            if cached and cached.get("level"):
                ad = cached
            else:
                try:
                    ad = await m.process_account_uid_pass(u, p)
                except Exception:
                    ad = None

            if not ad:
                failed += 1
                continue

            lvl = int(ad.get("level", 1) or 1)
            acc_id = str(ad.get("account_id", u))

            _add_account_to_pool({
                "type": "guest",
                "uid": u,
                "password": p,
                "account_id": acc_id,
                "nickname": ad.get("nickname", ""),
                "region": ad.get("region", ""),
                "level": lvl,
                "initial_exp": int(ad.get("exp", 0) or 0),
                "last_exp": int(ad.get("exp", 0) or 0),
                "gained_exp": 0,
                "matches_played": 0,
                "created_at": time.time(),
                "last_update": time.time(),
            })
            existing.add(u)

            USER_DELETED.discard(u)
            USER_DELETED.discard(acc_id)
            USER_PAUSED.discard(u)
            USER_PAUSED.discard(acc_id)

            added += 1
            await asyncio.sleep(0.3)

        except Exception as e:
            failed += 1
            errors.append(str(e))

    cfg = _get_auto()
    if not cfg.get("limit_enabled"):
        users2 = load_users()
        for acc in users2.get("accounts", []):
            if acc.get("type") != "guest":
                continue
            u = str(acc.get("uid", "")).strip()
            p = str(acc.get("password", "")).strip()
            if not u or not p:
                continue
            if u in USER_PAUSED or u in USER_DELETED:
                continue
            old = USER_WORKERS.get(u)
            if old and not old.done():
                continue
            await start_worker_for(u, p)
    else:
        await _enforce_concurrent_limits()

    return web.json_response({
        "status": "ok",
        "added": added,
        "failed": failed,
        "errors": errors[:5],
    })


async def h_acc_delete(req):
    if is_locked():
        return web.json_response({"status": "error", "error": "Site Locked"}, status=403)

    try:
        d = await req.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid JSON"})

    uid = str(d.get("uid", "")).strip()
    if not uid:
        return web.json_response({"status": "error", "error": "UID missing"})

    await _delete_account_logic(uid)
    return web.json_response({"status": "ok"})


async def h_acc_pause(req):
    if is_locked():
        return web.json_response({"status": "error", "error": "Site Locked"}, status=403)

    try:
        d = await req.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid JSON"})

    uid = str(d.get("uid", "")).strip()
    if not uid:
        return web.json_response({"status": "error", "error": "UID missing"})

    real_uid = None
    cred = bot_state.account_credentials.get(uid)
    if cred:
        real_uid = str(cred.get("auth_uid", ""))

    USER_PAUSED.add(uid)
    if real_uid:
        USER_PAUSED.add(real_uid)

    _PENDING.pop(uid, None)
    if real_uid:
        _PENDING.pop(real_uid, None)

    await stop_worker_for(uid)
    if real_uid:
        await stop_worker_for(real_uid)

    if uid in bot_state.accounts:
        bot_state.accounts[uid]["status"] = "PAUSED"
    if real_uid and real_uid in bot_state.accounts:
        bot_state.accounts[real_uid]["status"] = "PAUSED"

    await _enforce_concurrent_limits()
    return web.json_response({"status": "ok"})


async def h_acc_resume(req):
    if is_locked():
        return web.json_response({"status": "error", "error": "Site Locked"}, status=403)

    try:
        d = await req.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid JSON"})

    uid = str(d.get("uid", "")).strip()
    if not uid:
        return web.json_response({"status": "error", "error": "UID missing"})

    real_uid = None
    cred = bot_state.account_credentials.get(uid)
    if cred:
        real_uid = str(cred.get("auth_uid", ""))

    USER_PAUSED.discard(uid)
    if real_uid:
        USER_PAUSED.discard(real_uid)

    acc = _find_account_in_pool(uid)
    if not acc and real_uid:
        acc = _find_account_in_pool(real_uid)

    cfg = _get_auto()
    if acc:
        pool_uid = str(acc.get("uid", "")).strip()
        pool_pw = str(acc.get("password", "")).strip()
        if pool_uid and pool_pw:
            if not cfg.get("limit_enabled"):
                await start_worker_for(pool_uid, pool_pw)
            else:
                await _enforce_concurrent_limits()

    return web.json_response({"status": "ok"})


# ==================== LOCK APIs ====================
async def h_lock_status(req):
    return web.json_response({
        "status": "ok",
        "locked": is_locked(),
    })


async def h_lock_verify(req):
    try:
        d = await req.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid JSON"})

    pw = str(d.get("password", "")).strip()
    if not pw:
        return web.json_response({"status": "error", "error": "Password required"})

    if pw == OWNER_PASSWORD:
        return web.json_response({"status": "ok"})
    return web.json_response({"status": "error", "error": "Wrong password"})


async def h_lock_toggle(req):
    try:
        d = await req.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid JSON"})

    locked = bool(d.get("locked", False))
    save_lock({"locked": locked, "updated_at": time.time()})

    try:
        if locked:
            print("[lock] SITE LOCKED -> stopping all workers", flush=True)
            await stop_all_workers()
        else:
            print("[lock] SITE UNLOCKED -> starting all workers", flush=True)
            await start_all_workers()
    except Exception as e:
        print(f"[lock] worker toggle error: {e}", flush=True)

    return web.json_response({"status": "ok", "locked": locked})


async def h_lock_stats(req):
    try:
        users = load_users()
        accs = users.get("accounts", [])
        total_exp = 0
        total_acc = len(accs)

        seen = set()
        for uid, a in list(bot_state.accounts.items()):
            seen.add(str(uid))
            total_exp += int(a.get("gained_exp", 0) or 0)

        for acc in accs:
            key = str(acc.get("uid", ""))
            if key in seen:
                continue
            total_exp += int(acc.get("gained_exp", 0) or 0)

        return web.json_response({
            "status": "ok",
            "total_accounts": total_acc,
            "total_gained_exp": total_exp,
        })
    except Exception as e:
        return web.json_response({
            "status": "error",
            "error": str(e),
            "total_accounts": 0,
            "total_gained_exp": 0,
        })


async def h_lock_download(req):
    try:
        users = load_users()
        accs = users.get("accounts", [])

        out: List[Dict[str, Any]] = []
        total_exp = 0

        for acc in accs:
            uid = str(acc.get("uid", ""))
            entry = {
                "uid": uid,
                "password": str(acc.get("password", "")),
                "account_id": str(acc.get("account_id", uid)),
                "name": str(acc.get("nickname", "")),
                "region": str(acc.get("region", "")).upper(),
                "level": int(acc.get("level", 1) or 1),
                "initial_exp": int(acc.get("initial_exp", 0) or 0),
                "current_exp": int(acc.get("last_exp", 0) or 0),
                "gained_exp": int(acc.get("gained_exp", 0) or 0),
                "matches_played": int(acc.get("matches_played", 0) or 0),
                "created_at": acc.get("created_at"),
                "last_update": acc.get("last_update"),
            }
            live = bot_state.accounts.get(uid)
            if live:
                entry["name"] = live.get("nickname", entry["name"]) or entry["name"]
                entry["region"] = str(live.get("region", entry["region"])).upper() or entry["region"]
                entry["level"] = int(live.get("level", entry["level"]) or entry["level"])
                entry["current_exp"] = int(live.get("current_exp", entry["current_exp"]) or 0)
                entry["gained_exp"] = int(live.get("gained_exp", entry["gained_exp"]) or 0)
                entry["matches_played"] = int(live.get("matches_played", entry["matches_played"]) or 0)

            total_exp += entry["gained_exp"]
            out.append(entry)

        payload = {
            "exported_at": time.time(),
            "exported_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_accounts": len(out),
            "total_gained_exp": total_exp,
            "accounts": out,
        }

        body = json.dumps(payload, indent=2, ensure_ascii=False)
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"shappno_accounts_{ts}.json"

        return web.Response(
            body=body,
            content_type="application/json",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


# ==================== AUTO APIs ====================
async def h_auto_get(req):
    cfg = _get_auto()
    return web.json_response({
        "status": "ok",
        "delete_enabled": cfg["delete_enabled"],
        "target_level": cfg["target_level"],
        "limit_enabled": cfg["limit_enabled"],
        "lw_limit": cfg["lw_limit"],
        "br_limit": cfg["br_limit"],
    })


async def h_auto_set_delete(req):
    try:
        d = await req.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid JSON"})

    enabled = bool(d.get("enabled", False))
    tgt = int(d.get("target_level", 22) or 22)
    if tgt < 1 or tgt > 100:
        return web.json_response({"status": "error", "error": "Target level must be 1-100"})

    cfg = _get_auto()
    cfg["delete_enabled"] = enabled
    cfg["target_level"] = tgt
    _set_auto(cfg)

    print(f"[AUTO] delete_enabled={enabled} target_level={tgt}", flush=True)
    return web.json_response({
        "status": "ok",
        "delete_enabled": enabled,
        "target_level": tgt,
    })


async def h_auto_set_limit(req):
    try:
        d = await req.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid JSON"})

    enabled = bool(d.get("enabled", False))
    lw = int(d.get("lw_limit", 5) or 5)
    br = int(d.get("br_limit", 5) or 5)

    if enabled:
        if lw < 1 or lw > 100:
            return web.json_response({"status": "error", "error": "LW limit must be 1-100"})
        if br < 1 or br > 100:
            return web.json_response({"status": "error", "error": "BR limit must be 1-100"})

    cfg = _get_auto()
    cfg["limit_enabled"] = enabled
    cfg["lw_limit"] = lw
    cfg["br_limit"] = br
    _set_auto(cfg)

    print(f"[AUTO] limit_enabled={enabled} lw={lw} br={br}", flush=True)

    try:
        await _enforce_concurrent_limits()
    except Exception as e:
        print(f"[AUTO] enforce error: {e}", flush=True)

    return web.json_response({
        "status": "ok",
        "limit_enabled": enabled,
        "lw_limit": lw,
        "br_limit": br,
    })
    
# ==================== MAIN REGISTER FUNCTION (new — Railway-ready) ====================
async def register_dashboard_routes(
    host: str = "0.0.0.0",
    port: int = 20335,
    extra_route_registrar=None
):

    app = web.Application()

    # ---- Pages ----
    app.router.add_get("/", h_root)
    app.router.add_get("/dashboard", h_dashboard)
    app.router.add_get(LOCK_PATH, h_lock_page)
    app.router.add_get("/favicon.ico", h_favicon)

    # ---- Stats ----
    app.router.add_get("/api/stats", h_stats)

    # ---- Account APIs ----
    app.router.add_post("/api/account/add-job", h_acc_add_job)
    app.router.add_post("/api/account/bulk-add", h_acc_bulk_add)
    app.router.add_post("/api/account/delete", h_acc_delete)
    app.router.add_post("/api/account/pause", h_acc_pause)
    app.router.add_post("/api/account/resume", h_acc_resume)

    # ---- Auto APIs ----
    app.router.add_get("/api/auto/get", h_auto_get)
    app.router.add_post("/api/auto/set-delete", h_auto_set_delete)
    app.router.add_post("/api/auto/set-limit", h_auto_set_limit)

    # ---- Lock APIs ----
    app.router.add_get("/api/lock-status", h_lock_status)
    app.router.add_post("/api/lock-verify", h_lock_verify)
    app.router.add_post("/api/lock-toggle", h_lock_toggle)
    app.router.add_get("/api/lock-stats", h_lock_stats)
    app.router.add_get("/api/lock-download", h_lock_download)

    # ---- Extra routes (main.py থেকে BR API) ----
    if extra_route_registrar:
        try:
            extra_route_registrar(app)
            print("[auth] extra routes registered from main.py", flush=True)
        except Exception as e:
            print(f"[auth] extra route register failed: {e}", flush=True)

    # ---- Catch-all SPA (সবার শেষে) ----
    async def _not_found(req):
        if req.path.startswith("/api/"):
            return web.json_response({"status": "error", "error": "Not Found"}, status=404)
        return web.Response(text=DASHBOARD_HTML, content_type="text/html", charset="utf-8")

    app.router.add_route("*", "/{tail:.*}", _not_found)

    # ---- Runner ----
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    print(f"\033[92m[+] Dashboard: http://{host}:{port}\033[0m", flush=True)
    print(f"\033[96m[+] Owner Lock: http://{host}:{port}{LOCK_PATH}\033[0m", flush=True)
    print(f"\033[93m[i] Owner Password: {OWNER_PASSWORD}\033[0m", flush=True)

    # ---- Background tasks ----
    asyncio.create_task(_auto_delete_watcher())
    asyncio.create_task(_limiter_watcher())
    asyncio.create_task(_periodic_refresh())

    # ---- Startup workers ----
    if not is_locked():
        asyncio.create_task(start_all_workers())
    else:
        print("[startup] site is LOCKED -> no workers started", flush=True)

    return runner


# ==================== BACKWARD COMPAT (old signature) ====================
async def start_web_dashboard(host: str = "0.0.0.0", port: int = 20335):
    """
    পুরোনো signature — auth_layer এর internal use জন্য।
    main.py আর এটা কল করবে না (register_dashboard_routes কল করবে)।
    """
    return await register_dashboard_routes(
        host=host,
        port=port,
        extra_route_registrar=None,
    )


# ==================== EXPORTS ====================
__all__ = [
    "bot_state",
    "register_dashboard_routes",
    "start_web_dashboard",
    "USER_WORKERS",
    "USER_PAUSED",
    "USER_DELETED",
    "load_users",
    "save_users",
    "_CURRENT_OWNER",
    "_set_mode",
    "_clear_mode",
    "_get_mode",
    "_update_saved_mode",
    "is_locked",
    "load_lock",
    "save_lock",
]


# ==================== HELPER FOR main.py compat ====================
def _update_saved_mode(owner: str, key: str, mode: str):
    """main.py থেকে কল হতে পারে — account এর mode users.json এ save করে।"""
    try:
        users = load_users()
        for a in users.get("accounts", []):
            a_uid = str(a.get("uid", ""))
            a_accid = str(a.get("account_id", ""))
            if a_uid == str(key) or a_accid == str(key):
                a["mode"] = mode
                break
        save_users(users)
    except Exception as e:
        print(f"[update-saved-mode] {e}", flush=True)