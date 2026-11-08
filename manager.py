# -*- coding: utf-8 -*-
"""
Main API Manager — GG-style gateway logic for the Railway Like API.

One public /like endpoint (the "main API") routes every request to ONE
current backend slot (an account file: account.txt = slot 0,
accountN.txt = slot N). Each slot has its own limit and an attach
on/off switch:

  - request goes to the current attached slot
  - slot limit reached  -> slot is skipped, main API auto-transfers
    to the next attached slot
  - Next API (Telegram / Dashboard) -> manual transfer to the next
    attached slot, even if the current one still has remain
  - 0 likes delivered   -> NOTHING is counted (no slot limit cut,
    no key limit cut) — the pre-deduction is refunded

Customer keys work exactly like the GG gateway file: Telegram-created
keys with type r1 (daily reset at 04:00 BD) / r2 (no reset), a per-key
limit and an optional expiry.

State storage: a local manager_state.json file beside this file
(no Firebase, no external setup). The Telegram bot and the dashboard
only talk to this module, never to the store directly.
"""

import os
import json
import threading
from datetime import datetime, timedelta, timezone

BD_TZ = timezone(timedelta(hours=6))
DEFAULT_SLOT_LIMIT = 30
EXP_FMT = "%Y-%m-%d %H:%M:%S"


def bd_now():
    return datetime.now(BD_TZ)


def logical_day(now=None):
    """GG rule: the day flips at 04:00 BD, so before 04:00 it is still
    'yesterday'."""
    now = now or bd_now()
    return (now - timedelta(hours=4)).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
# Stores
# ----------------------------------------------------------------------
class LocalStore:
    backend = "local"

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            try:
                if os.path.exists(self.path):
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
            return {}

    def save(self, state):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, self.path)


def make_store(base_dir):
    """Local JSON state file only — no Firebase, nothing to configure."""
    return LocalStore(os.path.join(base_dir, "manager_state.json"))


# -
# Manager
# ----------------------------------------------------------------------
def _default_state():
    return {
        "keys": {},
        "slots": {},
        "current": None,
        "rotate_last": None,
        "slot_order": [],
        "alerts_sent": {},
        "global_used": 0,
        "last_reset_date": "",
        "settings": {"auto_attach_new": True, "default_limit": DEFAULT_SLOT_LIMIT, "alerts_enabled": True, "pool_size": 3},
    }


def _is_int(v):
    # bool is a subclass of int — True/False are NOT valid counts.
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_expiry(v):
    if v is None:
        return True
    if not isinstance(v, str):
        return False
    try:
        datetime.strptime(v, EXP_FMT)
    except Exception:
        return False
    return True


def validate_state(data):
    """Deep-validate a manager state dict before it is applied.

    Returns (True, None) or (False, reason). Never raises — a bad
    state must be rejected with a reason, never crash the caller
    (V4 H1: a corrupt backup used to 500 mid-restore / break /like).
    Used by Manager.import_state and by the full-backup restore,
    which validates the whole zip BEFORE writing anything."""
    try:
        if not isinstance(data, dict):
            return False, "state must be a JSON object"
        keys = data.get("keys", {})
        slots = data.get("slots", {})
        if not isinstance(keys, dict):
            return False, "state 'keys' must be an object"
        if not isinstance(slots, dict):
            return False, "state 'slots' must be an object"
        for name, info in keys.items():
            if not isinstance(info, dict):
                return False, f"key '{name}' must be an object"
            if info.get("type") not in ("r1", "r2"):
                return False, f"key '{name}' type must be r1 or r2"
            if not _is_int(info.get("limit")) or info["limit"] < 1:
                return False, f"key '{name}' limit must be an integer >= 1"
            if not _is_int(info.get("used")) or info["used"] < 0:
                return False, f"key '{name}' used must be an integer >= 0"
            if not _valid_expiry(info.get("expiry")):
                return False, f"key '{name}' expiry must be null or YYYY-MM-DD HH:MM:SS"
        for n, slot in slots.items():
            if not isinstance(n, str) or not n.isdigit():
                return False, f"slot id '{n}' must be a non-negative integer string"
            if not isinstance(slot, dict):
                return False, f"slot '{n}' must be an object"
            if not isinstance(slot.get("attached"), bool):
                return False, f"slot '{n}' attached must be true/false"
            if not _is_int(slot.get("limit")) or slot["limit"] < 1:
                return False, f"slot '{n}' limit must be an integer >= 1"
            if not _is_int(slot.get("used")) or slot["used"] < 0:
                return False, f"slot '{n}' used must be an integer >= 0"
        settings = data.get("settings", {})
        if not isinstance(settings, dict):
            return False, "state 'settings' must be an object"
        for k in settings:
            if k not in ("auto_attach_new", "alerts_enabled", "default_limit", "pool_size"):
                return False, f"unknown settings key '{k}'"
        for k in ("auto_attach_new", "alerts_enabled"):
            if k in settings and not isinstance(settings[k], bool):
                return False, f"settings '{k}' must be true/false"
        for k in ("default_limit", "pool_size"):
            if k in settings and (not _is_int(settings[k]) or settings[k] < 1):
                return False, f"settings '{k}' must be an integer >= 1"
        order = data.get("slot_order", [])
        if not isinstance(order, list) or any(not _is_int(x) or x < 0 for x in order):
            return False, "state 'slot_order' must be a list of non-negative integers"
        if "global_used" in data and (not _is_int(data["global_used"]) or data["global_used"] < 0):
            return False, "state 'global_used' must be an integer >= 0"
        if "last_reset_date" in data and not isinstance(data["last_reset_date"], str):
            return False, "state 'last_reset_date' must be a string"
        if "rotate_last" in data and data["rotate_last"] is not None and (not _is_int(data["rotate_last"]) or data["rotate_last"] < 0):
            return False, "state 'rotate_last' must be null or a non-negative integer"
        if "alerts_sent" in data and not isinstance(data["alerts_sent"], dict):
            return False, "state 'alerts_sent' must be an object"
        return True, None
    except Exception as _e:
        return False, f"invalid state: {_e}"


class Manager:
    """All gateway state + rules. `discovered` everywhere is the sorted
    list of slot numbers whose account file exists on disk right now
    (0 = account.txt, N = accountN.txt)."""

    def __init__(self, store, builtin_keys=None):
        self.store = store
        self.builtin_keys = dict(builtin_keys or {})  # {key: limit}
        self._lock = threading.RLock()
        state = _default_state()
        loaded = store.load()
        if isinstance(loaded, dict):
            for k, v in loaded.items():
                state[k] = v
        state.setdefault("keys", {})
        state.setdefault("slots", {})
        state.setdefault("slot_order", [])
        state.setdefault("alerts_sent", {})
        state.setdefault("settings", {"auto_attach_new": True, "default_limit": DEFAULT_SLOT_LIMIT, "alerts_enabled": True, "pool_size": 3})
        state["settings"].setdefault("auto_attach_new", True)
        state["settings"].setdefault("default_limit", DEFAULT_SLOT_LIMIT)
        state["settings"].setdefault("alerts_enabled", True)
        state["settings"].setdefault("pool_size", 3)
        state.setdefault("rotate_last", None)
        self._s = state

    # -- internal ------------------------------------------------------
    def _save(self):
        try:
            self.store.save(self._s)
        except Exception:
            pass

    def _slot(self, n):
        key = str(int(n))
        slot = self._s["slots"].get(key)
        if slot is None:
            slot = {
                "attached": bool(self._s["settings"].get("auto_attach_new", True)),
                "limit": int(self._s["settings"].get("default_limit", DEFAULT_SLOT_LIMIT)),
                "used": 0,
            }
            self._s["slots"][key] = slot
        slot.setdefault("attached", True)
        slot.setdefault("limit", int(self._s["settings"].get("default_limit", DEFAULT_SLOT_LIMIT)))
        slot.setdefault("used", 0)
        return slot

    def _key(self, name):
        info = self._s["keys"].get(name)
        if info is None and name in self.builtin_keys:
            info = {
                "type": "r2",
                "limit": int(self.builtin_keys[name]),
                "used": 0,
                "expiry": None,
                "created": bd_now().strftime(EXP_FMT),
                "builtin": True,
            }
            self._s["keys"][name] = info
        return info

    @staticmethod
    def _expired(info, now=None):
        exp = info.get("expiry")
        if not exp:
            return False
        try:
            exp_dt = datetime.strptime(exp, EXP_FMT)
        except Exception:
            return False
        return (now or bd_now()).replace(tzinfo=None) > exp_dt

    def effective_order(self, discovered):
        """Routing/display order: admin-arranged slot_order first
        (Telegram/UI move buttons), then any other slots by number."""
        disc = sorted(int(x) for x in discovered)
        order = [int(x) for x in self._s.get("slot_order", []) if int(x) in disc]
        order += [n for n in disc if n not in order]
        return order

    def move_slot(self, n, direction, discovered):
        """direction -1 = earlier (higher priority), +1 = later."""
        with self._lock:
            order = self.effective_order(discovered)
            n = int(n)
            if n not in order:
                return order
            i = order.index(n)
            j = i + int(direction)
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
                self._s["slot_order"] = order
                self._save()
            return order

    def _available(self, discovered):
        out = []
        for n in self.effective_order(discovered):
            slot = self._slot(n)
            if slot["attached"] and int(slot["used"]) < int(slot["limit"]):
                out.append(n)
        return out

    # -- rotation pool --------------------------------------------------
    def pool_slots(self, discovered):
        """The active rotation pool: the first `pool_size` available
        (attached + has remain) slots in effective order. Requests
        rotate inside this pool; when a member's limit ends it drops
        out and the next slot behind slides into the pool."""
        with self._lock:
            size = max(1, int(self._s["settings"].get("pool_size", 3)))
            return self._available(discovered)[:size]

    def _pick_slot(self, discovered, exclude=()):
        excl = {int(x) for x in exclude}
        avail = [n for n in self._available(discovered) if n not in excl]
        if not avail:
            return None
        size = max(1, int(self._s["settings"].get("pool_size", 3)))
        pool = avail[:size]
        last = self._s.get("rotate_last")
        if last is not None and int(last) in pool:
            nxt = pool[(pool.index(int(last)) + 1) % len(pool)]
        else:
            nxt = pool[0]
        self._s["rotate_last"] = nxt
        return nxt

    def _resolve_current(self, discovered):
        avail = self._available(discovered)
        if not avail:
            return None
        cur = self._s.get("current")
        if cur is not None and int(cur) in avail:
            return int(cur)
        nxt = avail[0]
        self._s["current"] = nxt
        return nxt

    # -- daily reset (GG: 04:00 BD) ------------------------------------
    def check_daily_reset(self):
        with self._lock:
            today = logical_day()
            if self._s.get("last_reset_date") == today:
                return False
            self._s["last_reset_date"] = today
            self._s["global_used"] = 0
            for info in self._s["keys"].values():
                if info.get("type") == "r1":
                    info["used"] = 0
            for slot in self._s["slots"].values():
                slot["used"] = 0
            self._save()
            return True

    # -- slots ----------------------------------------------------------
    def register_slot(self, n):
        """Called on upload/create so a new file shows up in the
        manager with the default limit + auto-attach setting."""
        with self._lock:
            self._slot(n)
            self._save()
            return dict(self._slot(n))

    def slot_view(self, discovered):
        with self._lock:
            self.check_daily_reset()
            pool = self.pool_slots(discovered)
            cur = pool[0] if pool else None
            self._s["current"] = cur
            self._save()
            out = []
            for n in self.effective_order(discovered):
                slot = self._slot(n)
                out.append({
                    "n": n,
                    "attached": bool(slot["attached"]),
                    "limit": int(slot["limit"]),
                    "used": int(slot["used"]),
                    "remain": max(0, int(slot["limit"]) - int(slot["used"])),
                    "current": (cur is not None and n == cur),
                    "in_pool": n in pool,
                    "exhausted": int(slot["used"]) >= int(slot["limit"]),
                })
            return out

    def current_slot(self, discovered):
        """Head of the rotation pool (first pool member), or None."""
        with self._lock:
            self.check_daily_reset()
            pool = self.pool_slots(discovered)
            cur = pool[0] if pool else None
            self._s["current"] = cur
            self._save()
            return cur

    def next_api(self, discovered):
        """Manual window shift: send the pool's head API to the back
        of the order, so the next API comes to the front of the pool.
        Returns (new_head, changed)."""
        with self._lock:
            self.check_daily_reset()
            avail = self._available(discovered)
            if not avail:
                self._save()
                return None, False
            if len(avail) == 1:
                self._save()
                return avail[0], False
            head = avail[0]
            order = self.effective_order(discovered)
            order = [n for n in order if n != head] + [head]
            self._s["slot_order"] = order
            self._s["rotate_last"] = None
            self._save()
            new_avail = self._available(discovered)
            return (new_avail[0] if new_avail else None), True

    def set_attached(self, n, attached):
        with self._lock:
            slot = self._slot(n)
            slot["attached"] = bool(attached)
            self._save()
            return dict(slot)

    def set_limit(self, n, limit):
        with self._lock:
            slot = self._slot(n)
            slot["limit"] = max(1, int(limit))
            self._save()
            return dict(slot)

    def reset_slot_usage(self, n):
        with self._lock:
            slot = self._slot(n)
            slot["used"] = 0
            self._save()
            return dict(slot)

    def reset_all_usage(self):
        with self._lock:
            for slot in self._s["slots"].values():
                slot["used"] = 0
            self._s["global_used"] = 0
            self._save()

    def forget_slot(self, n):
        with self._lock:
            self._s["slots"].pop(str(int(n)), None)
            if self._s.get("current") is not None and int(self._s["current"]) == int(n):
                self._s["current"] = None
            self._save()

    # -- settings -------------------------------------------------------
    def settings(self):
        with self._lock:
            return dict(self._s["settings"])

    def update_settings(self, auto_attach_new=None, default_limit=None, alerts_enabled=None, pool_size=None):
        with self._lock:
            if auto_attach_new is not None:
                self._s["settings"]["auto_attach_new"] = bool(auto_attach_new)
            if default_limit is not None:
                self._s["settings"]["default_limit"] = max(1, int(default_limit))
            if alerts_enabled is not None:
                self._s["settings"]["alerts_enabled"] = bool(alerts_enabled)
            if pool_size is not None:
                self._s["settings"]["pool_size"] = max(1, int(pool_size))
            self._save()
            return dict(self._s["settings"])

    # -- keys (GG gateway) ----------------------------------------------
    def create_key(self, name, k_type, limit, duration="no"):
        with self._lock:
            if name in self.builtin_keys:
                return None, "reserved"
            if name in self._s["keys"]:
                return None, "exists"
            exp_str = None
            duration = (duration or "no").strip().lower()
            if duration != "no":
                import re as _re
                m = _re.match(r"^(\d+)([hdwmy])$", duration)
                if not m:
                    return None, "invalid duration format"
                val, unit = int(m.group(1)), m.group(2)
                now = bd_now()
                if unit == "h":
                    exp_date = now + timedelta(hours=val)
                elif unit == "d":
                    exp_date = now + timedelta(days=val)
                elif unit == "w":
                    exp_date = now + timedelta(weeks=val)
                elif unit == "m":
                    exp_date = now + timedelta(days=val * 30)
                else:
                    exp_date = now + timedelta(days=val * 365)
                exp_str = exp_date.strftime(EXP_FMT)
            info = {
                "type": k_type,
                "limit": int(limit),
                "used": 0,
                "expiry": exp_str,
                "created": bd_now().strftime(EXP_FMT),
            }
            self._s["keys"][name] = info
            self._save()
            return dict(info), None

    def revoke_key(self, name):
        with self._lock:
            if name in self._s["keys"]:
                del self._s["keys"][name]
                self._save()
                return True
            return False

    def list_keys(self):
        with self._lock:
            self.check_daily_reset()
            return {k: dict(v) for k, v in self._s["keys"].items()}

    def key_info(self, name):
        with self._lock:
            self.check_daily_reset()
            info = self._key(name)
            return dict(info) if info else None

    # -- request flow ----------------------------------------------------
    def begin(self, key_name, discovered, exclude=()):
        """Validate key + slot, then pre-deduct (GG style).

        Returns (error_code, detail, slot_n):
          error_code None on success; otherwise one of
          "invalid_key" / "expired" / "key_limit" / "no_slot".
          detail carries the key info dict when available.
        On success the caller MUST call rollback() when the like ends
        with 0 delivered (user rule: 0 like = no limit cut)."""
        with self._lock:
            self.check_daily_reset()
            if not key_name:
                return "invalid_key", None, None
            info = self._key(key_name)
            if info is None:
                return "invalid_key", None, None
            if self._expired(info):
                return "expired", dict(info), None
            if int(info["used"]) >= int(info["limit"]):
                return "key_limit", dict(info), None
            slot_n = self._pick_slot(discovered, exclude=exclude)
            if slot_n is None:
                return "no_slot", dict(info), None
            info["used"] = int(info["used"]) + 1
            info["last_used"] = bd_now().strftime(EXP_FMT)
            slot = self._slot(slot_n)
            slot["used"] = int(slot["used"]) + 1
            self._s["global_used"] = int(self._s.get("global_used", 0)) + 1
            self._save()
            return None, dict(info), slot_n

    def rollback(self, key_name, slot_n):
        """Refund a pre-deduction (backend fail / 0 likes)."""
        with self._lock:
            info = self._s["keys"].get(key_name)
            if info is not None:
                info["used"] = max(0, int(info["used"]) - 1)
            if slot_n is not None:
                slot = self._s["slots"].get(str(int(slot_n)))
                if slot is not None:
                    slot["used"] = max(0, int(slot["used"]) - 1)
            self._s["global_used"] = max(0, int(self._s.get("global_used", 0)) - 1)
            self._save()

    def skip_from(self, slot_n, discovered, exclude=()):
        """Move the current pointer past slot_n to the next available
        attached slot that is not in `exclude`. Used when a slot turns
        out to be dead (no tokens) mid-request. Returns the new current
        slot number, or None when no other slot can serve."""
        with self._lock:
            self.check_daily_reset()
            excl = {int(x) for x in exclude}
            avail = [n for n in self._available(discovered) if n not in excl]
            if not avail:
                self._save()
                return None
            after = [n for n in avail if n > int(slot_n)]
            before = [n for n in avail if n < int(slot_n)]
            nxt = (after + before)[0]
            self._s["current"] = nxt
            self._save()
            return nxt

    def remain_info(self, key_name):
        """GG /remain: (info_dict, error) — error None / 'invalid' / 'expired'."""
        with self._lock:
            self.check_daily_reset()
            info = self._key(key_name)
            if info is None:
                return None, "invalid"
            if self._expired(info):
                return dict(info), "expired"
            out = dict(info)
            out["key"] = key_name
            out["remaining"] = max(0, int(info["limit"]) - int(info["used"]))
            out["remains"] = f"({out['remaining']}/{int(info['limit'])})"
            out["expiry"] = info.get("expiry") or "Lifetime/No Date"
            return out, None

    def key_remain(self, key_name):
        with self._lock:
            info = self._key(key_name)
            if info is None:
                return 0, 0
            return max(0, int(info["limit"]) - int(info["used"])), int(info["limit"])

    # -- alerts (once per logical day per event) ---------------------------
    def should_alert(self, kind, ident=""):
        with self._lock:
            if not self._s["settings"].get("alerts_enabled", True):
                return False
            day = logical_day()
            sent = self._s.get("alerts_sent", {})
            todays = sent.get(day, [])
            tag = f"{kind}:{ident}"
            if tag in todays:
                return False
            todays.append(tag)
            self._s["alerts_sent"] = {day: todays}
            self._save()
            return True

    # -- backup / restore ---------------------------------------------------
    def export_state(self):
        import copy as _copy
        with self._lock:
            return _copy.deepcopy(self._s)

    def import_state(self, data):
        with self._lock:
            try:
                ok, reason = validate_state(data)
                if not ok:
                    return False, reason
                new_state = _default_state()
                for k in ("keys", "slots", "slot_order", "global_used", "last_reset_date", "settings"):
                    if k in data:
                        new_state[k] = data[k]
                # Preserve alert history + rotation pointer when the
                # backup does not carry them (V4: import no longer
                # silently resets alerts_sent / rotate_last).
                if isinstance(data.get("alerts_sent"), dict):
                    new_state["alerts_sent"] = data["alerts_sent"]
                else:
                    new_state["alerts_sent"] = self._s.get("alerts_sent", {})
                if data.get("rotate_last") is None:
                    new_state["rotate_last"] = self._s.get("rotate_last")
                else:
                    new_state["rotate_last"] = data["rotate_last"]
                new_state["settings"].setdefault("auto_attach_new", True)
                new_state["settings"].setdefault("default_limit", DEFAULT_SLOT_LIMIT)
                new_state["settings"].setdefault("alerts_enabled", True)
                new_state["settings"].setdefault("pool_size", 3)
                new_state["current"] = None
                self._s = new_state
                self._save()
                return True, None
            except Exception as _e:
                return False, f"invalid state: {_e}"

    # -- daily report ---------------------------------------------------------
    def daily_report(self, discovered):
        with self._lock:
            st = self.global_status(discovered)
            keys = []
            for name, info in self._s["keys"].items():
                keys.append({
                    "name": name,
                    "type": info.get("type"),
                    "used": int(info.get("used", 0)),
                    "limit": int(info.get("limit", 0)),
                    "expiry": info.get("expiry") or "Lifetime",
                    "last_used": info.get("last_used") or "—",
                })
            keys.sort(key=lambda k: -k["used"])
            return {
                "date": logical_day(),
                "global_used": st["global_used"],
                "total_limit": st["total_limit"],
                "remain": st["remain"],
                "current": st["current"],
                "slots": st["slots"],
                "keys": keys,
            }

    # -- status -----------------------------------------------------------
    def global_status(self, discovered):
        with self._lock:
            self.check_daily_reset()
            view = self.slot_view(discovered)
            total = sum(s["limit"] for s in view if s["attached"])
            remain = sum(s["remain"] for s in view if s["attached"])
            pool = self.pool_slots(discovered)
            return {
                "total_limit": total,
                "global_used": int(self._s.get("global_used", 0)),
                "remain": remain,
                "current": pool[0] if pool else None,
                "pool": pool,
                "slots": view,
                "backend": self.store.backend,
            }
