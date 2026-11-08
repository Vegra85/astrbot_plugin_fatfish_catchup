# -*- coding: utf-8 -*-
"""跟天使之心一起装（鱼力max 插件共用的小工具，每个插件各放一份，不互相 import）

天使之心（astrbot_plugin_angel_heart，2.2.x）放行一轮给主模型的时候，在它的「工作账本」里记一笔「处理中」，
等这一轮「真发出去了」（after_message_sent）才记成完成。AstrBot 4.29 里事件被 stop_event() 停掉、或者结果是空的，
after_message_sent 不会触发 —— 这一笔就一直「处理中」，天使之心要等 300 秒才当它超时：这期间这个群谁 @ 都不回。
所以：我们拦下一轮（stop_event / 清空结果不发）之前，看一眼事件上有没有天使之心的 work_id，有就替它把这一笔记成结束。
只用 duck typing：没装、没开、它改了内部结构 → 什么都不做（返回 False），不报错。
"""

ANGEL = "astrbot_plugin_angel_heart"


def release_angel(context, event, why=""):
    """→ True = 替天使之心把这一轮记成结束了"""
    try:
        wid = str(event.get_extra("angelheart_work_id", "") or "")
    except Exception:
        return False
    if not wid:
        return False
    try:
        md = context.get_registered_star(ANGEL)
    except Exception:
        return False
    if md is None or getattr(md, "activated", True) is False:
        return False
    try:
        ledger = getattr(getattr(getattr(md, "star_cls", None), "angel_context", None), "work_ledger", None)
        if ledger is None or not hasattr(ledger, "complete_work"):
            return False
        ledger.complete_work(
            event.unified_msg_origin,
            wid,
            status="failed",
            result_summary=("被别的插件拦下：" + str(why or ""))[:80],
        )
    except Exception:
        return False
    try:
        event.set_extra("angelheart_work_id", "")  # 同一轮只记一次
    except Exception:
        pass
    return True
