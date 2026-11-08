#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跟天使之心一起装：被我们拦下的那一轮替它记成结束（不装 AstrBot 也能跑）：python tests/test_angel.py [插件目录 默认上一层]"""

import importlib.util
import sys
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
spec = importlib.util.spec_from_file_location("angel", d / "core/angel.py")
angel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(angel)
RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(("  [过]   " if c else "  [没过] ") + m + ("" if c or not why else f"\n           {str(why)[:600]}"))


class Ledger:
    def __init__(self):
        self.done = []

    def complete_work(self, chat_id, work_id, status="done", result_summary=""):
        self.done.append((chat_id, work_id, status, result_summary))


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Ctx:
    def __init__(self, star=None, boom=False):
        self.star, self.boom = star, boom

    def get_registered_star(self, name):
        if self.boom:
            raise RuntimeError("x")
        return self.star if name == angel.ANGEL else None


class Ev:
    def __init__(self, wid=""):
        self.extra = {"angelheart_work_id": wid} if wid else {}
        self.unified_msg_origin = "aiocqhttp:GroupMessage:123"

    def get_extra(self, k, default=None):
        return self.extra.get(k, default)

    def set_extra(self, k, v):
        self.extra[k] = v


print("[跟天使之心一起装：拦下的那一轮替它记成结束]")
led = Ledger()
star = Obj(activated=True, star_cls=Obj(angel_context=Obj(work_ledger=led)))
ev = Ev("w1")
ok = angel.release_angel(Ctx(star), ev, "测试")
check(
    ok and led.done == [("aiocqhttp:GroupMessage:123", "w1", "failed", "被别的插件拦下：测试")],
    "天使之心放行过的这一轮（事件上有它的 work_id）→ 它的工作账本里记成结束（不用等 300 秒）",
    led.done,
)
check(not angel.release_angel(Ctx(star), ev, "再来一次") and len(led.done) == 1, "同一轮只记一次", led.done)
check(not angel.release_angel(Ctx(star), Ev(""), "x") and len(led.done) == 1, "事件上没有它的 work_id（它没放行过）：不动")
check(not angel.release_angel(Ctx(None), Ev("w2"), "x"), "没装天使之心：不动、不报错")
check(not angel.release_angel(Ctx(Obj(activated=False, star_cls=None)), Ev("w3"), "x"), "装了没开：不动")
check(not angel.release_angel(Ctx(Obj(activated=True, star_cls=Obj())), Ev("w4"), "x"), "它改了内部结构（找不到工作账本）：不动、不报错")
check(not angel.release_angel(Ctx(star, boom=True), Ev("w5"), "x"), "查插件出错：不动、不报错")


class BadLedger:
    def complete_work(self, *a, **k):
        raise ValueError("x")


check(
    not angel.release_angel(Ctx(Obj(activated=True, star_cls=Obj(angel_context=Obj(work_ledger=BadLedger())))), Ev("w6"), "x"),
    "它的工作账本报错：吞掉，不影响我们",
)

ok = sum(1 for c, _ in RES if c)
bad = [m for c, m in RES if not c]
print("\n" + "=" * 50 + f"\n  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
for m in bad:
    print("    -", m)
sys.exit(1 if bad else 0)
