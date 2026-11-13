# -*- coding: utf-8 -*-
"""SE 过滤门（V7 外挂的第二段，接在主程序 _output_gate 后面）

为什么需要它：本机实测，7B 对 prompt 里的「规则/示范」服从率极低（换话题就失效）。
但代码层句级删除是 100% 生效的——您 V6 的「酒句」就是这么治的。

2026-11-04 改（V7.1）：**不再删句**。用户原话「不删，但对问题句加惩罚」——
7B 真正会照着念的通道是「记忆里逐字相同的旧对话」（18 篇实验 3），所以：
  云端复盘用同一人设给每句对话生成「参考句」，再拿它对比原句写出判词；
  运行时这句话被记忆/上下文召回时，紧跟着附一段批注（判词 + 参考句），
  让 7B 自己看见「这句当时不对，该这么说」。默认不再执行 apply() 删句。

设计约束：
  1. annotate() 只做「插批注」，不改写、不生成、不删句；
  2. apply() 保留（环境变量 SE_DELETE=1 才被主程序调用），默认不参与；
  3. 规则文件按 mtime 热加载，改完立即生效，不用重启主程序；
  4. 任何异常都原样返回，绝不打断聊天；
  5. 不 import 主程序任何模块，只读 SE/persona_rules.json 与 SE/judges.json。

对外：apply(text) -> str（删句，默认不启用）
      annotate(text) -> str（插批注，V7.1 主用）
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
JUDGES_PATH = Path(os.getenv("SE_JUDGES_PATH") or (SE_HOME / "judges.json"))
HITS_LOG = SE_HOME / "se_hits.log"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

log = logging.getLogger("se_filter")

# 句子切分：句末 + 逗号 + 换行都算边界。
# 为什么连逗号也切：7B 常见句子是「好的呢，本助手去查查。」——
# 只按句号切会把整句删掉，连正话一起没了。按小句切才能「删客套、留正话」。
_SPLIT_RE = re.compile(r"(?<=[。！？!?…~～；;，,\n])")

_CACHE = {"key": None, "words": []}
_JUDGE_CACHE = {"key": None, "items": []}

ANNOTATE_MARK = "※ 复盘"            # 已经批注过就不再批；也用这个当「别当台词抄」的记号
MAX_ANNOTATE = 2                     # 一段记忆里最多插几条，别把 prompt 撑爆



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


def _load_judges() -> list[dict]:
    """返回判词表 [{本助手, 参考, 判词, 类型}]；按 mtime 热加载，长的排前面。"""
    try:
        key = JUDGES_PATH.stat().st_mtime
    except OSError:
        return []
    if key == _JUDGE_CACHE["key"]:
        return _JUDGE_CACHE["items"]
    items = []
    try:
        data = json.loads(JUDGES_PATH.read_text(encoding="utf-8"))
        for r in (data.get("判词") or []):
            if not isinstance(r, dict) or r.get("status", "启用") != "启用":
                continue
            bad = str(r.get("本助手") or "").strip(" 　")
            note = str(r.get("判词") or "").strip()
            if len(bad) >= 6 and note:                 # 太短的句子没法定向，直接跳过
                items.append({"本助手": bad,
                              "参考": str(r.get("参考") or "").strip(),
                              "判词": note,
                              "类型": str(r.get("类型") or "").strip()})
    except Exception as e:
        log.warning("读判词库失败（本段批注跳过）：%s", e)
        items = []
    items.sort(key=lambda x: len(x["本助手"]), reverse=True)   # 长句先匹配，别让短句抢位
    _JUDGE_CACHE.update(key=key, items=items)
    return items


def _find_sentence(text: str, bad: str) -> str:
    """在 text 里找这句话。7B 复述/记忆截断很常见，所以整句找不到就退到前缀。"""
    if bad and bad in text:
        return bad
    for n in (24, 20, 16, 12, 8):
        head = bad[:n]
        if len(head) >= 6 and head in text:
            return head
    return ""


def _sentence_end(text: str, start: int) -> int:
    """从 start 往后找到这句的结尾（句末标点/换行），返回批注的插入点。"""
    for i in range(start, len(text)):
        ch = text[i]
        if ch in "。！？!?…":
            return i + 1
        if ch == "\n":
            return i                            # 批注贴着句子，放到换行前
    return len(text)


def annotate(text: str) -> str:
    """给「问题句」后面补一条批注（判词 + 参考句）。

    V7.1 的修复动作：**不删句**，只在这句后面加一句批注让 7B 自己看见
    「这句当时不对，该这么说」。依据：18 篇实验 3 —— 7B 会照念「逐字相同的旧对话」，
    而记忆召回正好就是逐字相同的旧对话，这是它唯一真的会听的通道。
    """
    try:
        t = str(text or "")
        if not t.strip():
            return t
        items = _load_judges()
        if not items:
            return t
        added = 0
        for it in items:
            if added >= MAX_ANNOTATE:
                break
            hit = _find_sentence(t, it["本助手"])
            if not hit:
                continue
            pos = t.find(hit)
            if pos < 0:
                continue
            raw_end = pos + len(hit)
            seg = t[raw_end:raw_end + 6]
            if ANNOTATE_MARK in seg or ANNOTATE_MARK in t[raw_end:raw_end + 8]:
                continue                            # 紧跟着就是批注 → 已经批过了
            end = _sentence_end(t, raw_end)
            if ANNOTATE_MARK in t[raw_end:end]:
                continue                            # 这句尾巴里已经有批注
            note = "%s（%s）：%s" % (
                ANNOTATE_MARK, it["类型"] or "毛病", it["判词"].rstrip("。"))
            if it.get("参考"):
                note += "。该说「%s」" % it["参考"]
            t = t[:end] + " " + note + t[end:]
            added += 1
            log.info("SE 批注：%s → 在「%s」后补判词", it["类型"] or "其他", hit[:16])
        return t
    except Exception as e:
        log.warning("SE 批注异常（原样放行）：%s", e)
        return str(text or "")


# 出口清洗（2026-11-04 12:10 加）：不给它「照抄批注」的机会。
# 真事：11:30 三条回复里 7B 自己伪造了「（复盘批注/重复：…）」并跟着发出去，还编了「重复」「内鬼」两个不存在的类型
# ——正是 18 篇说的「7B 会把看到的格式原样抄出来」。所以在出口无条件把这玩意儿抹掉。
_NOTE_MARK = re.compile(r"[ \t]*※\s*复盘[^\n]*")               # 现在的批注格式（7B 抄出来就抹）
_NOTE_BLOCK = re.compile(r"[（(]\s*复盘批注[^\n]*[）)]")     # 老格式（2026-11-04 12:10 之前）
_NOTE_TAIL = re.compile(r"[（(]?\s*复盘批注[^\n）)]*[）)]?")  # 没配对的残尾 / 裸的
_NOTE_ANY = re.compile(r"复盘批注[^\n]{0,160}")               # 兜底：只剩关键词
_NOTE_ALERT = re.compile(r"【复盘提醒[^】]*】[^\n]*")           # System 侧提醒块被抄出来
_REF_TAIL = re.compile(r"[，、；,;]?\s*(?:这句)?该这么说[:：]?\s*「[^」]{0,200}」[。]?")  # 批注里那半句常被单独抄出来


def recent_note(messages, limit: int = 2) -> str:
    """给「刚说错的那几句」生成一条提醒块——塞进 system，**不塞回鱼自己的台词里**。

    为什么单独做：11:30 那次 7B 把「（复盘批注/…）」当自己的话往下接，自己编了「重复」「内鬼」两个类型。
    把批注挂在 assistant 的台词后面，等于教它接着这么写；改成 System 侧的提醒块，照抄的动机小很多。
    """
    try:
        items = _load_judges()
        if not items or not messages:
            return ""
        lines = []
        for role, content in reversed(list(messages)):
            if role != "assistant" or not content:
                continue
            for it in items:
                if it["本助手"] and _find_sentence(str(content), it["本助手"]):
                    lines.append("· 你刚才那句「%s」不行（%s）：%s%s" % (
                        it["本助手"][:26], it["类型"] or "毛病", it["判词"].rstrip("。"),
                        "。该说：「%s」" % it["参考"] if it["参考"] else ""))
                    break
            if len(lines) >= limit:
                break
        if not lines:
            return ""
        return "【复盘提醒（你自己刚说过的话，别照抄这句批注）】\n" + "\n".join(lines)
    except Exception as e:
        log.warning("recent_note 异常（跳过）：%s", e)
        return ""


def strip_notes(text: str) -> str:
    """把回复里任何「※ 复盘…」/「（复盘批注…）」抹掉（含 7B 自己伪造的）。抹完只剩标点就返回空。"""
    t = str(text or "")
    if not t:
        return t
    out = t
    if "※" in out:                                    # 现在的格式
        out = _NOTE_MARK.sub("", out)
        if "※" in out:
            out = re.sub(r"※[^\n]{0,160}", "", out)
    if "复盘批注" in out:                              # 老格式（兼容）
        out = _NOTE_BLOCK.sub("", out)
        if "复盘批注" in out:
            out = _NOTE_TAIL.sub("", out)
        if "复盘批注" in out:
            out = _NOTE_ANY.sub("", out)
    if "复盘提醒" in out:                              # System 侧提醒块被抄出来
        out = _NOTE_ALERT.sub("", out)
        out = re.sub(r"复盘提醒[^\n]{0,120}", "", out)
    if "该这么说" in out:                              # 只抄了后半截的
        out = _REF_TAIL.sub("", out)
    out = re.sub(r"([（(])\s*[）)]", "", out)          # 空括号
    out = re.sub(r"([，、；;])\s*([。！？!?])", r"\2", out)
    out = re.sub(r"([。！？!?…~～])\s*。", r"\1", out)   # 「活泼就好！。」→「活泼就好！」
    out = re.sub(r"。。+", "。", out)
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    out = re.sub(r"[，、；;,]+$", "。", out)            # 尾巴上剩个逗号，补成句号
    if out and not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", out):
        return ""                                     # 只剩「」。）这种残渣 → 当没说
    return out


def _self_test() -> int:
    import tempfile
    global RULES_PATH, JUDGES_PATH
    total, fails = 0, []

    def check(cond, name):
        nonlocal total
        total += 1
        if not cond:
            fails.append(name)

    check(apply("") == "" and apply("没有规则时原样") == "没有规则时原样", "无规则时放行")
    check(annotate("") == "" and annotate("没有判词时原样") == "没有判词时原样", "无判词时放行")
    old = RULES_PATH
    old_j = JUDGES_PATH
    try:
        tmp = Path(tempfile.mkdtemp())
        RULES_PATH = tmp / "r.json"
        JUDGES_PATH = tmp / "j.json"
        RULES_PATH.write_text(json.dumps({"规则": [
            {"id": "R001", "status": "启用", "禁用词": ["谢谢你夸奖", "好的呢"]},
            {"id": "R002", "status": "弃用", "禁用词": ["下雨了"]},
        ]}, ensure_ascii=False), encoding="utf-8")
        _CACHE["key"] = None
        out = apply("谢谢你夸奖。本助手今天状态不错！")
        check(out.strip() == "本助手今天状态不错！", "删句：客套句被整句删掉，正话保留")
        check(apply("好的呢，本助手去查查。").strip() == "本助手去查查。", "删句：句首客套被删")
        check(apply("下雨了，记得带伞。") == "下雨了，记得带伞。", "弃用规则不生效")
        check(apply("谢谢你夸奖。").strip() == "谢谢你夸奖。", "全删光→原样放行")
        RULES_PATH.write_text(json.dumps({"规则": []}, ensure_ascii=False), encoding="utf-8")
        _CACHE["key"] = None
        check(apply("谢谢你夸奖。") == "谢谢你夸奖。", "规则清空后立即失效（热加载）")

        # ---- V7.1 批注 ----
        JUDGES_PATH.write_text(json.dumps({"判词": [{
            "本助手": "无聊的话我们聊点别的，比如你最近有啥兴趣爱好或者想干的事儿？",
            "参考": "那就别聊考试，本助手陪你瞎扯。",
            "判词": "这句让主人不开心：又把话头抛回去问他",
            "类型": "追问", "status": "启用"}]}, ensure_ascii=False), encoding="utf-8")
        _JUDGE_CACHE["key"] = None
        mem = ("用户：考试好无聊的，没啥好聊的\n"
               "助手：无聊的话我们聊点别的，比如你最近有啥兴趣爱好或者想干的事儿？\n")
        a1 = annotate(mem)
        check(("※ 复盘" in a1) and ("本助手陪你瞎扯" in a1), "批注：命中原句后附上判词+参考句")
        check(a1.count("※ 复盘") == 1, "批注：不会重复插")
        check(annotate(a1).count("※ 复盘") == 1, "批注：已经批过的不再批")
        cut = "助手：无聊的话我们聊点别的，比如你最近有\n"
        check("※ 复盘" in annotate(cut), "批注：原句被截断也认得（前 12 字匹配）")
        check(annotate("用户：今天下雨了\n助手：记得带伞。\n") ==
              "用户：今天下雨了\n助手：记得带伞。\n", "批注：没命中就一字不动")
        JUDGES_PATH.write_text(json.dumps({"判词": [
            {"本助手": "这句不该批", "判词": "太短", "status": "启用"},
            {"本助手": "无聊的话我们聊点别的，比如你最近有啥兴趣爱好或者想干的事儿？",
             "参考": "x", "判词": "y", "status": "弃用"}]}, ensure_ascii=False), encoding="utf-8")
        _JUDGE_CACHE["key"] = None
        check(annotate(mem) == mem, "批注：太短的/弃用的都不参与")

        # ---- 出口清洗（7B 自己伪造的批注也要抹掉）----
        check(strip_notes("本助手就是嘴硬心软嘛。（复盘批注/内鬼：「也有不小心的时候」显得不自然。）") ==
              "本助手就是嘴硬心软嘛。", "清洗：抹掉模型伪造的内联批注")
        check(strip_notes("活泼就好！（复盘批注/重复：前后两句话完全一样，显得不自然。这句该这么说："
                          "「那本助手这就去上网，回头给你讲点有趣的事儿。」）。") == "活泼就好！",
              "清洗：带「这句该这么说」和多余句号的残尾也抹干净")
        check(strip_notes("哈哈。（复盘批注/内鬼：随便说说") == "哈哈。", "清洗：括号没闭合的残尾也抹掉")
        check(strip_notes("今天很安静。") == "今天很安静。", "清洗：没批注一字不动")
        check(strip_notes("（复盘批注/重复：全是批注）") == "", "清洗：整条都是批注 → 返回空")
        check(strip_notes("行吧，本助手知道了。这句该这么说：「嗯，记下了。」") == "行吧，本助手知道了。",
              "清洗：只抄了后半截「该这么说」也抹掉")
    finally:
        RULES_PATH = old
        JUDGES_PATH = old_j
        _CACHE["key"] = None
        _JUDGE_CACHE["key"] = None

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
    if "--judges" in a:
        items = _load_judges()
        if not items:
            print("（判词库是空的）")
        for it in items:
            print("\n[%s] 病句：%s" % (it.get("类型") or "其他", it["本助手"]))
            print("       判词：%s" % it["判词"])
            print("       该说：%s" % (it.get("参考") or "（空）"))
        sys.exit(0)
    if "--annotate" in a:
        i = a.index("--annotate")
        sample = a[i + 1] if i + 1 < len(a) else "助手：今天天气不错。"
        print("输入：%s" % sample)
        print("输出：%s" % annotate(sample))
        sys.exit(0)
    sample = a[0] if a else "谢谢你夸奖。本助手今天状态不错！"
    print("输入：%s" % sample)
    print("输出：%s" % apply(sample))
