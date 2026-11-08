# -*- coding: utf-8 -*-
"""
鱼力max · 掉线补回（AstrBot 插件）

痛点：AstrBot 重启、NapCat 掉线、模型挂了的那段时间，别人私聊它、群里 @ 它的话石沉大海 —— 对方以为它不理人。

认出掉线：
  ① AstrBot 重启：插件每 30 秒记一次心跳；开机时拿上次的心跳比，中间空了多久就是掉线的那段
  ② NapCat 断开又连上：AstrBot 的 aiocqhttp 适配器自己不发「连上了」的事件（只在日志里打一句），
     插件挂在它底下的 aiocqhttp（CQHttp.subscribe("meta_event")）收元事件：lifecycle.connect = 重新连上；心跳隔了一大截 = 中间断过
  ③ 模型挂了：找它的这一轮模型报错 / 什么都没回（逐条自己记；活人感拦下的报错本插件排在它前面先看到）
     → 按会话算「好了没有」：这个会话回上了一句再补这个会话的；他本人又来找它，之前没回上的并进那一轮
     （v0.1.0 复查：以前是全局的，一个会话一直挂、别处正常 → 每条再试一遍、往群里发报错）
恢复以后补：get_recent_contact 看哪些对话在那段时间有新消息 → get_friend_msg_history / get_group_msg_history 拿聊天记录
  → 挑出冲着它说的、它还没回的（core/pick.py）→ 每个对话合成一条，当成他刚发来的交给 AstrBot 正常走一遍
  （人设、对话记录、别的插件的钩子都照常；群管说了算让它安静着、被禁言着也照样管得住）
  → 这一轮给模型一段事实（这是你掉线 / 没回上的时候他发的，几分钟前、哪几条）→ 群里回的时候 @ 他

钩子（顺序写死在这里）：
  每条消息（max-3）     记「收到过」（在线时收到过的不补）、哪些对话有人说话、它的 QQ 号
  回话请求（30）        补回的这一轮：加那段事实（临时段，不进对话记录）；找它的这一轮：记「正在等模型回」，
                        他之前模型出错没回上的几条并进这一轮（也是临时段）
  模型回完（max-5）     回上了 → 这个会话好了（这个会话之前没回上的去补）；报错 / 什么都没回 → 记下来
  发送前（max）         框架把报错发出来了 → 这一轮没回上（排在活人感兜底前面：它拦下之前先看到）
  发送前（-1000）       补回的这一轮：群里 @ 他；这期间它已经回过他了就不发（拦之前替天使之心收口）
  发完（-1000）         回上了：记下它刚回过这个对话
判断逻辑在 core/（不依赖 AstrBot，可以单独测）。
"""

import asyncio
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
import astrbot.api.message_components as Comp

try:  # 这一轮临时加给模型的段落（不进对话记录）；老版本没有 → 不加
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None

from .core.angel import release_angel
from .core.conf import num, str_list, truthy
from .core.notes import KIND_NAME, model_note, resolve_tz, skipped_text, status_text, unit_where
from .core.pick import bot_names, dedupe_units, is_cmd, merge_failed, names_called, pick, rec_lines, unit_key
from .core.track import MAX_TRIES, Tracker, alive_says, should_boot

PLUGIN_NAME = "astrbot_plugin_fatfish_catchup"
VERSION = "0.1.0"
TAG = "[掉线补回]"
AIGUARD = "astrbot_plugin_fatfish_aiguard"
ALIVE = "astrbot_plugin_fatfish_alive"
GROUPADMIN = "astrbot_plugin_fatfish_groupadmin"
EXTRA = "fatfish_catchup"
HB_SEC = 30  # 心跳：多久记一次「还活着」
WAIT_SEC = 8  # 连上以后等几秒再拉记录（让 QQ 把离线消息同步下来）
ASK_TIMEOUT = 150  # 找它的这一轮这么久都没结果：看看是不是没回上
BOOT_WAIT = 600  # 开机后最多等 QQ 连上多久
# AstrBot 调模型出错时原样发到聊天里的那几种开头（4.29 源码里对的；只认开头，正常回话里提到这些词不算）
ERR_HEAD = re.compile(
    r"^\s*(?:Error occurred during AI execution|Error occurred while processing agent(?: request)?|"
    r"LLM 响应错误\s*[:：]|LLM 请求失败\s*[:：]|All chat models failed|AstrBot 请求失败|AstrBot 处理失败|"
    r"Traceback \(most recent call last\)|APIConnectionError|APITimeoutError|RateLimitError|AuthenticationError)",
    re.I,
)


class FatfishCatchup(Star):
    """掉线补回：/补回 看最近一次掉线补了什么；管理员 /补回 现在：手动补一次"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.data_dir = Path(os.getcwd()) / "data" / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.tr = Tracker(self._load())
        self._boot_t = time.time()
        self._booting = False  # 开机补回还在等 QQ 连上（这期间的「连上了」交给它）
        self._self_ids = {}  # 平台 -> 它的 QQ 号
        self._nick = {}  # 平台 -> 它的 QQ 昵称
        self._napcat_seen = {}  # 平台 -> 最后一次收到 NapCat 的任何东西（消息 / 通知 / 心跳）
        self._hb_iv = {}  # 平台 -> NapCat 心跳间隔（秒）
        self._subs = {}  # id(CQHttp) -> (CQHttp, 处理函数)
        self._lock = asyncio.Lock()
        self._tasks = set()
        self._loops = []
        self._sess_tasks = {}  # 会话 -> 这个会话好了以后排着的那一次补回
        self._napcat_task = None  # 断开又连上以后排着的那一次补回（NapCat 来回闪断时只补一次，时间窗往前扩）
        self._napcat_from = {}  # 平台 -> 这次要从几点开始看
        self._failed_eids = {}  # 这一轮记成了没回上的事件 id -> 时间（之后发出去的不算回上：活人感会把报错换成一句系统提示发出去）
        self._empties = {}  # 模型什么都没回的那几轮：事件 id -> 事件（到时候看有没有别人替它发出去 —— 活人感的空回复救援用 event.send，不走「发完」）
        self._closed = False
        self._saved_at = 0.0
        self._tz_cache = (None, None)
        self._cmd_cache = (0.0, ())
        self._register_web()

    async def initialize(self):
        self.tr.hb = time.time()
        self._save(force=True)
        self._start_loops()
        try:
            self._subscribe()  # 热重载插件时平台已经在了
        except Exception as e:
            logger.warning(f"{TAG} 挂 NapCat 元事件出错：{e!r}"[:200])
        prev = self.tr.prev_hb
        boot = self._on() and should_boot(prev, self.tr.prev_clean, self._boot_t)
        if boot:
            self._booting = True
            self._spawn(self._boot_catchup(prev))
        gap = int(max(0, self._boot_t - prev))
        if not prev:
            why = "：第一次装上，从现在开始记"
        elif boot:
            why = f"：上次活着是 {self._hhmm(prev)}（中间空了 {gap} 秒{'' if self.tr.prev_clean else '，上次没正常关'}）→ QQ 连上以后看看有没有要补的"
        else:
            why = f"：上次正常关、中间只空了 {gap} 秒（热重载 / 改配置 / 秒重启）→ 不跑开机补回"
        logger.info(f"{TAG} v{VERSION} 已启用{'' if self._on() else '（总开关关着：不补）'}{why}")

    async def terminate(self):
        self._closed = True
        self._unsubscribe()
        for t in list(self._tasks) + self._loops:
            t.cancel()
        self.tr.hb = time.time()
        self._save(force=True, clean=True)

    # ───────── 配置 / 存档 ─────────
    def _on(self):
        return truthy(self.config.get("enabled"), True)

    def _conf(self):
        c = self.config
        return {
            "max_age_min": num(c.get("max_age_min", 120), 120, 1, 7 * 1440),
            "max_lines": int(num(c.get("max_lines", 5), 5, 1, 20)),
            "private": truthy(c.get("private"), True),
            "group": truthy(c.get("group"), True),
            "gap_sec": num(c.get("gap_sec", 5), 5, 0, 120),
            "model_fail": truthy(c.get("model_fail"), True),
            "max_convs": int(num(c.get("max_convs", 10), 10, 1, 100)),
            "nick_counts": truthy(c.get("nick_counts"), False),
        }

    def _names(self, pid):
        """「叫它名字」认哪些：配置里填的；QQ 昵称默认不算（「机器人又挂了」只是提到它），配置开了才算"""
        c = self._conf()
        return bot_names(str_list(self.config.get("names")), self._nick.get(pid, ""), c["nick_counts"])

    def _cmd_prefixes(self):
        """唤醒前缀 / 指令前缀（这些开头的不补、不拼）：AstrBot 配置里的 wake_prefix，再加上「/」"""
        try:
            ps = list(self.context.get_config().get("wake_prefix") or [])
        except Exception:
            ps = []
        return tuple(dict.fromkeys([str(p) for p in ps if str(p or "").strip()] + ["/"]))

    def _cmd_names(self):
        """AstrBot 现在认的指令名（含别名、指令组）：补的那条 @ 着它 / 是私聊，一开头是这些就会被当成指令执行（不带「/」也会）"""
        now = time.time()
        if now - self._cmd_cache[0] < 60:
            return self._cmd_cache[1]
        names = set()
        try:
            from astrbot.core.star.star_handler import star_handlers_registry

            for h in star_handlers_registry:
                for f in getattr(h, "event_filters", None) or []:
                    fn = getattr(f, "get_complete_command_names", None)
                    if callable(fn):
                        names.update(str(n).strip() for n in (fn() or []) if str(n or "").strip())
        except Exception:
            pass  # 拿不到就只认前缀
        self._cmd_cache = (now, tuple(sorted(names)))
        return self._cmd_cache[1]

    def _tz(self):
        """AstrBot 配置里的时区；拿不到 = 北京时间（不退回服务器的时区）"""
        try:
            name = str(self.context.get_config().get("timezone") or "").strip()
        except Exception:
            name = ""
        if self._tz_cache[0] != name or self._tz_cache[1] is None:
            self._tz_cache = (name, resolve_tz(name))
        return self._tz_cache[1]

    def _hhmm(self, ts):
        from .core.notes import hhmm

        return hhmm(ts, time.time(), self._tz())

    def _load(self):
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save(self, force=False, clean=False):
        """AstrBot 被直接关掉时不会调 terminate()：有变化就尽快存（最多 3 秒一次，后台补上）"""
        now = time.time()
        if not force and (not self.tr.dirty or now - self._saved_at < 3):
            return
        self._saved_at = now
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.tr.dump(clean=clean), ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)
            self.tr.dirty = False
        except Exception as e:
            logger.warning(f"{TAG} 存档失败：{e!r}"[:200])

    def _spawn(self, coro):
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return None
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _start_loops(self):
        if self._loops or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loops = [loop.create_task(self._hb_loop()), loop.create_task(self._save_loop())]

    async def _save_loop(self):
        while not self._closed:
            await asyncio.sleep(3)
            try:
                self._save()
            except Exception:
                pass

    async def _hb_loop(self):
        """心跳：记「还活着」（下次开机拿它算掉线了多久）；顺手看看等模型回的那几轮有没有结果"""
        n = 0
        while not self._closed:
            try:
                await asyncio.sleep(HB_SEC)
                now = time.time()
                self.tr.hb = now
                self.tr.dirty = True
                if not self._conf()["model_fail"]:
                    self.tr.drop_failed()  # 「模型出错没回上的也补」关了：不记、不补，记着的也不要了
                self._settle(now)
                n += 1
                if n % 20 == 0:
                    self.tr.prune(now, keep_sec=max(4 * 3600, self._conf()["max_age_min"] * 60 + 3600))
                self._subscribe()  # 平台重载过的话重新挂上
                self._save()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{TAG} 心跳出错：{e!r}"[:200])

    # ───────── 别的鱼力max 插件（duck typing）─────────
    def _peer(self, name):
        try:
            md = self.context.get_registered_star(name)
        except Exception:
            return None
        if md is None or not getattr(md, "activated", True):
            return None
        return getattr(md, "star_cls", None)

    def _is_ai(self, uid):
        """认 AI 认出来的别人家的 AI：它发的不补"""
        fn = getattr(self._peer(AIGUARD), "is_bot", None)
        if callable(fn):
            try:
                return bool(fn(str(uid)))
            except Exception:
                return False
        return False

    def _may_speak(self, gid, uid, role):
        """群管说了算：这个群它现在能不能说话（安静着只回 @ 的 —— 补回本来就是回 @ 它的；静音 / 禁言着就先不补）"""
        fn = getattr(self._peer(GROUPADMIN), "may_speak", None)
        if not callable(fn):
            return True, ""
        try:
            ok, why = fn(str(gid), str(uid), addressed=True, admin=role in ("owner", "admin"))
            return bool(ok), str(why or "")
        except Exception:
            return True, ""

    def _alive_says(self, t0, t1, umo):
        """活人感的兜底在这段时间里、这个会话记了「回不上」吗 → "fail" / "ok" / None（没装、没记、对不上；规则在 core/track.py）"""
        fn = getattr(self._peer(ALIVE), "snapshot", None)
        if not callable(fn):
            return None
        try:
            recent = ((fn() or {}).get("guard") or {}).get("recent") or []
            return alive_says(recent, t0, t1, umo)
        except Exception:
            return None

    # ───────── 平台 / NapCat ─────────
    def _platforms(self):
        out = []
        try:
            insts = list(getattr(getattr(self.context, "platform_manager", None), "platform_insts", None) or [])
        except Exception:
            insts = []
        for p in insts:
            try:
                if p.meta().name == "aiocqhttp":
                    out.append(p)
            except Exception:
                continue
        return out

    @staticmethod
    def _bot(plat):
        bot = getattr(plat, "bot", None)
        if bot is None:
            try:
                bot = plat.get_client()
            except Exception:
                bot = None
        return bot

    async def _call(self, bot, action, timeout=15, **params):
        return await asyncio.wait_for(bot.call_action(action, **params), timeout)

    def _subscribe(self):
        """挂到 aiocqhttp 上收元事件（连上了 / 心跳）—— AstrBot 的适配器自己不转这些"""
        plats = self._platforms()
        live = {id(self._bot(p)) for p in plats}
        for k in [k for k in self._subs if k not in live]:  # 平台重载过：旧的那个不用了
            bot, h = self._subs.pop(k)
            try:
                bot.unsubscribe("meta_event", h)
            except Exception:
                pass
        for plat in plats:
            bot = self._bot(plat)
            if bot is None or id(bot) in self._subs or not hasattr(bot, "subscribe"):
                continue
            try:
                pid = plat.meta().id
            except Exception:
                continue

            async def handler(ev, _pid=pid):
                await self._on_meta(_pid, ev)

            try:
                bot.subscribe("meta_event", handler)
                self._subs[id(bot)] = (bot, handler)
            except Exception as e:
                logger.warning(f"{TAG} 挂不上 NapCat 的连上 / 心跳事件（只能靠重启时的心跳认掉线）：{e!r}"[:200])

    def _unsubscribe(self):
        for bot, h in list(self._subs.values()):
            try:
                bot.unsubscribe("meta_event", h)
            except Exception:
                pass
        self._subs.clear()

    async def _on_meta(self, pid, ev):
        try:
            if self._closed or not self._on():
                return
            now = time.time()
            last = self._napcat_seen.get(pid, 0.0)
            mt = ev.get("meta_event_type")
            if mt == "heartbeat":
                try:
                    iv = float(ev.get("interval") or 0) / 1000
                    if iv > 0:
                        self._hb_iv[pid] = iv
                except (TypeError, ValueError):
                    pass
                gap = now - last
                if last and gap > max(90.0, 3 * self._hb_iv.get(pid, 30.0)) and not self._booting:
                    self._napcat_back(pid, last, now, f"心跳断了 {int(gap)} 秒")
            elif mt == "lifecycle" and ev.get("sub_type") == "connect":
                if last and not self._booting:
                    self._napcat_back(pid, last, now, "重新连上")
            self._napcat_seen[pid] = now
        except Exception as e:
            logger.warning(f"{TAG} 处理 NapCat 元事件出错：{e!r}"[:200])

    def _napcat_back(self, pid, t_from, now, why):
        self._napcat_from[pid] = min(t_from, self._napcat_from.get(pid, t_from))
        if self._napcat_task is not None and not self._napcat_task.done():
            logger.info(f"{TAG} NapCat 又{why}：并进排着的那一次补回（从 {self._hhmm(self._napcat_from[pid])} 看起）")
            return
        logger.info(f"{TAG} NapCat {why}（上次收到它的东西是 {self._hhmm(t_from)}）→ {WAIT_SEC} 秒后看看那段时间有没有漏掉的")

        async def go():
            await asyncio.sleep(WAIT_SEC)
            froms, self._napcat_from = self._napcat_from, {}
            for p, f in froms.items():
                await self._run("napcat", f - 30, time.time() + 2, only_pid=p)

        self._napcat_task = self._spawn(go())

    async def _boot_catchup(self, prev_hb):
        """开机：等 QQ 连上 → 等几秒让离线消息同步 → 补重启那段时间漏掉的"""
        try:
            t_conn = await self._wait_connected()
            if t_conn is None:
                logger.warning(f"{TAG} 开机 {BOOT_WAIT // 60} 分钟了 QQ 还没连上：这次不补（之后手动 /补回 现在）")
                return
            await asyncio.sleep(WAIT_SEC)
            pend = self.tr.take_pending()
            t_from = min([prev_hb] + [float(r.get("t") or prev_hb) for r in pend]) - 60
            await self._run("restart", t_from, t_conn + 2, pending=pend)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"{TAG} 开机补回出错：{e!r}"[:200])
        finally:
            self._booting = False

    async def _wait_connected(self):
        """→ QQ 连上（问得通 get_login_info）的时间；等不到 → None"""
        end = time.time() + BOOT_WAIT
        while time.time() < end and not self._closed:
            for plat in self._platforms():
                bot = self._bot(plat)
                if bot is None:
                    continue
                try:
                    d = await self._call(bot, "get_login_info", timeout=5)
                    pid = plat.meta().id
                    if (d or {}).get("user_id"):
                        self._self_ids[pid] = str(d["user_id"])
                        self._nick[pid] = str(d.get("nickname") or "")
                        return time.time()
                except Exception:
                    continue
            await asyncio.sleep(2)
        return None

    # ───────── 补一次 ─────────
    async def _run(self, kind, t_from, t_to, pending=(), only_pid=None, include_failed=False, failed_umo=None):
        """看 [t_from, t_to] 这段时间里有没有冲它说的、它没回的 → 每个对话合成一条交给模型 → 记下来 → (补了几个对话, 说明)"""
        if not self._on():
            return 0, "总开关关着"
        async with self._lock:
            conf, now = self._conf(), time.time()
            max_age = conf["max_age_min"] * 60
            units, skipped, notes = [], {}, []
            pend_keys = {k for r in pending for k in (r.get("keys") or [r.get("key")]) if k}
            redo_all = self.tr.redo_keys(include_failed=conf["model_fail"]) | pend_keys
            prefixes = self._cmd_prefixes()
            for plat in self._platforms():
                pid = plat.meta().id
                if only_pid and pid != only_pid:
                    continue
                bot = self._bot(plat)
                if bot is None:
                    continue
                self_id = await self._self_id(pid, bot)
                if not self_id:
                    notes.append("QQ 没连上")
                    continue
                convs = await self._convs(bot, pid, t_from, now, conf, pending)
                names = self._names(pid)
                pre = pid + ":"
                done = {k[len(pre):] for k in self.tr.done if k.startswith(pre)}
                seen = {k[len(pre):] for k in self.tr.seen if k.startswith(pre)}
                redo = {k[len(pre):] for k in redo_all if k.startswith(pre)}
                for scene, peer in convs:
                    if not conf[scene]:
                        continue
                    try:
                        msgs = await self._history(bot, scene, peer, max(t_from, now - max_age - 600))
                    except Exception as e:
                        notes.append(f"{'私聊' if scene == 'private' else '群'} {peer} 拿不到聊天记录")
                        logger.warning(f"{TAG} 拿 {scene} {peer} 的聊天记录失败：{e!r}"[:200])
                        continue
                    got, sk = pick(
                        msgs, scene, self_id, t_from, t_to, now, max_age, conf["max_lines"], names=names,
                        done=done, seen=seen, redo=redo, is_ai=self._is_ai, cmd_prefixes=prefixes, cmd_names=self._cmd_names(),
                    )
                    for k, v in sk.items():
                        skipped[k] = skipped.get(k, 0) + v
                    for u in got:
                        u.update(pid=pid, scene=scene, peer=str(peer), self_id=self_id, key=unit_key(pid, scene, peer, u["uid"]))
                        for ln in u["lines"]:
                            ln["keys"] = [pre + ln["mid"]]
                        units.append(u)
            if include_failed and conf["model_fail"]:
                units += self._failed_units(now, conf, skipped, umo=failed_umo)
            sent = await self._send_units(kind, units, t_from, t_to, conf, skipped)
            rec = {
                "kind": kind, "from": t_from + (60 if kind == "restart" else 30 if kind == "napcat" else 0), "to": t_to,
                "at": time.time(), "units": [{"where": unit_where(u), "n": len(u["lines"])} for u in sent],
                "skipped": skipped, "note": "；".join(dict.fromkeys(notes)),
            }
            self.tr.log_outage(rec)
            self._save(force=True)
            line = (
                f"{KIND_NAME.get(kind, kind)}：补了 {len(sent)} 个对话"
                + (f"（{'、'.join(r['where'] + '（' + str(r['n']) + ' 条）' for r in rec['units'][:6])}）" if sent else "")
                + (f"；没补：{skipped_text(skipped)}" if skipped else "")
                + (f"；{rec['note']}" if rec["note"] else "")
            )
            logger.info(f"{TAG} {line}")
            return len(sent), line

    def _failed_units(self, now, conf, skipped, umo=None):
        """模型出错没回上的（自己记的）→ 对话；umo = 只拿这个会话的（它好了），None = 都拿（手动补）"""
        recs = []
        for r in self.tr.take_failed(now, conf["max_age_min"] * 60, umo=umo):
            if self._is_ai(r.get("uid")):
                skipped["别人家的 AI"] = skipped.get("别人家的 AI", 0) + 1
                continue
            recs.append(r)
        return merge_failed(recs, conf["max_lines"], self._cmd_prefixes(), self._cmd_names())

    async def _send_units(self, kind, units, t_from, t_to, conf, skipped):
        """对话们 → 一个一个交给 AstrBot（中间隔几秒）→ 发出去了的那些"""
        n0 = sum(len(u["lines"]) for u in units)
        units = dedupe_units(units)  # 聊天记录和「模型出错没回上」两条路捡到同一条：只补一次
        if sum(len(u["lines"]) for u in units) < n0:
            skipped["两条路都捡到了（只补一次）"] = n0 - sum(len(u["lines"]) for u in units)
        units = sorted(units, key=lambda u: (u["last"]))
        if len(units) > conf["max_convs"]:
            skipped["一次最多补几个对话（多的没补）"] = len(units) - conf["max_convs"]
            units = units[-conf["max_convs"]:]  # 留最近的
        sent = []
        for i, u in enumerate(units):
            if u["scene"] == "group":
                ok, why = self._may_speak(u["peer"], u["uid"], u.get("role"))
                if not ok:
                    skipped[f"群管说了算：{why}"] = skipped.get(f"群管说了算：{why}", 0) + len(u["lines"])
                    continue
            if i and sent:
                await asyncio.sleep(conf["gap_sec"])
            try:
                if await self._dispatch(kind, u, t_from, t_to):
                    sent.append(u)
            except Exception as e:
                logger.warning(f"{TAG} 补 {unit_where(u)} 没成：{e!r}"[:200])
        return sent

    async def _dispatch(self, kind, u, t_from, t_to):
        """一个对话 → 合成一条他发来的消息，交给 AstrBot 正常走一遍（这一轮带着补回的事实）"""
        plat = next((p for p in self._platforms() if p.meta().id == u["pid"]), None)
        if plat is None:
            return False
        keys = list(dict.fromkeys(k for ln in u["lines"] for k in (ln.get("keys") or []) if k))
        if any(self.tr.tries(k) >= MAX_TRIES for k in keys):
            return False
        text = "\n".join(ln["text"] for ln in u["lines"])
        last = u["lines"][-1]
        self_id, uid, group = str(u["self_id"]), str(u["uid"]), u["scene"] == "group"
        segs = [{"type": "text", "data": {"text": text}}]
        if group:
            segs = [{"type": "at", "data": {"qq": self_id}}, {"type": "text", "data": {"text": " " + text}}]
        mid = last.get("mid") or ""
        payload = {
            "post_type": "message",
            "message_type": "group" if group else "private",
            "sub_type": "normal" if group else "friend",
            "message_id": int(mid) if str(mid).isdigit() else mid,
            "user_id": int(uid) if uid.isdigit() else uid,
            "self_id": int(self_id) if self_id.isdigit() else self_id,
            "time": int(time.time()),
            "message": segs,
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": int(uid) if uid.isdigit() else uid, "nickname": u["name"], "card": u["name"] if group else "", "role": u.get("role") or "member"},
        }
        if group:
            payload["group_id"] = int(u["peer"]) if str(u["peer"]).isdigit() else u["peer"]
        from aiocqhttp import Event

        abm = await plat.convert_message(Event.from_payload(payload))
        if abm is None:
            return False
        event = plat.create_event(abm)
        now = time.time()
        event.set_extra(
            EXTRA,
            {
                "cause": kind, "scene": u["scene"], "peer": str(u["peer"]), "uid": uid, "name": u["name"], "role": u.get("role") or "",
                "how": u.get("how") or "", "lines": [[ln["t"], ln["text"]] for ln in u["lines"]], "from": t_from, "to": t_to,
                "unit": u.get("key") or unit_key(u["pid"], u["scene"], u["peer"], uid), "keys": keys, "mid": str(mid), "at": now,
                "line_keys": [list(ln.get("keys") or []) for ln in u["lines"]],
            },
        )
        self.tr.mark_done(keys, now)
        plat.commit_event(event)
        logger.info(
            f"{TAG} 补 {unit_where(u)}：{len(u['lines'])} 条（最早 {self._hhmm(u['first'])}）→ 交给模型回"
        )
        return True

    async def _self_id(self, pid, bot):
        if self._self_ids.get(pid):
            if pid not in self._nick:
                self._nick[pid] = ""
            return self._self_ids[pid]
        try:
            d = await self._call(bot, "get_login_info", timeout=5)
            self._self_ids[pid] = str((d or {}).get("user_id") or "")
            self._nick[pid] = str((d or {}).get("nickname") or "")
        except Exception:
            return ""
        return self._self_ids[pid]

    async def _convs(self, bot, pid, t_from, now, conf, pending=()):
        """这段时间里有新消息的对话 → [(scene, 对方)]：先问 NapCat 的最近会话；问不到就用自己记的「哪些对话有人说过话」"""
        out = []
        try:
            data = await self._call(bot, "get_recent_contact", count=50)
            if not isinstance(data, list):
                raise ValueError(f"最近会话不是列表：{type(data).__name__}")
            for c in data:
                try:
                    ct, peer, mt = int(c.get("chatType") or 0), str(c.get("peerUin") or ""), float(c.get("msgTime") or 0)
                except (TypeError, ValueError):
                    continue
                if peer.isdigit() and ct in (1, 2) and mt >= t_from - 5:
                    out.append(("private" if ct == 1 else "group", peer))
        except Exception as e:
            logger.info(f"{TAG} 问不到 NapCat 的最近会话（{e!r}）→ 改看自己记的对话"[:200])
            lim = max(t_from, now - conf["max_age_min"] * 60) - 3600
            for ck, t in sorted(self.tr.convs.items(), key=lambda kv: -kv[1]):
                p, _, rest = ck.partition("|")
                scene, _, peer = rest.partition("|")
                if p == pid and scene in ("private", "group") and t >= lim:
                    out.append((scene, peer))
                if len(out) >= 30:
                    break
        for r in pending:  # 上次关掉时正在等模型回的那几个对话也看一眼
            if r.get("pid") == pid and r.get("scene") in ("private", "group"):
                out.append((r["scene"], str(r.get("peer") or "")))
        return list(dict.fromkeys(x for x in out if x[1]))

    async def _history(self, bot, scene, peer, since):
        """从最新的往回翻聊天记录，翻到 since 以前为止（最多 3 页）"""
        api = "get_friend_msg_history" if scene == "private" else "get_group_msg_history"
        idk = {"user_id": int(peer)} if scene == "private" else {"group_id": int(peer)}
        count = 30 if scene == "private" else 50
        out, seen, seq = [], set(), 0
        for _ in range(3):
            d = await self._call(bot, api, **idk, message_seq=seq, count=count, reverseOrder=False)
            msgs = d.get("messages") if isinstance(d, dict) else (d if isinstance(d, list) else None)
            msgs = [m for m in (msgs or []) if isinstance(m, dict)]
            fresh = [m for m in msgs if m.get("message_id") not in seen]
            if not fresh:
                break
            seen.update(m.get("message_id") for m in fresh)
            out += fresh
            oldest = min(fresh, key=lambda m: float(m.get("time") or 0))
            if float(oldest.get("time") or 0) <= since or len(msgs) < count:
                break
            seq = oldest.get("message_seq") or oldest.get("message_id")
            if not seq:
                break
        return out

    # ───────── 模型挂了 → 好了以后补 ─────────
    def _settle(self, now):
        """找它的那几轮过了 ASK_TIMEOUT 秒还没结果：模型什么都没回（也没别人替它发出去）/ 活人感记了这个会话回不上 → 算没回上；
        别的 → 不知道（多半被别的插件拦了）不算"""
        for k in [k for k in self._empties if k not in self.tr.asking]:
            self._empties.pop(k, None)
        for eid, r in list(self.tr.asking.items()):
            t_req = float(r.get("t_req") or r.get("t") or now)
            if now - t_req < ASK_TIMEOUT:
                continue
            ev = self._empties.pop(eid, None)
            if not self._conf()["model_fail"]:
                self.tr.drop(eid)
                continue
            if r.get("empty") and getattr(ev, "_has_send_oper", False):
                self.tr.drop(eid)  # 模型空回，但有人替它发出去了（活人感补调了一次）：不算没回上
                continue
            said = self._alive_says(t_req - 2, t_req + ASK_TIMEOUT, r.get("umo"))
            if said == "fail" or (r.get("empty") and said is None):
                why = "活人感的兜底记了回不上" if said == "fail" else "模型什么都没回"
                if self.tr.fail(eid, why, now):
                    logger.info(f"{TAG} {r.get('name')}（{'私聊' if r.get('scene') == 'private' else '群 ' + str(r.get('peer'))}）这一轮没回上：{why} → 这个会话回上了一句再补")
            else:
                self.tr.drop(eid)

    def _session_back(self, umo, md=None):
        """这个会话回上了一句（它好了）：这个会话之前出错没回上的，等一会儿（让这一轮先发完）去补。别的会话不动"""
        if not self._conf()["model_fail"]:
            return
        mine = self.tr.failed_in(umo)
        if not mine:
            return
        if umo in self._sess_tasks and not self._sess_tasks[umo].done():
            return
        since = float((md or {}).get("since") or min(float(r.get("t") or time.time()) for r in mine))

        async def go():
            await asyncio.sleep(15)
            await self._run("model", since, time.time(), include_failed=True, only_pid="-", failed_umo=umo)

        logger.info(f"{TAG} {umo} 回上了一句（这个会话从 {self._hhmm(since)} 起出错）→ 15 秒后补这个会话没回上的 {len(mine)} 条")
        self._sess_tasks[umo] = self._spawn(go())

    # ───────── 读消息 ─────────
    @staticmethod
    def _raw(event):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return raw if hasattr(raw, "get") else None

    def _event_info(self, event):
        """找它的这一条 → 记录（不是找它的 → None）：私聊都算；群里 @ 它 / 引用它 / 叫它名字 / 唤醒词"""
        pid = event.get_platform_id()
        self_id = str(event.get_self_id() or "")
        uid = str(event.get_sender_id() or "")
        gid = str(event.get_group_id() or "")
        private = event.is_private_chat() or not gid
        parts, at_me, quoted = [], False, False
        for c in event.get_messages() or []:
            try:
                if isinstance(c, Comp.Plain):
                    parts.append(c.text or "")
                elif isinstance(c, Comp.At):
                    q = str(getattr(c, "qq", "") or "")
                    if q and q == self_id:
                        at_me = True
                    elif q and q != "all":
                        parts.append(f"@{getattr(c, 'name', '') or q} ")
                elif isinstance(c, Comp.Reply):
                    quoted = quoted or str(getattr(c, "sender_id", "") or "") == self_id
                elif isinstance(c, Comp.Image):
                    parts.append("[图片]")
                elif isinstance(c, Comp.Record):
                    parts.append("[语音]")
            except Exception:
                continue
        text = "".join(parts).strip()
        how = ""
        if not private:
            if at_me:
                how = "@ 了你"
            elif quoted:
                how = "引用了你的话"
            elif names_called(text, self._names(pid)):
                how = "叫了你的名字"
            else:
                try:
                    orig = str(getattr(event.message_obj, "message_str", "") or "").strip()
                    prefixes = self.context.get_config(umo=event.unified_msg_origin).get("wake_prefix") or []
                    if any(p and orig.startswith(p) for p in prefixes):
                        how = "叫了你"
                except Exception:
                    pass
            if not how:
                return None
        mid = str(getattr(event.message_obj, "message_id", "") or "")
        try:
            t = float(getattr(event.message_obj, "timestamp", 0) or time.time())
        except (TypeError, ValueError):
            t = time.time()
        raw = self._raw(event) or {}
        role = str(((raw.get("sender") or {}) if hasattr(raw, "get") else {}).get("role") or "")
        scene = "private" if private else "group"
        peer = uid if private else gid
        return {
            "key": f"{pid}:{mid}", "keys": [f"{pid}:{mid}"], "mid": mid, "pid": pid, "scene": scene, "peer": peer,
            "uid": uid, "name": event.get_sender_name() or uid, "role": role, "text": text or "[只 @ 了你]", "t": t,
            "t_req": time.time(), "unit": unit_key(pid, scene, peer, uid), "how": how, "self_id": self_id,
            "umo": event.unified_msg_origin,
        }

    def _note_failed(self, eid):
        now = time.time()
        self._failed_eids[eid] = now
        if len(self._failed_eids) > 200:
            for k in [k for k, t in self._failed_eids.items() if now - t > 600]:
                self._failed_eids.pop(k, None)

    @staticmethod
    def _plain(res):
        try:
            return "".join(c.text for c in (res.chain if res else []) if isinstance(c, Comp.Plain))
        except Exception:
            return ""

    # ───────── 钩子 ─────────
    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize - 3)
    async def on_message(self, event: AstrMessageEvent):
        """每条消息：记「收到过」、这个对话有人说话了、NapCat 还活着"""
        try:
            if not self._on():
                return
            self._start_loops()
            pid, now = event.get_platform_id(), time.time()
            self._napcat_seen[pid] = now
            sid = str(event.get_self_id() or "")
            if sid and sid.isdigit():
                self._self_ids.setdefault(pid, sid)
            raw = self._raw(event)
            if raw is not None and raw.get("post_type") != "message":
                return  # 通知（撤回 / 戳一戳 / 禁言…）不是有人说话
            if event.get_extra(EXTRA):
                return  # 自己合成的那条
            info = self._event_info(event)
            if info and info["mid"]:
                # 只记找它的（私聊、群里 @ / 引用 / 叫名字）：群里别人之间聊天本来就不补，记了只是占地方
                self.tr.heard(info["key"], now)
            gid = str(event.get_group_id() or "")
            if gid and not event.is_private_chat():
                self.tr.conv(f"{pid}|group|{gid}", now)
            else:
                self.tr.conv(f"{pid}|private|{event.get_sender_id()}", now)
        except Exception as e:
            logger.warning(f"{TAG} 记消息出错（这条照常）：{e!r}"[:200])

    @filter.on_llm_request(priority=30)
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            if not self._on():
                return
            now = time.time()
            info = event.get_extra(EXTRA)
            if info and TextPart is not None:
                req.extra_user_content_parts.append(TextPart(text=model_note(info, now, self._tz())).mark_as_temp())
            rec = self._event_info(event)
            if info and rec:
                # 补回的这一轮：记下这一轮带的几条（又没回上的话，下次补的还是这几条，不只最后一句）
                rec.update(keys=list(info.get("keys") or []), t=float(info["lines"][0][0]), text=info["lines"][-1][1],
                           unit=info.get("unit") or rec["unit"], lines=[list(x) for x in info["lines"]],
                           line_keys=list(info.get("line_keys") or []))
            elif rec:
                self._fold(rec, req, now)
            if rec:
                self.tr.ask(id(event), rec)
        except Exception as e:
            logger.warning(f"{TAG} 回话请求那一步出错（这一轮照常）：{e!r}"[:200])

    def _fold(self, rec, req, now):
        """他本人又来找它了：他之前模型出错没回上的那几条并进这一轮一起交给模型（临时段；不另补一次）。
        先占着：这一轮回上了算补过；没回上 / 被拦了放回去"""
        conf = self._conf()
        if not conf["model_fail"] or TextPart is None:
            return
        prefixes, cmds = self._cmd_prefixes(), self._cmd_names()
        recs = [r for r in self.tr.fold_for(rec["unit"], now, conf["max_age_min"] * 60)
                if rec["key"] not in (r.get("keys") or [r.get("key")])]
        lines = sorted((t, x) for r in recs for t, x, _ in rec_lines(r) if not is_cmd(x, prefixes, cmds))
        if not lines:
            return
        lines = lines[-conf["max_lines"]:]
        keys = sorted({k for r in recs for k in (r.get("keys") or [r.get("key")]) if k})
        info = {"cause": "model", "fold": True, "scene": rec["scene"], "name": rec["name"], "how": rec["how"],
                "lines": [[t, x] for t, x in lines]}
        req.extra_user_content_parts.append(TextPart(text=model_note(info, now, self._tz())).mark_as_temp())
        rec["folded"] = keys
        self.tr.fold_start(keys, now)
        logger.info(f"{TAG} {unit_where(rec)} 又来找它了：他之前没回上的 {len(lines)} 条并进这一轮一起回")

    @filter.on_llm_response(priority=sys.maxsize - 5)
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            if not self._on():
                return
            role = str(getattr(resp, "role", "") or "")
            text = str(getattr(resp, "completion_text", "") or "")
            eid, umo = id(event), event.unified_msg_origin
            if role == "err":
                self._note_failed(eid)  # 之后发出去的（报错原文 / 活人感换上的系统提示）不算回过他
                if not self._conf()["model_fail"]:
                    self.tr.drop(eid)  # 「模型出错没回上的也补」关了：不记
                    return
                r = self.tr.fail(eid, "模型报错：" + text.strip()[:60])
                if r:
                    logger.info(f"{TAG} {r.get('name')} 这一轮模型报错没回上 → 这个会话回上了一句再补")
                return
            chain = getattr(resp, "result_chain", None)
            has = bool(text.strip()) or bool(getattr(chain, "chain", None)) or bool(getattr(resp, "tools_call_name", None))
            if role == "assistant" and has:
                md = self.tr.session_ok(umo)  # 按会话算：这个会话回上了一句 = 这个会话好了（别的会话不动）
                self._session_back(umo, md)
            elif role == "assistant" and not getattr(event, "_has_send_oper", False):
                r = self.tr.asking_rec(eid)
                if r is not None:
                    r["empty"] = True  # 什么都没回（到时候再看一眼有没有别人替它发出去）
                    self._empties[eid] = event
        except Exception as e:
            logger.warning(f"{TAG} 模型回完那一步出错：{e!r}"[:200])

    @filter.on_decorating_result(priority=sys.maxsize)
    async def on_decorate_first(self, event: AstrMessageEvent):
        """框架把报错原样发出来了：这一轮没回上（排在活人感的兜底前面 —— 它拦下之前先看到）"""
        try:
            if not self._on():
                return
            eid = id(event)
            if self.tr.asking_rec(eid) is None:
                return
            if ERR_HEAD.match(self._plain(event.get_result())):
                self._note_failed(eid)
                if not self._conf()["model_fail"]:
                    self.tr.drop(eid)  # 「模型出错没回上的也补」关了：不记
                    return
                r = self.tr.fail(eid, "框架报错：" + self._plain(event.get_result()).strip()[:60])
                if r:
                    logger.info(f"{TAG} {r.get('name')} 这一轮模型出错没回上（框架报错）→ 这个会话回上了一句再补")
        except Exception as e:
            logger.warning(f"{TAG} 发送前那一步出错：{e!r}"[:200])

    @filter.on_decorating_result(priority=-1000)
    async def on_decorate_late(self, event: AstrMessageEvent):
        """补回的这一轮：这期间它已经回过他了就不发；群里 @ 他"""
        try:
            info = event.get_extra(EXTRA)
            if not info or not self._on():
                return
            res = event.get_result()
            if res is None or not res.chain:
                return
            if self.tr.replied_after(info.get("unit") or "", float(info.get("at") or 0)):
                release_angel(self.context, event, "补回的这期间已经回过他了")
                event.set_result(MessageEventResult())
                event.stop_event()
                logger.info(f"{TAG} {info.get('name')}：补回的这期间它已经回过他了 → 补的这句不发")
                return
            if info.get("scene") != "group":
                return
            uid = str(info.get("uid") or "")
            if any(isinstance(c, Comp.At) and str(getattr(c, "qq", "")) == uid for c in res.chain):
                return
            ps = self.context.get_config(umo=event.unified_msg_origin).get("platform_settings") or {}
            if ps.get("reply_with_mention"):
                return  # AstrBot 自己会 @ 他
            if not all(isinstance(c, (Comp.Plain, Comp.Image)) for c in res.chain):
                return  # 跟 AstrBot 一样：只给纯文字 / 图文加 @
            res.chain.insert(0, Comp.At(qq=uid, name=str(info.get("name") or "")))
            if len(res.chain) > 1 and isinstance(res.chain[1], Comp.Plain):
                res.chain[1].text = " " + (res.chain[1].text or "").lstrip()
            if ps.get("reply_with_quote") and info.get("mid"):
                res.chain.insert(0, Comp.Reply(id=info["mid"]))  # 加了 @ 以后 AstrBot 不再加引用：替它加上
        except Exception as e:
            logger.warning(f"{TAG} 补回的这一轮加 @ 出错（照常发）：{e!r}"[:200])

    @filter.after_message_sent(priority=-1000)
    async def after_sent(self, event: AstrMessageEvent):
        """发出去了：这个对话它回上了"""
        try:
            if not self._on():
                return
            if self._failed_eids.pop(id(event), None) is not None:
                return  # 这一轮没回上（发出去的是报错 / 活人感换上的系统提示），不算回过他
            res = event.get_result()
            text = self._plain(res)
            if not res or not res.chain or ERR_HEAD.match(text):
                return
            info = self._event_info(event)
            unit = (event.get_extra(EXTRA) or {}).get("unit") or (info or {}).get("unit")
            if not unit:
                pid, uid, gid = event.get_platform_id(), str(event.get_sender_id() or ""), str(event.get_group_id() or "")
                unit = unit_key(pid, "group" if gid else "private", gid or uid, uid)
            self.tr.ok(id(event), unit)
        except Exception as e:
            logger.warning(f"{TAG} 发完那一步出错：{e!r}"[:200])

    @filter.on_platform_loaded()
    async def on_platform_loaded(self):
        """平台刚建好：挂上 NapCat 的元事件（插件比平台先加载，initialize 那会儿还没有平台）"""
        try:
            self._subscribe()
        except Exception as e:
            logger.warning(f"{TAG} 挂 NapCat 元事件出错：{e!r}"[:200])

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        try:
            self._subscribe()
        except Exception as e:
            logger.warning(f"{TAG} 挂 NapCat 元事件出错：{e!r}"[:200])

    # ───────── 指令 ─────────
    @filter.command("补回")
    async def cmd(self, event: AstrMessageEvent, op: str = ""):
        """/补回：最近一次掉线多久、补了哪些对话；AstrBot 管理员 /补回 现在：手动补一次"""
        op = str(op or "").strip()
        try:
            if op in ("现在", "马上", "立刻"):
                if not event.is_admin():
                    yield event.plain_result("只有 AstrBot 管理员能手动补。")
                    return
                n, line = await self._manual()
                yield event.plain_result(line)
                return
            if op:
                yield event.plain_result("用法：/补回（看最近一次掉线补了什么）；AstrBot 管理员：/补回 现在")
                return
            yield event.plain_result(status_text(self.tr, self._conf(), time.time(), self._tz(), VERSION, self._on()))
        except Exception as e:
            logger.warning(f"{TAG} 指令出错：{e!r}"[:200])
            yield event.plain_result("出错了，看日志。")

    async def _manual(self):
        """手动补一次：最近「最长补多久」以内的（装上以前的不算 —— 那时候没记「收到过」，分不清哪些是故意没回的）"""
        now = time.time()
        t_from = max(now - self._conf()["max_age_min"] * 60, self.tr.installed)
        return await self._run("manual", t_from, now, include_failed=True)

    # ───────── 面板（AstrBot WebUI → 插件 → 打开页面）─────────
    def _register_web(self):
        try:
            if importlib.util.find_spec("astrbot.api.web") is None:  # 4.29 以后才有：老版本就不开面板
                return
        except Exception:
            return
        self.context.register_web_api(f"/{PLUGIN_NAME}/state", self.web_state, ["GET"], "掉线补回：状态")
        self.context.register_web_api(f"/{PLUGIN_NAME}/action", self.web_action, ["POST"], "掉线补回：手动补一次")

    def snapshot(self):
        now, tz = time.time(), self._tz()
        from .core.notes import hhmm

        return {
            "version": VERSION,
            "enabled": self._on(),
            "config": self._conf(),
            "installed": self.tr.installed,
            "napcat": [{"pid": p, "seen": t, "seen_text": hhmm(t, now, tz)} for p, t in self._napcat_seen.items()],
            "hooked": bool(self._subs),
            "sess_down": len(self.tr.sess_down),
            "failed_sessions": len({str(r.get("umo") or "") for r in self.tr.failed}),
            "failed": [
                {"where": unit_where(r), "t": r.get("t"), "text": str(r.get("text") or "")[:60], "why": r.get("why", "")}
                for r in self.tr.failed[-20:]
            ],
            "outages": [
                dict(o, kind_name=KIND_NAME.get(o.get("kind"), o.get("kind")), at_text=hhmm(o.get("at") or now, now, tz),
                     from_text=hhmm(o["from"], now, tz) if o.get("from") else "", to_text=hhmm(o["to"], now, tz) if o.get("to") else "",
                     skipped_text=skipped_text(o.get("skipped")))
                for o in self.tr.outages[::-1]
            ],
            "status": status_text(self.tr, self._conf(), now, tz, VERSION, self._on()),
        }

    async def web_state(self):
        return {"status": "ok", "data": self.snapshot()}

    async def web_action(self):
        from astrbot.api.web import request

        body = await request.json({})
        body = body if isinstance(body, dict) else {}
        if str(body.get("op") or "") != "run":
            return {"status": "error", "message": "不认识的操作"}
        if self._lock.locked():
            return {"status": "error", "message": "正在补，等这一次补完"}
        n, line = await self._manual()
        return {"status": "ok", "data": dict(self.snapshot(), last=line)}
