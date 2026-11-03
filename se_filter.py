# -*- coding: utf-8 -*-
"""SE 过滤门（V7 外挂的第二段，接在主程序 _output_gate 后面）

为什么需要它：本机实测，7B 对 prompt 里的「规则/示范」服从率极低（换话题就失效）。
但代码层句级删除是 100% 生效的——您 V6 的「酒句」就是这么治的。
所以 SE 的产出改成「禁用词表」，由本文件在输出后强制执行。

设计约束：
  1. 只做「删句」，不改写、不生成，避免把回复越洗越怪；
  2. 全删光就原样放行（宁可漏洗，不能吞回复——和 OutputGate 同一条铁律）；
  3. 规则文件按 mtime 热加载，改完立即生效，不用重启主程序；
  4. 任何异常都原样返回，绝不打断聊天；
  5. 不 import 主程序任何模块，只读 SE/persona_rules.json。

对外：apply(text) -> str
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

SE_HOME = Path(__file__).resolve().parent / "SE"
RULES_PATH = Path(os.getenv("SE_RULES_PATH") or (SE_HOME / "persona_rules.json"))
HITS_LOG = SE_HOME / "se_hits.log"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

log = logging.getLogger("se_filter")

# 句子切分：句末 + 逗号 + 换行都算边界。
# 为什么连逗号也切：7B 常见句子是「好的呢，本鱼去查查。」——
# 只按句号切会把整句删掉，连正话一起没了。按小句切才能「删客套、留正话」。
_SPLIT_RE = re.compile(r"(?<=[。！？!?…~～；;，,\n])")

_CACHE = {"key": None, "words": []}


def _load_words() -> list[tuple[str, str]]:
    """返回 [(禁用词, 规则id), ...]；按 mtime 热加载。"""
    try:
        key = RULES_PATH.stat().st_mtime
    except OSError:
        return []
    if key == _CACHE["key"]:
        return _CACHE["words"]
    words = []
    try:
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        for r in (data.get("规则") or []):
            if not isinstance(r, dict) or r.get("status", "启用") != "启用":
                continue
            rid = str(r.get("id", ""))
            for w in (r.get("禁用词") or []):
                w = str(w or "").strip()
                if 2 <= len(w) <= 12:
                    words.append((w, rid))
    except Exception as e:
        log.warning("读规则失败（本段过滤跳过）：%s", e)
        words = []
    _CACHE.update(key=key, words=words)
    return words


def _hit(word: str, rid: str, text: str):
    try:
        HITS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(HITS_LOG, "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\n" % (rid, word, text[:40].replace("\n", " ")))
    except Exception:
        pass


def apply(text: str) -> str:
    """把含禁用词的句子整句删掉。异常/全删光时原样返回。"""
    try:
        t = str(text or "")
        if not t.strip():
            return t
        words = _load_words()
        if not words:
            return t
        parts = [p for p in _SPLIT_RE.split(t) if p]
        kept, dropped = [], []
        for p in parts:
            low = p.lower()
            bad = next(((w, rid) for w, rid in words if w and w.lower() in low), None)
            if bad:
                dropped.append(bad)
            else:
                kept.append(p)
        if not dropped:
            return t
        out = "".join(kept).strip()
        if not out:                       # 全删光：宁可不洗，也不能让鱼哑火
            log.info("过滤会把整条删光，原样放行：%r", t[:40])
            return t
        for w, rid in dropped[:6]:
            _hit(w, rid, t)
        log.info("SE 过滤：删 %d 句 %r → %r", len(dropped), t[:40], out[:40])
        return out
    except Exception as e:
        log.warning("SE 过滤异常（原样放行）：%s", e)
        return str(text or "")


def _self_test() -> int:
    import tempfile
    global RULES_PATH
    total, fails = 0, []

    def check(cond, name):
        nonlocal total
        total += 1
        if not cond:
            fails.append(name)

    check(apply("") == "" and apply("没有规则时原样") == "没有规则时原样", "无规则时放行")
    old = RULES_PATH
    try:
        tmp = Path(tempfile.mkdtemp())
        RULES_PATH = tmp / "r.json"
        RULES_PATH.write_text(json.dumps({"规则": [
            {"id": "R001", "status": "启用", "禁用词": ["谢谢你夸奖", "好的呢"]},
            {"id": "R002", "status": "弃用", "禁用词": ["下雨了"]},
        ]}, ensure_ascii=False), encoding="utf-8")
        _CACHE["key"] = None
        out = apply("谢谢你夸奖。本鱼今天状态不错！")
        check(out.strip() == "本鱼今天状态不错！", "删句：客套句被整句删掉，正话保留")
        check(apply("好的呢，本鱼去查查。").strip() == "本鱼去查查。", "删句：句首客套被删")
        check(apply("下雨了，记得带伞。") == "下雨了，记得带伞。", "弃用规则不生效")
        check(apply("谢谢你夸奖。").strip() == "谢谢你夸奖。", "全删光→原样放行")
        RULES_PATH.write_text(json.dumps({"规则": []}, ensure_ascii=False), encoding="utf-8")
        _CACHE["key"] = None
        check(apply("谢谢你夸奖。") == "谢谢你夸奖。", "规则清空后立即失效（热加载）")
    finally:
        RULES_PATH = old
        _CACHE["key"] = None

    print("se_filter 自测 %d/%d 通过" % (total - len(fails), total))
    for f in fails:
        print("  × " + f)
    return 0 if not fails else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--self-test" in a:
        sys.exit(_self_test())
    if "--words" in a:
        for w, rid in _load_words():
            print("%s  %s" % (rid, w))
        sys.exit(0)
    sample = a[0] if a else "谢谢你夸奖。本鱼今天状态不错！"
    print("输入：%s" % sample)
    print("输出：%s" % apply(sample))
