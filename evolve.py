# -*- coding: utf-8 -*-
"""SE (Soul-Evolver) · V7 外挂进化模块

设计原则（老板定的）：
  1. 完全外部独立：不 import 主程序任何模块，不改主程序一行代码；
  2. 复盘靠云端大模型（它擅长这个），落地靠本地文本（7B 只负责听话）；
  3. 症状-解药极简字典，永久沉淀；错题本记 fail_count，治不好就换说法；
  4. 写前必备份，一键可撤销，出任何异常都只记日志、绝不写坏规则。

B 方案注入通道（零改代码）：
  memory_core_brief.md 每轮都会进本地 7B 的 system 记忆块，且永不发往云端。
  本模块只在其中维护一个带标记的「托管区块」，原文一字不动。
  缺点（已知并接受）：brief 是启动时读一次的常量，写完要重启机器人才生效。

对外（给将来的 A/C 方案留的口子，B 方案当前不调用）：
  get_persona_injection(keyword) -> str   # 纯字典查表，<0.1ms，永不抛异常

命令行：
  python evolve.py --self-test        # 自测，不联网、不写盘
  python evolve.py --dry-run          # 干跑：读日志+拼prompt，不调API、不写盘
  python evolve.py --review           # 真复盘（到点才跑；--force 强制）
  python evolve.py --show             # 看规则库
  python evolve.py --status           # 看生效状态（是否待重启）
  python evolve.py --preview          # 看现在会写进 brief 的那段字
  python evolve.py --undo             # 撤销到上一版（规则+brief 一起回退）
  python evolve.py --uninstall        # 摘掉 brief 里的托管区块，恢复原样
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# ============================ 0. 路径 ============================
SE_HOME = Path(__file__).resolve().parent
BOT_HOME = Path(os.getenv("FEISHU_BOT_HOME") or SE_HOME.parent).resolve()

CONFIG_PATH = SE_HOME / "evolve_config.json"
RULES_PATH = SE_HOME / "persona_rules.json"
BAK_PATH = SE_HOME / "persona_rules.bak"
STATE_PATH = SE_HOME / "se_state.json"
ARCHIVE_DIR = SE_HOME / "archive"
SAMPLE_DIR = SE_HOME / "se_samples"

MEM_DIR = Path(os.getenv("SE_MEM_DIR") or (BOT_HOME / "Mem_Data"))
BOT_LOG = Path(os.getenv("SE_BOT_LOG") or (BOT_HOME / "bot_v5.log"))
BRIEF_PATH = Path(os.getenv("SE_BRIEF_PATH") or (BOT_HOME / "memory_core_brief.md"))


def _bot_log() -> Path:
    """主程序日志：显式环境变量优先，否则按配置里的候选名找第一个存在的。"""
    if os.getenv("SE_BOT_LOG"):
        return BOT_LOG
    try:
        names = (load_config().get("review") or {}).get("log_candidates") or ["bot_v5.log"]
        for name in names:
            p = BOT_HOME / str(name)
            if p.exists():
                return p
    except Exception:
        pass
    return BOT_LOG
BRIEF_BAK = SE_HOME / "memory_core_brief.bak"
BRIEF_ORIG = SE_HOME / "memory_core_brief.orig.md"

SE_BEGIN = "<!-- SE-BEGIN 由SE外挂维护，勿手改 -->"
SE_END = "<!-- SE-END -->"

try:                                   # 任务计划/管道下中文别乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(SE_HOME / "se_review.log", encoding="utf-8")],
    )
except Exception:                      # 日志文件被占用也别死
    logging.basicConfig(level=logging.INFO)
log = logging.getLogger("se")

DEFAULTS = {
    "stage": "fast",
    "trigger_time": "23:00",
    "review": {"source": "Mem_Data", "min_turns": 6, "max_input_chars": 9000,
               "max_new_rules": 3, "sample_keep_days": 7},
    "inject": {"enabled": False, "max_rules": 1, "max_pairs": 2, "max_chars": 220,
               "header": "【像这样回话（照这个口气，别照抄原句）】"},
    "api": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "timeout": 60, "max_calls_per_day": 2,
            "price_in_per_m": 2.0, "price_out_per_m": 8.0},
    "rules": {"max_total": 20, "retire_fail_count": 3,
              "forbid_patterns": [
                  r"(不|别|勿|禁止)(要|再)?(说|用|叫|称|提|管自己叫)[^。；，！？]{0,3}本鱼",
                  r"(不|别|勿|禁止)(要|再)?自称",
                  r"本鱼[^。；，！？]{0,6}(改成|换成|改为|改称)[^。；，！？]{0,4}我",
                  r"(改用|换成)[「\"]?我[」\"]?(来|称呼|自称|说话)",
                  r"(自称|说自己是?)(AI|人工智能|大模型|模型|机器人|程序)",
                  r"(我|本鱼)(就)?是[^。；，]{0,3}(AI|人工智能|大模型|模型|机器人)",
              ]},
}


# ============================ 1. 配置 ============================
def _deep(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS, ensure_ascii=False))
    try:
        if CONFIG_PATH.exists():
            cfg = _deep(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception as e:
        log.warning("配置读不了，用默认值：%s", e)
    if cfg.get("stage") not in ("fast", "stable"):
        cfg["stage"] = "fast"
    return cfg


def _load_env():
    """复用主程序 .env 的 CLOUD_API_KEY —— 不复制密钥，不新建配置文件。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(BOT_HOME / ".env")
    except Exception:
        pass


def _api_key() -> str:
    _load_env()
    return (os.getenv("CLOUD_API_KEY") or "").strip()


# ============================ 2. 读当日对话 ============================
_CREATED_RE = re.compile(r"<!--\s*created:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
_SPEAKERS = ("老板：", "小肥鱼：")


def collect_today(date: str | None = None) -> tuple[str, int, int]:
    """返回 (对话文本, 实体数, 老板句数)。V6 实体库是唯一带「问+答」双边的语料。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    chunks, files = [], 0
    try:
        for p in sorted(MEM_DIR.glob("mem_*.txt")):
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            m = _CREATED_RE.search(raw)
            day = m.group(1) if m else datetime.fromtimestamp(
                p.stat().st_mtime).strftime("%Y-%m-%d")
            if day != date:
                continue
            body = "\n".join(l.strip() for l in raw.splitlines()
                             if l.startswith(_SPEAKERS)).strip()
            if body:
                chunks.append(body)
                files += 1
    except Exception as e:
        log.warning("读 Mem_Data 失败：%s", e)
    text = "\n".join(chunks).strip()
    if not text:
        text = _collect_from_bot_log()
        if text:
            log.warning("今天没有记忆实体，只有主日志半边对话（复盘质量差）")
    return text, files, text.count("老板：")


def _collect_from_bot_log(limit_lines: int = 800) -> str:
    """兜底：bot_v5.log 只有老板的话且没有日期，仅当今天刚运行过才有意义。"""
    try:
        lg = _bot_log()
        if not lg.exists():
            return ""
        lines = lg.read_text(encoding="utf-8", errors="replace").splitlines()[-limit_lines:]
        out, buf = [], None
        for ln in lines:
            m = re.match(r"^\d{2}:\d{2}:\d{2} \[INFO\] 收到 (主人|访客) \| ?(.*)$", ln)
            if m:
                if buf:
                    out.append(buf)
                buf = "老板：" + m.group(2)
            elif buf is not None and ln.strip() and not re.match(r"^\d{2}:\d{2}:\d{2} ", ln):
                buf += ln.strip()
            elif buf is not None:
                out.append(buf)
                buf = None
        if buf:
            out.append(buf)
        return "\n".join(out).strip()
    except Exception as e:
        log.warning("读主日志失败：%s", e)
        return ""


# ============================ 3. 脱敏（上云前必过） ============================
_REDACT = [
    ("密钥", re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[密钥]"),
    ("串码", re.compile(r"\b[A-Fa-f0-9]{16,}\b"), "[串码]"),      # 门钥匙/手机密码那种
    ("路径", re.compile(r"[A-Za-z]:\\[^\s，。、「」『』]{1,80}"), "[路径]"),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    ("长数字", re.compile(r"(?<!\d)\d{12,}(?!\d)"), "[长数字]"),
]


def redact(text: str) -> tuple[str, dict]:
    stats = {}
    for name, pat, rep in _REDACT:
        text, n = pat.subn(rep, text)
        if n:
            stats[name] = n
    return text, stats


# ============================ 4. 强指令 Prompt ============================
SYSTEM = ("你是小肥鱼人格工程的复盘师。你只做一件事：从当天对话里找出「小肥鱼（本地 7B）不像人」"
          "的具体症状，并给出 2~3 组「示范对答」当例子，教 7B 怎么说人话。"
          "只输出 JSON，不要解释、不要 markdown、不要客套。")


def _demo_text(pairs, limit: int = 3) -> str:
    out = []
    for p in (pairs or [])[:limit]:
        if isinstance(p, dict) and p.get("本鱼"):
            out.append(str(p["本鱼"]))
    return " / ".join(out)


def build_prompt(log_text: str, rules: list, cfg: dict) -> list[dict]:
    n = int(cfg["review"]["max_new_rules"])
    wrong = [r for r in rules if isinstance(r, dict) and int(r.get("fail_count", 0) or 0) > 0]
    alive = [r for r in rules if isinstance(r, dict) and r.get("status", "启用") == "启用"]

    def _fmt_wrong(rs):
        if not rs:
            return "（无）"
        return "\n".join(
            "- 症状：%s（已失败 %d 次）；上一版示范：「%s」→ 没被学去，请换一种说法和话题"
            % (str(r.get("症状", ""))[:40], int(r.get("fail_count", 0) or 0),
               (_demo_text(r.get("示范")) or str(r.get("解药", "")))[:50]) for r in rs[:6])

    head = (
        "【任务】\n下面是 %s 老板与小肥鱼的对话（已脱敏）。找出最多 %d 个真实存在的「症状」，"
        "每个症状给 2~3 组「示范对答」——用例子教它，不要讲道理。\n\n"
        "【什么叫症状】必须是**换个话题还会犯的行为模式**，不是今天聊的某件事。\n"
        "判断标准：把今天的话题换掉，这毛病还在吗？在，才是症状。\n"
        "常见症状：客服腔、复读、防备过严、装不熟、把老板当外人、空话敷衍、编造事实、答非所问、莫名说教。\n"
        "今天某个新功能、某件具体的事，只能当「证据」，不能当「症状」。\n\n"
        "【什么叫示范】一组两句：「老板：」写老板会说的话，「本鱼：」写你希望它那样回的话。\n"
        "1. 本鱼那句不超过 40 字，口语、像真人、可以有点脾气；\n"
        "2. **只写正确说法，绝对不要写「别说什么」的反例**——7B 会把反例原样抄出来；\n"
        "3. 每组换一个话题（生活、学习、闲聊都行），口气保持一致；\n"
        "4. 不出现「还有什么可以帮您」「谢谢你夸奖」这类客套，不用 emoji。\n\n"
        "【人设铁律——示范里不许违反】\n"
        "- 自称「本鱼」是设定，永远不改，不许改用「我」；\n"
        "- 不说自己是 AI / 模型 / 云端 / 程序；\n"
        "- 一次最多 2 句、40 字内；\n"
        "- 不用 emoji、不客套收尾；\n"
        "- 不编造，不知道就说不知道；对客人不透露主人隐私。\n\n"
        "【禁用词（这一步最重要）】从当天小肥鱼说的话里，摘出 1~4 个**字面短语**（3~10 字，必须是原文出现过的片段），"
        "它们就是造成这个症状的具体客套/空话。代码会在句级把含这些短语的整句删掉，所以：\n"
        "- 宁少勿多，只摘真正该删的；摘错了鱼会变得张口就断句；\n"
        "- 不写正则、不写整句、不写「不要…」这种指令；\n"
        "- 不摘老板说过的话，不摘「本鱼」「老板」这类正常称呼。\n\n"
        "【硬约束】\n"
        "1. 只输出 JSON，顶层必须是 {\"规则\": [...]}；\n"
        "2. 每条 4 个字段：症状(≤30字)、示范(2~3组，每项 {\"老板\":\"…\",\"本鱼\":\"…\"})、"
        "禁用词(数组，1~4个短语，可空)、证据(引一句当天原话，≤30字)；可选「触发词」(挑不出写 [])；\n"
        "3. 最多 %d 条；没有真症状就输出 {\"规则\": []}，不许为了交差硬造；\n"
        "4. 症状必须在当天对话里真的出现过；\n"
        "5. 【红线】如果今天的对话只围绕一件事展开、看不出跨话题的行为模式，"
        "就输出 {\"规则\": []}。空手而归，好过建一堆只在今天有用的卡。\n\n"
        "【错题本：已经试过、没被学去的】\n%s\n\n"
        "【已在生效的规则，别重复建】\n%s\n\n"
        "【当天对话】\n%s"
    ) % (datetime.now().strftime("%Y-%m-%d"), n, n,
         _fmt_wrong(wrong),
         "\n".join("- %s %s" % (r.get("id", ""), str(r.get("症状", ""))[:40])
                   for r in alive[:8]) or "（无）",
         log_text)
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": head}]


# ============================ 5. 调 API ============================
def call_api(messages: list[dict], cfg: dict) -> tuple[str, dict]:
    """返回 (文本, usage)。失败只返回空文本 + 原因，绝不抛异常（铁律：最多重试 1 次）。"""
    key = _api_key()
    if not key:
        return "", {"error": "没配 CLOUD_API_KEY"}
    try:
        from openai import OpenAI          # 懒加载：注入路径永远不 import openai
    except Exception as e:
        return "", {"error": "openai 库不可用：%s" % e}

    base = (os.getenv("CLOUD_BASE_URL") or cfg["api"]["base_url"]).strip()
    model = (os.getenv("CLOUD_MODEL") or cfg["api"]["model"]).strip()
    kwargs = dict(model=model, messages=messages, temperature=0.2, max_tokens=1200)
    try:
        cli = OpenAI(base_url=base, api_key=key,
                     timeout=cfg["api"]["timeout"], max_retries=0)
        try:
            r = cli.chat.completions.create(**kwargs, response_format={"type": "json_object"})
        except Exception as e:
            log.warning("JSON 模式失败（%s），去掉参数重试一次", type(e).__name__)
            r = cli.chat.completions.create(**kwargs)
    except Exception as e:
        log.error("API 两次都失败：%s: %s", type(e).__name__, str(e)[:120])
        return "", {"error": "api_failed"}
    text = (r.choices[0].message.content or "").strip()
    u = getattr(r, "usage", None)
    usage = {"in": getattr(u, "prompt_tokens", 0) or 0,
             "out": getattr(u, "completion_tokens", 0) or 0}
    return text, usage


# ============================ 6. JSON 修复（三级降级） ============================
def _match_bracket(s: str, start: int) -> int:
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth, in_str, esc = 0, False, False
    for j in range(start, len(s)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return j
    return -1


def extract_json(raw: str):
    """整体 → 去代码围栏 → 括号配对扫描。全失败返回 None。"""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for i, ch in enumerate(s):
        if ch in "{[":
            end = _match_bracket(s, i)
            if end > i:
                try:
                    return json.loads(s[i:end + 1])
                except Exception:
                    continue
    return None


# ============================ 7. 校验 → 规则 ============================
_BAD_TRIGGER = {"", "你", "我", "他", "的", "了", "吗", "呢", "吧", "啊"}


def _forbidden(cure: str, cfg: dict) -> str:
    """铁律护栏：解药撞上人设底线（比如让鱼改掉「本鱼」自称）就直接拦下。"""
    pats = (cfg.get("rules") or {}).get("forbid_patterns") or []
    for pat in pats:
        try:
            if re.search(pat, cure):
                return pat
        except re.error:
            continue
    return ""


def _line(s, limit: int = 40) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


def normalize(obj, cfg: dict) -> tuple[list[dict], list[dict]]:
    """返回 (可用规则, 被铁律护栏拦下的规则)。
    规则主体是「示范对答」：7B 学例子比学规则有效（2026-11-02 本机实测）。
    老格式（只有解药）仍兼容，渲染时退化成一条说明。"""
    items = []
    if isinstance(obj, dict):
        for k in ("规则", "rules", "症状列表", "data", "result"):
            if isinstance(obj.get(k), list):
                items = obj[k]
                break
        else:
            if "症状" in obj:
                items = [obj]
    elif isinstance(obj, list):
        items = obj

    max_pairs = int((cfg.get("inject") or {}).get("max_pairs", 3))
    out, blocked = [], []
    for it in items[: int(cfg["review"]["max_new_rules"])]:
        if not isinstance(it, dict):
            continue
        sym = _line(it.get("症状") or it.get("symptom"), 40)
        ev = _line(it.get("证据"), 40)
        cure = _line(it.get("解药") or it.get("cure"), 60)

        pairs = []
        raw = it.get("示范") or it.get("examples") or []
        for p in (raw if isinstance(raw, list) else []):
            if not isinstance(p, dict):
                continue
            a = _line(p.get("老板") or p.get("user"), 40)
            b = _line(p.get("本鱼") or p.get("assistant") or p.get("鱼"), 44)
            a = re.sub(r"^(老板|主人|用户)[：:]\s*", "", a)
            b = re.sub(r"^(本鱼|小肥鱼)[：:]\s*", "", b)
            if a and b:
                pairs.append({"老板": a, "本鱼": b})
            if len(pairs) >= max_pairs:
                break

        words = it.get("触发词") or it.get("keywords") or []
        if isinstance(words, str):
            words = re.split(r"[、,，\s]+", words)
        clean = []
        for w in (words if isinstance(words, list) else []):
            w = re.sub(r"\s+", "", str(w or ""))
            if 2 <= len(w) <= 6 and w not in _BAD_TRIGGER and w not in clean:
                clean.append(w)

        banned = []
        raw_b = it.get("禁用词") or it.get("banned") or []
        if isinstance(raw_b, str):
            raw_b = re.split(r"[、,，;；\s]+", raw_b)
        for w in (raw_b if isinstance(raw_b, list) else []):
            w = _line(w, 12)
            # 3 字起：避免「好的」「谢谢」这种两字泛词把正常话也删了
            if 3 <= len(w) <= 12 and w not in banned:
                banned.append(w)
        banned = banned[:6]

        if not sym:
            continue
        if not pairs and not cure and not banned:
            log.info("丢弃规则（既没有示范也没有禁用词）：%s", sym[:20])
            continue
        try:
            prio = max(1, min(3, int(it.get("严重度") or 2)))
        except Exception:
            prio = 2

        hit = ""
        for p in pairs:
            hit = hit or _forbidden(p["本鱼"], cfg)
        hit = hit or _forbidden(cure, cfg)
        if hit:
            log.warning("规则被铁律护栏拦下（%s）：%s → %s", hit, sym[:20],
                        (pairs[0]["本鱼"] if pairs else cure)[:30])
            blocked.append({"症状": sym, "示范": pairs, "解药": cure, "命中": hit})
            continue
        out.append({"症状": sym, "示范": pairs, "解药": cure, "禁用词": banned,
                    "触发词": clean[:6], "证据": ev, "优先级": prio})
    return out, blocked


# ============================ 8. 合并（错题本核心） ============================
def _key(s: str) -> str:
    return re.sub(r"[\s，。、！？,.:：;；~～…\-—「」『』\"'（）()]+", "", str(s or ""))[:24]


def merge_rules(data: dict, new_items: list[dict], cfg: dict) -> tuple[dict, dict]:
    rules = data.get("规则") if isinstance(data.get("规则"), list) else []
    seq = int(data.get("seq") or 0)
    idx = {_key(r.get("症状")): r for r in rules if isinstance(r, dict)}
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    retire_at = int(cfg["rules"]["retire_fail_count"])
    added = updated = 0

    for it in new_items:
        k = _key(it["症状"])
        old = idx.get(k)
        if isinstance(old, dict):
            old["fail_count"] = int(old.get("fail_count", 0) or 0) + 1        # 错题本 +1
            hist = old.get("示范历史") if isinstance(old.get("示范历史"), list) else []
            if old.get("示范"):
                hist.append(_demo_text(old["示范"]))
            elif old.get("解药"):
                hist.append(str(old["解药"]))
            old["示范历史"] = [h for h in hist if h][-3:]
            old["示范"] = it.get("示范") or old.get("示范") or []            # 换新示范
            if it.get("解药"):
                old["解药"] = it["解药"]
            old["禁用词"] = it.get("禁用词") or old.get("禁用词") or []      # 换新禁用词
            old["触发词"] = it.get("触发词") or old.get("触发词") or []
            old["证据"] = it.get("证据", old.get("证据", ""))
            old["最近"] = now
            old["优先级"] = max(int(old.get("优先级", 2) or 2), int(it.get("优先级", 2)))
            if old["fail_count"] >= retire_at:
                old["status"] = "弃用"
                log.warning("规则 %s 连续 %d 次失败，已弃用", old.get("id"), old["fail_count"])
            updated += 1
        else:
            seq += 1
            r = {"id": "R%03d" % seq, "症状": it["症状"], "示范": it.get("示范") or [],
                 "解药": it.get("解药", ""), "禁用词": it.get("禁用词") or [],
                 "触发词": it.get("触发词") or [],
                 "优先级": int(it.get("优先级", 2) or 2),
                 "fail_count": 0, "首次": now, "最近": now,
                 "证据": it.get("证据", ""), "示范历史": [], "status": "启用"}
            rules.append(r)
            idx[k] = r
            added += 1

    cap = int(cfg["rules"]["max_total"])
    if len(rules) > cap:                     # 先踢弃用，再踢最久没被提及的
        rules.sort(key=lambda r: (r.get("status", "启用") == "启用", str(r.get("最近", ""))))
        rules = rules[len(rules) - cap:]
    data["规则"] = rules
    data["seq"] = seq
    data["updated"] = now
    return data, {"新增": added, "更新错题本": updated, "总数": len(rules)}


# ============================ 9. 备份 / 原子写 ============================
def _atomic_write(path: Path, text: str):
    """原子写；newline="" 表示原样落盘，不做换行转换（LF/CRLF 由调用方决定）。"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_exact(p: Path) -> str:
    """读文本但不转换换行——brief 的 CRLF 必须原样保留。"""
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write_exact(p: Path, s: str):
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)


def _eol_of(s: str) -> str:
    return "\r\n" if "\r\n" in s else "\n"


def load_rules() -> dict:
    try:
        if RULES_PATH.exists():
            d = json.loads(RULES_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception as e:
        log.warning("规则库坏了（按空库处理）：%s", e)
    return {}


def save_rules(data: dict, keep_archive: int = 10):
    """写前必做：上一版 → persona_rules.bak（一键撤销）+ archive 时间戳副本。"""
    if RULES_PATH.exists():
        try:
            shutil.copy2(RULES_PATH, BAK_PATH)
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(RULES_PATH, ARCHIVE_DIR / (
                "persona_rules.%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")))
        except Exception as e:
            log.error("备份失败，本次拒绝写入（宁可不进化，不许丢规则）：%s", e)
            raise
    _atomic_write(RULES_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    try:
        olds = sorted(ARCHIVE_DIR.glob("persona_rules.*.json"))
        for p in olds[:-keep_archive]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def undo_rules() -> tuple[bool, str]:
    """撤销到上一版。当前坏版本先进 archive，连撤两次不会原地打转。"""
    if not BAK_PATH.exists():
        return False, "没有 persona_rules.bak，撤不了"
    try:
        if RULES_PATH.exists():
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(RULES_PATH, ARCHIVE_DIR / (
                "persona_rules.undone-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")))
        _atomic_write(RULES_PATH, BAK_PATH.read_text(encoding="utf-8"))
        return True, "已撤销到上一版（当前版已存入 archive）"
    except Exception as e:
        return False, "撤销失败：%s" % e


# ============================ 10. brief 托管区块 ============================
def _strip_block(t: str) -> str:
    i = t.find(SE_BEGIN)
    if i < 0:
        return t
    j = t.find(SE_END, i)
    return t if j < 0 else t[:i] + t[j + len(SE_END):]


def _core(t: str) -> str:
    """校验用：去掉托管区块 + 折叠首尾空白。原文必须一字不动。"""
    return _strip_block(t or "").strip()


def render_block(cfg: dict, rules: list) -> str:
    """把启用中的规则渲染成「示范对答」塞进 brief。
    7B 学例子比学规则有效（2026-11-02 本机实测：规则版服从率 0，示范版语气 3/3 被带过去）。"""
    inj = cfg["inject"]
    rs = [r for r in (rules or []) if isinstance(r, dict) and r.get("status", "启用") == "启用"]
    rs.sort(key=lambda r: (int(r.get("优先级", 1) or 1), str(r.get("最近", ""))), reverse=True)
    max_pairs = int(inj.get("max_pairs", 2))
    lines, total, used = [], 0, 0
    for r in rs:
        if used >= int(inj["max_rules"]):
            break
        chunk = []
        for p in (r.get("示范") or [])[:max_pairs]:
            if isinstance(p, dict) and p.get("老板") and p.get("本鱼"):
                chunk.append("老板：" + str(p["老板"]))
                chunk.append("本鱼：" + str(p["本鱼"]))
        if not chunk and r.get("解药"):
            chunk.append("·" + str(r["解药"]))
        if not chunk:
            continue
        cost = sum(len(x) for x in chunk)
        if total + cost > int(inj["max_chars"]):
            break
        lines.extend(chunk)
        total += cost
        used += 1
    if not lines:
        return ""
    return str(inj["header"]).strip() + "\n" + "\n".join(lines)


def patch_brief(block: str) -> tuple[bool, str]:
    """把规则写进 brief 的托管区块。原文有任何变动就拒绝写入；换行风格原样保留。"""
    try:
        orig = _read_exact(BRIEF_PATH)
    except Exception as e:
        return False, "读不到 brief：%s" % e
    if not block.strip():
        return False, "区块是空的，拒绝写入"
    eol = _eol_of(orig)
    body = block.strip().replace("\r\n", "\n").replace("\n", eol)
    new = _core(orig) + eol + eol + SE_BEGIN + eol + body + eol + SE_END + eol
    if _core(new) != _core(orig):               # 硬校验：只准加区块
        return False, "校验失败：会动到原文，已拒绝写入"
    try:
        if not BRIEF_ORIG.exists():
            _write_exact(BRIEF_ORIG, orig)   # 原件第一次落地，永不再改
        _write_exact(BRIEF_BAK, orig)
        _atomic_write(BRIEF_PATH, new)
        return True, "已写入 brief 区块（%d 字）" % len(block.strip())
    except Exception as e:
        return False, "写 brief 失败：%s" % e


def uninstall_brief() -> tuple[bool, str]:
    """整包卸载：摘掉区块，brief 恢复原样（优先按原件字节还原，保证逐字节相同）。"""
    try:
        if not BRIEF_PATH.exists():
            return False, "brief 不存在"
        cur = _read_exact(BRIEF_PATH)
        if SE_BEGIN not in cur:
            return False, "brief 里没有 SE 区块，本来就没动过"
        _write_exact(BRIEF_BAK, cur)
        stripped = _strip_block(cur)
        if BRIEF_ORIG.exists():                 # 除区块外与原件一致（老板没手改过）→ 直接字节还原
            try:
                orig = _read_exact(BRIEF_ORIG)
                if _core(stripped) == _core(orig):
                    _atomic_write(BRIEF_PATH, orig)
                    return True, "已摘除 SE 区块，按原件逐字节还原"
            except Exception:
                pass
        _atomic_write(BRIEF_PATH, stripped.rstrip() + _eol_of(cur))
        return True, "已摘除 SE 区块（原件有改动，只摘块不覆盖）"
    except Exception as e:
        return False, "卸载失败：%s" % e


def sync_brief(cfg: dict) -> tuple[bool, str]:
    """规则库变了 → 重渲染区块。开关关着就只当没这回事。"""
    if not cfg["inject"].get("enabled"):
        return False, "注入开关关着（inject.enabled=false），没动 brief"
    block = render_block(cfg, load_rules().get("规则") or [])
    if not block:
        return uninstall_brief()
    return patch_brief(block)


# ============================ 11. 复盘主流程 ============================
def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_state(st: dict):
    try:
        _atomic_write(STATE_PATH, json.dumps(st, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning("状态写不了（不影响进化）：%s", e)


def _due(cfg: dict, st: dict) -> bool:
    last = str(st.get("last_review") or "")
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return True
    gap = timedelta(hours=20) if cfg["stage"] == "fast" else timedelta(days=6, hours=20)
    return datetime.now() - last_dt >= gap


def review(force: bool = False, dry_run: bool = False) -> dict:
    cfg = load_config()
    st = _load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    if st.get("date") != today:
        st["date"], st["calls"] = today, 0

    if not dry_run:
        if not force and not _due(cfg, st):
            return {"ok": False, "skip": True, "why": "还没到复盘时间（stage=%s，上次 %s）"
                    % (cfg["stage"], st.get("last_review"))}
        if not force and int(st.get("calls", 0)) >= int(cfg["api"]["max_calls_per_day"]):
            return {"ok": False, "skip": True, "why": "今天 API 次数已用完"}

    text, files, turns = collect_today()
    if not text:
        return {"ok": False, "skip": True, "why": "今天没有对话，跳过"}
    if turns < int(cfg["review"]["min_turns"]):
        return {"ok": False, "skip": True, "why": "今天只聊了 %d 句（门槛 %d），样本太少"
                % (turns, cfg["review"]["min_turns"])}

    clean, red_stats = redact(text)
    clean = clean[-int(cfg["review"]["max_input_chars"]):]
    rules = load_rules().get("规则") or []
    messages = build_prompt(clean, rules, cfg)

    if dry_run:
        return {"ok": True, "dry_run": True, "date": today,
                "实体数": files, "老板句数": turns,
                "原文长度": len(text), "脱敏命中": red_stats,
                "送审长度": len(messages[1]["content"]),
                "prompt预览": messages[1]["content"][:1200]}

    try:                                    # 原始样本（脱敏后）留本地，便于事后核查
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(SAMPLE_DIR / ("%s.txt" % today.replace("-", "")), clean)
        _prune_samples(int(cfg["review"]["sample_keep_days"]))
    except Exception:
        pass

    st["calls"] = int(st.get("calls", 0)) + 1
    raw, usage = call_api(messages, cfg)
    cost = (usage.get("in", 0) / 1e6 * cfg["api"]["price_in_per_m"]
            + usage.get("out", 0) / 1e6 * cfg["api"]["price_out_per_m"])
    st["cost"] = round(float(st.get("cost", 0) or 0) + cost, 4)

    obj = extract_json(raw)
    if obj is None:
        _save_state(st)
        log.error("API 返回不是 JSON，本次不写库。原始前 200 字：%s", (raw or "")[:200])
        return {"ok": False, "why": "非 JSON，已跳过（规则库没动）",
                "raw_head": (raw or "")[:200]}

    items, blocked = normalize(obj, cfg)
    if not items:
        st["last_review"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        _save_state(st)
        return {"ok": True, "why": "今天没有值得建卡的症状", "新增": 0,
                "usage": usage, "被铁律拦下": blocked}

    data = load_rules()
    data, stats = merge_rules(data, items, cfg)
    data.setdefault("version", 1)
    data["统计"] = {"复盘次数": int((data.get("统计") or {}).get("复盘次数", 0) or 0) + 1,
                  "累计花费估算": round(float(st.get("cost", 0) or 0), 4)}
    try:
        save_rules(data)
    except Exception as e:
        return {"ok": False, "why": "备份失败，拒绝写入：%s" % e}

    brief_ok, brief_msg = sync_brief(cfg)
    st["last_review"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    _save_state(st)
    log.info("复盘完成：%s，tokens in=%d out=%d，约 %.4f 元",
             stats, usage.get("in", 0), usage.get("out", 0), cost)
    return {"ok": True, "stats": stats, "usage": usage, "cost": round(cost, 4),
            "brief": brief_msg, "brief_ok": brief_ok, "items": items,
            "被铁律拦下": blocked}


def _prune_samples(keep_days: int):
    try:
        cutoff = datetime.now() - timedelta(days=max(1, keep_days))
        for p in SAMPLE_DIR.glob("*.txt"):
            try:
                d = datetime.strptime(p.stem, "%Y%m%d")
            except Exception:
                continue
            if d < cutoff:
                p.unlink(missing_ok=True)
    except Exception:
        pass


# ============================ 12. 注入接口（给将来 A/C 方案留的口子） ============================
_INJ = {"key": None, "rules": [], "max_rules": 2, "max_chars": 160, "header": ""}


def get_persona_injection(keyword: str) -> str:
    """极轻量、永不抛异常、按 mtime 热加载（改规则不用重启主程序）。
    纯字典查表 + 子串匹配，<0.1ms，不调模型、不写盘、不联网。
    B 方案当前不调用它；将来切 A/C 时才接进主程序。"""
    try:
        if not RULES_PATH.exists():
            return ""
        key = (RULES_PATH.stat().st_mtime,
               CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0)
        if key != _INJ["key"]:
            cfg = load_config()
            inj = cfg["inject"]
            data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
            rs = [r for r in (data.get("规则") or [])
                  if isinstance(r, dict) and r.get("status", "启用") == "启用"]
            rs.sort(key=lambda r: (int(r.get("优先级", 1) or 1), str(r.get("最近", ""))),
                    reverse=True)
            _INJ.update(key=key, rules=rs, max_rules=int(inj["max_rules"]),
                        max_chars=int(inj["max_chars"]), header=str(inj["header"]))
        q = str(keyword or "")
        if not _INJ["rules"] or not q:
            return ""
        picked, total = [], 0
        for r in _INJ["rules"]:
            if len(picked) >= _INJ["max_rules"]:
                break
            words = r.get("触发词") or []
            if not any(isinstance(w, str) and w and w in q for w in words):
                continue
            line = "·" + str(r.get("解药", "")).strip()
            if len(line) <= 1:
                continue
            if total + len(line) > _INJ["max_chars"]:
                break
            picked.append(line)
            total += len(line)
        if not picked:
            return ""
        return _INJ["header"] + "\n" + "\n".join(picked)
    except Exception:
        return ""


# ============================ 12.5 飞书指令通道 ============================
# 零改主程序的做法：老板说的话会被主程序存成 Mem_Data 里的记忆实体，
# SE 用一个「每 2 分钟扫一次」的批处理任务去认关键词，然后自己调飞书 API 回话。
# 主程序完全不知道这件事，也不会被打断。
CMD_WORDS = (
    ("立即进化", ("立即进化", "马上进化", "开始进化", "进化一下")),
    ("撤销进化", ("撤销进化", "撤回进化", "回退进化")),
    ("看规则", ("查看规则", "看看进化", "进化情况", "进化到哪了", "看规则")),
)


def _cmd_of(text: str) -> str:
    for name, words in CMD_WORDS:
        if any(w in text for w in words):
            return name
    return ""


def _feishu_token() -> tuple[str, str]:
    try:
        import requests
        _load_env()
        app_id = (os.getenv("FEISHU_APP_ID") or "").strip()
        secret = (os.getenv("FEISHU_APP_SECRET") or "").strip()
        if not app_id or not secret:
            return "", "没配 FEISHU_APP_ID / FEISHU_APP_SECRET"
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": secret}, timeout=10)
        j = r.json() or {}
        if j.get("code") != 0:
            return "", "token 失败：%s" % j.get("msg")
        return str(j.get("tenant_access_token") or ""), ""
    except Exception as e:
        return "", "取 token 异常：%s" % type(e).__name__


def feishu_notify(text: str) -> tuple[bool, str]:
    """SE 自己给老板发消息（不经主程序）。失败只返回原因，绝不抛异常。"""
    try:
        import requests
        token, err = _feishu_token()
        if not token:
            return False, err
        rid = (os.getenv("OWNER_OPEN_ID") or "").strip()
        if not rid:
            return False, "没配 OWNER_OPEN_ID"
        r = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers={"Authorization": "Bearer " + token},
            json={"receive_id": rid, "msg_type": "text",
                  "content": json.dumps({"text": text}, ensure_ascii=False)},
            timeout=15)
        j = r.json() or {}
        if j.get("code") != 0:
            return False, "发送失败：%s" % j.get("msg")
        return True, "已发送"
    except Exception as e:
        return False, "发送异常：%s" % type(e).__name__


def _rules_summary() -> str:
    data = load_rules()
    rs = [r for r in (data.get("规则") or []) if isinstance(r, dict)]
    words = [w for r in rs if r.get("status", "启用") == "启用" for w in (r.get("禁用词") or [])]
    lines = ["【SE · 进化模块】当前 %d 条规则，%d 个禁用词生效" % (len(rs), len(words))]
    for r in rs[:5]:
        lines.append("%s [%s] %s" % (r.get("id", ""), r.get("status", "启用"),
                                     str(r.get("症状", ""))[:24]))
    if data.get("updated"):
        lines.append("上次更新：" + str(data["updated"]).replace("T", " "))
    lines.append("花费估算：%.4f 元" % float(_load_state().get("cost", 0) or 0))
    return "\n".join(lines)


def watch_once(dry: bool = False, mem_dir: Path | None = None) -> dict:
    """扫一遍新记忆实体，看老板有没有下指令。返回处理结果（非演练时会回飞书）。"""
    st = _load_state()
    done = [x for x in (st.get("watch_done") or []) if isinstance(x, str)]
    last = float(st.get("watch_last_ts") or 0)
    src = mem_dir or MEM_DIR
    handled, note = "", []

    try:
        files = sorted(src.glob("mem_*.txt"), key=lambda p: p.stat().st_mtime)[-60:]
    except Exception as e:
        return {"ok": False, "why": "扫不了目录：%s" % e}

    newest = last
    for p in files:
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        newest = max(newest, mt)
        if mt <= last or p.name in done:
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        said = "\n".join(l.strip() for l in raw.splitlines() if l.startswith("老板："))
        cmd = _cmd_of(said)
        done.append(p.name)
        if not cmd:
            continue
        if cmd == "立即进化":
            res = review(force=True, dry_run=dry)
            if dry:
                handled = "立即进化（演练，未花钱）"
            elif res.get("ok"):
                stts = res.get("stats") or {}
                handled = "进化完成：新增 %s 条，禁用词已更新" % stts.get("新增", 0)
            else:
                handled = "进化没成：%s" % res.get("why", "")
        elif cmd == "撤销进化":
            if dry:
                handled = "撤销进化（演练）"
            else:
                good, msg = undo_rules()
                handled = "已撤销到上一版（禁用词随之回退）" if good else msg
        else:
            handled = _rules_summary()
        note.append(handled)
        break                                   # 一次只处理最新一条指令

    st["watch_last_ts"] = newest
    st["watch_done"] = done[-200:]
    if not dry:
        _save_state(st)
    out = {"ok": True, "dry": dry, "有指令": bool(note), "结果": note}
    if note and not dry:
        ok, msg = feishu_notify(note[0])
        out["回话"] = msg
    return out


# ============================ 13. 状态 / 预览 ============================
def _boot_time_today() -> str:
    """从主日志猜主程序今天最后一次启动的时刻（HH:MM:SS）。日志行没有日期，只能当天用。"""
    try:
        lg = _bot_log()
        if not lg.exists():
            return ""
        tail = lg.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        for ln in reversed(tail):
            if "记忆库：" in ln:
                m = re.match(r"^(\d{2}:\d{2}:\d{2})", ln)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ""


def status() -> dict:
    cfg = load_config()
    data = load_rules()
    rs = data.get("规则") or []
    alive = [r for r in rs if r.get("status", "启用") == "启用"]
    block = ""
    has_block = False
    try:
        if BRIEF_PATH.exists():
            t = BRIEF_PATH.read_text(encoding="utf-8")
            has_block = SE_BEGIN in t
            i = t.find(SE_BEGIN)
            if i >= 0:
                j = t.find(SE_END, i)
                block = t[i + len(SE_BEGIN):j].strip() if j > i else ""
    except Exception:
        pass

    pending = ""
    if has_block and BRIEF_PATH.exists():
        try:
            wrote = datetime.fromtimestamp(BRIEF_PATH.stat().st_mtime).strftime("%H:%M:%S")
            boot = _boot_time_today()
            if boot:
                pending = "已生效（进程启动 %s 晚于写入 %s）" % (boot, wrote) \
                    if boot > wrote else "待重启（写入 %s，进程启动于 %s）" % (wrote, boot)
            else:
                pending = "未知（日志里没找到启动记录）"
        except Exception:
            pending = "未知"

    return {"stage": cfg["stage"], "注入开关": cfg["inject"]["enabled"],
            "规则总数": len(rs), "启用中": len(alive),
            "brief有区块": has_block, "区块字数": len(block),
            "生效状态": pending or ("未写入" if not has_block else "未知"),
            "最近复盘": _load_state().get("last_review"),
            "累计花费估算": _load_state().get("cost")}


# ============================ 14. 自测 ============================
def _self_test() -> int:
    total, fails = 0, []

    def check(cond, name):
        nonlocal total
        total += 1
        if not cond:
            fails.append(name)

    check(extract_json('{"规则":[{"症状":"a","解药":"b"}]}')["规则"][0]["症状"] == "a",
          "JSON 直接解析")
    check(extract_json('```json\n{"规则":[]}\n```') == {"规则": []}, "JSON 去代码围栏")
    check(extract_json('好的，结果如下：{"规则":[]} 以上') == {"规则": []}, "JSON 括号配对扫描")
    check(extract_json('不是 JSON') is None, "非 JSON 返回 None")

    fake_cfg = {"review": {"max_new_rules": 3},
                "inject": {"max_rules": 2, "max_pairs": 2, "max_chars": 220, "header": "H"},
                "rules": {"max_total": 20, "retire_fail_count": 3,
                          "forbid_patterns": DEFAULTS["rules"]["forbid_patterns"]}}
    it, _b = normalize({"规则": [{"症状": "防备过严",
                                  "示范": [{"老板": "你认识我是谁吗", "本鱼": "认识，天天见。"}],
                                  "触发词": ["老板", "你", "外人"], "证据": "得问老板"}]}, fake_cfg)
    check(bool(it) and it[0]["触发词"] == ["老板", "外人"], "单字/泛词触发词剔除")
    check(bool(it) and it[0]["示范"][0]["本鱼"] == "认识，天天见。", "示范对答解析（含「老板：」前缀清除）")

    no_kw, _b = normalize({"规则": [{"症状": "客服腔",
                                     "示范": [{"老板": "谢谢啊", "本鱼": "记着就行。"}]}]}, fake_cfg)
    check(bool(no_kw) and no_kw[0]["触发词"] == [], "没触发词的规则不再被丢弃（常驻模式照用）")

    blocked_test, blocked_list = normalize(
        {"规则": [{"症状": "自称AI", "示范": [{"老板": "你是啥", "本鱼": "我是AI助手"}]}]}, fake_cfg)
    check(blocked_test == [] and len(blocked_list) == 1, "铁律护栏：示范里「我是AI」被拦下")

    legit, _b = normalize({"规则": [{"症状": "装不熟",
                                     "示范": [{"老板": "给你换个新窝",
                                               "本鱼": "换就换，别给本鱼整太花哨的。"}]}]}, fake_cfg)
    check(bool(legit), "护栏不误伤：「别给本鱼…」是正常说话")

    ban, _b = normalize({"规则": [{"症状": "客套腔",
                                   "示范": [{"老板": "谢谢啊", "本鱼": "记着。"}],
                                   "禁用词": ["好的", "谢谢你夸奖"]}]}, fake_cfg)
    check(bool(ban) and ban[0]["禁用词"] == ["谢谢你夸奖"], "禁用词：两字泛词被剔，短语保留")

    data, st = merge_rules({"规则": [], "seq": 0}, it, fake_cfg)
    check(st["新增"] == 1 and data["规则"][0]["fail_count"] == 0, "新规则入库")

    again, _b = normalize({"规则": [{"症状": "防备过严！",
                                     "示范": [{"老板": "你谁啊", "本鱼": "老熟人。"}],
                                     "触发词": ["外人"]}]}, fake_cfg)
    data, st = merge_rules(data, again, fake_cfg)
    check(st["更新错题本"] == 1 and data["规则"][0]["fail_count"] == 1,
          "同症状归一化 → fail_count +1")
    check(data["规则"][0]["示范"][0]["本鱼"] == "老熟人。" and bool(data["规则"][0]["示范历史"]),
          "示范替换 + 旧示范入历史")

    block = render_block(fake_cfg, data["规则"])
    check(block.startswith("H") and "老板：" in block and "老熟人。" in block,
          "区块渲染成示范对答")

    global RULES_PATH, BAK_PATH, ARCHIVE_DIR, BRIEF_PATH, BRIEF_BAK, BRIEF_ORIG, SAMPLE_DIR
    old = (RULES_PATH, BAK_PATH, ARCHIVE_DIR, BRIEF_PATH, BRIEF_BAK, BRIEF_ORIG, SAMPLE_DIR)
    try:
        tmp = Path(tempfile.mkdtemp())
        RULES_PATH, BAK_PATH, ARCHIVE_DIR = tmp / "r.json", tmp / "r.bak", tmp / "arc"
        BRIEF_PATH, BRIEF_BAK, BRIEF_ORIG = tmp / "brief.md", tmp / "brief.bak", tmp / "brief.orig"
        SAMPLE_DIR = tmp / "samples"

        orig = "【本鱼是谁】\r\n- 门钥匙：0123456789abcdef\r\n- 远程地址每次重启都变\r\n"
        _write_exact(BRIEF_PATH, orig)
        ok1, m1 = patch_brief(block)
        after = _read_exact(BRIEF_PATH)
        ok2 = ("门钥匙：0123456789abcdef" in after and SE_BEGIN in after
               and SE_END in after and "\r\n" in after)
        ok3, m3 = uninstall_brief()
        exact = _read_exact(BRIEF_PATH).encode("utf-8") == orig.encode("utf-8")
        check(ok1 and ok2 and ok3 and exact, "brief 写入→摘除 逐字节还原（CRLF 不丢）")
        if not (ok1 and ok2 and ok3 and exact):
            print("    [%s][%s][%s] 现状=%r" % (m1, m3, exact, _read_exact(BRIEF_PATH)[:60]))

        save_rules({"规则": [{"id": "R001"}], "seq": 1})
        save_rules({"规则": [{"id": "R002"}], "seq": 2})
        ok4, _ = undo_rules()
        check(ok4 and json.loads(RULES_PATH.read_text(encoding="utf-8"))["seq"] == 1,
              "备份 + 撤销回上一版")
    finally:
        (RULES_PATH, BAK_PATH, ARCHIVE_DIR, BRIEF_PATH, BRIEF_BAK, BRIEF_ORIG,
         SAMPLE_DIR) = old

    print("\n自测 %d/%d 通过" % (total - len(fails), total))
    for f in fails:
        print("  × " + f)
    return 0 if not fails else 1


# ============================ 15. CLI ============================
def _arg(argv: list, flag: str, default: str = "") -> str:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return _self_test()

    if "--show" in argv:
        data = load_rules()
        rs = data.get("规则") or []
        print("规则库：%s（%d 条）" % (RULES_PATH, len(rs)))
        for r in rs:
            print("\n%s [%s] fail=%d 优先级=%s"
                  % (r.get("id"), r.get("status", "启用"),
                     int(r.get("fail_count", 0) or 0), r.get("优先级")))
            print("  症状：%s" % r.get("症状"))
            for p in (r.get("示范") or []):
                print("    %s → %s" % (p.get("老板", ""), p.get("本鱼", "")))
            if r.get("解药"):
                print("  说明：%s" % r.get("解药"))
            print("  触发词：%s" % ("、".join(r.get("触发词") or []) or "（无）"))
            if r.get("证据"):
                print("  证据：%s" % r.get("证据"))
            if r.get("禁用词"):
                print("  禁用词：%s" % "、".join(r.get("禁用词") or []))
            if r.get("示范历史"):
                print("  旧示范：%s" % " | ".join(r.get("示范历史") or []))
        return 0

    if "--status" in argv:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0

    if "--watch-once" in argv:
        print(json.dumps(watch_once(dry=("--dry" in argv)), ensure_ascii=False, indent=2))
        return 0

    if "--notify" in argv:
        ok, msg = feishu_notify(_arg(argv, "--notify", "【SE】测试消息"))
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1

    if "--summary" in argv:
        print(_rules_summary())
        return 0

    if "--preview" in argv:
        cfg = load_config()
        block = render_block(cfg, load_rules().get("规则") or [])
        print(block or "（没有可写的规则）")
        return 0

    if "--inject" in argv:
        print(get_persona_injection(_arg(argv, "--inject")))
        return 0

    if "--filters" in argv:
        data = load_rules()
        rows = [(r.get("id", ""), str(r.get("症状", ""))[:24], w)
                for r in (data.get("规则") or [])
                if isinstance(r, dict) and r.get("status", "启用") == "启用"
                for w in (r.get("禁用词") or [])]
        if not rows:
            print("（当前没有可执行的禁用词）")
            return 0
        for rid, sym, w in rows:
            print("%s  %-24s  %s" % (rid, sym, w))
        print("共 %d 个禁用词，句级删除已生效（se_filter 热加载）" % len(rows))
        return 0

    if "--apply" in argv:
        cfg = load_config()
        ok, msg = sync_brief(cfg)
        print(("✅ " if ok else "❌ ") + msg)
        if ok:
            print(render_block(cfg, load_rules().get("规则") or []))
        return 0 if ok else 1

    if "--reset" in argv:
        cfg = load_config()
        data = load_rules()
        if data.get("规则"):
            try:
                ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                _atomic_write(ARCHIVE_DIR / ("persona_rules.reset-%s.json"
                                             % datetime.now().strftime("%Y%m%d-%H%M%S")),
                              json.dumps(data, ensure_ascii=False, indent=2))
            except Exception as e:
                print("❌ 归档失败，不敢清空：%s" % e)
                return 1
            save_rules({"version": 1, "规则": [], "seq": 0,
                        "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")})
        ok, msg = sync_brief(cfg)
        print("✅ 规则库已清空（旧规则进 archive）：%s；brief：%s" % (RULES_PATH.name, msg))
        return 0

    if "--undo" in argv:
        cfg = load_config()
        good, msg = undo_rules()
        print(("✅ " if good else "❌ ") + msg)
        if good:
            b_ok, b_msg = sync_brief(cfg)
            print("  brief：" + b_msg if b_ok else "  brief：" + b_msg)
        return 0 if good else 1

    if "--uninstall" in argv:
        good, msg = uninstall_brief()
        print(("✅ " if good else "❌ ") + msg)
        return 0 if good else 1

    res = review(force=("--force" in argv) or ("--dry-run" in argv),
                 dry_run="--dry-run" in argv)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if (res.get("ok") or res.get("skip")) else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:                       # 兜底：外挂崩了也不许影响别的东西
        log.error("SE 顶层异常：%s: %s", type(e).__name__, e)
        sys.exit(1)
