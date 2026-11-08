import os, json, base64, binascii, asyncio, time, threading, ssl, random, re, hmac, zipfile
from io import BytesIO
from collections import deque
from functools import wraps
from threading import RLock
from urllib.parse import quote_plus
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response, session, redirect, url_for, render_template
import requests as req_lib, aiohttp, urllib3
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import like_pb2, like_count_pb2, uid_generator_pb2

app = Flask(__name__, template_folder=".")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("UI_SECRET_KEY") or os.urandom(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

OWNER = "TG: @fsfuhx"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
TOKEN_DIR = os.path.join(BASE, "tokens_cache")
FAILED_DIR = os.path.join(BASE, "failed_accounts")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TOKEN_DIR, exist_ok=True)
os.makedirs(FAILED_DIR, exist_ok=True)

ACCOUNTS_FILE = os.path.join(BASE, "account.txt")
TOKEN_FILE = os.path.join(TOKEN_DIR, "token_bd.json")

ALLOWED_KEYS = {"FXFUHXFFKING": 999999}
ADMIN_KEYS = ["FXFUHXFFKING"]

MAX_TOKEN_ATTEMPTS = 10
RETRY_BACKOFF = 25
TOKEN_CONCURRENCY = 4

JWT_API_URL = "http://loginbp.ffbd.store/jwt"

POLL_TIMEOUT = 4.0
POLL_PROBE = 5

RELEASE_VERSION = "OB55"
MSDK_APP_VER = "1.0.0"
UNITY_VERSION = "2018.4.12f1"
UNITY_USERAGENT = "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)"

HOST_BP = "https://clientbp.ppmainecoonghj.com"
HOST_IND = "https://clientbp.ppmainecoonghj.com"
HOST_US = "https://clientbp.ppmainecoonghj.com"

OB_VERSION_URL = "https://version.ggwhitehawk.com/live/ver.php"


# ---------- UI / Dashboard state ----------
UI_SETTINGS_FILE = os.path.join(BASE, "ui_settings.json")
LOG_BUFFER = deque(maxlen=1000)
LOG_LOCK = RLock()
SETTINGS_LOCK = RLock()
UI_SETTINGS = {"jwt_api_url": None, "auto_refresh_enabled": True, "force_refresh_0300_enabled": True}

def _load_ui_settings():
    global JWT_API_URL
    try:
        if os.path.exists(UI_SETTINGS_FILE):
            with open(UI_SETTINGS_FILE, "r") as _f:
                _d = json.load(_f)
            if isinstance(_d, dict):
                if isinstance(_d.get("jwt_api_url"), str) and _d["jwt_api_url"].startswith("http"):
                    UI_SETTINGS["jwt_api_url"] = _d["jwt_api_url"].strip()
                    JWT_API_URL = UI_SETTINGS["jwt_api_url"]
                if isinstance(_d.get("auto_refresh_enabled"), bool):
                    UI_SETTINGS["auto_refresh_enabled"] = _d["auto_refresh_enabled"]
                if isinstance(_d.get("force_refresh_0300_enabled"), bool):
                    UI_SETTINGS["force_refresh_0300_enabled"] = _d["force_refresh_0300_enabled"]
                elif isinstance(_d.get("force_refresh_0350_enabled"), bool):
                    # migrate old 03:50 key -> 03:00 (পুরানো OFF setting হারাবে না)
                    UI_SETTINGS["force_refresh_0300_enabled"] = _d["force_refresh_0350_enabled"]
    except Exception:
        pass

def _save_ui_settings():
    try:
        with SETTINGS_LOCK:
            with open(UI_SETTINGS_FILE, "w") as _f:
                json.dump(UI_SETTINGS, _f, indent=2)
    except Exception as _e:
        log(f"[UI] settings save failed: {_e}")

def _ui_settings_snapshot():
    return {
        "jwt_api_url": JWT_API_URL,
        "auto_refresh_enabled": bool(UI_SETTINGS.get("auto_refresh_enabled", True)),
        "force_refresh_0300_enabled": bool(UI_SETTINGS.get("force_refresh_0300_enabled", True)),
    }

def fetch_ob_version() -> None:
    global RELEASE_VERSION, MSDK_APP_VER
    params = {
        "version": MSDK_APP_VER,
        "lang": "hi",
        "device": "android",
        "channel": "android",
        "appstore": "googleplay",
        "region": "BD",
        "whitelist_version": "1.3.0",
        "whitelist_sp_version": "1.0.0",
    }
    try:
        r = req_lib.get(OB_VERSION_URL, params=params, timeout=15, verify=False)
        r.raise_for_status()
        data = r.json()
        if data.get("latest_release_version"):
            RELEASE_VERSION = data["latest_release_version"]
        if data.get("remote_version"):
            MSDK_APP_VER = data["remote_version"]
        log(f"Auto Up => OB={RELEASE_VERSION}  Ver={MSDK_APP_VER}")
    except req_lib.exceptions.RequestException as e:
        log(f"Auto Up Failed (request error) => {e}")
    except json.JSONDecodeError as e:
        log(f"Auto Up Failed (JSON parse error) => {e}")
    except Exception as e:
        log(f"Auto Up Failed (unexpected error) => {e}")

TOKEN_CACHE = []
LAST_REFRESH = None
CACHE_LOCK = RLock()

_load_ui_settings()

# ---------- Main API Manager (GG-style gateway) ----------
import manager as _mgr_mod

MANAGER = _mgr_mod.Manager(
    _mgr_mod.make_store(BASE),
    builtin_keys=ALLOWED_KEYS,
)


def _discovered_slots():
    """Slot numbers whose account file exists: 0 = account.txt, N = accountN.txt."""
    slots = []
    if os.path.exists(ACCOUNTS_FILE):
        slots.append(0)
    slots += [n for n, _f in _discover_numbered_accounts()]
    return sorted(set(slots))


def _slot_paths(n):
    if int(n) == 0:
        return ACCOUNTS_FILE, TOKEN_FILE
    return _account_numbered_path(int(n)), os.path.join(TOKEN_DIR, f"token{int(n)}.json")


def _slot_tokens(n):
    acc_path, tok_path = _slot_paths(n)
    tokens = load_tokens_from(tok_path)
    if not tokens:
        accs = load_accounts_from(acc_path)
        tokens = generate_tokens_from(accs)
        if tokens:
            save_tokens_to(tok_path, tokens)
    return tokens


def _notify_admin(text):
    """Best-effort admin alert via the Telegram bot (no-op if bot off)."""
    try:
        import telegram_bot as _tg

        _tg.notify_admin(text)
    except Exception:
        pass


SLOT_TEST_RESULTS = {}
SLOT_TEST_LOCK = RLock()


def _slot_test(n):
    """Health test for one slot: account count, cached tokens, and a
    sample JWT generation (first 3 accounts) — sends NO likes."""
    acc_path, tok_path = _slot_paths(n)
    accs = load_accounts_from(acc_path)
    cached = load_tokens_from(tok_path)
    sample = accs[:3]
    tokens, failed = ([], [])
    if sample:
        tokens, failed = _gen_tokens_with_failed(sample)
    result = {
        "slot": int(n),
        "accounts": len(accs),
        "cached_tokens": len(cached),
        "sample_size": len(sample),
        "sample_ok": len(tokens),
        "sample_failed": len(failed),
        "ok": bool(sample) and len(tokens) > 0,
        "time": get_bd_time().strftime("%I:%M %p"),
    }
    with SLOT_TEST_LOCK:
        SLOT_TEST_RESULTS[int(n)] = result
    return result


def _slot_test_async(n):
    with SLOT_TEST_LOCK:
        SLOT_TEST_RESULTS[int(n)] = {"slot": int(n), "running": True}
    def _run():
        try:
            _slot_test(n)
        except Exception as _e:
            with SLOT_TEST_LOCK:
                SLOT_TEST_RESULTS[int(n)] = {"slot": int(n), "ok": False, "error": str(_e)}
    threading.Thread(target=_run, daemon=True).start()

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

def log(msg):
    try:
        _ts = get_bd_time().strftime('%H:%M:%S')
    except Exception:
        _ts = time.strftime('%H:%M:%S')
    line = f"[{_ts}] [LIKEAPI] {msg}"
    print(line, flush=True)
    try:
        with LOG_LOCK:
            LOG_BUFFER.append(line)
    except Exception:
        pass

def get_bd_time():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Dhaka"))
    except Exception:
        return datetime.utcnow() + timedelta(hours=6)

def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

def build_headers(token=None):
    h = {}
    h['User-Agent']      = UNITY_USERAGENT
    h['Accept']          = "*/*"
    h['Accept-Encoding'] = "deflate, gzip"
    h['X-GA-SV']         = str(int(time.time()))
    if token:
        h['Authorization'] = f"Bearer {token}"
    h['X-GA']            = "v1 1"
    h['ReleaseVersion']  = RELEASE_VERSION
    h['Content-Type']    = "application/x-www-form-urlencoded"
    h['X-Unity-Version'] = UNITY_VERSION
    return h

def host_for(server):
    s = (server or "BD").upper()
    if s == "IND":
        return HOST_IND
    if s in {"BR", "US", "SAC", "NA"}:
        return HOST_US
    return HOST_BP

def jwt_account_id(token: str):
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        pad_len = (-len(parts[1])) % 4
        payload_b64 = parts[1] + ("=" * pad_len)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("account_id") or payload.get("accountId") or payload.get("uid")
    except Exception:
        return None

def dedupe_tokens(token_list):
    seen = {}
    no_id = 0
    for entry in token_list:
        raw = entry["token"]
        bare = raw[7:] if raw.startswith("Bearer ") else raw
        aid = jwt_account_id(bare)
        if aid is None:
            no_id += 1
            seen.setdefault(f"__unknown_{no_id}", entry)
            continue
        if aid not in seen:
            seen[aid] = entry
    unique = list(seen.values())
    stats = {
        "input": len(token_list),
        "unique": len(unique),
        "duplicates": len(token_list) - len(unique),
        "unparsable": no_id,
    }
    return unique, stats

async def _fetch_one_async(session, uid, pw, sem):
    url = f"{JWT_API_URL}?uid={uid}&password={quote_plus(pw)}"
    try:
        async with sem:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    j = await r.json(content_type=None)
                    if isinstance(j, list) and j:
                        return j[0].get("token")
                    elif isinstance(j, dict):
                        return j.get("token") or j.get("jwt_token") or j.get("access_token")
    except Exception:
        return None
    return None

async def _gen_all_async(accs):
    connector = aiohttp.TCPConnector(
        ssl=_SSL_CTX, limit=0, limit_per_host=0,
        ttl_dns_cache=300, force_close=False, enable_cleanup_closed=True,
    )
    tokens = []
    remaining = list(accs)
    sem = asyncio.Semaphore(TOKEN_CONCURRENCY)

    async with aiohttp.ClientSession(connector=connector) as session:
        for attempt in range(1, MAX_TOKEN_ATTEMPTS + 1):
            if not remaining:
                break
            log(f"  round {attempt}/{MAX_TOKEN_ATTEMPTS}: trying {len(remaining)} accounts, {TOKEN_CONCURRENCY} concurrent...")

            results = await asyncio.gather(
                *[
                    _fetch_one_async(session, u, p, sem)
                    for u, p in remaining
                ],
                return_exceptions=True
            )

            next_remaining = []
            ok_this_round = 0
            for (u, p), r in zip(remaining, results):
                if isinstance(r, str) and r:
                    tokens.append({"token": r})
                    ok_this_round += 1
                else:
                    next_remaining.append((u, p))

            log(f"    round {attempt}: +{ok_this_round} ok, {len(next_remaining)} still failing")
            remaining = next_remaining

            if remaining and attempt < MAX_TOKEN_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF + random.uniform(0, 0.2))

    if remaining:
        log(f"[WARN] {len(remaining)} accounts failed after {MAX_TOKEN_ATTEMPTS} rounds")
    log(f"[OK] {len(tokens)}/{len(accs)} tokens generated")
    return tokens, remaining

def load_accounts_from(path):
    accs = []
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    if ":" in line:
                        u, p = line.strip().split(":", 1)
                        if u and p:
                            accs.append((u, p))
    except Exception:
        pass
    return accs

def load_accounts():
    return load_accounts_from(ACCOUNTS_FILE)

def _gen_tokens_with_failed(accs):
    """Returns (tokens, failed_accs) — used by refresh to track and save failed accounts."""
    if not accs:
        return [], []
    log(f"Gen {len(accs)} tokens (retry-until-success, max {MAX_TOKEN_ATTEMPTS} rounds)...")
    start = time.time()
    tokens, failed = run_async(_gen_all_async(accs))
    elapsed = time.time() - start
    log(f"[OK] {len(tokens)}/{len(accs)} tokens in {elapsed:.2f}s | failed: {len(failed)}")
    return tokens, failed

def generate_tokens_from(accs):
    tokens, _ = _gen_tokens_with_failed(accs)
    return tokens

def generate_tokens():
    return generate_tokens_from(load_accounts())

def save_tokens_to(path, t):
    try:
        with open(path, "w") as f:
            json.dump(t, f)
    except Exception:
        pass

def load_tokens_from(path):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                d = json.load(f)
                return d if isinstance(d, list) else d.get("tokens", [])
    except Exception:
        pass
    return []

def save_tokens(t):
    save_tokens_to(TOKEN_FILE, t)

def load_tokens():
    return load_tokens_from(TOKEN_FILE)

def get_tokens():
    global TOKEN_CACHE, LAST_REFRESH
    with CACHE_LOCK:
        if TOKEN_CACHE:
            return TOKEN_CACHE
        t = load_tokens()
        if not t:
            t = generate_tokens()
            if t:
                save_tokens(t)
        TOKEN_CACHE = t
        LAST_REFRESH = datetime.now()
        return t

def refresh_tokens():
    global TOKEN_CACHE, LAST_REFRESH
    with CACHE_LOCK:
        t = generate_tokens()
        if t:
            TOKEN_CACHE = t
            save_tokens(t)
            LAST_REFRESH = datetime.now()
            log(f"[OK] Refreshed {len(t)} tokens")
        return t

def _account_numbered_path(n):
    """accountN.txt path for n — exact first, then leading-zero variant (account001.txt for n=1)."""
    exact = os.path.join(BASE, f"account{n}.txt")
    if os.path.exists(exact):
        return exact
    try:
        for fname in os.listdir(BASE):
            m = re.fullmatch(r"account(\d+)\.txt", fname)
            if m and int(m.group(1)) == n:
                return os.path.join(BASE, fname)
    except Exception:
        pass
    return exact

def _discover_numbered_accounts():
    """All accountN.txt files on disk, sorted by N. No hard-coded upper limit. One entry per N (exact accountN.txt wins over leading-zero variant)."""
    by_n = {}
    try:
        for fname in os.listdir(BASE):
            m = re.fullmatch(r"account(\d+)\.txt", fname)
            if m:
                n = int(m.group(1))
                exact = f"account{n}.txt"
                if n not in by_n or fname == exact:
                    by_n[n] = fname
    except Exception:
        pass
    return sorted(by_n.items())

TOKEN_GAP = 10

def refresh_one_account(label):
    """Force JWT gen for ONE account file only. Returns (count, failed_count)."""
    global TOKEN_CACHE, LAST_REFRESH
    if label == "account.txt":
        acc_path, tok_path, is_main = ACCOUNTS_FILE, TOKEN_FILE, True
    else:
        m = re.fullmatch(r"account(\d+)\.txt", label)
        if not m:
            return 0, 0
        n = int(m.group(1))
        acc_path = os.path.join(BASE, label)
        tok_path = os.path.join(TOKEN_DIR, f"token{n}.json")
        is_main = False
    if not os.path.exists(acc_path):
        log(f"[FORCE-GEN] {label} not found")
        return 0, 0
    accs = load_accounts_from(acc_path)
    if not accs:
        log(f"[FORCE-GEN] {label} empty — skipped")
        return 0, 0
    log(f"[FORCE-GEN] {label} ({len(accs)} accounts) — generating JWT...")
    failed_path = os.path.join(FAILED_DIR, f"failed_{label}")
    if os.path.exists(failed_path):
        try:
            os.remove(failed_path)
        except Exception:
            pass
    toks, failed_accs = _gen_tokens_with_failed(accs)
    if failed_accs:
        try:
            with open(failed_path, "w") as ff:
                for u, pw in failed_accs:
                    ff.write(f"{u}:{pw}\n")
            log(f"[FORCE-GEN] {len(failed_accs)} failed saved => failed_{label}")
        except Exception as e:
            log(f"[FORCE-GEN] Could not save failed_{label}: {e}")
    if toks:
        if is_main:
            with CACHE_LOCK:
                TOKEN_CACHE = toks
                save_tokens(toks)
                LAST_REFRESH = datetime.now()
        else:
            save_tokens_to(tok_path, toks)
        log(f"[FORCE-GEN] {label} => {len(toks)} tokens saved")
        return len(toks), len(failed_accs)
    log(f"[FORCE-GEN] {label} => token gen failed (0 tokens)")
    return 0, len(failed_accs)

def refresh_all_accounts():
    global TOKEN_CACHE, LAST_REFRESH

    slots = []
    if os.path.exists(ACCOUNTS_FILE):
        slots.append(("account.txt", ACCOUNTS_FILE, TOKEN_FILE, True))
    for n, fname in _discover_numbered_accounts():
        acc_path = os.path.join(BASE, fname)
        tok_path = os.path.join(TOKEN_DIR, f"token{n}.json")
        slots.append((fname, acc_path, tok_path, False))

    if not slots:
        log("[ALL-REFRESH] No account files found — nothing to do.")
        return {}

    log(f"[ALL-REFRESH] Starting sequential refresh — {len(slots)} file(s), {TOKEN_GAP}s gap between each.")

    summary = {}

    for idx, (label, acc_path, tok_path, is_main) in enumerate(slots):

        if idx > 0:
            log(f"[ALL-REFRESH] Gap: waiting {TOKEN_GAP}s before {label}...")
            time.sleep(TOKEN_GAP)

        accs = load_accounts_from(acc_path)
        if not accs:
            log(f"[ALL-REFRESH] {label} => empty or unreadable, skipped")
            summary[label] = 0
            continue

        log(f"[ALL-REFRESH] [{idx+1}/{len(slots)}] Processing {label} ({len(accs)} accounts)...")

        # আগের failed file মুছে দাও
        failed_path = os.path.join(FAILED_DIR, f"failed_{label}")
        if os.path.exists(failed_path):
            try:
                os.remove(failed_path)
                log(f"[ALL-REFRESH] Deleted old failed file: failed_{label}")
            except Exception as e:
                log(f"[ALL-REFRESH] Could not delete failed_{label}: {e}")

        t, failed_accs = _gen_tokens_with_failed(accs)

        # Failed accounts আলাদা file এ সেভ করো
        if failed_accs:
            try:
                with open(failed_path, "w") as ff:
                    for u, p in failed_accs:
                        ff.write(f"{u}:{p}\n")
                log(f"[ALL-REFRESH] {len(failed_accs)} failed accounts saved => failed_{label}")
            except Exception as e:
                log(f"[ALL-REFRESH] Could not save failed_{label}: {e}")
        else:
            log(f"[ALL-REFRESH] {label} => no failures!")

        if t:
            if is_main:
                with CACHE_LOCK:
                    TOKEN_CACHE = t
                    save_tokens(t)
                    LAST_REFRESH = datetime.now()
            else:
                save_tokens_to(tok_path, t)
            tok_name = os.path.basename(tok_path)
            log(f"[ALL-REFRESH] {label} => {len(t)} tokens saved to {tok_name}")
            summary[label] = len(t)
        else:
            log(f"[ALL-REFRESH] {label} => token gen failed")
            summary[label] = 0

    total = sum(summary.values())
    log(f"[ALL-REFRESH] Complete — {len(summary)} file(s) processed, {total} total tokens.")
    return summary

def auto_refresh():
    # 6h JWT auto-generate. Respects UI toggle (persisted in ui_settings.json).
    # OFF = never fires. ON = fires every 6h from when it was turned ON / startup.
    elapsed = 0
    while True:
        time.sleep(60)
        if not UI_SETTINGS.get("auto_refresh_enabled", True):
            elapsed = 0
            continue
        elapsed += 60
        if elapsed >= 21600:
            elapsed = 0
            log("Auto refresh (6h) — all accounts...")
            try:
                refresh_all_accounts()
            except Exception as _e:
                log(f"Auto refresh failed: {_e}")

def scheduled_refresh_300am():
    while True:
        now = get_bd_time()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        # Sleep in 60s chunks so turning the toggle OFF stops the pending fire.
        slept = 0
        cancelled = False
        while slept < wait_sec:
            time.sleep(min(60, wait_sec - slept))
            slept += 60
            if not UI_SETTINGS.get("force_refresh_0300_enabled", True):
                cancelled = True
                log("03:00 force refresh skipped — disabled from UI")
                break
        if cancelled:
            time.sleep(60)
            continue
        log("03:00 BDT force refresh firing — all accounts...")
        try:
            refresh_all_accounts()
        except Exception as _e:
            log(f"03:00 refresh failed: {_e}")
        time.sleep(60)

threading.Thread(target=auto_refresh, daemon=True).start()
threading.Thread(target=scheduled_refresh_300am, daemon=True).start()

KEY = b'Yg&tc%DEuh6%Zc^8'
IV = b'6oyZDr22E3ychjM%'

def enc_msg(data):
    c = AES.new(KEY, AES.MODE_CBC, IV)
    return binascii.hexlify(c.encrypt(pad(data, AES.block_size))).decode()

def enc_uid(uid):
    m = uid_generator_pb2.uid_generator()
    m.krishna_ = int(uid)
    m.teamXdarks = 1
    return enc_msg(m.SerializeToString())

def enc_like(uid, region):
    m = like_pb2.like()
    m.uid = int(uid)
    m.region = region
    return enc_msg(m.SerializeToString())

def get_info(encrypted, server, token):
    url = f"{host_for(server)}/GetPlayerPersonalShow"
    try:
        r = req_lib.post(url, data=bytes.fromhex(encrypted), headers=build_headers(token), verify=False, timeout=3)
        if r.status_code == 200 and r.content:
            obj = like_count_pb2.Info()
            obj.ParseFromString(r.content)
            return obj
    except Exception:
        pass
    return None

def fetch_player_info(encrypted, server, tokens, probe=25):
    result = [None]
    found = threading.Event()

    def try_token(t):
        if found.is_set():
            return
        info = parse_info(get_info(encrypted, server, t["token"]))
        if info and not found.is_set():
            result[0] = info
            found.set()

    sample = tokens[:probe]
    ex = ThreadPoolExecutor(max_workers=min(probe, 25))
    try:
        futs = [ex.submit(try_token, t) for t in sample]
        found.wait(timeout=3)
        for f in futs:
            f.cancel()
    finally:
        ex.shutdown(wait=False)

    return result[0]

def parse_info(obj):
    try:
        if not obj:
            return None
        j = json.loads(MessageToJson(obj))
        a = j.get("AccountInfo") or j.get("basicInfo", {})
        uid = int(a.get("UID", a.get("uid", 0)))
        likes = int(a.get("Likes", a.get("liked", 0)))
        name = str(a.get("PlayerNickname", a.get("nickname", "Unknown")))
        return {"uid": uid, "likes": likes, "name": name} if uid > 0 else None
    except Exception:
        return None

async def _send_one(session, enc_bytes, token, url):
    try:
        async with session.post(url, data=enc_bytes, headers=build_headers(token), timeout=aiohttp.ClientTimeout(total=5)) as r:
            return r.status in (200, 201, 204)
    except Exception:
        return False

async def _fire_all_single(enc_bytes, token_list, url):
    connector = aiohttp.TCPConnector(
        ssl=_SSL_CTX, limit=0, limit_per_host=0,
        ttl_dns_cache=300, force_close=False, enable_cleanup_closed=True,
    )
    log(f"Firing {len(token_list)} tokens in ONE group...")
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(
            *[_send_one(session, enc_bytes, t["token"], url) for t in token_list],
            return_exceptions=True
        )
    ok = sum(1 for r in results if r is True)
    failed = len(token_list) - ok
    log(f"  group: +{ok} ok, {failed} failed")

    if failed:
        failed_tokens = [t for t, r in zip(token_list, results) if r is not True]
        log(f"  retrying {len(failed_tokens)} failures once...")
        async with aiohttp.ClientSession(connector=connector) as session:
            results2 = await asyncio.gather(
                *[_send_one(session, enc_bytes, t["token"], url) for t in failed_tokens],
                return_exceptions=True
            )
        ok2 = sum(1 for r in results2 if r is True)
        ok += ok2
        failed = len(failed_tokens) - ok2
        log(f"  retry: +{ok2} ok, {failed} still failing")

    return ok, failed

def send_all_likes(uid, server, token_list):
    enc_bytes = bytes.fromhex(enc_like(uid, server))
    url = f"{host_for(server)}/LikeProfile"
    ok, failed = run_async(_fire_all_single(enc_bytes, token_list, url))
    return ok, failed

def build_response_text(primary: dict, stats: dict) -> str:
    primary_json = json.dumps(primary, indent=2, ensure_ascii=False)
    stats_block = json.dumps(stats, indent=1, ensure_ascii=False)
    return f"{primary_json}\n{stats_block}"

def process_like(uid, server, key, token_list=None, token_file_path=None):
    if key not in ALLOWED_KEYS:
        return {"error": "Invalid key"}, 403
    if not uid or not uid.isdigit():
        return {"error": "Valid UID required"}, 400

    if token_list is None:
        if token_file_path:
            token_list = load_tokens_from(token_file_path)
            if not token_list:
                return {"error": "No tokens available for this endpoint"}, 500
        else:
            token_list = get_tokens()

    if not token_list:
        return {"error": "No tokens available"}, 500

    unique_tokens = token_list
    log(f"Tokens: {len(unique_tokens)} (dedupe disabled — all sending live)")

    encrypted = enc_uid(uid)
    before = fetch_player_info(encrypted, server, unique_tokens, probe=POLL_PROBE)

    if not before:
        primary = {
            "LikesGivenByAPI": 0,
            "LikesafterCommand": 0,
            "LikesbeforeCommand": 0,
            "Owner": OWNER,
            "PlayerNickname": "Unknown",
            "UID": int(uid),
            "remains": "(∞/∞)",
            "status": 0,
        }
        stats = {
            "total token on this api": len(unique_tokens),
            "Token sent to server": 0,
            "Token success": 0,
            "Ob version": RELEASE_VERSION,
            "Owner": OWNER,
        }
        return primary, stats, 200

    fire_stats = {"ok": 0, "failed": 0}
    like_done = threading.Event()

    def fire():
        ok, failed = send_all_likes(uid, server, unique_tokens)
        fire_stats["ok"] = ok
        fire_stats["failed"] = failed
        like_done.set()

    threading.Thread(target=fire, daemon=True).start()

    after = before
    deadline = time.time() + POLL_TIMEOUT
    improved = False

    MIN_POLL_REMAINING = 1.2

    time.sleep(0.5)
    while time.time() < deadline - MIN_POLL_REMAINING:
        candidate = fetch_player_info(encrypted, server, unique_tokens, probe=POLL_PROBE)
        if candidate and candidate["likes"] > before["likes"]:
            after = candidate
            improved = True
            if like_done.is_set():
                time.sleep(0.6)
                final = fetch_player_info(encrypted, server, unique_tokens, probe=POLL_PROBE)
                if final and final["likes"] > after["likes"]:
                    after = final
                break
        elif like_done.is_set() and time.time() > deadline - 1.0:
            break
        time.sleep(0.4)

    if not improved:
        like_done.wait(timeout=1.0)
        final = fetch_player_info(encrypted, server, unique_tokens, probe=POLL_PROBE)
        if final:
            after = final

    given = max(0, after["likes"] - before["likes"])

    primary = {
        "LikesGivenByAPI": given,
        "LikesafterCommand": after["likes"],
        "LikesbeforeCommand": before["likes"],
        "Owner": OWNER,
        "PlayerNickname": after["name"],
        "UID": after["uid"],
        "remains": "(∞/∞)",
        "status": 1 if given > 0 else 2,
    }
    stats = {
        "total token on this api": len(unique_tokens),
        "Token sent to server": len(unique_tokens),
        "Token success": fire_stats["ok"],
        "Ob version": RELEASE_VERSION,
        "Owner": OWNER,
    }
    return primary, stats, 200

@app.route("/")
def index():
    return jsonify({
        "status": "online",
        "owner": OWNER,
        "ob_version": RELEASE_VERSION,
        "tokens": len(get_tokens()),
        "host_bp": HOST_BP,
        "mode": "single-group firing",
        "client": {"release": RELEASE_VERSION, "unity": UNITY_VERSION},
        "endpoints": {
            "/like": "MAIN API (gateway) ?uid=X&server_name=BD&key=KEY — routes to the current attached slot",
            "/likeN": "Direct per-file API (/like0 = account.txt)",
            "/remain": "Check a customer key: ?key=KEY -> limit/used/remaining/expiry",
            "/likeN": "Per-account-file endpoints — dynamic, accountN.txt যতটা থাকবে ততটা চলবে",
            "/token-stats": "Unique vs duplicate token count",
            "/refresh": "?key=ADMIN_KEY",
            "/update": "?key=ADMIN_KEY",
        }
    })

@app.route("/like")
def like():
    """MAIN API (gateway) — one link for everything.

    GG-style customer key check + per-slot limit/attach routing handled
    by MANAGER (manager.py). The request is served by the CURRENT
    attached slot; when that slot's limit ends the main API transfers
    itself to the next attached slot. 0 likes delivered -> key and
    slot limits are refunded (not cut). Response format is exactly the
    same two-block format as the direct /likeN APIs — the top block is
    byte-identical (remains stays "(∞/∞)"); the key's remain is added
    in the stats block below as "key remains".
    Direct per-file access still lives at /likeN (/like0 = account.txt).
    """
    uid = request.args.get("uid")
    server = request.args.get("server_name", "BD")
    key = request.args.get("key", "")
    discovered = _discovered_slots()
    # Dead-slot skip: if the current slot has no usable tokens (e.g. all
    # its JWTs failed), refund and move on to the next attached slot
    # instead of leaving the main API stuck on a dead API.
    tried = set()
    while True:
        err, info, slot_n = MANAGER.begin(key, discovered, exclude=tried)
        if err == "invalid_key":
            return jsonify({"error": "Invalid or missing API key", "status": 403}), 403
        if err == "expired":
            return jsonify({"error": "Key has expired", "status": 403}), 403
        if err == "no_slot" and tried:
            return jsonify({"error": "No tokens available on any attached API", "status": 0}), 500
        if err == "key_limit":
            if MANAGER.should_alert("key_limit", key):
                _notify_admin(f"🔑 <b>Key-র limit শেষ:</b> <code>{key}</code>\nওই key দিয়ে আর like যাবে না (আজকের জন্য)।")
            return jsonify({"error": "Daily/Total Limit reached for this key.", "status": 429, "remains": f"(0/{info['limit']})"}), 429
        if err == "no_slot":
            if MANAGER.should_alert("all_over", ""):
                _notify_admin("🔴 <b>সব attached API-র limit শেষ / detached!</b>\nMain API এখন কাউকে like দিতে পারছে না। নতুন API attach করো অথবা limit reset করো।")
            return jsonify({"error": "All main APIs are limit-over or detached. Try later.", "status": 503}), 503
        try:
            tokens = _slot_tokens(slot_n)
        except Exception:
            tokens = None
        if tokens:
            break
        MANAGER.rollback(key, slot_n)
        tried.add(slot_n)
        log(f"[MAIN] /like{slot_n} has no tokens — rotating to next API (tried: {sorted(tried)})")
    try:
        result = process_like(uid, server, ADMIN_KEYS[0], token_list=tokens)
    except Exception as e:
        MANAGER.rollback(key, slot_n)
        return jsonify({"error": str(e)}), 500
    if len(result) == 2:
        MANAGER.rollback(key, slot_n)
        return jsonify(result[0]), result[1]
    primary, stats, code = result
    given = primary.get("LikesGivenByAPI", 0)
    if not (primary.get("status") == 1 and given and int(given) > 0):
        # User rule: 0 likes delivered -> limit is NOT cut (refund key + slot)
        MANAGER.rollback(key, slot_n)
    if primary.get("status") == 1 and given and int(given) > 0:
        _view = {s["n"]: s for s in MANAGER.slot_view(discovered)}
        _sv = _view.get(slot_n)
        if _sv and _sv["exhausted"] and MANAGER.should_alert("slot_exhausted", slot_n):
            _pool = MANAGER.pool_slots(discovered)
            _notify_admin(
                f"⚠️ <b>/like{slot_n}-এর limit শেষ</b> ({_sv['used']}/{_sv['limit']})\n"
                f"Rotation pool এখন: {', '.join('/like' + str(_x) for _x in _pool) if _pool else '— কোনো API available নাই —'}"
            )
    remain, limit = MANAGER.key_remain(key)
    # TOP block must stay byte-identical to the direct /likeN response
    # (primary["remains"] stays "(∞/∞)", same fields, same order) — it
    # is already integrated in many places and must not change.
    # The customer key's remain goes in the stats block BELOW instead.
    stats["key remains"] = f"({remain}/{limit})"
    log(f"[MAIN] UID {uid} served by /like{slot_n} — given={given} key remain={remain}/{limit}")
    return Response(build_response_text(primary, stats), status=code, mimetype="application/json")


@app.route("/remain", methods=["GET"])
def remain_route():
    """GG-style key check: /remain?key=KEY -> limit / used / remaining / expiry."""
    req_key = request.args.get("key", "")
    info, err = MANAGER.remain_info(req_key)
    if err == "invalid":
        return jsonify({"error": "Invalid or missing key", "status": 403}), 403
    if err == "expired":
        return jsonify({"error": "Key has expired", "status": 0}), 403
    return jsonify({
        "key": info["key"],
        "type": info.get("type"),
        "limit": int(info["limit"]),
        "used": int(info["used"]),
        "remaining": info["remaining"],
        "remains": info["remains"],
        "expiry": info["expiry"],
    })


@app.route("/like<int:n>")
def like_numbered(n):
    # Dynamic: file থাকলেই endpoint চলবে — /like100 এর জন্য account100.txt লাগবে।
    # নতুন accountN.txt বানালে restart ছাড়াই /likeN কাজ করবে।
    # /like0 = account.txt (main file) direct access
    if int(n) == 0:
        acc_path, tok_path = ACCOUNTS_FILE, TOKEN_FILE
    else:
        acc_path = _account_numbered_path(n)
        tok_path = os.path.join(TOKEN_DIR, f"token{n}.json")
    if not os.path.exists(acc_path):
        return jsonify({"error": f"account{n}.txt not found"}), 404

    tokens = load_tokens_from(tok_path)
    if not tokens:
        accs = load_accounts_from(acc_path)
        tokens = generate_tokens_from(accs)
        if tokens:
            save_tokens_to(tok_path, tokens)

    try:
        result = process_like(
            request.args.get("uid"),
            request.args.get("server_name", "BD"),
            request.args.get("key", ""),
            token_list=tokens,
        )
        if len(result) == 2:
            return jsonify(result[0]), result[1]
        primary, stats, code = result
        return Response(build_response_text(primary, stats), status=code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/token-stats")
def token_stats():
    tokens = get_tokens()
    unique, stats = dedupe_tokens(tokens)
    return jsonify({
        "input_tokens": stats["input"],
        "unique_accounts": stats["unique"],
        "duplicates": stats["duplicates"],
        "unparsable": stats["unparsable"],
        "theoretical_max_likes_per_fire": stats["unique"],
        "note": "Garena counts 1 like per account_id, not per token."
    })

@app.route("/refresh")
def refresh():
    key = request.args.get("key", "")
    if key not in ADMIN_KEYS:
        return jsonify({"error": "Admin key required"}), 403
    start = time.time()
    summary = refresh_all_accounts()
    total = sum(summary.values())

    # failed_accounts ফোল্ডারে কতগুলো file আছে দেখাও
    failed_files = {}
    try:
        for fname in os.listdir(FAILED_DIR):
            if fname.startswith("failed_") and fname.endswith(".txt"):
                fpath = os.path.join(FAILED_DIR, fname)
                with open(fpath, "r") as ff:
                    count = sum(1 for line in ff if ":" in line)
                failed_files[fname] = count
    except Exception:
        pass

    return jsonify({
        "status": "success",
        "total_tokens": total,
        "per_account": summary,
        "failed_accounts": failed_files,
        "failed_dir": FAILED_DIR,
        "elapsed": f"{time.time()-start:.2f}s",
        "time": get_bd_time().strftime("%I:%M %p BDT"),
    })

@app.route("/update")
def update_version():
    key = request.args.get("key", "")
    if key not in ADMIN_KEYS:
        return jsonify({"error": "Admin key required"}), 403
    old = RELEASE_VERSION
    fetch_ob_version()
    return jsonify({
        "status": "updated",
        "ob_previous": old,
        "ob_current": RELEASE_VERSION,
        "msdk_ver": MSDK_APP_VER,
    })


@app.route("/reset")
def reset_tokens():
    # SAME as /refresh (force JWT gen). ?file=accountN.txt দিলে শুধু ওই ফাইলের JWT gen হবে।
    key = request.args.get("key", "")
    if key not in ADMIN_KEYS:
        return jsonify({"error": "Admin key required"}), 403
    fname = (request.args.get("file") or request.args.get("account") or "").strip()
    if fname:
        if fname == "account.txt" or re.fullmatch(r"account\d+\.txt", fname):
            start = time.time()
            count, failed = refresh_one_account(fname)
            return jsonify({"status": "success", "file": fname, "tokens": count, "failed": failed, "elapsed": f"{time.time()-start:.2f}s", "time": get_bd_time().strftime("%I:%M %p BDT")})
        return jsonify({"error": "file must be account.txt or accountN.txt"}), 400
    start = time.time()
    summary = refresh_all_accounts()
    total = sum(summary.values())
    return jsonify({"status": "success", "total_tokens": total, "per_account": summary, "elapsed": f"{time.time()-start:.2f}s", "time": get_bd_time().strftime("%I:%M %p BDT")})

@app.route("/env")
def env_info():
    return jsonify({
        "ob_version": RELEASE_VERSION,
        "msdk_app_ver": MSDK_APP_VER,
        "token_count": len(get_tokens()),
        "last_refresh": LAST_REFRESH.isoformat() if LAST_REFRESH else None,
        "unity": UNITY_VERSION,
        "host_bp": HOST_BP,
        "firing_mode": "single-group",
        "poll_timeout": POLL_TIMEOUT,
        "max_token_attempts": MAX_TOKEN_ATTEMPTS,
        "jwt_api_url": JWT_API_URL,
        "token_concurrency": TOKEN_CONCURRENCY,
        "token_dir": TOKEN_DIR,
        "data_dir": DATA_DIR,
        "python": os.sys.version,
    })


# ================= UI (password-gated) =================
# UI password = existing ADMIN_KEYS entries (same key used for /refresh /update).
# /like, /likeN etc. do NOT use UI session — they keep their own ?key= check.

def _ui_check_password(pw):
    if not isinstance(pw, str) or not pw:
        return False
    try:
        for _k in ADMIN_KEYS:
            if hmac.compare_digest(pw, _k):
                return True
    except Exception:
        return False
    return False

def _ui_login_required(f):
    @wraps(f)
    def _wrapped(*a, **kw):
        if not session.get("ui_authed"):
            if request.path.startswith("/ui/api/"):
                return jsonify({"error": "login required"}), 401
            return redirect(url_for("ui_login", next=request.path))
        return f(*a, **kw)
    return _wrapped

def _account_file_info(fname):
    acc_path = os.path.join(BASE, fname)
    n = None
    m = re.fullmatch(r"account(\d+)\.txt", fname)
    if m:
        n = int(m.group(1))
    if fname == "account.txt":
        tok_path = TOKEN_FILE
    elif n is not None:
        tok_path = os.path.join(TOKEN_DIR, f"token{n}.json")
    else:
        tok_path = None
    accs = load_accounts_from(acc_path) if os.path.exists(acc_path) else []
    toks = load_tokens_from(tok_path) if tok_path and os.path.exists(tok_path) else []
    return {"file": fname, "accounts": len(accs), "tokens": len(toks), "number": n}

def _list_account_files():
    out = []
    if os.path.exists(ACCOUNTS_FILE):
        out.append(_account_file_info("account.txt"))
    for _n, _fname in _discover_numbered_accounts():
        out.append(_account_file_info(_fname))
    return out

def _valid_account_name(name):
    if not isinstance(name, str):
        return False
    if name == "account.txt":
        return True
    return re.fullmatch(r"account\d+\.txt", name) is not None

@app.route("/ui")
def ui_home():
    if not session.get("ui_authed"):
        return redirect(url_for("ui_login"))
    return render_template("ui.html")

@app.route("/ui/login", methods=["GET", "POST"])
def ui_login():
    if request.method == "POST":
        pw = request.form.get("password", "") or (request.get_json(silent=True) or {}).get("password", "")
        if _ui_check_password(pw):
            session["ui_authed"] = True
            session.permanent = True
            log("[UI] login success")
            nxt = request.args.get("next") or url_for("ui_home")
            if request.is_json or request.headers.get("Accept", "").find("application/json") >= 0:
                return jsonify({"ok": True, "next": nxt})
            return redirect(nxt)
        log("[UI] login failed — wrong password")
        if request.is_json:
            return jsonify({"error": "wrong password"}), 401
        return render_template("login.html", error="Wrong password"), 401
    if session.get("ui_authed"):
        return redirect(url_for("ui_home"))
    return render_template("login.html", error=None)

@app.route("/ui/logout")
def ui_logout():
    session.pop("ui_authed", None)
    return redirect(url_for("ui_login"))

@app.route("/ui/api/status")
@_ui_login_required
def ui_api_status():
    accounts = _list_account_files()
    return jsonify({
        "ok": True,
        "time_bd": get_bd_time().strftime("%Y-%m-%d %H:%M:%S BDT"),
        "ob_version": RELEASE_VERSION,
        "jwt_api_url": JWT_API_URL,
        "auto_refresh_enabled": bool(UI_SETTINGS.get("auto_refresh_enabled", True)),
        "force_refresh_0300_enabled": bool(UI_SETTINGS.get("force_refresh_0300_enabled", True)),
        "last_refresh": LAST_REFRESH.isoformat() if LAST_REFRESH else None,
        "accounts": accounts,
        "total_accounts": sum(a["accounts"] for a in accounts),
        "total_tokens": sum(a["tokens"] for a in accounts),
        "main_tokens": len(TOKEN_CACHE) if TOKEN_CACHE else len(load_tokens()),
    })

@app.route("/ui/api/logs")
@_ui_login_required
def ui_api_logs():
    try:
        limit = max(1, min(500, int(request.args.get("limit", "200"))))
    except Exception:
        limit = 200
    with LOG_LOCK:
        lines = list(LOG_BUFFER)[-limit:]
        total = len(LOG_BUFFER)
    return jsonify({"ok": True, "total": total, "logs": lines})

@app.route("/ui/api/settings", methods=["POST"])
@_ui_login_required
def ui_api_settings():
    global JWT_API_URL
    data = request.get_json(silent=True) or {}
    changed = []
    if "jwt_api_url" in data:
        url = (data.get("jwt_api_url") or "").strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            return jsonify({"error": "JWT API URL must start with http:// or https://"}), 400
        url = url.rstrip("/")
        if url != JWT_API_URL:
            JWT_API_URL = url
            UI_SETTINGS["jwt_api_url"] = url
            changed.append("jwt_api_url")
            log(f"[UI] JWT API URL changed to {url}")
    if "auto_refresh_enabled" in data:
        v = bool(data.get("auto_refresh_enabled"))
        if v != UI_SETTINGS.get("auto_refresh_enabled", True):
            UI_SETTINGS["auto_refresh_enabled"] = v
            changed.append("auto_refresh_enabled")
            log(f"[UI] 6h auto-refresh {'ENABLED' if v else 'DISABLED (permanent OFF)'}")
    if "force_refresh_0300_enabled" in data:
        v = bool(data.get("force_refresh_0300_enabled"))
        if v != UI_SETTINGS.get("force_refresh_0300_enabled", True):
            UI_SETTINGS["force_refresh_0300_enabled"] = v
            changed.append("force_refresh_0300_enabled")
            log(f"[UI] 03:00 force refresh {'ENABLED' if v else 'DISABLED'}")
    if changed:
        _save_ui_settings()
    return jsonify({"ok": True, "changed": changed, "settings": _ui_settings_snapshot()})

@app.route("/ui/api/refresh", methods=["POST"])
@_ui_login_required
def ui_api_refresh():
    def _run():
        try:
            log("[UI] manual refresh started")
            summary = refresh_all_accounts()
            log(f"[UI] manual refresh done: {summary}")
        except Exception as _e:
            log(f"[UI] manual refresh failed: {_e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": "refresh started in background — watch logs"})


def _parse_account_content(filename, raw):
    """Accept .txt (uid:password lines) AND .json account files:
    a list of {"uid": ..., "password": ...} objects, a {uid: password}
    map, a {"accounts": [...]} wrapper, or a list of "uid:password"
    strings. Returns (lines, error) — lines are normalized uid:password.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    name = (filename or "").lower()
    data = None
    if name.endswith(".json") or text.lstrip()[:1] in "[{":
        try:
            data = json.loads(text)
        except Exception:
            data = None
    lines = []
    if data is not None:
        items = data
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            items = data["accounts"]
        if isinstance(items, dict) and any(
            k in items for k in ("uid", "UID", "account_id", "accountId", "user_id", "id")
        ) and any(k in items for k in ("password", "pass", "pwd", "Password")):
            # single account object: {"uid": "...", "password": "..."}
            items = [items]
        if isinstance(items, dict):
            for u, v in items.items():
                if isinstance(v, dict):
                    p = v.get("password") or v.get("pass") or v.get("pwd") or v.get("Password")
                else:
                    p = v
                if u and p:
                    lines.append(f"{u}:{p}")
        elif isinstance(items, list):
            for it in items:
                if isinstance(it, str):
                    if ":" in it:
                        lines.append(it.strip())
                elif isinstance(it, (list, tuple)) and len(it) >= 2:
                    lines.append(f"{it[0]}:{it[1]}")
                elif isinstance(it, dict):
                    u = it.get("uid") or it.get("UID") or it.get("account_id") or it.get("accountId") or it.get("user_id") or it.get("id")
                    p = it.get("password") or it.get("pass") or it.get("pwd") or it.get("Password")
                    if u and p:
                        lines.append(f"{u}:{p}")
        lines = [ln for ln in lines if ":" in ln]
        if not lines:
            return None, "could not find any uid/password in the JSON file"
        return lines, None
    for row in text.splitlines():
        s = row.strip()
        if not s:
            continue
        if ":" not in s:
            return None, f"bad line (need uid:password): {s[:40]}"
        lines.append(s)
    if not lines:
        return None, "file is empty — no uid:password lines"
    return lines, None


def _next_free_slot_number():
    used = {n for n, _ in _discover_numbered_accounts()}
    if os.path.exists(ACCOUNTS_FILE):
        used.add(0)
    n = 1
    while n in used:
        n += 1
    return n


def _target_name_for_upload(orig_name, single_target):
    """Map an uploaded file to its account .txt name + slot number.
    account.json -> account.txt (slot 0), account5.json -> account5.txt,
    any other name -> next free accountN.txt slot."""
    if single_target:
        t = os.path.basename(single_target)
        if _valid_account_name(t):
            if t == "account.txt":
                return t, 0
            return t, int(re.fullmatch(r"account(\d+)\.txt", t).group(1))
        return None, None
    base = os.path.basename(orig_name or "")
    m = re.fullmatch(r"account(\d*)\.(txt|json)", base, re.IGNORECASE)
    if m:
        num = m.group(1)
        if num == "":
            return "account.txt", 0
        return f"account{int(num)}.txt", int(num)
    n = _next_free_slot_number()
    return f"account{n}.txt", n


@app.route("/ui/api/accounts/upload", methods=["POST"])
@_ui_login_required
def ui_api_accounts_upload():
    # Multiple upload: একসাথে অনেকগুলো .txt / .json file দেওয়া যাবে।
    # প্রতিটা file accountN.txt হিসেবে save হয়ে manager-এ slot register
    # হবে (attach/limit = manager settings অনুযায়ী) + auto FORCE JWT GEN।
    files = request.files.getlist("file") or request.files.getlist("files")
    files = [f for f in files if f and (f.filename or "").strip()]
    if not files:
        return jsonify({"error": "no file uploaded — choose .txt or .json file(s)"}), 400
    single_target = (request.form.get("target") or "").strip() if len(files) == 1 else ""
    results, errors, saved = [], [], []
    for f in files:
        orig = (f.filename or "").strip()
        target, slot_n = _target_name_for_upload(orig, single_target)
        if target is None:
            errors.append({"file": orig, "error": "target name must be account.txt or accountN.txt"})
            continue
        try:
            raw = f.read()
        except Exception as _e:
            errors.append({"file": orig, "error": f"could not read file: {_e}"})
            continue
        lines, perr = _parse_account_content(orig, raw)
        if perr:
            errors.append({"file": orig, "error": perr})
            continue
        path = os.path.join(BASE, target)
        existed = os.path.exists(path)
        try:
            with open(path, "w") as out:
                for ln in lines:
                    out.write(ln + "\n")
        except Exception as _e:
            errors.append({"file": orig, "error": str(_e)})
            continue
        MANAGER.register_slot(slot_n)
        if existed:
            # fresh accounts on an existing slot -> its limit starts fresh too
            MANAGER.reset_slot_usage(slot_n)
        saved.append(target)
        results.append({"file": target, "from": orig, "accounts": len(lines), "replaced": existed, "slot": slot_n, "endpoint": f"/like{slot_n}"})
        log(f"[UI] account file {'replaced' if existed else 'uploaded'}: {orig} -> {target} ({len(lines)} accounts) — auto FORCE JWT GEN queued")
    if saved:
        def _run_all():
            for tgt in saved:
                try:
                    c, failed = refresh_one_account(tgt)
                    log(f"[UI] upload auto FORCE JWT GEN done: {tgt} => {c} tokens, {failed} failed")
                except Exception as _e:
                    log(f"[UI] upload auto FORCE JWT GEN failed for {tgt}: {_e}")
        threading.Thread(target=_run_all, daemon=True).start()
    if not results:
        return jsonify({"error": "no file could be saved", "errors": errors}), 400
    return jsonify({"ok": True, "results": results, "errors": errors, "msg": f"{len(results)} file(s) uploaded — FORCE JWT GEN running in background"})


@app.route("/ui/api/accounts/<path:name>/force-gen", methods=["POST"])
@_ui_login_required
def ui_api_account_force_gen(name):
    name = (name or "").strip()
    if not _valid_account_name(name):
        return jsonify({"error": "invalid account file name"}), 400
    if not os.path.exists(os.path.join(BASE, name)):
        return jsonify({"error": f"{name} not found"}), 404
    def _run():
        try:
            log(f"[UI] manual FORCE JWT GEN started: {name}")
            c, failed = refresh_one_account(name)
            log(f"[UI] FORCE JWT GEN done: {name} => {c} tokens, {failed} failed")
        except Exception as _e:
            log(f"[UI] FORCE JWT GEN failed for {name}: {_e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "file": name, "msg": "FORCE JWT GEN started in background — watch logs"})


@app.route("/ui/api/manager")
@_ui_login_required
def ui_api_manager():
    disc = _discovered_slots()
    st = MANAGER.global_status(disc)
    st["settings"] = MANAGER.settings()
    st["keys"] = MANAGER.daily_report(disc)["keys"]
    with SLOT_TEST_LOCK:
        st["tests"] = dict(SLOT_TEST_RESULTS)
    return jsonify({"ok": True, **st})


@app.route("/ui/api/manager/next", methods=["POST"])
@_ui_login_required
def ui_api_manager_next():
    cur, changed = MANAGER.next_api(_discovered_slots())
    if cur is None:
        return jsonify({"error": "no attached API with remain available"}), 400
    log(f"[UI] MAIN API manual transfer -> /like{cur} (changed={changed})")
    return jsonify({"ok": True, "current": cur, "changed": changed, "msg": f"Main API now on /like{cur}"})


@app.route("/ui/api/manager/slot/<int:n>", methods=["POST"])
@_ui_login_required
def ui_api_manager_slot(n):
    d = request.get_json(silent=True) or {}
    if "attached" in d:
        MANAGER.set_attached(n, bool(d["attached"]))
    if "limit" in d:
        try:
            MANAGER.set_limit(n, int(d["limit"]))
        except Exception:
            return jsonify({"error": "limit must be a number"}), 400
    if d.get("reset"):
        MANAGER.reset_slot_usage(n)
    return jsonify({"ok": True, "slots": MANAGER.slot_view(_discovered_slots())})


@app.route("/ui/api/manager/settings", methods=["POST"])
@_ui_login_required
def ui_api_manager_settings():
    d = request.get_json(silent=True) or {}
    s = MANAGER.update_settings(
        auto_attach_new=d.get("auto_attach_new") if "auto_attach_new" in d else None,
        default_limit=d.get("default_limit") if "default_limit" in d else None,
        alerts_enabled=d.get("alerts_enabled") if "alerts_enabled" in d else None,
        pool_size=d.get("pool_size") if "pool_size" in d else None,
    )
    return jsonify({"ok": True, "settings": s})


@app.route("/ui/api/manager/reset-all", methods=["POST"])
@_ui_login_required
def ui_api_manager_reset_all():
    MANAGER.reset_all_usage()
    return jsonify({"ok": True, "msg": "All API usage reset"})


@app.route("/ui/api/manager/slot/<int:n>/test", methods=["POST"])
@_ui_login_required
def ui_api_manager_slot_test(n):
    _slot_test_async(n)
    return jsonify({"ok": True, "msg": f"/like{n} test started — কিছুক্ষণ পর result আসবে"})


@app.route("/ui/api/manager/slot/<int:n>/move", methods=["POST"])
@_ui_login_required
def ui_api_manager_slot_move(n):
    d = request.get_json(silent=True) or {}
    try:
        direction = int(d.get("dir", 0))
    except Exception:
        direction = 0
    if direction not in (-1, 1):
        return jsonify({"error": "dir must be -1 or 1"}), 400
    MANAGER.move_slot(n, direction, _discovered_slots())
    return jsonify({"ok": True, "slots": MANAGER.slot_view(_discovered_slots())})


@app.route("/ui/api/keys", methods=["POST"])
@_ui_login_required
def ui_api_keys_create():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    k_type = (d.get("type") or "").strip().lower()
    if not name or k_type not in ("r1", "r2"):
        return jsonify({"error": "name + type (r1/r2) লাগবে"}), 400
    try:
        limit = int(d.get("limit"))
    except Exception:
        return jsonify({"error": "limit number হতে হবে"}), 400
    info, err = MANAGER.create_key(name, k_type, limit, (d.get("duration") or "no"))
    if err == "reserved":
        return jsonify({"error": "এই নামটা reserved (master key)"}), 400
    if err == "exists":
        return jsonify({"error": "এই নামে key আগেই আছে"}), 409
    if err:
        return jsonify({"error": "invalid duration format (e.g. 1d, 1w, no)"}), 400
    log(f"[UI] key created from dashboard: {name} ({k_type}, limit {limit})")
    return jsonify({"ok": True, "key": name, "info": info})


@app.route("/ui/api/keys/<path:name>", methods=["DELETE"])
@_ui_login_required
def ui_api_keys_delete(name):
    if MANAGER.revoke_key(name):
        log(f"[UI] key revoked from dashboard: {name}")
        return jsonify({"ok": True, "revoked": name})
    return jsonify({"error": "key not found"}), 404


@app.route("/ui/api/manager/state-export")
@_ui_login_required
def ui_api_manager_state_export():
    payload = json.dumps(MANAGER.export_state(), ensure_ascii=False, indent=1)
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=manager_state.json"},
    )


@app.route("/ui/api/manager/state-import", methods=["POST"])
@_ui_login_required
def ui_api_manager_state_import():
    data = request.get_json(silent=True)
    if data is None and "file" in request.files:
        try:
            data = json.loads(request.files["file"].read().decode("utf-8-sig"))
        except Exception as _e:
            return jsonify({"error": f"backup file পড়া যায়নি: {_e}"}), 400
    ok, err = MANAGER.import_state(data)
    if not ok:
        return jsonify({"error": err or "invalid state"}), 400
    log("[UI] manager state restored from backup")
    return jsonify({"ok": True, "msg": "State restore হয়েছে ✅"})


@app.route("/ui/api/dashboard")
@_ui_login_required
def ui_api_dashboard():
    # V3 single call for the whole dashboard: status + manager + failed.
    # Same pieces as /ui/api/status and /ui/api/manager — those endpoints
    # stay untouched, this one only bundles them so the UI polls once.
    accounts = _list_account_files()
    disc = _discovered_slots()
    st = MANAGER.global_status(disc)
    st["settings"] = MANAGER.settings()
    st["keys"] = MANAGER.daily_report(disc)["keys"]
    with SLOT_TEST_LOCK:
        st["tests"] = dict(SLOT_TEST_RESULTS)
    status = {
        "ok": True,
        "time_bd": get_bd_time().strftime("%Y-%m-%d %H:%M:%S BDT"),
        "ob_version": RELEASE_VERSION,
        "jwt_api_url": JWT_API_URL,
        "auto_refresh_enabled": bool(UI_SETTINGS.get("auto_refresh_enabled", True)),
        "force_refresh_0300_enabled": bool(UI_SETTINGS.get("force_refresh_0300_enabled", True)),
        "last_refresh": LAST_REFRESH.isoformat() if LAST_REFRESH else None,
        "accounts": accounts,
        "total_accounts": sum(a["accounts"] for a in accounts),
        "total_tokens": sum(a["tokens"] for a in accounts),
        "main_tokens": len(TOKEN_CACHE) if TOKEN_CACHE else len(load_tokens()),
    }
    return jsonify({"ok": True, "status": status, "manager": st, "failed": _failed_summary()})


@app.route("/ui/api/backup")
@_ui_login_required
def ui_api_backup():
    # V3 full backup ZIP: manager state + UI settings + every account
    # file + every token cache + every failed file.
    stamp = get_bd_time().strftime("%Y-%m-%d-%H%M")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manager_state.json", json.dumps(MANAGER.export_state(), ensure_ascii=False, indent=1))
        if os.path.exists(UI_SETTINGS_FILE):
            z.write(UI_SETTINGS_FILE, "ui_settings.json")
        for info in _list_account_files():
            z.write(os.path.join(BASE, info["file"]), info["file"])
        if os.path.isdir(TOKEN_DIR):
            for fname in sorted(os.listdir(TOKEN_DIR)):
                if fname.endswith(".json"):
                    z.write(os.path.join(TOKEN_DIR, fname), "tokens_cache/" + fname)
        if os.path.isdir(FAILED_DIR):
            for fname in sorted(os.listdir(FAILED_DIR)):
                if fname.startswith("failed_") and fname.endswith(".txt"):
                    z.write(os.path.join(FAILED_DIR, fname), "failed_accounts/" + fname)
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=fuhx-likeapi-backup-{stamp}.zip"},
    )


# V4 restore size limits (M2): raw upload / per-entry / total / count.
RESTORE_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
RESTORE_MAX_ENTRY_BYTES = 5 * 1024 * 1024
RESTORE_MAX_TOTAL_BYTES = 50 * 1024 * 1024
RESTORE_MAX_ENTRIES = 200


def _read_zip_entry_capped(zf, name):
    """Read one zip entry with a decompressed-size cap (zip-bomb guard:
    ZipInfo.file_size comes from the header and can lie)."""
    with zf.open(name) as fh:
        data = fh.read(RESTORE_MAX_ENTRY_BYTES + 1)
    if len(data) > RESTORE_MAX_ENTRY_BYTES:
        raise ValueError(f"entry too large (max 5 MB): {name}")
    return data


@app.route("/ui/api/backup/restore", methods=["POST"])
@_ui_login_required
def ui_api_backup_restore():
    global TOKEN_CACHE, LAST_REFRESH
    if "file" not in request.files:
        return jsonify({"error": "backup .zip file লাগবে"}), 400
    f = request.files["file"]
    if not (f.filename or "").lower().endswith(".zip"):
        return jsonify({"error": "only .zip backup files allowed"}), 400
    try:
        raw = f.read(RESTORE_MAX_UPLOAD_BYTES + 1)
    except Exception as _e:
        return jsonify({"error": f"could not read file: {_e}"}), 400
    if len(raw) > RESTORE_MAX_UPLOAD_BYTES:
        return jsonify({"error": "backup zip too large (max 10 MB)"}), 400
    try:
        zf = zipfile.ZipFile(BytesIO(raw))
    except Exception:
        return jsonify({"error": "invalid zip file"}), 400
    names = zf.namelist()
    if len(names) > RESTORE_MAX_ENTRIES:
        return jsonify({"error": f"too many entries in backup (max {RESTORE_MAX_ENTRIES})"}), 400
    for name in names:
        if name.startswith(("/", "\\")) or ".." in name.split("/"):
            return jsonify({"error": f"unsafe path in backup: {name}"}), 400
    total = 0
    for info in zf.infolist():
        if info.file_size > RESTORE_MAX_ENTRY_BYTES:
            return jsonify({"error": f"entry too large (max 5 MB): {info.filename}"}), 400
        total += info.file_size
        if total > RESTORE_MAX_TOTAL_BYTES:
            return jsonify({"error": "backup too large when unpacked (max 50 MB)"}), 400
    # ---- Phase 1: parse + validate EVERYTHING, write nothing yet (V4 H1).
    # Any validation failure here returns 400 with zero disk changes.
    state_data = None
    settings_raw = None
    account_files = {}
    token_files = {}
    failed_files = {}
    errors = []
    try:
        for name in names:
            if name.endswith("/"):
                continue
            if name == "manager_state.json":
                try:
                    state_data = json.loads(_read_zip_entry_capped(zf, name).decode("utf-8-sig"))
                except Exception as _e:
                    return jsonify({"error": f"manager_state.json is not valid JSON: {_e}"}), 400
                ok, reason = _mgr_mod.validate_state(state_data)
                if not ok:
                    return jsonify({"error": f"invalid manager state: {reason}"}), 400
            elif name == "ui_settings.json":
                settings_raw = _read_zip_entry_capped(zf, name)
                try:
                    settings_data = json.loads(settings_raw.decode("utf-8-sig"))
                except Exception as _e:
                    return jsonify({"error": f"ui_settings.json is not valid JSON: {_e}"}), 400
                if not isinstance(settings_data, dict):
                    return jsonify({"error": "ui_settings.json must be a JSON object"}), 400
            elif re.fullmatch(r"account(\d*)\.txt", name):
                content = _read_zip_entry_capped(zf, name).decode("utf-8-sig", errors="replace")
                bad = [ln.strip() for ln in content.splitlines() if ln.strip() and ":" not in ln]
                if bad:
                    return jsonify({"error": f"bad line in {name} (need uid:password): {bad[0][:40]}"}), 400
                account_files[name] = content
            elif name.startswith("tokens_cache/") and re.fullmatch(r"token(_bd|\d+)?\.json", os.path.basename(name)):
                tok_raw = _read_zip_entry_capped(zf, name)
                try:
                    json.loads(tok_raw.decode("utf-8-sig"))
                except Exception as _e:
                    return jsonify({"error": f"token file {name} is not valid JSON: {_e}"}), 400
                token_files[os.path.basename(name)] = tok_raw
            elif name.startswith("failed_accounts/") and re.fullmatch(r"failed_account\d*\.txt", os.path.basename(name)):
                failed_files[os.path.basename(name)] = _read_zip_entry_capped(zf, name)
            else:
                errors.append({"file": name, "error": "skipped — unknown backup entry"})
    except ValueError as _e:
        return jsonify({"error": str(_e)}), 400
    # ---- Phase 2: write files (all inputs already validated).
    restored = []
    replaced_slots = []
    try:
        if settings_raw is not None:
            with open(UI_SETTINGS_FILE, "wb") as out:
                out.write(settings_raw)
            _load_ui_settings()
            restored.append("ui_settings.json")
        for name, content in account_files.items():
            target = name
            existed = os.path.exists(os.path.join(BASE, target))
            with open(os.path.join(BASE, target), "w", encoding="utf-8") as out:
                out.write(content)
            n = 0 if target == "account.txt" else int(re.fullmatch(r"account(\d+)\.txt", target).group(1))
            MANAGER.register_slot(n)
            if existed:
                replaced_slots.append(n)
            restored.append(name)
        for base, tok_raw in token_files.items():
            with open(os.path.join(TOKEN_DIR, base), "wb") as out:
                out.write(tok_raw)
            restored.append("tokens_cache/" + base)
        for base, data in failed_files.items():
            with open(os.path.join(FAILED_DIR, base), "wb") as out:
                out.write(data)
            restored.append("failed_accounts/" + base)
    except Exception as _e:
        return jsonify({"error": f"restore failed while writing files: {_e}"}), 500
    # ---- Phase 3: import manager state LAST (V4 H1/M4).
    if state_data is not None:
        try:
            ok, err = MANAGER.import_state(state_data)
        except Exception as _e:
            ok, err = False, str(_e)
        if not ok:
            return jsonify({"error": f"manager state could not be applied: {err or 'invalid state'}"}), 400
        restored.append("manager_state.json")
    else:
        # No state in this backup: keep the old behaviour — a replaced
        # account file restarts its usage counter from zero.
        for n in replaced_slots:
            try:
                MANAGER.reset_slot_usage(n)
            except Exception:
                pass
    for n in _discovered_slots():
        try:
            MANAGER.register_slot(n)
        except Exception:
            pass
    # Restored token files must take effect at once (V4 M5): drop the
    # in-memory main-token cache so /like0 reads the restored files.
    with CACHE_LOCK:
        TOKEN_CACHE = []
        LAST_REFRESH = None
    log(f"[UI] full backup restored: {len(restored)} files, {len(errors)} errors")
    return jsonify({"ok": True, "restored": restored, "errors": errors,
                    "msg": f"{len(restored)} file(s) restored"})

def _failed_summary():
    # Failed accounts — failed_accounts/failed_accountN.txt থেকে পড়ে দেখায়।
    # প্রতি refresh-এ আগের failed ফাইল মুছে নতুন failed লেখা হয় (refresh_all / refresh_one-এর behavior) — UI-তে সবসময় latest-টাই দেখাবে।
    out = []
    try:
        for fname in sorted(os.listdir(FAILED_DIR)):
            if not (fname.startswith("failed_") and fname.endswith(".txt")):
                continue
            src = fname[len("failed_"):]
            fpath = os.path.join(FAILED_DIR, fname)
            uids = []
            try:
                with open(fpath, "r") as ff:
                    for line in ff:
                        s = line.strip()
                        if not s:
                            continue
                        uids.append(s.split(":", 1)[0] if ":" in s else s)
            except Exception:
                pass
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                mtime = None
            out.append({"file": fname, "source": src, "count": len(uids), "uids": uids, "updated": mtime})
    except Exception as _e:
        return {"ok": False, "error": str(_e), "failed": [], "total_failed": 0}
    return {"ok": True, "failed": out, "total_failed": sum(x["count"] for x in out)}


@app.route("/ui/api/failed")
@_ui_login_required
def ui_api_failed():
    summary = _failed_summary()
    if not summary.get("ok"):
        return jsonify(summary), 500
    return jsonify(summary)


@app.route("/ui/api/failed/<path:source>/retry", methods=["POST"])
@_ui_login_required
def ui_api_failed_retry(source):
    # Failed group retry: same refresh_one_account() the force-gen path
    # uses — re-generates JWT for the source file, failed list rewrites.
    source = (source or "").strip()
    if not _valid_account_name(source):
        return jsonify({"error": "invalid account file name"}), 400
    if not os.path.exists(os.path.join(BASE, source)):
        return jsonify({"error": f"{source} not found"}), 404
    def _run():
        try:
            c, failed = refresh_one_account(source)
            log(f"[UI] failed retry done: {source} => {c} tokens, {failed} failed")
        except Exception as _e:
            log(f"[UI] failed retry failed for {source}: {_e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": f"{source} retry started — JWT GEN running in background"})

@app.route("/ui/api/failed/<path:name>")
@_ui_login_required
def ui_api_failed_one(name):
    # Download one failed file (uid:password) — UI password-gated, তাই full content দেওয়া safe।
    name = os.path.basename((name or "").strip())
    if not (name.startswith("failed_") and name.endswith(".txt")):
        return jsonify({"error": "invalid failed file name"}), 400
    if not re.fullmatch(r"failed_account\d*\.txt", name):
        return jsonify({"error": "invalid failed file name"}), 400
    fpath = os.path.join(FAILED_DIR, name)
    if not os.path.exists(fpath):
        return jsonify({"error": f"{name} not found"}), 404
    try:
        with open(fpath, "r") as ff:
            content = ff.read()
    except Exception as _e:
        return jsonify({"error": str(_e)}), 500
    return Response(content, mimetype="text/plain", headers={"Content-Disposition": f"attachment; filename={name}"})

@app.route("/ui/api/accounts", methods=["GET", "POST"])
@_ui_login_required
def ui_api_accounts():
    if request.method == "GET":
        return jsonify({"ok": True, "accounts": _list_account_files()})
    data = request.get_json(silent=True) or {}
    name = (data.get("file") or "").strip()
    content = data.get("content") or ""
    if not _valid_account_name(name):
        return jsonify({"error": "file must be account.txt or accountN.txt (e.g. account100.txt)"}), 400
    path = os.path.join(BASE, name)
    if os.path.exists(path):
        return jsonify({"error": f"{name} already exists — use edit"}), 409
    # validate lines: empty or uid:password
    lines = []
    for raw in str(content).splitlines():
        s = raw.strip()
        if not s:
            continue
        if ":" not in s:
            return jsonify({"error": f"bad line (need uid:password): {s[:40]}"}), 400
        lines.append(s)
    try:
        with open(path, "w") as f:
            for ln in lines:
                f.write(ln + "\n")
        log(f"[UI] account file created: {name} ({len(lines)} accounts)")
        try:
            MANAGER.register_slot(0 if name == "account.txt" else int(re.fullmatch(r"account(\d+)\.txt", name).group(1)))
        except Exception:
            pass
    except Exception as _e:
        return jsonify({"error": str(_e)}), 500
    if lines:
        def _run_create():
            try:
                c, failed = refresh_one_account(name)
                log(f"[UI] create auto FORCE JWT GEN done: {name} => {c} tokens, {failed} failed")
            except Exception as _e:
                log(f"[UI] create auto FORCE JWT GEN failed for {name}: {_e}")
        threading.Thread(target=_run_create, daemon=True).start()
    return jsonify({"ok": True, "file": name, "accounts": len(lines)})

@app.route("/ui/api/accounts/<path:name>", methods=["GET", "PUT", "DELETE"])
@_ui_login_required
def ui_api_account_one(name):
    name = (name or "").strip()
    if not _valid_account_name(name):
        return jsonify({"error": "invalid account file name"}), 400
    path = os.path.join(BASE, name)
    if request.method == "GET":
        if not os.path.exists(path):
            return jsonify({"error": f"{name} not found"}), 404
        try:
            with open(path, "r") as f:
                content = f.read()
        except Exception as _e:
            return jsonify({"error": str(_e)}), 500
        info = _account_file_info(name)
        return jsonify({"ok": True, "file": name, "content": content, **info})
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        content = data.get("content")
        if content is None:
            return jsonify({"error": "content required"}), 400
        lines = []
        for raw in str(content).splitlines():
            s = raw.strip()
            if not s:
                continue
            if ":" not in s:
                return jsonify({"error": f"bad line (need uid:password): {s[:40]}"}), 400
            lines.append(s)
        try:
            with open(path, "w") as f:
                for ln in lines:
                    f.write(ln + "\n")
            log(f"[UI] account file updated: {name} ({len(lines)} accounts)")
            try:
                MANAGER.reset_slot_usage(0 if name == "account.txt" else int(re.fullmatch(r"account(\d+)\.txt", name).group(1)))
            except Exception:
                pass
        except Exception as _e:
            return jsonify({"error": str(_e)}), 500
        def _run_put():
            try:
                c, failed = refresh_one_account(name)
                log(f"[UI] edit auto FORCE JWT GEN done: {name} => {c} tokens, {failed} failed")
            except Exception as _e:
                log(f"[UI] edit auto FORCE JWT GEN failed for {name}: {_e}")
        threading.Thread(target=_run_put, daemon=True).start()
        return jsonify({"ok": True, "file": name, "accounts": len(lines)})
    # DELETE
    if not os.path.exists(path):
        return jsonify({"error": f"{name} not found"}), 404
    try:
        os.remove(path)
        # also remove its token cache so stale tokens are not reused
        if name == "account.txt":
            tok = TOKEN_FILE
        else:
            m = re.fullmatch(r"account(\d+)\.txt", name)
            tok = os.path.join(TOKEN_DIR, f"token{m.group(1)}.json") if m else None
        if tok and os.path.exists(tok):
            os.remove(tok)
        log(f"[UI] account file deleted: {name} (+ token cache if any)")
        try:
            MANAGER.forget_slot(0 if name == "account.txt" else int(re.fullmatch(r"account(\d+)\.txt", name).group(1)))
        except Exception:
            pass
    except Exception as _e:
        return jsonify({"error": str(_e)}), 500
    return jsonify({"ok": True, "deleted": name})


# ---------- Telegram Manager Bot (GG style) ----------
try:
    import telegram_bot as _tg_bot

    _tg_bot.start_bot(MANAGER, _discovered_slots, log, callbacks={"test_slot": _slot_test})
except Exception as _e:
    try:
        log(f"[TG] telegram bot not started: {_e}")
    except Exception:
        pass


if __name__ == "__main__":
    fetch_ob_version()

    port = int(os.environ.get("PORT", 4753))
    _cached = sum(
        len(load_tokens_from(os.path.join(TOKEN_DIR, f)))
        for f in os.listdir(TOKEN_DIR) if f.endswith(".json")
    ) if os.path.isdir(TOKEN_DIR) else 0

    print(f"LIKE API v9 — port {port} — OB {RELEASE_VERSION} — cached tokens {_cached}")
    app.run(host='0.0.0.0', port=port, debug=False)