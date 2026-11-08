# -*- coding: utf-8 -*-
"""从 NapCat 拿回来的聊天记录里挑出「冲着它说的、它还没回的」，按对话合成一份。不依赖 AstrBot。

一个「对话」：私聊 = 那个人；群里 = 找过它的那个人（群里三个人 @ 了它，就是三个对话，各回各的、各 @ 各的）。
挑的规矩（别补错）：
  - 只看掉线那段时间里来的；太久以前的不补（默认 2 小时）
  - 补过的不补；在线时收到过的不补（收到过 = 当时处理过：回了，或者被别的插件故意拦了 —— 比如群管让它安静）
    例外：收到了但没回上的（重启打断的、模型出错的）照样补
  - 别人家的 AI 发的不补；它自己发的不算
  - 群里只补冲着它说的（@ 它 / 引用它 / 叫它名字），和他紧跟着补充的几句；群里别人之间聊天不补
  - 它在那之后已经回过的不补：私聊里它后来说过话；群里它后来 @ 了他、引用了他、或者没 @ 谁但他是最后一个找它的
  - 每个对话最多几条（留最近的，冲它说的那句一定留）
  - 指令不补、也不拼进去：以唤醒前缀 / 指令前缀开头的（「/reset」「/new」），和一开头就是 AstrBot 认的指令名的
    （@ 了它、私聊的时候不带「/」也算指令）—— 拼成「/reset\n在吗」交回 AstrBot 会被当成指令执行
  - 同一条消息只进一个对话（聊天记录和「模型出错没回上」两条路都捡到了也只补一次）
"""

import re

FOLLOW_SEC = 120  # 群里冲它说完，这么久里他紧跟着补的几句也算（「@它 在吗」「帮我看个东西」）
_PLACE = {
    "image": "[图片]", "mface": "[表情]", "face": "[表情]", "record": "[语音]", "video": "[视频]",
    "file": "[文件]", "forward": "[聊天记录]", "json": "[卡片]", "xml": "[卡片]", "poke": "[戳一戳]",
    "dice": "[骰子]", "rps": "[猜拳]", "location": "[位置]", "music": "[音乐]", "share": "[链接]",
    "contact": "[名片]",
}
_CQ = re.compile(r"\[CQ:([a-z_]+)((?:,[^\]]*)?)\]")


def _cq_segs(s):
    """老格式（字符串 + CQ 码）→ 消息段列表"""
    out, pos = [], 0
    for m in _CQ.finditer(s or ""):
        if m.start() > pos:
            out.append({"type": "text", "data": {"text": s[pos:m.start()]}})
        data = {}
        for kv in (m.group(2) or "").lstrip(",").split(","):
            if "=" in kv:
                k, _, v = kv.partition("=")
                data[k] = v
        out.append({"type": m.group(1), "data": data})
        pos = m.end()
    if pos < len(s or ""):
        out.append({"type": "text", "data": {"text": s[pos:]}})
    return out


def seg_info(msg, self_id):
    """一条 OneBot 消息 → {"text", "at_me", "at_all", "at": [@ 了谁], "reply": 引用的消息 id 或 ""}"""
    segs = msg.get("message") if isinstance(msg, dict) else None
    if isinstance(segs, str):
        segs = _cq_segs(segs)
    if not isinstance(segs, list):
        segs = [{"type": "text", "data": {"text": str((msg or {}).get("raw_message") or "")}}]
    parts, at, at_me, at_all, reply = [], [], False, False, ""
    self_id = str(self_id or "")
    for s in segs:
        if not isinstance(s, dict):
            continue
        t, d = s.get("type"), s.get("data") or {}
        if t == "text":
            parts.append(str(d.get("text") or ""))
        elif t == "at":
            q = str(d.get("qq") or "")
            if self_id and q == self_id:
                at_me = True
            elif q == "all":
                at_all = True
            elif q:
                at.append(q)
                parts.append(f"@{d.get('name') or q} ")
        elif t == "reply":
            reply = str(d.get("id") or "")
        elif t == "markdown":
            parts.append(str(d.get("content") or d.get("markdown") or ""))
        elif t == "image":
            parts.append(str(d.get("summary") or "").strip() or "[图片]")
        elif t in _PLACE:
            parts.append(_PLACE[t])
    return {"text": re.sub(r"[ \t]+", " ", "".join(parts)).strip(), "at_me": at_me, "at_all": at_all, "at": at, "reply": reply}


def is_cmd(text, prefixes=("/",), names=()):
    """是指令 → 不补：以唤醒前缀 / 指令前缀开头（「/reset」「/new」）；或者一开头就是 AstrBot 认的指令名
    （names；交回 AstrBot 的那条 @ 着它 / 是私聊，不带「/」也会被当成指令：「reset 在吗」）"""
    t = str(text or "").lstrip()
    if any(p and t.startswith(p) for p in prefixes or ()):
        return True
    t = re.sub(r"\s+", " ", t).strip()
    return any(n and (t == n or t.startswith(n + " ")) for n in names or ())


def bot_names(cfg_names, nick="", nick_counts=False):
    """「叫它名字」认哪些名字：配置里填的；QQ 昵称默认不算（「机器人又挂了」这种只是提到它，不是在找它），配置开了才算"""
    out = [str(n).strip() for n in (cfg_names or ()) if str(n or "").strip()]
    if nick_counts and str(nick or "").strip():
        out.append(str(nick).strip())
    return out


def dedupe_units(units):
    """同一条消息只进一个对话（先来的留着）；去掉以后空了的对话不要 → [对话]"""
    used, out = set(), []
    for u in units:
        lines = []
        for ln in u.get("lines") or []:
            ks = [k for k in (ln.get("keys") or []) if k]
            if ks and any(k in used for k in ks):
                continue
            used.update(ks)
            lines.append(ln)
        if lines:
            u = dict(u, lines=lines, first=lines[0]["t"], last=lines[-1]["t"])
            out.append(u)
    return out


def names_called(text, names):
    """叫没叫它的名字（名字至少 2 个字，一个字太容易误认）"""
    t = str(text or "").lower()
    return any(len(str(n or "").strip()) >= 2 and str(n).strip().lower() in t for n in names or ())


def norm(msgs, self_id, names=()):
    """聊天记录 → 按时间排好的 [{"mid", "t", "uid", "name", "self", "info", "addr"}]（同一条只留一次）"""
    out, seen = [], set()
    for m in msgs or ():
        if not isinstance(m, dict):
            continue
        if m.get("post_type") not in (None, "message", "message_sent"):
            continue
        mid = str(m.get("message_id") if m.get("message_id") is not None else "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        try:
            t = float(m.get("time") or 0)
        except (TypeError, ValueError):
            continue
        sender = m.get("sender") if isinstance(m.get("sender"), dict) else {}
        uid = str(m.get("user_id") or sender.get("user_id") or "")
        info = seg_info(m, self_id)
        out.append(
            {
                "mid": mid,
                "t": t,
                "uid": uid,
                "name": str(sender.get("card") or sender.get("nickname") or uid),
                "role": str(sender.get("role") or ""),
                "self": bool(self_id) and uid == str(self_id),
                "info": info,
            }
        )
    out.sort(key=lambda x: (x["t"], x["mid"]))
    return out


def _addressed(x, bot_mids, names):
    inf = x["info"]
    return bool(inf["at_me"] or (inf["reply"] and inf["reply"] in bot_mids) or names_called(inf["text"], names))


def pick(msgs, scene, self_id, t_from, t_to, now, max_age, max_lines=5, names=(), done=(),
         seen=(), redo=(), is_ai=None, bot_mids=(), cmd_prefixes=("/",), cmd_names=()):
    """一个对话的聊天记录 → (要补的 [对话], 没补的 {原因: 条数})

    t_from / t_to：掉线那段时间；max_age：太久以前的不补（秒）；done：补过的消息 id；seen：在线时收到过的；
    redo：收到了但没回上的（照样补）；is_ai(QQ) → 别人家的 AI；bot_mids：已知是它发的消息 id（引用它认得出）；
    cmd_prefixes / cmd_names：唤醒前缀 / 指令前缀、AstrBot 认的指令名（这些开头的不补、不拼）
    对话：{"uid", "name", "role", "lines": [{"mid", "t", "text"}], "first", "last", "how"}"""
    items = norm(msgs, self_id, names)
    bots = [x for x in items if x["self"]]
    bot_ids = {x["mid"] for x in bots} | {str(b) for b in bot_mids or ()}
    redo = set(redo or ())
    skipped = {}

    def skip(why, n=1):
        skipped[why] = skipped.get(why, 0) + n

    for x in items:
        x["addr"] = (not x["self"]) and (scene == "private" or x["mid"] in redo or _addressed(x, bot_ids, names))
    # 它后来回过谁：群里它的一句话「盖住」谁之前的话
    covered = {}  # QQ -> 它最后一次回他的时间
    last_bot = 0.0
    by_mid = {x["mid"]: x for x in items}
    for b in bots:
        last_bot = max(last_bot, b["t"])
        if scene == "private":
            continue
        inf = b["info"]
        whom = set(inf["at"])
        q = by_mid.get(inf["reply"]) if inf["reply"] else None
        if q is not None and not q["self"]:
            whom.add(q["uid"])
        if not whom:
            # 没 @ 谁、没引用谁：算它回的是在它之前最后一个找它的人
            prev = [x for x in items if not x["self"] and x["addr"] and x["t"] <= b["t"]]
            if prev:
                whom.add(prev[-1]["uid"])
        for u in whom:
            covered[u] = max(covered.get(u, 0.0), b["t"])

    units = {}
    for x in items:
        if x["self"]:
            continue
        if x["t"] < t_from or x["t"] > t_to:
            continue  # 不在掉线那段时间里
        if x["t"] < now - max_age:
            skip("太久以前")
            continue
        if x["mid"] in done:
            skip("补过了")
            continue
        if x["mid"] in seen and x["mid"] not in redo:
            continue  # 在线时收到过（当时处理过）：不算漏掉的
        if is_ai is not None and is_ai(x["uid"]):
            skip("别人家的 AI")
            continue
        if is_cmd(x["info"]["text"], cmd_prefixes, cmd_names):
            skip("指令（不补）")
            continue
        u = units.get(x["uid"])
        if scene == "group":
            if not x["addr"]:
                # 群里没冲它说：只有紧跟在他冲它说的那句后面的补充才算
                if u is None or x["t"] - u["lines"][-1]["t"] > FOLLOW_SEC:
                    continue
        if u is None:
            u = units[x["uid"]] = {"uid": x["uid"], "name": x["name"], "role": x["role"], "lines": [], "hows": set()}
        line = {"mid": x["mid"], "t": x["t"], "text": x["info"]["text"] or "[只 @ 了你]", "addr": x["addr"]}
        u["lines"].append(line)
        if scene == "group" and x["addr"]:
            inf = x["info"]
            u["hows"].add("@ 了你" if inf["at_me"] else "引用了你的话" if inf["reply"] in bot_ids else "叫了你的名字")

    out = []
    for uid, u in units.items():
        after = last_bot if scene == "private" else covered.get(uid, 0.0)
        keep = [ln for ln in u["lines"] if ln["t"] > after]
        if len(keep) < len(u["lines"]):
            skip("它后来回过了", len(u["lines"]) - len(keep))
        if scene == "group" and keep and not any(ln["addr"] for ln in keep):
            skip("它后来回过了", len(keep))  # 冲它说的那句回过了，剩下的是他跟着补的
            keep = []
        if not keep:
            continue
        if len(keep) > max(1, int(max_lines)):
            n = max(1, int(max_lines))
            tail = keep[-n:]
            if scene == "group" and not any(ln["addr"] for ln in tail):
                last_addr = [ln for ln in keep if ln["addr"]][-1]
                tail = [last_addr] + tail[1:]
            skip("一个对话最多补几条（多的没带上）", len(keep) - len(tail))
            keep = tail
        out.append(
            {
                "uid": uid,
                "name": u["name"],
                "role": u["role"],
                "lines": [{"mid": ln["mid"], "t": ln["t"], "text": ln["text"]} for ln in keep],
                "first": keep[0]["t"],
                "last": keep[-1]["t"],
                "how": "、".join(sorted(u["hows"])),
            }
        )
    out.sort(key=lambda u: u["last"])
    return out, skipped


def unit_key(pid, scene, peer, uid):
    """对话的名字：私聊「平台|private|QQ」，群里「平台|group|群|QQ」"""
    if scene == "private":
        return f"{pid}|private|{uid or peer}"
    return f"{pid}|group|{peer}|{uid}"


def rec_lines(r):
    """一条「没回上」的记录 → [(时间, 原话, 消息 id 们)]。
    补回的那一轮又没回上的：记录里带着那一轮的几条（不只最后一句），每条各带各的消息 id"""
    keys = [k for k in (r.get("keys") or [r.get("key")]) if k]
    ls = r.get("lines")
    if isinstance(ls, list) and ls:
        lk = r.get("line_keys") if isinstance(r.get("line_keys"), list) else []
        out = []
        for i, x in enumerate(ls):
            try:
                t, text = float(x[0]), str(x[1])
            except (TypeError, ValueError, IndexError):
                continue
            ks = lk[i] if i < len(lk) and isinstance(lk[i], list) else []
            out.append((t, text, [k for k in ks if k]))
        if out:
            if not any(ks for _, _, ks in out):
                out[-1] = (out[-1][0], out[-1][1], keys)
            return out
    try:
        t = float(r.get("t") or 0)
    except (TypeError, ValueError):
        t = 0.0
    return [(t, str(r.get("text") or "[只 @ 了你]"), keys)]


def merge_failed(recs, max_lines=5, cmd_prefixes=("/",), cmd_names=()):
    """模型出错没回上的几条（自己记的）→ 按对话合成 [对话]（同 pick 的格式，多一个 key / scene / peer）；指令不补"""
    units = {}
    for r in sorted(recs or (), key=lambda r: float(r.get("t") or 0)):
        k = r.get("unit") or unit_key(r.get("pid"), r.get("scene"), r.get("peer"), r.get("uid"))
        u = units.get(k)
        if u is None:
            u = units[k] = {
                "key": k, "pid": r.get("pid"), "scene": r.get("scene"), "peer": str(r.get("peer") or ""),
                "uid": str(r.get("uid") or ""), "name": r.get("name") or str(r.get("uid") or ""),
                "role": r.get("role") or "", "lines": [], "how": r.get("how") or "", "self_id": r.get("self_id") or "",
            }
        for t, text, ks in rec_lines(r):
            if is_cmd(text, cmd_prefixes, cmd_names):
                continue
            mid = ks[-1].rpartition(":")[2] if ks else ""
            u["lines"].append({"mid": mid or str(r.get("mid") or ""), "t": t, "text": text, "keys": ks})
    out = []
    for u in units.values():
        if not u["lines"]:
            continue
        u["lines"].sort(key=lambda ln: ln["t"])
        u["lines"] = u["lines"][-max(1, int(max_lines)):]
        u["first"], u["last"] = u["lines"][0]["t"], u["lines"][-1]["t"]
        out.append(u)
    out.sort(key=lambda u: u["last"])
    return out
