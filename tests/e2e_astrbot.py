#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试：真的 AstrBot（测过 4.29.4）+ 这个插件 + 认 AI（旁边有它的源码就一起装，测 duck typing）
          + 假 QQ 端（假 NapCat：反向 WebSocket、聊天记录、最近会话、断开再连）+ 假模型（OpenAI 兼容；消息里有 FAILNOW 的第一次回 402）
  A 在线时：私聊、群里 @ 它都照常回（插件记「收到过」）
  B 模型出错（按会话算好了没有）：群里老王 @ 它、私聊阿珍各一句模型报 402 → 插件记成没回上 →
      别的会话（阿木私聊）回上了一句：这两句都不重试 → 这个群小橙 @ 它回上了：15 秒后补老王那句（@ 他）→
      阿珍又来私聊：她之前没回上的那句并进这一轮一起回（不另补一次）
  C 停掉 AstrBot（把心跳改成 3 小时前，假装掉线了 3 小时），假 QQ 这边攒：
      私聊「/reset」+ 两条（30 分钟前）、群里 @ 它 + 紧跟着补一句、群友之间聊天、群里只是提到它的 QQ 昵称、
      别人家的 AI @ 它、两个半小时前的私聊、群里 @ 它但它关之前已经 @ 回过他的
    → 再起来 → 假 NapCat 的历史接口返回这些 → 私聊那人补一次（两条合一次，「/reset」不补不拼）、群里那人补一次（回的时候 @ 他）；
      太老的、群友聊天、提到昵称的、别人家的 AI、回过的都不补；日志写清楚没补的原因
  D /补回 看得到最近一次掉线、补了谁；再重启一次（没有新消息）→ 补过的不重复补
  E NapCat 断开又连上（AstrBot 没重启）：断开期间的私聊 → 连上以后补回（请求里写着 QQ 断开又连上）
  F 管理员 /补回 现在：手动补一次（没收到过的私聊补上）；普通人没权限
  G 面板接口；存档在 plugin_data；整个过程没有 Traceback
  H 跟别的鱼力max 插件一起装（旁边有它们的源码才测）：
    群管说了算让一个群静音着 → 那个群掉线时 @ 它的先不补（问了它的公开方法 may_speak）；
    再装上活人感：模型出错时活人感的兜底把报错拦下（私聊换成一句系统提示、群里不发）→ 照样记成没回上 →
    这个群别人回上了 → 补群里那句（@ 他）；私聊那人又来找它 → 并进那一轮（活人感发的系统提示不算回过他）
  I 面板里改配置（热重载）：不跑开机补回；关掉「模型出错没回上的也补」以后出错的不记、不补
用法：python tests/e2e_astrbot.py [插件目录 默认上一层] [--keep] [--alone（不装别的插件）] [--sib 别的鱼力max 插件所在的文件夹 …]
  别的鱼力max 插件（认 AI / 群管说了算 / 活人感）默认在这个插件旁边找；--sib 可以多给几个文件夹
  需要装了 astrbot 的 Python（astrbot 命令在同一个 bin 里或 PATH 里）；只在临时文件夹里跑，不连外网；端口随机
"""

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web
import jwt
import websockets

PLUGIN_SRC = Path(
    sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else Path(__file__).resolve().parent.parent
).resolve()
KEEP = "--keep" in sys.argv
PNAME = PLUGIN_SRC.name
BIN = Path(sys.executable).parent
ASTRBOT = str(BIN / "astrbot") if (BIN / "astrbot").exists() else "astrbot"
SIB_DIRS = [PLUGIN_SRC.parent] + [Path(sys.argv[i + 1]) for i, a in enumerate(sys.argv[:-1]) if a == "--sib"]


def sibling(name):
    """旁边的别的鱼力max 插件（--alone 时不装）"""
    if "--alone" in sys.argv:
        return None
    return next((d / name for d in SIB_DIRS if (d / name / "main.py").is_file()), None)


AIGUARD_SRC = sibling("astrbot_plugin_fatfish_aiguard")
GA_SRC = sibling("astrbot_plugin_fatfish_groupadmin")
ALIVE_SRC = sibling("astrbot_plugin_fatfish_alive")
SELF = 99999
A, B, C, D, E, F, AIB, H, ADMIN = 10001, 20002, 30003, 40004, 50005, 60006, 70007, 80008, 90009  # 号都是编的
OWNER, K, L, M = 11011, 12012, 13013, 14014
G1, G2 = 7001, 7002
SECRET = "e2e-secret-0123456789abcdef0123456789"
HEAD = "# 补回（系统记录，不是谁说的话）"
RES = []


def check(cond, msg, why=""):
    RES.append((bool(cond), msg))
    print(("  [过]   " if cond else "  [没过] ") + msg + ("" if cond or not why else f"\n           {str(why)[:900]}"), flush=True)
    return bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


LLM_PORT, WS_PORT, DASH_PORT = free_port(), free_port(), free_port()
ROOT = Path(tempfile.mkdtemp(prefix="astrbot-e2e-cu-"))
calls = []  # 模型收到的每个请求的最后一条（转成字符串）
NAMES = {SELF: "测试鱼", A: "阿木", B: "小橙", C: "老王", D: "阿珍", E: "阿乐", F: "路人", AIB: "隔壁机器人", H: "阿飞", ADMIN: "大刘",
         OWNER: "群主阿强", K: "阿凯", L: "阿亮", M: "阿梅"}
ROLES = {OWNER: "owner"}


# ───────────── 假模型 ─────────────
async def chat_completions(request):
    body = await request.json()
    msgs = body.get("messages") or []
    last = msgs[-1].get("content") if msgs else ""
    last = last if isinstance(last, str) else json.dumps(last, ensure_ascii=False)
    calls.append(last)
    if "FAILNOW" in last and HEAD not in last:  # 第一次：假装欠费（402 不重试）
        return web.json_response({"error": {"message": "Insufficient Balance", "type": "invalid_request_error"}}, status=402)
    return web.json_response(
        {
            "id": "x", "object": "chat.completion", "created": int(time.time()), "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"收到第{len(calls)}句"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


async def models(request):
    return web.json_response({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})


# ───────────── 假 QQ 端（NapCat）：聊天记录在外面，重启 AstrBot 以后接着用 ─────────────
HIST = {}  # ("private", QQ) / ("group", 群) -> [OneBot 消息]
SENT = []  # (scene, 对方, 文字, 消息段, 时间)
MID = [1000]


def new_mid():
    MID[0] += 1
    return MID[0]


def segs_of(text, at_bot=False, quote=None):
    segs = []
    if quote:
        segs.append({"type": "reply", "data": {"id": str(quote)}})
    if at_bot:
        segs.append({"type": "at", "data": {"qq": str(SELF)}})
        segs.append({"type": "text", "data": {"text": " " + text}})
    elif text:
        segs.append({"type": "text", "data": {"text": text}})
    return segs


def make_msg(scene, peer, uid, text, t=None, at_bot=False, quote=None, segs=None):
    mid = new_mid()
    ev = {
        "post_type": "message", "message_type": scene, "sub_type": "normal" if scene == "group" else "friend",
        "message_id": mid, "message_seq": mid, "real_id": mid, "user_id": uid, "self_id": SELF,
        "time": int(t if t is not None else time.time()), "message": segs or segs_of(text, at_bot, quote),
        "raw_message": text, "font": 0,
        "sender": {"user_id": uid, "nickname": NAMES.get(uid, str(uid)), "card": NAMES.get(uid, "") if scene == "group" else "", "role": ROLES.get(uid, "member")},
    }
    if scene == "group":
        ev["group_id"] = peer
    HIST.setdefault((scene, peer), []).append(ev)
    return ev


class FakeQQ:
    def __init__(self):
        self.ws = None
        self.new_sent = asyncio.Event()

    async def connect(self):
        for _ in range(120):
            try:
                self.ws = await websockets.connect(f"ws://127.0.0.1:{WS_PORT}/ws/", additional_headers={"X-Self-ID": str(SELF), "X-Client-Role": "Universal"})
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not self.ws:
            raise RuntimeError("连不上 AstrBot 的反向 WebSocket")
        asyncio.create_task(self.loop())
        await self.ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": SELF, "time": int(time.time())}))

    async def loop(self):
        try:
            await self._loop()
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _loop(self):
        async for raw in self.ws:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if "action" not in d:
                continue
            act, p, echo = d["action"], d.get("params") or {}, d.get("echo")
            data = {}
            if act in ("send_group_msg", "send_private_msg", "send_msg"):
                msg = p.get("message")
                segs = msg if isinstance(msg, list) else [{"type": "text", "data": {"text": str(msg)}}]
                text = "".join((s.get("data") or {}).get("text", "") for s in segs if isinstance(s, dict) and s.get("type") == "text")
                if p.get("group_id"):
                    scene, peer = "group", int(p["group_id"])
                else:
                    scene, peer = "private", int(p.get("user_id") or 0)
                ev = make_msg(scene, peer, SELF, text, segs=segs)
                SENT.append((scene, peer, text, segs, time.time()))
                data = {"message_id": ev["message_id"]}
                self.new_sent.set()
            elif act == "get_group_member_info":
                uid = int(p.get("user_id") or 0)
                data = {"user_id": uid, "nickname": NAMES.get(uid, str(uid)), "card": NAMES.get(uid, ""), "role": ROLES.get(uid, "member")}
            elif act == "get_stranger_info":
                uid = int(p.get("user_id") or 0)
                data = {"user_id": uid, "nickname": NAMES.get(uid, str(uid))}
            elif act == "get_login_info":
                data = {"user_id": SELF, "nickname": NAMES[SELF]}
            elif act == "get_msg":
                mid = int(p.get("message_id") or 0)
                data = next((m for ms in HIST.values() for m in ms if m["message_id"] == mid), {})
            elif act == "get_recent_contact":
                out = []
                for (scene, peer), ms in HIST.items():
                    if ms:
                        last = max(ms, key=lambda m: m["time"])
                        out.append({"lastestMsg": last, "peerUin": str(peer), "msgTime": str(last["time"]), "chatType": 1 if scene == "private" else 2, "msgId": str(last["message_id"])})
                out.sort(key=lambda c: -int(c["msgTime"]))
                data = out[: int(p.get("count") or 10)]
            elif act in ("get_friend_msg_history", "get_group_msg_history"):
                key = ("private", int(p.get("user_id") or 0)) if act == "get_friend_msg_history" else ("group", int(p.get("group_id") or 0))
                ms = sorted(HIST.get(key, []), key=lambda m: (m["time"], m["message_id"]))
                seq = int(p.get("message_seq") or 0)
                if seq:
                    ms = [m for m in ms if m["message_seq"] < seq]
                data = {"messages": ms[-int(p.get("count") or 20):]}
                self.history_calls = getattr(self, "history_calls", 0) + 1
            await self.ws.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": echo}, ensure_ascii=False))

    async def say(self, scene, peer, uid, text, at_bot=False):
        ev = make_msg(scene, peer, uid, text, at_bot=at_bot)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        return ev

    async def wait_sent(self, since, scene=None, peer=None, timeout=8.0, n=1):
        end = time.time() + timeout
        while time.time() < end:
            got = [s for s in SENT if s[4] >= since and (scene is None or s[0] == scene) and (peer is None or s[1] == peer)]
            if len(got) >= n:
                return got
            self.new_sent.clear()
            try:
                await asyncio.wait_for(self.new_sent.wait(), max(0.05, end - time.time()))
            except asyncio.TimeoutError:
                pass
        return [s for s in SENT if s[4] >= since and (scene is None or s[0] == scene) and (peer is None or s[1] == peer)]


def setup_root():
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT))
    subprocess.run([ASTRBOT, "init", "-y"], cwd=ROOT, env=env, check=True, capture_output=True)
    for src in [PLUGIN_SRC] + [x for x in (AIGUARD_SRC, GA_SRC) if x]:
        shutil.copytree(src, ROOT / "data" / "plugins" / src.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "tests", ".ruff_cache"))
    code = f"from astrbot.core.config.astrbot_config import AstrBotConfig;AstrBotConfig(config_path=r'{ROOT / 'data' / 'cmd_config.json'}')"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True, capture_output=True)
    p = ROOT / "data" / "cmd_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    cfg["platform"] = [{"id": "napcat", "type": "aiocqhttp", "enable": True, "ws_reverse_host": "127.0.0.1", "ws_reverse_port": WS_PORT, "ws_reverse_token": ""}]
    cfg["provider"] = [{
        "id": "fake", "type": "openai_chat_completion", "provider_type": "chat_completion", "enable": True,
        "key": ["sk-fake"], "api_base": f"http://127.0.0.1:{LLM_PORT}/v1", "model": "fake-model", "timeout": 30, "modalities": ["text"],
    }]
    cfg.setdefault("provider_settings", {})["default_provider_id"] = "fake"
    cfg["admins_id"] = [str(ADMIN)]
    cfg["platform_settings"]["rate_limit"]["count"] = 1000
    cfg.setdefault("dashboard", {})["port"] = DASH_PORT
    cfg["dashboard"]["host"] = "127.0.0.1"
    cfg["dashboard"]["jwt_secret"] = SECRET
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cdir = ROOT / "data" / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{PNAME}_config.json").write_text(json.dumps({"gap_sec": 1.0, "names": ["小鱼"]}, ensure_ascii=False), encoding="utf-8")
    if AIGUARD_SRC:  # 认 AI：不自动认、不刹车；隔壁机器人手动填成别人家的 AI
        (cdir / "astrbot_plugin_fatfish_aiguard_config.json").write_text(
            json.dumps({"enable_detect": False, "brake_enable": False, "always_bot": [str(AIB)]}), encoding="utf-8"
        )


def tracebacks(text):
    """日志里的每段 Traceback（到下一行带时间的日志为止）"""
    out = []
    for chunk in text.split("Traceback (most recent call last):")[1:]:
        body = []
        for ln in chunk.splitlines():
            if re.match(r"^\[\d\d:\d\d:\d\d", ln):
                break
            body.append(ln)
        out.append("\n".join(body))
    return out


def log_text():
    return (ROOT / "astrbot_stdout.log").read_text(encoding="utf-8", errors="replace")


def req_with(word, since=0):
    return [x for x in calls[since:] if word in x]


async def main():
    setup_root()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", LLM_PORT).start()
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT), DASHBOARD_PORT=str(DASH_PORT), PYTHONUNBUFFERED="1")
    log = open(ROOT / "astrbot_stdout.log", "w", encoding="utf-8")

    def start():
        return subprocess.Popen([ASTRBOT, "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)

    def stop(p):
        p.terminate()
        try:
            p.wait(20)
        except Exception:
            p.kill()

    state_file = ROOT / "data" / "plugin_data" / PNAME / "state.json"
    proc = start()
    qq = FakeQQ()
    try:
        await qq.connect()
        await asyncio.sleep(4)
        text = log_text()
        check(PNAME in text and "Traceback" not in text and "[掉线补回] v" in text and "第一次装上" in text,
              "AstrBot 起来了、插件加载了（第一次装上：从现在开始记）、没有报错", text[-1500:])
        if AIGUARD_SRC:
            check("astrbot_plugin_fatfish_aiguard" in text, "认 AI 一起装上了（测「别人家的 AI 不补」用它的公开方法 is_bot）")

        # ═════════ A 在线 ═════════
        print("\n[A 在线时照常回]")
        t0 = time.time()
        await qq.say("private", A, A, "你好呀")
        check(await qq.wait_sent(t0, "private", A), "私聊照常回")
        t0 = time.time()
        await qq.say("group", G1, B, "大家好", at_bot=True)
        check(await qq.wait_sent(t0, "group", G1), "群里 @ 它照常回")

        # ═════════ B 模型出错（按会话算好了没有）═════════
        print("\n[B 模型出错没回上 → 这个会话好了以后补；别的会话好了不算]")
        t0, n0 = time.time(), len(calls)
        await qq.say("group", G1, C, "FAILNOW 帮我查一下明天的天气", at_bot=True)
        got = await qq.wait_sent(t0, "group", G1, timeout=8)
        await asyncio.sleep(1)
        check(got and "LLM 响应错误" in got[-1][2], "群里老王 @ 它、模型 402：AstrBot 把报错发出来了（没装活人感的兜底）", got)
        check("这一轮模型出错没回上" in log_text(), "插件记下：这一轮没回上")
        t1 = time.time()
        await qq.say("private", D, D, "FAILNOW 帮我订个闹钟")
        await qq.wait_sent(t1, "private", D, timeout=8)
        await asyncio.sleep(1)
        lb, t2 = len(log_text()), time.time()
        await qq.say("private", A, A, "在吗")
        check(await qq.wait_sent(t2, "private", A), "阿木私聊的一句回上了（别的会话好了）")
        await asyncio.sleep(18)
        again = [x for x in SENT if x[4] >= t2 and ((x[0] == "group" and x[1] == G1) or (x[0] == "private" and x[1] == D))]
        check(not req_with(HEAD, n0) and not again and "回上了一句" not in log_text()[lb:],
              "别的会话回上了：老王那句、阿珍那句都不重试（不会每条再试一遍、往群里再发报错）", (again, log_text()[lb:][-400:]))
        t3 = time.time()
        await qq.say("group", G1, B, "我也问一句", at_bot=True)
        got = await qq.wait_sent(t3, "group", G1)
        check(got, "这个群小橙 @ 它：回上了（这个群好了）")
        check("回上了一句" in log_text()[lb:], "日志：这个会话回上了一句 → 15 秒后补这个会话没回上的")
        tb = got[-1][4] if got else t3
        got = await qq.wait_sent(tb + 0.01, "group", G1, timeout=30)
        rq = req_with(HEAD, n0)
        r = rq[-1] if rq else ""
        check(got and len(rq) == 1 and got[-1][3] and got[-1][3][0].get("type") == "at" and str(got[-1][3][0]["data"].get("qq")) == str(C),
              "15 秒后：老王那句补回了，回的时候 @ 了他", (got, len(rq)))
        check("「老王」在群里@ 了你" in r and "出了错，没回上" in r and "「FAILNOW 帮我查一下明天的天气」" in r and "现在是" in r,
              "补的那一轮请求里写着：谁、当时出了错没回上、原话（「」包着）、几点（只写事实）", r[-600:])
        check(not [x for x in SENT if x[4] >= t2 and x[0] == "private" and x[1] == D] and not [x for x in req_with(HEAD, n0) if "帮我订个闹钟" in x],
              "群里好了也不补私聊阿珍那句（不是同一个会话）")
        n1, t4 = len(calls), time.time()
        await qq.say("private", D, D, "人呢")
        got = await qq.wait_sent(t4, "private", D, timeout=10)
        rq = req_with(HEAD, n1)
        r = rq[-1] if rq else ""
        check(got and len(rq) == 1 and "「阿珍」这一条之前还发过 1 条，那时候你这边出了错，没回上" in r and "FAILNOW 帮我订个闹钟" in r and "人呢" in r,
              "阿珍又来私聊：她之前没回上的那句并进这一轮一起交给模型（请求里只写事实）", r[-600:])
        await asyncio.sleep(17)
        check(len([x for x in SENT if x[4] >= t4 and x[0] == "private" and x[1] == D]) == 1 and len(req_with("帮我订个闹钟", n1)) == 1,
              "并进去回上了：那句算补过了，不另补一次", [x[2] for x in SENT if x[4] >= t4 and x[1] == D])

        # ═════════ C 停掉 AstrBot，假 QQ 这边攒消息 ═════════
        print("\n[C AstrBot 掉线 3 小时（重启）→ 起来以后补]")
        stop(proc)
        await asyncio.sleep(1)
        st = json.loads(state_file.read_text(encoding="utf-8"))
        # AstrBot 被 SIGTERM 直接关掉不会调插件的 terminate()：靠的是平时勤存（心跳 30 秒一次，开机时往前多看 60 秒）
        check(st.get("hb", 0) > time.time() - 60, "被直接关掉（没调 terminate）：存档里的心跳也是一分钟以内的", {k: st.get(k) for k in ("hb", "clean")})
        now = time.time()
        st["hb"] = now - 3 * 3600  # 假装 3 小时前就掉线了
        state_file.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        for ms in HIST.values():  # 在线时那几句也挪到 3 个多小时前（掉线以前说的）
            for m in ms:
                m["time"] -= int(3.5 * 3600)
        make_msg("private", A, A, "/reset", t=now - 31 * 60)  # 掉线时发的指令：不补、不拼（拼成「/reset\n…」会被当成指令执行）
        make_msg("private", A, A, "掉线时第一句", t=now - 30 * 60)
        make_msg("private", A, A, "掉线时第二句", t=now - 29 * 60)
        make_msg("private", D, D, "两个半小时前的话", t=now - 150 * 60)
        make_msg("group", G1, E, "掉线时找你", t=now - 20 * 60, at_bot=True)
        make_msg("group", G1, E, "就是想问问", t=now - 19.5 * 60)
        make_msg("group", G1, F, "今天天气不错", t=now - 18 * 60)
        make_msg("group", G1, AIB, "本小姐来了", t=now - 17 * 60, at_bot=True)
        make_msg("group", G1, F, "测试鱼又挂了", t=now - 16 * 60)  # 只是提到它的 QQ 昵称：默认不算找它
        make_msg("group", G1, B, "关之前找你的", t=now - 25 * 60, at_bot=True)
        make_msg("group", G1, SELF, " 早回过你了", t=now - 24 * 60, segs=[{"type": "at", "data": {"qq": str(B)}}, {"type": "text", "data": {"text": " 早回过你了"}}])
        n0, t_up = len(calls), time.time()
        proc = start()
        qq = FakeQQ()
        await qq.connect()
        got_p = await qq.wait_sent(t_up, "private", A, timeout=40)
        got_g = await qq.wait_sent(t_up, "group", G1, timeout=15)
        await asyncio.sleep(6)
        text = log_text()
        check("上次活着是" in text and "中间空了" in text, "开机：跟上次的心跳比，中间空了多久", "")
        rq = req_with(HEAD, n0)
        ra = [x for x in rq if "「阿木」私聊里发给你的" in x]
        check(got_p and len([s for s in SENT if s[0] == "private" and s[1] == A and s[4] >= t_up]) == 1, "私聊阿木：补回了一次", got_p)
        check(len(ra) == 1 and "掉线时第一句" in ra[0] and "掉线时第二句" in ra[0] and "（30 分钟前）" in ra[0] and "掉线了（重启）" in ra[0],
              "他那两条合成一次交给模型（请求里：两条原话、几分钟前、那时候掉线了）", (ra or [""])[-1][-600:])
        sa = [s for s in SENT if s[0] == "private" and s[1] == A and s[4] >= t_up]
        check(ra and "/reset" not in ra[0] and "下面这 2 条" in ra[0] and sa and sa[0][2].startswith("收到第"),
              "掉线时发的「/reset」：不补、没拼进去（回他的是模型的话，不是重置对话）", (sa, (ra or [""])[-1][-400:]))
        re_ = [x for x in rq if "「阿乐」在群里@ 了你" in x]
        check(len(re_) == 1 and "掉线时找你" in re_[0] and "就是想问问" in re_[0], "群里 @ 它的阿乐：补一次（紧跟着补的那句也带上）", (re_ or [""])[-1][-500:])
        check(got_g and len(got_g) == 1 and got_g[0][3] and got_g[0][3][0].get("type") == "at" and str(got_g[0][3][0]["data"].get("qq")) == str(E),
              "群里回的时候 @ 了阿乐", got_g)
        check(not any(w in x for x in calls[n0:] for w in ("两个半小时前的话", "今天天气不错", "关之前找你的")), "太老的、群友之间聊天、它关之前已经回过的：都没补")
        check(not any("测试鱼又挂了" in x for x in calls[n0:]), "群里只是提到它的 QQ 昵称（「测试鱼又挂了」）：默认不算找它，不补")
        if AIGUARD_SRC:
            check(not any("本小姐来了" in x for x in calls[n0:]), "别人家的 AI @ 它的：不补（问了认 AI）")
        want = 2 if AIGUARD_SRC else 3  # 没装认 AI：认不出别人家的 AI，它 @ 的也补
        check(len(rq) == want, f"一共补了 {want} 个对话（{len(rq)}）", [x[-200:] for x in rq])
        line = next((ln for ln in text.splitlines() if "AstrBot 重启：补了" in ln), "")
        check(f"补了 {want} 个对话" in line and "太久以前 1 条" in line and "它后来回过了 1 条" in line and "指令（不补） 1 条" in line
              and (not AIGUARD_SRC or "别人家的 AI 1 条" in line),
              "日志一行写清楚：补了谁、没补的原因", line)

        # ═════════ D /补回、再重启不重复补 ═════════
        print("\n[D /补回；再重启一次不重复补]")
        t0 = time.time()
        await qq.say("private", B, B, "/补回")
        got = await qq.wait_sent(t0, "private", B)
        s = got[-1][2] if got else ""
        check("AstrBot 重启" in s and "私聊 阿木（2 条）" in s and "群 7001 阿乐（2 条）" in s and "太久以前 1 条" in s, "/补回：最近一次掉线多久、补了谁、没补的", s)
        await asyncio.sleep(4)
        stop(proc)
        n0, t_up = len(calls), time.time()
        proc = start()
        qq = FakeQQ()
        await qq.connect()
        await asyncio.sleep(18)
        check(not req_with(HEAD, n0), "再重启一次（没有新消息）：补过的不重复补", [x[-200:] for x in req_with(HEAD, n0)])
        check(log_text().count("AstrBot 重启：补了 0 个对话") >= 1, "日志：这次没有要补的")

        # ═════════ E NapCat 断开又连上 ═════════
        print("\n[E NapCat 断开又连上（AstrBot 没重启）]")
        await asyncio.sleep(2)
        await qq.ws.close()
        await asyncio.sleep(3)
        make_msg("private", A, A, "断线时发的", t=time.time() - 1)
        n0, t_up = len(calls), time.time()
        qq = FakeQQ()
        await qq.connect()
        got = await qq.wait_sent(t_up, "private", A, timeout=30)
        rq = req_with(HEAD, n0)
        check(got and len(rq) == 1 and "断线时发的" in rq[0] and "QQ 断开又连上" in rq[0], "连上以后：断开期间的私聊补回了（请求里写着 QQ 断开又连上）", (got, [x[-300:] for x in rq]))
        check("NapCat 重新连上" in log_text(), "日志：NapCat 重新连上 → 看看那段时间有没有漏掉的")

        # ═════════ F 手动补 ═════════
        print("\n[F 管理员 /补回 现在]")
        make_msg("private", H, H, "你怎么不理我", t=time.time() - 60)  # 没推给 AstrBot（假装那时候漏了）
        t0 = time.time()
        await qq.say("private", B, B, "/补回 现在")
        got = await qq.wait_sent(t0, "private", B)
        check(got and "管理员" in got[-1][2], "普通人 /补回 现在：没权限", got)
        n0, t0 = len(calls), time.time()
        await qq.say("private", ADMIN, ADMIN, "/补回 现在")
        got_h = await qq.wait_sent(t0, "private", H, timeout=20)
        got_a = await qq.wait_sent(t0, "private", ADMIN, timeout=20)
        check(got_h and req_with("你怎么不理我", n0), "管理员 /补回 现在：漏掉的那条私聊补回了", got_h)
        check(got_a and "手动补一次：补了 1 个对话" in got_a[-1][2], "回管理员：补了几个对话", got_a)

        # ═════════ G 面板、存档、日志 ═════════
        print("\n[G 面板接口、存档、日志]")
        token = jwt.encode({"username": "astrbot", "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
        hdr = {"Authorization": f"Bearer {token}"}
        base = f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}"
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/state", headers=hdr) as r:
                d = await r.json(content_type=None)
            stt = (d or {}).get("data") or {}
            kinds = [o.get("kind") for o in stt.get("outages", [])]
            check(r.status == 200 and {"restart", "napcat", "model", "manual"} <= set(kinds) and stt.get("hooked"),
                  "GET state：几次掉线都在（重启 / NapCat / 模型 / 手动），挂上了 NapCat 的元事件", (r.status, kinds, stt.get("hooked")))
            async with s.post(f"{base}/action", headers=hdr, json={"op": "run"}) as r:
                d = await r.json(content_type=None)
            check(r.status == 200 and (d or {}).get("status") == "ok", "POST action：面板里手动补一次", str(d)[:300])
            async with s.get(f"{base}/state") as r:
                check(r.status in (401, 403), f"没登录读不到（{r.status}）")
            async with s.get(f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/views/panel", headers=hdr) as r:
                raw = json.dumps(await r.json(content_type=None), ensure_ascii=False)
            check(r.status == 200 and "/_t/" in raw, "面板页 panel 认出来了", raw[:300])
        await asyncio.sleep(4)
        st = json.loads(state_file.read_text(encoding="utf-8"))
        check(st.get("done") and st.get("outages"), "补过的、掉线记录存在 data/plugin_data 里", list(st))
        bad_tb = [c[:800] for c in tracebacks(log_text()) if "Insufficient Balance" not in c]
        check(not bad_tb, "整个过程 AstrBot 日志里没有 Traceback（B 段故意让模型 402，AstrBot 自己打的那段不算）", bad_tb[:1] or "")
        text = log_text()
        check("出错（这" not in text and "补回出错" not in text and "没成：" not in text, "插件自己没报过错")

        # ═════════ H 跟别的鱼力max 插件一起装 ═════════
        if GA_SRC and ALIVE_SRC:
            print("\n[H 跟群管说了算、活人感一起装]")
            t0 = time.time()
            await qq.say("group", G2, OWNER, "闭嘴", at_bot=True)
            await qq.wait_sent(t0, "group", G2)
            await asyncio.sleep(1.5)
            check("让它静音" in log_text(), "（准备）群主让它在 7002 静音（群管说了算）")
            stop(proc)
            await asyncio.sleep(1)
            make_msg("group", G2, M, "静音的时候找你", at_bot=True)  # 掉线时 @ 它（那个群还在静音）
            shutil.copytree(ALIVE_SRC, ROOT / "data" / "plugins" / ALIVE_SRC.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "tests", ".ruff_cache"))
            (ROOT / "data" / "config" / f"{ALIVE_SRC.name}_config.json").write_text(
                json.dumps({g: {"enable": False} for g in ("gather", "rhythm", "budget", "send")}), encoding="utf-8"
            )  # 活人感只开兜底（别的模块会攒、会掷骰子，测试里不好对时间）
            n0, t_up, l0 = len(calls), time.time(), len(log_text())
            proc = start()
            qq = FakeQQ()
            await qq.connect()
            for _ in range(40):  # 等这次开机的补回跑完（只看这次重启以后的日志）
                await asyncio.sleep(1)
                if "AstrBot 重启：补了" in log_text()[l0:]:
                    break
            await asyncio.sleep(2)
            line = [ln for ln in log_text()[l0:].splitlines() if "AstrBot 重启：补了" in ln][-1:]
            check(line and "群管说了算：群管让它静音着" in line[0] and not req_with("静音的时候找你", n0)
                  and not [x for x in SENT if x[0] == "group" and x[1] == G2 and x[4] >= t_up],
                  "那个群还在静音：掉线时 @ 它的先不补（日志写着群管说了算）", line)
            check("astrbot_plugin_fatfish_alive" in log_text()[l0:], "活人感装上了")
            n0, t0, l1 = len(calls), time.time(), len(log_text())
            await qq.say("private", K, K, "FAILNOW 私聊问个事")
            got_k = await qq.wait_sent(t0, "private", K, timeout=10)
            await qq.say("group", G1, L, "FAILNOW 群里问个事", at_bot=True)
            await asyncio.sleep(4)
            got_l = [x for x in SENT if x[0] == "group" and x[1] == G1 and x[4] >= t0]
            check(got_k and got_k[-1][2].startswith("（系统提示）") and "LLM 响应错误" not in got_k[-1][2],
                  "私聊模型出错：活人感把报错换成了一句系统提示", got_k)
            check(not got_l, "群里模型出错：活人感把报错拦下了（什么都没发）", got_l)
            check(log_text()[l1:].count("这一轮模型出错没回上") >= 2, "两轮都照样记成没回上（排在活人感前面看到了报错）")
            t1 = time.time()
            await qq.say("private", A, A, "你好了吗")
            await qq.wait_sent(t1, "private", A)
            t2 = time.time()
            await qq.say("group", G1, M, "群里有人吗", at_bot=True)
            got_m = await qq.wait_sent(t2, "group", G1)
            tm = got_m[-1][4] if got_m else t2
            got_l2 = await qq.wait_sent(tm + 0.01, "group", G1, timeout=30)
            rl = [x for x in req_with(HEAD, n0) if "群里问个事" in x]
            check(got_l2 and rl and "出了错，没回上" in rl[-1] and got_l2[-1][3][0].get("type") == "at" and str(got_l2[-1][3][0]["data"].get("qq")) == str(L),
                  "这个群阿梅 @ 它回上了 → 群里那句补回了，@ 了他", (got_l2, [x[-300:] for x in rl]))
            check(not [x for x in req_with(HEAD, n0) if "私聊问个事" in x] and not [x for x in SENT if x[0] == "private" and x[1] == K and x[4] >= t1],
                  "阿木私聊、群里都回上了，阿凯那句也不补（不是同一个会话）")
            n1, t3 = len(calls), time.time()
            await qq.say("private", K, K, "还在吗")
            got_k2 = await qq.wait_sent(t3, "private", K, timeout=10)
            rk = [x for x in req_with(HEAD, n1) if "私聊问个事" in x]
            check(got_k2 and len(got_k2) == 1 and rk and "「阿凯」这一条之前还发过 1 条" in rk[-1] and not got_k2[-1][2].startswith("（系统提示）"),
                  "阿凯又来私聊：之前没回上的那句并进这一轮一起回（活人感发过系统提示，不算回过他）", (got_k2, [x[-300:] for x in rk]))
            bad_tb = [c[:800] for c in tracebacks(log_text()) if "Insufficient Balance" not in c]
            check(not bad_tb, "一起装也没有 Traceback（故意 402 的不算）", bad_tb[:1] or "")
        else:
            print("\n[H 跳过：旁边没有群管说了算 / 活人感的源码（--sib 指给它）]")

        # ═════════ I 热重载、关掉「模型出错也补」═════════
        print("\n[I 面板里改配置（热重载）：不跑开机补回；关掉「模型出错没回上的也补」→ 不记不补]")
        l0, n0 = len(log_text()), len(calls)
        token = jwt.encode({"username": "astrbot", "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
        conf_new = {"enabled": True, "max_age_min": 120, "max_lines": 5, "private": True, "group": True, "gap_sec": 1.0,
                    "model_fail": False, "max_convs": 10, "names": ["小鱼"], "nick_counts": False}
        async with aiohttp.ClientSession() as s:
            async with s.put(f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/config",
                             headers={"Authorization": f"Bearer {token}"}, json={"config": conf_new}) as r:
                d = await r.json(content_type=None)
        check(r.status == 200 and "热重载" in str(d), "面板里改配置（关掉「模型出错没回上的也补」）→ 插件热重载", str(d)[:300])
        for _ in range(30):
            await asyncio.sleep(1)
            if "不跑开机补回" in log_text()[l0:]:
                break
        await asyncio.sleep(14)
        tl = log_text()[l0:]
        line = next((ln for ln in tl.splitlines() if "不跑开机补回" in ln), "")
        check(line and "AstrBot 重启：补了" not in tl and not req_with(HEAD, n0),
              "热重载：日志写着上次正常关、只空了几秒 → 不跑开机补回（也真没补）", line or tl[-600:])
        t0 = time.time()
        await qq.say("private", E, E, "FAILNOW 关了以后出错")
        await qq.wait_sent(t0, "private", E, timeout=10)
        await asyncio.sleep(2)
        t1 = time.time()
        await qq.say("private", E, E, "好了吗")
        got = await qq.wait_sent(t1, "private", E, timeout=10)
        await asyncio.sleep(17)
        tl = log_text()[l0:]
        st = json.loads(state_file.read_text(encoding="utf-8"))
        check(got and "这一轮模型出错没回上" not in tl and "这一轮模型报错没回上" not in tl and not req_with(HEAD, n0) and not st.get("failed"),
              "关了以后：模型出错的不记、不补；他再来找它也不并进去", (tl[-500:], st.get("failed")))
        bad_tb = [c[:800] for c in tracebacks(log_text()) if "Insufficient Balance" not in c]
        check(not bad_tb, "热重载以后也没有 Traceback（故意 402 的不算）", bad_tb[:1] or "")
    finally:
        stop(proc)
        await runner.cleanup()
        log.close()

    ok = sum(1 for c, _ in RES if c)
    bad = [m for c, m in RES if not c]
    print("\n" + "=" * 60)
    print(f"  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
    for m in bad:
        print("    -", m)
    print("=" * 60)
    if KEEP or bad:
        print("AstrBot 目录留着：", ROOT)
    else:
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(1 if bad else 0)


asyncio.run(main())
