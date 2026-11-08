# -*- coding: utf-8 -*-
"""读配置：开关认「开 / on / 1」这些写法，数字写坏了用默认，名单认逗号 / 空格分开的字符串。不依赖 AstrBot。"""

import re


def truthy(v, default=True):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "开")


def num(v, default, lo=None, hi=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float(default)
    if x != x:  # NaN
        return float(default)
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def str_list(v):
    """["小鱼", "鱼鱼"] 或 "小鱼，鱼鱼" → ["小鱼", "鱼鱼"]"""
    if v is None:
        return []
    if isinstance(v, str):
        v = re.split(r"[,，、\s]+", v)
    try:
        return [str(x).strip() for x in v if str(x).strip()]
    except TypeError:
        return []
