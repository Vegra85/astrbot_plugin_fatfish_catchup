# -*- coding: utf-8 -*-
"""写给模型的、写给管理员看的字。不依赖 AstrBot。

给模型的只写事实：这是它掉线 / 没回上的时候谁发的、几分钟前、哪几条、那段时间多长、现在几点。
怎么接、要不要解释、怎么说由模型自己定（硬规矩 E1：不写台词、不写「你可以说……」、不写语气）。
他打的原话一律压成一行、用「」包起来：原话里的换行、他手打的「# 补回」不会变成一段新的标题。
"""

import re
from datetime import datetime, timedelta, timezone

# 拿不到 AstrBot 配置里的时区时按北京时间算（用户多半在国内；服务器的时区常常是 UTC）
BEIJING = timezone(timedelta(hours=8), "北京时间")


def resolve_tz(name):
    """AstrBot 配置里的时区名 → tzinfo；空的、写错的、这台机器没有时区数据的 → 北京时间"""
    name = str(name or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001  Windows 没装 tzdata、名字写错都退回北京时间
            pass
    return BEIJING


def one_line(text, n=200):
    """他打的字 → 一行（换行压成空格），太长截断"""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]

HEAD = "# 补回（系统记录，不是谁说的话）"
CAUSE = {
    "restart": "那时候你这边掉线了（重启），没收到、没回",
    "napcat": "那时候你这边掉线了（QQ 断开又连上），没收到、没回",
    "model": "那时候你这边出了错，没回上",
    "manual": "你当时没收到、没回",
}
KIND_NAME = {"restart": "AstrBot 重启", "napcat": "QQ（NapCat）断开又连上", "model": "模型出错", "manual": "手动补一次"}


def hhmm(ts, now_ts=None, tz=None):
    """时间戳 → 「14:32」；不是今天的带上「昨天」/日期"""
    try:
        t = datetime.fromtimestamp(float(ts), tz)
        n = datetime.fromtimestamp(float(now_ts if now_ts is not None else ts), tz)
    except Exception:  # noqa: BLE001  时区坏了也不能连累补回
        return "?"
    d = (t.date() - n.date()).days
    if d == 0:
        return t.strftime("%H:%M")
    if d == -1:
        return "昨天 " + t.strftime("%H:%M")
    return t.strftime("%m-%d %H:%M")


def ago(sec):
    """秒数 → 「刚刚」「18 分钟前」「2 小时前」"""
    sec = max(0, int(sec))
    if sec < 60:
        return "不到 1 分钟前"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    h, m = divmod(sec // 60, 60)
    return f"{h} 小时前" if not m or h >= 6 else f"{h} 小时 {m} 分钟前"


def span_text(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return "不到 1 分钟"
    if sec < 3600:
        return f"{sec // 60} 分钟"
    h, m = divmod(sec // 60, 60)
    if h < 48:
        return f"{h} 小时" + (f" {m} 分钟" if m else "")
    return f"{h // 24} 天"


def model_note(info, now, tz=None):
    """补回的这一轮给模型的那段（只写事实）
    info：{"cause", "scene", "name", "how", "lines": [[时间, 原话]], "from", "to"}"""
    cause = info.get("cause") or "manual"
    lines = info.get("lines") or []
    who = one_line(info.get("name"), 24) or "对方"
    where = "私聊里发给你的" if info.get("scene") == "private" else f"在群里{info.get('how') or '找你'}"
    if info.get("fold"):
        # 他本人又来找它了：之前没回上的那几条并进这一轮（这一条是他刚发的）
        out = [HEAD, f"「{who}」这一条之前还发过 {len(lines)} 条，{CAUSE['model']}："]
    else:
        out = [HEAD, f"下面这 {len(lines)} 条是「{who}」{where}，{CAUSE.get(cause, CAUSE['manual'])}："]
    for t, text in lines:
        out.append(f"- {hhmm(t, now, tz)}（{ago(now - float(t))}）「{one_line(text)}」")
    f, t = info.get("from"), info.get("to")
    if cause in ("restart", "napcat") and f and t and float(t) > float(f):
        out.append(f"你掉线的那段：{hhmm(f, now, tz)} 到 {hhmm(t, now, tz)}（{span_text(float(t) - float(f))}）。")
    out.append(f"现在是 {hhmm(now, now, tz)}。")
    return "\n".join(out)


def unit_where(u):
    """日志 / 面板里怎么称呼这个对话"""
    if u.get("scene") == "private":
        return f"私聊 {u.get('name') or u.get('uid')}"
    return f"群 {u.get('peer')} {u.get('name') or u.get('uid')}"


def skipped_text(sk):
    return "、".join(f"{k} {v} 条" for k, v in sorted((sk or {}).items(), key=lambda kv: -kv[1]))


def status_text(tr, conf, now, tz=None, version="", on=True):
    """/补回 的回复"""
    lines = [f"鱼力max · 掉线补回 v{version}" + ("" if on else "（总开关关着：不补）")]
    last = tr.outages[-1] if tr.outages else None
    if not last:
        lines.append("还没掉过线（装上以后没遇到过要补的）。")
    else:
        what = KIND_NAME.get(last.get("kind"), last.get("kind"))
        span = ""
        if last.get("from") and last.get("to") and last.get("kind") in ("restart", "napcat"):
            span = f"，{hhmm(last['from'], now, tz)}–{hhmm(last['to'], now, tz)}（{span_text(float(last['to']) - float(last['from']))}）"
        lines.append(f"最近一次：{what}{span}，{hhmm(last.get('at') or now, now, tz)} 补的")
        units = last.get("units") or []
        if units:
            lines.append("补了：" + "、".join(f"{u.get('where')}（{u.get('n')} 条）" for u in units[:10])
                         + (f" 等 {len(units)} 个对话" if len(units) > 10 else ""))
        else:
            lines.append("补了：没有要补的" + (f"（{last.get('note')}）" if last.get("note") else ""))
        if last.get("skipped"):
            lines.append("没补：" + skipped_text(last["skipped"]))
    if tr.failed:
        sess = {str(r.get("umo") or "") for r in tr.failed}
        since = min(float(r.get("t") or now) for r in tr.failed)
        lines.append(f"模型出错没回上、等着补的：{len(tr.failed)} 条（{len(sess)} 个会话，最早 {hhmm(since, now, tz)}）"
                     "；哪个会话回上了一句就补哪个会话的")
    lines.append(
        f"设置：最长补 {int(conf['max_age_min'])} 分钟以前的 · 每个对话最多 {int(conf['max_lines'])} 条 · "
        f"私聊{'开' if conf['private'] else '关'} · 群{'开' if conf['group'] else '关'} · 对话之间隔 {conf['gap_sec']:g} 秒 · "
        f"模型出错也算：{'开' if conf['model_fail'] else '关'}"
    )
    return "\n".join(lines)
