#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Thunder Trail Telegram Bot — VPS ready
Login / OTP / cookies / play / leaderboard — sab bot se

VPS setup:
  pip install requests cryptography python-telegram-bot==20.7
  export BOT_TOKEN="123456:ABC..."
  python thunder_bot.py

Commands:
  /start
  /login <10digit>
  /otp <code>
  /play [target]
  /status
  /board
  /cookie  (show if saved)
"""

import os, sys, json, time, random, base64, hashlib, hmac as hmac_mod, logging, threading
from pathlib import Path

try:
    import requests
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    print("pip install requests cryptography")
    sys.exit(1)

try:
    from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, ContextTypes, filters
    )
except ImportError:
    print("pip install 'python-telegram-bot==20.7'")
    sys.exit(1)

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8517712429:AAEeF1pw0DnwxQGYclHnLmbAkKLsW4Az-6c").strip()
BASE = "https://thunder-zone.coke2home.com"
DATA_DIR = Path(os.environ.get("THUNDER_DATA", Path(__file__).resolve().parent / "thunder_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TARGET = 2500
MAX_SCORE = 4000
MAX_PLAYS = 5

TIERS = [
    (15000, 1000, 1950), (30000, 800, 1540), (45000, 700, 1350),
    (60000, 600, 1150), (75000, 500, 963), (90000, 400, 770),
    (105000, 200, 385), (120000, 100, 193), (135000, 50, 96),
    (150000, 20, 39), (165000, 10, 19), (180000, 5, 10),
    (float("inf"), 5, 10),
]
SCORING = {"thunder": 15, "thunder2": 15, "gully": 50, "firefox": 50, "heart": 0, "trap": 0}
DIST_FULL = {"thunder": .2, "thunder2": .2, "heart": 0, "trap": .45, "gully": .075, "firefox": .075}
DIST_DAMAGED = {"thunder": .175, "thunder2": .175, "heart": .2, "trap": .3, "gully": .075, "firefox": .075}
TYPE_ORDER = ["thunder", "thunder2", "heart", "trap", "gully", "firefox"]
DIRS = ["up", "down", "left", "right"]
DMAP = {"thunder": "thunder_box", "thunder2": "thunder_box", "gully": "gully_labs",
        "firefox": "firefox", "heart": "heart", "trap": "trap"}
GOOD = ("correct", "healed", "avoided", "fullhp-noop")
DRIP_KEY_B64 = "a9rc/DSXunC5PdkYlDB6KkX/evwfSTDUD8PQdaepxe0="
UA = ("Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("thunder-bot")

# tg_id -> {phone, waiting_otp, session}
USER = {}


def user_dir(tg_id):
    d = DATA_DIR / str(tg_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def cookie_path(tg_id, phone):
    return user_dir(tg_id) / ("cookies_%s.json" % phone[-10:])


def meta_path(tg_id):
    return user_dir(tg_id) / "meta.json"


def save_meta(tg_id, **kw):
    p = meta_path(tg_id)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except Exception:
            pass
    data.update(kw)
    p.write_text(json.dumps(data, indent=2))


def load_meta(tg_id):
    p = meta_path(tg_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


# ================= HTTP SESSION PER USER =================
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Origin": BASE,
        "Referer": BASE + "/",
        "sec-ch-ua": '"Chromium";v="126", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    return s


def save_cookies(s, path):
    data = [{"name": c.name, "value": c.value,
             "domain": c.domain or "thunder-zone.coke2home.com",
             "path": c.path or "/"} for c in s.cookies]
    Path(path).write_text(json.dumps(data, indent=2))


def load_cookies(s, path):
    try:
        data = json.loads(Path(path).read_text())
        s.cookies.clear()
        for c in data:
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain") or "thunder-zone.coke2home.com",
                          path=c.get("path") or "/")
        return True
    except Exception:
        return False


def api(s, method, path, body=None, retry=True):
    r = s.request(method, BASE + path, json=body, timeout=30)
    if r.status_code == 401 and retry and "/auth/" not in path:
        try:
            s.post(BASE + "/api/auth/refresh", timeout=15)
            r = s.request(method, BASE + path, json=body, timeout=30)
        except Exception:
            pass
    return r


def session_phone(s):
    try:
        r = api(s, "GET", "/api/auth/me", retry=False)
        d = r.json()
        u = d.get("user") or d.get("data") or d
        if d.get("isAuthenticated") or d.get("success") or u.get("phone_number"):
            return (u.get("phone_number") or u.get("phone") or "")[-10:]
    except Exception:
        pass
    return ""


# ================= ENGINE (same as termux) =================
def decrypt_drip(sid, enc):
    if not enc:
        return []
    try:
        key = hmac_mod.new(base64.b64decode(DRIP_KEY_B64), sid.encode(), hashlib.sha256).digest()
        raw = base64.b64decode(enc)
        pt = AESGCM(key).decrypt(raw[:12], raw[12:], None)
        return json.loads(pt.decode())
    except Exception as e:
        log.warning("drip decrypt: %s", e)
        return []


def splice_floats(floats, from_box, new):
    if not new:
        return
    start = 2 * int(from_box or 0)
    while len(floats) < start + len(new):
        floats.append(0.0)
    for i, v in enumerate(new):
        floats[start + i] = v


def chain_hash(sid, log_):
    joined = ",".join("%s:%s" % (e["boxId"], e["dir"]) for e in log_)
    return hashlib.sha256(("%s|%s" % (sid, joined)).encode()).hexdigest()


def delta_entries(log_, committed):
    return [{"boxId": e["boxId"], "dir": e["dir"], "atMs": e["atMs"],
             "spawnAtMs": e["spawnAtMs"]} for e in log_[committed:]]


def to_int32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


def js_imul(a, b):
    return to_int32(a * b)


def js_xor(a, b):
    return to_int32((a & 0xFFFFFFFF) ^ (b & 0xFFFFFFFF))


def js_ushr(a, n):
    return (a & 0xFFFFFFFF) >> n


def tier_idx(ms):
    for i, (until, _, _) in enumerate(TIERS):
        if ms < until:
            return i
    return len(TIERS) - 1


def good_diff(diff):
    if diff == 0:
        return True
    if diff % 5 or diff < 15:
        return False
    return diff not in (5, 10, 20, 25, 35, 40, 55, 70, 85)


def want_swipe(type_, score, target, hp=3):
    if type_ in ("trap", "heart"):
        return True
    ns = score + SCORING[type_]
    if ns > target:
        return False
    if ns == target or good_diff(target - ns):
        return True
    if hp <= 1:
        return True
    return not good_diff(target - score)


def resolve(type_, arrow, dir_, hp):
    if type_ == "trap":
        if dir_ is None:
            return {"kind": "miss", "points": 0, "hpDelta": -1}
        if dir_ == arrow:
            return {"kind": "wrong", "points": 0, "hpDelta": -1}
        return {"kind": "avoided", "points": 0, "hpDelta": 0}
    if dir_ is None:
        return {"kind": "miss", "points": 0, "hpDelta": -1}
    if dir_ != arrow:
        return {"kind": "wrong", "points": 0, "hpDelta": -1}
    if type_ == "heart":
        if hp >= 3:
            return {"kind": "fullhp-noop", "points": 0, "hpDelta": 0}
        return {"kind": "healed", "points": 0, "hpDelta": 1}
    return {"kind": "correct", "points": SCORING[type_], "hpDelta": 0}


class Engine:
    def __init__(self, seed, floats=None):
        self.drip = floats is not None
        self.floats = floats or []
        self.cursor = 0
        self.rng_state = (int(seed) & 0xFFFFFFFF) or 0x9E3779B9
        self.boxes = []
        self.next_box_id = 1
        self.next_spawn_at = 0
        self.elapsed = 0.0
        self.hp = 3
        self.score = 0
        self.combo = 0
        self.max_combo = 0
        self.hit = {}
        self.last_types = []
        self.gate = {"boxes": 0, "traps": 0, "others": 0, "hpAtLastSpawn": 3}
        self.log = []
        self.phase = "playing"
        self.dying = False
        self.spawned = 0

    def _mulberry32(self):
        t = to_int32(self.rng_state + 0x6D2B79F5)
        self.rng_state = t & 0xFFFFFFFF
        r = js_imul(js_xor(t, js_ushr(t, 15)), to_int32(1 | (t & 0xFFFFFFFF)))
        b = js_imul(js_xor(r, js_ushr(r, 7)), to_int32(61 | (r & 0xFFFFFFFF)))
        r2 = js_xor(r, to_int32(r + b))
        return (js_xor(r2, js_ushr(r2, 14)) & 0xFFFFFFFF) / 4294967296.0

    def x(self):
        if self.drip:
            if self.cursor >= len(self.floats):
                return None
            v = self.floats[self.cursor]
            self.cursor += 1
            return v
        return self._mulberry32()

    def remaining_boxes(self):
        if not self.drip:
            return 9999
        return (len(self.floats) - self.cursor) // 2

    def _pick_type(self, r):
        dist = DIST_FULL if self.hp >= 3 else DIST_DAMAGED
        lt = self.last_types
        o = lt[-1] if len(lt) >= 2 and lt[-1] == lt[-2] else None
        first = len(lt) == 0
        gate = self.gate["boxes"] >= 7 and self.gate["traps"] >= 2 and self.gate["others"] >= 3

        def build(skip):
            elig, wts = [], []
            for t in TYPE_ORDER:
                w = dist[t]
                if w <= 0:
                    continue
                if t == "heart" and not gate:
                    continue
                if skip and t == o:
                    continue
                if t == "trap" and first:
                    continue
                elig.append(t)
                wts.append(w)
            return elig, wts

        elig, wts = build(True)
        if not elig:
            elig, wts = build(False)
        acc = r * sum(wts)
        for i, w in enumerate(wts):
            acc -= w
            if acc < 0:
                return elig[i]
        return elig[-1]

    def _spawn(self, at_ms):
        g = self.gate
        if self.hp < g["hpAtLastSpawn"]:
            g["boxes"] = g["traps"] = g["others"] = 0
        g["hpAtLastSpawn"] = self.hp
        r1, r2 = self.x(), self.x()
        if r1 is None or r2 is None:
            return
        type_ = self._pick_type(r1)
        if type_ == "heart":
            g["boxes"] = g["traps"] = g["others"] = 0
        else:
            g["boxes"] += 1
            g["traps" if type_ == "trap" else "others"] += 1
        arrow = DIRS[int(r2 * 4) % 4]
        self.boxes.append({
            "id": self.next_box_id, "type": type_, "arrow": arrow,
            "spawnMs": at_ms, "travelMs": TIERS[tier_idx(at_ms)][2], "progress": 0.0,
        })
        self.next_box_id += 1
        self.spawned += 1
        self.last_types.append(type_)
        if len(self.last_types) > 2:
            self.last_types.pop(0)

    def _apply(self, res, type_):
        self.score += res["points"]
        self.hp = max(0, min(3, self.hp + res["hpDelta"]))
        if res["kind"] in GOOD:
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
            self.hit[type_] = self.hit.get(type_, 0) + 1
        else:
            self.combo = 0

    def step(self, dt):
        if self.phase != "playing":
            return
        self.elapsed += dt
        while self.elapsed >= self.next_spawn_at:
            st = self.next_spawn_at
            self._spawn(st)
            self.next_spawn_at += TIERS[tier_idx(st)][1]
        alive = []
        for b in self.boxes:
            b["progress"] = (self.elapsed - b["spawnMs"]) / b["travelMs"]
            if b["progress"] >= 1.0:
                res = resolve(b["type"], b["arrow"], None, self.hp)
                self._apply(res, b["type"])
                if self.hp <= 0:
                    self.elapsed = b["spawnMs"] + b["travelMs"]
                    self.phase = "gameover"
                    self.boxes = alive
                    return
                continue
            alive.append(b)
        self.boxes = alive

    def maybe_swipe(self, target=None):
        if self.phase != "playing" or self.dying or not self.boxes:
            return
        best = max(self.boxes, key=lambda b: b["progress"])
        if target is not None and not want_swipe(best["type"], self.score, target, self.hp):
            return
        kappa = max(0.52, min(0.97, random.gauss(0.78, 0.08)))
        at_ms = int(max(best["spawnMs"] + 1, best["spawnMs"] + best["travelMs"] * kappa))
        if best["type"] == "trap":
            dir_ = random.choice([d for d in DIRS if d != best["arrow"]])
        else:
            dir_ = best["arrow"]
        res = resolve(best["type"], best["arrow"], dir_, self.hp)
        self.log.append({
            "boxId": best["id"], "dir": dir_, "atMs": at_ms,
            "spawnAtMs": best["spawnMs"], "type": best["type"],
            "correct": res["kind"] in GOOD,
        })
        self._apply(res, best["type"])
        self.boxes.remove(best)

    def box_counts(self):
        out = {}
        for k, v in self.hit.items():
            key = DMAP.get(k, k)
            out[key] = out.get(key, 0) + v
        return out


# ================= GAME API =================
def get_me(s):
    r = api(s, "GET", "/api/thunder-trail/me")
    if r.status_code != 200:
        return {}
    d = r.json()
    return d.get("data") or d.get("user") or d


def me_stats(me):
    best = int(me.get("best_score") or me.get("best") or me.get("score") or 0)
    rank = me.get("rank") or me.get("current_rank")
    rem = int(me.get("plays_remaining") or me.get("plays_left") or
              me.get("remaining_plays") or me.get("plays_remaining_today") or 0)
    return best, rank, rem


def fetch_board(s):
    try:
        r = s.get(BASE + "/api/thunder-trail/leaderboard?limit=10", timeout=20)
        d = r.json()
        lines = ["🏆 Leaderboard (%s)" % (d.get("last_updated") or "")]
        for e in d.get("data") or []:
            lines.append("#%s %s = %s" % (e.get("rank"), e.get("username"), e.get("score")))
        return "\n".join(lines)
    except Exception as e:
        return "Board error: %s" % e


def send_otp(s, phone):
    r = api(s, "POST", "/api/auth/send-otp", {"phone_number": phone}, retry=False)
    try:
        j = r.json()
    except Exception:
        j = {}
    return r.status_code, j


def verify_otp(s, phone, otp, name="Player"):
    r = api(s, "POST", "/api/auth/verify-otp",
            {"phone_number": phone, "otp": otp}, retry=False)
    if r.status_code != 200:
        return False, "verify HTTP %s %s" % (r.status_code, r.text[:150])
    v = r.json()
    data = v.get("data") or v
    if not v.get("success") and not data.get("one_time_token") and not v.get("one_time_token"):
        return False, str(v.get("message") or v.get("error") or v)[:200]

    ott = v.get("one_time_token") or data.get("one_time_token")
    needs = bool(v.get("requiresSignup") or data.get("requiresSignup"))

    if ott:
        r2 = api(s, "POST", "/api/auth/validate-token", {"one_time_token": ott}, retry=False)
        try:
            needs = needs or bool(r2.json().get("requires_signup"))
        except Exception:
            pass

    if needs:
        api(s, "POST", "/api/auth/signup",
            {"phone_number": phone, "name": name}, retry=False)

    if ott:
        api(s, "POST", "/api/auth/exchange-token",
            {"one_time_token": ott}, retry=False)

    ph = session_phone(s)
    return True, ph or phone


def play_once(s, target, progress_cb=None):
    r = api(s, "POST", "/api/thunder-trail/sessions", {})
    if r.status_code not in (200, 201):
        return None, "session fail %s %s" % (r.status_code, r.text[:200])
    try:
        j = r.json()
    except Exception:
        return None, "bad session json"
    sess = j.get("data") or j
    sid = sess.get("session_id") or sess.get("id")
    token = sess.get("session_token") or sess.get("token")
    seed = int(sess.get("seed") or 0)
    if not sid or not token:
        return None, "no sid/token"

    floats = None
    if sess.get("drip_mode"):
        floats = decrypt_drip(sid, sess.get("drip_enc"))
        if not floats:
            return None, "empty drip"
    eng = Engine(seed, floats)
    committed = 0
    perf = 3000.0 + random.uniform(0, 500)

    def pull_drip(rr, fb):
        try:
            rj = rr.json()
            enc = rj.get("drip_enc") or (rj.get("data") or {}).get("drip_enc")
            from_b = rj.get("drip_from_box") or (rj.get("data") or {}).get("drip_from_box")
            if enc and eng.drip:
                splice_floats(eng.floats, from_b if from_b is not None else fb,
                              decrypt_drip(sid, enc))
        except Exception:
            pass

    def hb(final=False, final_ms=None):
        nonlocal committed
        at = final_ms if final_ms is not None else (perf + eng.elapsed if not final else eng.elapsed)
        body = {
            "session_token": token, "boxes_seen": eng.spawned,
            "score_so_far": eng.score, "paused": False, "at_ms": at,
            "boxes_committed": len(eng.log),
            "chain_hash": chain_hash(sid, eng.log),
            "input_log_delta": delta_entries(eng.log, committed),
        }
        rr = api(s, "POST", "/api/thunder-trail/sessions/%s/heartbeat" % sid, body)
        if rr.status_code == 200:
            committed = len(eng.log)
            pull_drip(rr, 0)
        return rr

    def refill():
        if not eng.drip:
            return
        fb = len(eng.floats) // 2
        rr = api(s, "POST", "/api/thunder-trail/sessions/%s/drip-refill" % sid,
                 {"session_token": token, "from_box": fb})
        if rr.status_code == 200:
            pull_drip(rr, fb)

    time.sleep(3.0)
    hb()
    last_hb = time.time()
    t0 = time.time()

    while eng.phase == "playing":
        wall = (time.time() - t0) * 1000.0
        while eng.elapsed < wall and eng.phase == "playing":
            eng.step(random.uniform(13.5, 20.0))
            if eng.elapsed > wall:
                eng.elapsed = wall
            if eng.score >= target and not eng.dying:
                eng.dying = True
            if not eng.dying:
                eng.maybe_swipe(target)
            if eng.drip and not eng.dying and eng.remaining_boxes() <= 22:
                refill()
        if eng.phase == "playing" and time.time() - last_hb >= random.uniform(3.8, 6.5):
            hr = hb()
            last_hb = time.time()
            if hr.status_code in (401, 403):
                break
            if progress_cb and eng.score and eng.score % 500 < 30:
                try:
                    progress_cb(eng.score)
                except Exception:
                    pass
        if time.time() - t0 > 480:
            eng.phase = "gameover"

    time.sleep(2.0)
    dur = int(eng.next_spawn_at + 1000)
    hb(final=True, final_ms=dur)
    counts = eng.box_counts()

    body = {
        "session_token": token, "final_score": eng.score,
        "duration_ms": dur, "max_combo": eng.max_combo,
        "box_counts": counts, "seed": seed, "input_log": eng.log,
    }
    r = api(s, "POST", "/api/thunder-trail/sessions/%s/score" % sid, body)
    msg = "score HTTP %s %s" % (r.status_code, r.text[:180])
    try:
        sd = (r.json().get("data") or r.json())
        if sd.get("is_bot"):
            return None, "is_bot=true — stop for today"
        if r.status_code in (200, 201, 202) and sd.get("accepted") is False:
            return None, "score rejected: %s" % msg
    except Exception:
        if r.status_code not in (200, 201, 202):
            return None, msg

    jbody = {
        "session_token": token, "seed": seed, "input_log": eng.log,
        "final_score": eng.score, "duration_ms": dur,
        "max_combo": eng.max_combo, "hit_counts": counts,
        "max_tier_reached": tier_idx(eng.elapsed) + 1,
    }
    api(s, "POST", "/api/thunder-trail/sessions/%s/journey" % sid, jbody)
    return eng.score, "OK score=%d boxes=%d combo=%d" % (eng.score, eng.spawned, eng.max_combo)


# ================= TELEGRAM HANDLERS =================
HELP = (
    "⚡ *Thunder Trail Bot*\n\n"
    "`/login 98XXXXXXXX` — number se OTP bhejo\n"
    "`/otp 123456` — OTP verify + cookies save\n"
    "`/play` — target 2500 se khelo\n"
    "`/play 2600` — custom target\n"
    "`/status` — best / rank / remaining\n"
    "`/board` — leaderboard\n"
    "`/logout` — cookies clear\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Use: /login 98XXXXXXXX")
        return
    phone = "".join(c for c in context.args[0] if c.isdigit())[-10:]
    if len(phone) < 10:
        await update.message.reply_text("10 digit number chahiye")
        return

    s = make_session()
    code, j = send_otp(s, phone)
    if code != 200 or j.get("success") is False:
        await update.message.reply_text(
            "OTP send fail (%s): %s\nBrowser se site ek baar open karke try." % (
                code, j.get("error") or j.get("message") or j))
        return

    USER[tg] = {"phone": phone, "waiting_otp": True, "session": s}
    save_meta(tg, phone=phone)
    await update.message.reply_text(
        "✅ OTP bhej diya *%s* pe.\nAb: `/otp 123456`" % phone, parse_mode="Markdown")


async def cmd_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    st = USER.get(tg) or {}
    if not st.get("waiting_otp") or not st.get("phone"):
        meta = load_meta(tg)
        if not meta.get("phone"):
            await update.message.reply_text("Pehle /login <number>")
            return
        st = {"phone": meta["phone"], "waiting_otp": True, "session": make_session()}
        USER[tg] = st

    if not context.args:
        await update.message.reply_text("Use: /otp 123456")
        return
    otp = context.args[0].strip()
    s = st.get("session") or make_session()
    ok, info = verify_otp(s, st["phone"], otp)
    if not ok:
        await update.message.reply_text("OTP fail: %s" % info)
        return

    path = cookie_path(tg, st["phone"])
    save_cookies(s, path)
    st["waiting_otp"] = False
    st["session"] = s
    USER[tg] = st
    save_meta(tg, phone=st["phone"], logged=True)
    await update.message.reply_text(
        "✅ Login OK (%s)\nCookies saved.\nAb `/play` ya `/status`" % info)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    meta = load_meta(tg)
    phone = meta.get("phone")
    if not phone:
        await update.message.reply_text("Pehle /login karo")
        return
    s = make_session()
    if not load_cookies(s, cookie_path(tg, phone)):
        await update.message.reply_text("Cookies nahi — /login dubara")
        return
    me = get_me(s)
    if not me:
        await update.message.reply_text("Session expire — /login dubara")
        return
    best, rank, rem = me_stats(me)
    await update.message.reply_text(
        "📱 %s\n🏆 best=%s  rank=%s\n🎮 remaining=%s  locked=%s" % (
            phone, best, rank, rem, me.get("is_locked")))


async def cmd_board(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = make_session()
    text = fetch_board(s)
    await update.message.reply_text(text)


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    meta = load_meta(tg)
    phone = meta.get("phone")
    if phone:
        p = cookie_path(tg, phone)
        if p.exists():
            p.unlink()
    USER.pop(tg, None)
    save_meta(tg, logged=False)
    await update.message.reply_text("Logged out / cookies cleared")


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    meta = load_meta(tg)
    phone = meta.get("phone")
    if not phone:
        await update.message.reply_text("Pehle /login <number> + /otp")
        return

    target = DEFAULT_TARGET
    plays = 1
    if context.args:
        try:
            target = int(context.args[0])
        except Exception:
            pass
    if len(context.args) > 1:
        try:
            plays = max(1, min(5, int(context.args[1])))
        except Exception:
            pass

    s = make_session()
    if not load_cookies(s, cookie_path(tg, phone)):
        await update.message.reply_text("Cookies missing — /login")
        return

    me = get_me(s)
    if not me:
        await update.message.reply_text("Session dead — /login")
        return
    best, rank, rem = me_stats(me)
    if me.get("is_locked"):
        await update.message.reply_text("Account locked")
        return
    if rem <= 0:
        await update.message.reply_text("Aaj plays khatam (0 remaining)")
        return

    await update.message.reply_text(
        "🎮 Start… target=%d plays=%d (remaining=%d best=%s)" % (
            target, min(plays, rem), rem, best))

    def run():
        results = []
        for i in range(min(plays, rem)):
            score, msg = play_once(s, target)
            results.append((score, msg))
            if score is None:
                break
            time.sleep(2)
        return results

    # run in thread so bot doesn't block
    result_holder = {}

    def worker():
        result_holder["r"] = run()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    while t.is_alive():
        await asyncio_sleep(2)

    lines = []
    for score, msg in result_holder.get("r") or []:
        lines.append("• %s — %s" % (score, msg))
    me = get_me(s)
    best, rank, rem = me_stats(me)
    lines.append("\nfinal best=%s rank=%s remaining=%s" % (best, rank, rem))
    await update.message.reply_text("\n".join(lines) or "No result")
    board = fetch_board(s)
    await update.message.reply_text(board)


async def asyncio_sleep(sec):
    import asyncio
    await asyncio.sleep(sec)


def main():
    token = BOT_TOKEN or os.environ.get("BOT_TOKEN", "")
    if not token:
        print("Set BOT_TOKEN env:\n  export BOT_TOKEN='123:ABC'\n  python thunder_bot.py")
        sys.exit(1)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("otp", cmd_otp))
    app.add_handler(CommandHandler("play", cmd_play))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("board", cmd_board))
    app.add_handler(CommandHandler("logout", cmd_logout))
    log.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
