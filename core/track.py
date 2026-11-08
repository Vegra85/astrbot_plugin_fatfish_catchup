# -*- coding: utf-8 -*-
"""补回要记的东西：心跳、收到过哪些消息、补过哪些、正在等模型回的、模型出错没回上的、它上次回每个对话的时间、最近几次掉线。
不依赖 AstrBot；整个存成一个 dict，main.py 负责写进 plugin_data（AstrBot 被直接关掉不会调 terminate，所以勤存）。

消息 id 一律带上平台：「napcat:1234」（两个 QQ 号的消息 id 会撞）。

「模型好了没有」按会话算（v0.1.0 复查）：某个会话一直出错、别的会话正常 —— 不拿别处的「回上了」去重试它
（不然每条都再试一遍、没装活人感时还会往群里发「LLM 响应错误」）。这个会话自己回上了一句才算它好了：
  - 同一个会话里别人那几条 → 补（群里：别的群友找它没回上的）
  - 他本人又来找它 → 他之前没回上的那几条并进这一轮一起给模型（不用另补一次）
    并进去的那几条先「占着」：这一轮回上了算补过；没回上 / 被拦了放回去等下次。占着的不会被别的路再补一遍
"""

import time

SEEN_MAX = 4000
DONE_KEEP = 2 * 86400
REPLIED_KEEP = 2 * 86400
CONV_KEEP = 7 * 86400
LOG_MAX = 20
MAX_TRIES = 2  # 同一条最多补两次（第一次补的时候模型又出错了，等它好了再补一次；再不行就算了）
BOOT_GAP = 90  # 正常关掉（调了 terminate）又起来、中间不到这么多秒：是热重载 / 秒重启，不跑开机补回
FOLD_HOLD = 600  # 并进正在回的那一轮的几条最多占着这么久（那一轮一直没结果也会放回去）


def should_boot(prev_hb, prev_clean, now, gap=BOOT_GAP):
    """开机要不要补：上次没正常关（崩了 / 被直接杀掉）或者中间空得够久 → 补；改配置热重载、秒重启 → 不补"""
    if not prev_hb:
        return False  # 第一次装上：那之前没记「收到过」，分不清哪些是故意没回的
    return (not prev_clean) or (now - float(prev_hb) > gap)


ALIVE_FAIL = ("errors", "rescue_failed", "fail_notice")


def alive_says(recent, t0, t1, umo):
    """活人感的兜底记录（它的 snapshot 里最近几条）→ 这个会话在 [t0, t1] 里：「回不上」"fail" / 补救成功 "ok" / 对不上 None。
    只认带着同一个会话的记录：它的记录是全局最近 10 条，只按时间对，会把别的会话的报错算到这一轮
    （活人感 v0.4.3 的记录不带会话 → 一条都对不上，等于不用；靠的是本插件逐条看到的报错 / 空回复）"""
    got = None
    for r in recent or ():
        if not isinstance(r, dict):
            continue
        try:
            t = float(r.get("t") or 0)
        except (TypeError, ValueError):
            continue
        if not (t0 <= t <= t1):
            continue
        if not umo or str(r.get("umo") or r.get("session") or "") != str(umo):
            continue
        k = str(r.get("kind") or "")
        if k in ALIVE_FAIL or (k == "draft" and "不发" in str(r.get("why") or "")):
            return "fail"
        if k == "rescued":
            got = "ok"
    return got


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


class Tracker:
    def __init__(self, data=None, clock=time.time):
        d = data if isinstance(data, dict) else {}
        self.clock = clock
        now = clock()
        self.prev_hb = _f(d.get("hb"))  # 上次活着的时间（开机时拿它算中间空了多久）
        self.prev_clean = bool(d.get("clean"))
        self.hb = now
        self.installed = _f(d.get("installed")) or now
        self.seen = {k: _f(v) for k, v in (d.get("seen") or {}).items()}
        self.done = {k: list(v) for k, v in (d.get("done") or {}).items() if isinstance(v, (list, tuple)) and len(v) == 2}
        self.pending = [r for r in (d.get("asking") or []) if isinstance(r, dict)]  # 上次关掉时正在等模型回的
        self.pending += [r for r in (d.get("pending") or []) if isinstance(r, dict)]
        self.failed = [r for r in (d.get("failed") or []) if isinstance(r, dict)]
        self.replied = {k: _f(v) for k, v in (d.get("replied") or {}).items()}
        self.convs = {k: _f(v) for k, v in (d.get("convs") or {}).items()}
        self.outages = [x for x in (d.get("outages") or []) if isinstance(x, dict)][-LOG_MAX:]
        sd = d.get("sess_down")
        self.sess_down = {k: v for k, v in sd.items() if isinstance(v, dict)} if isinstance(sd, dict) else {}
        self.asking = {}  # 事件 id -> 记录（只在内存里；存档时并进 asking 一起存，重启了就是「被打断的」）
        self.folding = {}  # 并进了正在回的那一轮的消息 id -> 时间（只在内存里）
        self.dirty = False

    def dump(self, clean=False):
        return {
            "hb": self.hb,
            "clean": bool(clean),
            "installed": self.installed,
            "seen": self.seen,
            "done": self.done,
            "asking": list(self.asking.values())[-200:],
            "pending": self.pending[-200:],
            "failed": self.failed[-200:],
            "replied": self.replied,
            "convs": self.convs,
            "outages": self.outages[-LOG_MAX:],
            "sess_down": self.sess_down,
        }

    # ───────── 收到的 / 补过的 ─────────
    def heard(self, key, t=None):
        self.seen[key] = self.clock() if t is None else float(t)
        self.dirty = True
        if len(self.seen) > SEEN_MAX * 1.2:
            for k in sorted(self.seen, key=self.seen.get)[: len(self.seen) - SEEN_MAX]:
                self.seen.pop(k, None)

    def conv(self, ck, t=None):
        """哪些对话有人说过话（问不到 NapCat 最近会话时才用；丢几秒不要紧，不催着存）"""
        self.convs[ck] = self.clock() if t is None else float(t)

    def tries(self, key):
        v = self.done.get(key)
        return int(v[1]) if v else 0

    def mark_done(self, keys, now=None):
        now = self.clock() if now is None else now
        for k in keys:
            v = self.done.get(k)
            self.done[k] = [v[0] if v else now, (int(v[1]) if v else 0) + 1]
        self.dirty = True

    def redo_keys(self, include_failed=True):
        """收到了但没回上的（重启打断的 + 模型出错的）→ 消息 id；include_failed=False：「模型出错也补」关了，只算重启打断的。
        并进正在回的那一轮的不算（那一轮自己会回）"""
        out = set()
        for r in self.pending + ([r for r in self.failed if not self._folding(r)] if include_failed else []):
            out.update(r.get("keys") or [r.get("key")])
        return {k for k in out if k}

    # ───────── 正在等模型回的 ─────────
    def ask(self, eid, rec):
        self.asking[eid] = rec
        self.dirty = True

    def asking_rec(self, eid):
        return self.asking.get(eid)

    def ok(self, eid, unit, now=None):
        """这个对话它回上了（并进这一轮的那几条算补过了）"""
        now = self.clock() if now is None else now
        r = self.asking.pop(eid, None)
        if r and r.get("folded"):
            self.fold_done(r["folded"], now)
        if unit:
            self.replied[unit] = now
        self.dirty = True

    def drop(self, eid):
        """这一轮被别的插件拦了 / 不知道结果：不算没回上（并进这一轮的那几条放回去）"""
        r = self.asking.pop(eid, None)
        if r and r.get("folded"):
            self.fold_end(r["folded"])

    def fail(self, eid, why, now=None):
        """这一轮模型出错没回上 → 记下来（等这个会话好了再补），这个会话记成挂着"""
        now = self.clock() if now is None else now
        r = self.asking.pop(eid, None)
        if r is None:
            return None
        if r.get("folded"):
            self.fold_end(r["folded"])  # 并进来的那几条也没回上：放回去，下次跟这一条一起补
        r = dict(r, why=why, failed_at=now)
        r.pop("folded", None)
        keys = set(r.get("keys") or [r.get("key")])
        self.failed = [x for x in self.failed if not (set(x.get("keys") or [x.get("key")]) & keys)] + [r]
        umo = str(r.get("umo") or "")
        md = self.sess_down.get(umo) or {"since": now, "n": 0}
        md["last"], md["n"] = now, int(md.get("n") or 0) + 1
        self.sess_down[umo] = md
        self.dirty = True
        return r

    def session_ok(self, umo, now=None):
        """这个会话回上了一句 → 它之前出错的记录（挂了多久）；本来就好好的 → None"""
        md = self.sess_down.pop(str(umo or ""), None)
        if md:
            self.dirty = True
        return md

    def failed_in(self, umo, now=None):
        """这个会话里等着补的（并进正在回的那一轮的不算）"""
        return [r for r in self.failed if str(r.get("umo") or "") == str(umo or "") and not self._folding(r, now)]

    def drop_failed(self):
        """「模型出错没回上的也补」关了：记着的也不要了"""
        if self.failed or self.sess_down:
            self.failed, self.sess_down = [], {}
            self.dirty = True

    def replied_after(self, unit, t):
        return self.replied.get(unit, 0.0) > float(t)

    # ───────── 补完 ─────────
    def take_pending(self):
        """开机补回用：上次关掉时正在等模型回的（拿走）"""
        out, self.pending = self.pending, []
        self.dirty = True
        return out

    def _eligible(self, r, now, max_age):
        keys = r.get("keys") or [r.get("key")]
        if now - _f(r.get("t")) > max_age:
            return False
        if self.replied_after(r.get("unit") or "", _f(r.get("t"))):
            return False
        return not any(self.tries(k) >= MAX_TRIES for k in keys)

    def take_failed(self, now, max_age, umo=None):
        """补回用：umo = 只拿这个会话的（它好了）；None = 都拿（手动补）。
        没过期、它后来没回过、没补够次数的 → 拿走；这几个会话里不够格的扔掉；别的会话的、并进正在回的那一轮的留着"""
        keep, out = [], []
        for r in self.failed:
            if (umo is not None and str(r.get("umo") or "") != str(umo)) or self._folding(r, now):
                keep.append(r)
                continue
            if self._eligible(r, now, max_age):
                out.append(r)
        self.failed = keep
        self.dirty = True
        return out

    def _folding(self, r, now=None):
        now = self.clock() if now is None else now
        return any(now - self.folding.get(k, -1e18) < FOLD_HOLD for k in (r.get("keys") or [r.get("key")]) if k)

    def fold_for(self, unit, now, max_age):
        """他本人又来找它了：他之前没回上的那几条（够格的、没被别的那一轮占着的）→ 并进这一轮"""
        return [r for r in self.failed
                if (r.get("unit") or "") == unit and not self._folding(r, now) and self._eligible(r, now, max_age)]

    def fold_start(self, keys, now=None):
        """并进这一轮了：先占着（不拿走：这一轮回上了才算补过）"""
        now = self.clock() if now is None else now
        for k in keys or ():
            if k:
                self.folding[k] = now

    def fold_end(self, keys):
        """那一轮没回上 / 被拦了：放回去，等下次"""
        for k in keys or ():
            self.folding.pop(k, None)

    def fold_done(self, keys, now=None):
        """并进去的那一轮回上了：这几条算补过了"""
        keys = set(k for k in keys or () if k)
        if not keys:
            return
        self.fold_end(keys)
        self.failed = [r for r in self.failed if not (set(r.get("keys") or [r.get("key")]) & keys)]
        self.mark_done(keys, now)

    # ───────── 掉线记录 ─────────
    def log_outage(self, rec):
        self.outages = (self.outages + [rec])[-LOG_MAX:]
        self.dirty = True

    def prune(self, now=None, keep_sec=4 * 3600):
        now = self.clock() if now is None else now
        self.seen = {k: v for k, v in self.seen.items() if now - v < keep_sec}
        self.done = {k: v for k, v in self.done.items() if now - _f(v[0]) < DONE_KEEP}
        self.replied = {k: v for k, v in self.replied.items() if now - v < REPLIED_KEEP}
        self.convs = {k: v for k, v in self.convs.items() if now - v < CONV_KEEP}
        self.failed = [r for r in self.failed if now - _f(r.get("t")) < keep_sec][-200:]
        self.pending = [r for r in self.pending if now - _f(r.get("t")) < keep_sec][-200:]
        for eid in [e for e, r in self.asking.items() if now - _f(r.get("t_req") or r.get("t")) > 6 * 3600]:
            self.asking.pop(eid, None)
        self.folding = {k: t for k, t in self.folding.items() if now - t < FOLD_HOLD}
        self.dirty = True
