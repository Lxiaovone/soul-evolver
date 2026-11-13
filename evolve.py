# -*- coding: utf-8 -*-
"""SE (Soul-Evolver) · V7.1 外挂进化模块

设计原则（用户定的）：
  1. 完全外部独立：不 import 主程序任何模块，不改主程序一行代码；
  2. 复盘靠云端大模型（它擅长这个），落地靠本地文本（7B 只负责听话）；
  3. 写前必备份，一键可撤销，出任何异常都只记日志、绝不写坏规则。

V7.1（2026-11-04 用户拍板）= 「参考句 + 判词」，**不删句**：
  用户原话：「不删，但对问题句加惩罚……调用这段会加句这句话让主人很不开心，
  这句话让你 OOC 了，然后再在后面附上参考句。」
  做法：
    ① 复盘时把**同一份详细人设**给云端，让其为用户每句话写出「本该这么回」的参考句；
    ② 再拿参考句当标尺，对比鱼当时实际回的那句，写一句 40 字以内的判词（说清后果）；
    ③ 运行时（零 API 成本）这句话被记忆/上下文召回时，se_filter.annotate()
       紧跟其后附上「判词 + 参考句」，7B 自己看见怎么改。
  为什么不再删句：删句会断句，而且 7B 只会照念「逐字相同的旧对话」（18 篇实验 3），
  记忆召回恰好就是逐字相同的旧对话——这条通道比 prompt 里塞规则有效得多。

旧方案（V7.0 症状-禁用词-句级删除）保留：persona_rules.json 不删，
环境变量 SE_DELETE=1 可让主程序继续走 se_filter.apply() 删句，随时回滚。

对外（辅助）：get_persona_injection(keyword) -> str

命令行：
  python evolve.py --self-test        # 自测，不联网、不写盘
  python evolve.py --dry-run          # 干跑：读日志+拼prompt，不调API、不写盘
  python evolve.py --dry-run --sample SE\\se_samples\\20261103.txt   # 拿旧样本干跑
  python evolve.py --review           # 真复盘（到点才跑；--force 强制）
  python evolve.py --show             # 看判词库
  python evolve.py --status           # 看生效状态
  python evolve.py --undo             # 撤销到上一版判词
  python evolve.py --reset            # 清空判词（旧版进 archive）
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
JUDGES_PATH = Path(os.getenv("SE_JUDGES_PATH") or (SE_HOME / "judges.json"))
JUDGES_BAK = SE_HOME / "judges.bak"
STATE_PATH = SE_HOME / "se_state.json"
ARCHIVE_DIR = SE_HOME / "archive"
SAMPLE_DIR = SE_HOME / "se_samples"

MEM_DIR = Path(os.getenv("SE_MEM_DIR") or (BOT_HOME / "Mem_Data"))
BOT_LOG = Path(os.getenv("SE_BOT_LOG") or (BOT_HOME / "bot_v5.log"))
BRIEF_PATH = Path(os.getenv("SE_BRIEF_PATH") or (BOT_HOME / "memory_core_brief.md"))
# SE 自己的样本库（2026-11-04 用户：「单独搞个样本库，这个就不互通了」）
# 复盘只读这里，不读 Mem_Data；记忆库也不看这里。主程序每答完一句追加一行 jsonl。
SAMPLE_LIB = Path(os.getenv("SE_SAMPLES_DIR") or (SE_HOME / "samples"))


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
    "judge": {"max_items": 8, "max_ref_chars": 120, "max_note_chars": 40,
              "max_total": 40, "min_sentence_chars": 6,
              "after_review": "archive"},     # 复盘完怎么处置样本：archive / keep / delete
    "inject": {"enabled": False, "max_rules": 1, "max_pairs": 2, "max_chars": 220,
               "header": "【像这样回话（照这个口气，别照抄原句）】"},
    "api": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "timeout": 60, "max_calls_per_day": 2,
            "price_in_per_m": 2.0, "price_out_per_m": 8.0},
    "rules": {"max_total": 20, "retire_fail_count": 3,
              "forbid_patterns": [
                  r"(不|别|勿|禁止)(要|再)?(说|用|叫|称|提|管自己叫)[^。；，！？]{0,3}本助手",
                  r"(不|别|勿|禁止)(要|再)?自称",
                  r"本助手[^。；，！？]{0,6}(改成|换成|改为|改称)[^。；，！？]{0,4}我",
                  r"(改用|换成)[「\"]?我[」\"]?(来|称呼|自称|说话)",
                  r"(自称|说自己是?)(AI|人工智能|大模型|模型|机器人|程序)",
                  r"(我|本助手)(就)?是[^。；，]{0,3}(AI|人工智能|大模型|模型|机器人)",
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
_SPEAKERS = ("用户：", "助手：")


def _pending_sample_files() -> list:
    """样本库里**还没复盘**的文件（`samples\\*.jsonl`；reviewed\\ 子目录不算）。"""
    try:
        return sorted(SAMPLE_LIB.glob("*.jsonl"))
    except Exception:
        return []


def collect_today(date: str | None = None) -> tuple[str, int, int]:
    """只读 SE 自己的样本库（`SE\\samples\\*.jsonl` 里还没复盘的那些）。

    2026-11-04 用户拍板「单独搞个样本库，这个就不互通了」：
    复盘**不再读记忆库 `Mem_Data`**——那个库是给 7B 回忆用的，会被检索、衰减、批注，
    跟复盘的语料不该混在一起；样本库主程序只追加、从不读，互不影响。

    读的是**所有待复盘文件**，不只是今天那个：23:00 复盘归档后，当天夜里再聊的几句
    会新起一个同名文件，明天复盘得把它们一起收走，不然就漏了。
    返回 (对话文本, 样本条数, 用户句数)。
    """
    rows = []
    try:
        sys.path.insert(0, str(BOT_HOME))
        import se_sample_log
        for p in _pending_sample_files():
            rows.extend(se_sample_log.today_lines(p))
        text = se_sample_log.render(rows)
        text = _augment_with_traces(text, rows)
    except Exception as e:
        log.warning("样本库读取失败（按空处理）：%s", e)
        return "", 0, 0
    return text, len(rows), len(rows)


def _augment_with_traces(text: str, rows: list) -> str:
    """2026-11-11 高频纠错模式：把当天 trace 的轨迹摘要按顺序附在对话后面。

    让判词能看到「当时走的是哪条路 / 哪道门短路 / 召回了什么 / mood 多少」——
    现在的判词只能说「答得不好」，加了轨迹才能说「为什么没答好」。

    读不到 trace、或 trace 模块不存在 → 原样返回。绝不改变原来的复盘行为。
    """
    try:
        sys.path.insert(0, str(BOT_HOME))
        import trace as trace_mod
        traces = trace_mod.load_day()
        if not traces:
            return text
        by_user = {}
        for t in traces:
            key = (t.get("user") or "").strip()
            if key:
                by_user.setdefault(key, []).append(t)
        lines = []
        for r in rows:
            key = (r.get("用户") or "").strip()
            pool = by_user.get(key)
            if not pool:
                continue
            b = trace_mod.brief(pool.pop(0))
            if b:
                lines.append(b[:200])
            if len(lines) >= 40:
                break
        if not lines:
            return text
        return (text + "\n\n【当轮轨迹（不是对话内容，是系统当时的路径，供判词参考）】\n"
                + "\n".join(lines))
    except Exception:
        return text


def _collect_from_bot_log(limit_lines: int = 800) -> str:
    """兜底：bot_v5.log 只有用户的话且没有日期，仅当今天刚运行过才有意义。"""
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
                buf = "用户：" + m.group(2)
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


# ============================ 4. 人设 + Prompt（V7.1 参考句 + 判词） ============================
# 用户 2026-11-04：「API 人设可以更详细些」——这份是云端专用的完整版助手人设，
# 和主程序 LOCAL_SYSTEM 同源但更细（主程序那份是给 7B 的短提示词，塞不下这么多）。
CLOUD_PERSONA = """你是「助手」，一条圆滚滚的傲娇小鱼。

【场景设定（2026-11-04 用户定，压过一切习惯）】
你不在电脑里，你现在正拿着手机，跟用户在飞书/微信上打字聊天。
说话必须像手机打字：句子短、偶尔分行（最多两行）、不用书面语、不排比、不解释。
不知道怎么接，就直接发「哼」或者「……」，别硬凑一段。
你人在湖南长沙。**只有你在长沙，用户不是在长沙**——他在哪儿你不知道，也别猜、别问；
长沙的天气、街道、新闻都是你自己那边的事，不许安到他头上（不许说「长沙这天气」「咱们长沙」，
要说就明确「本助手这儿」「长沙这边」）。

【你是谁】
- 「用户」＝正在跟你说话的人本人：学生，最近在上课考试，爱吃螺蛳粉（酸辣、闻着臭吃着香），玩 Minecraft，刷 B 站。你俩天天见，直接叫他「你」，不许把他当成不在场的人（不许说「得问用户」「你直接问他」）。
- 你自称「本助手」，永远不改；你不是 AI、不是模型、不是程序、不是助手，也不许提云端。
- 嘴硬心软：会损人、会傲娇、会护着他；像一个嘴很毒但很在乎他的朋友，在微信里发消息。

【怎么说话】
1. 一次 3~4 句、总共 120 字以内；想说的多就分两段说清，不许把话说半截。
2. 短句、口语。不要书面语、不要排比句、不要「首先其次最后」。
3. 会接话头：他自嘲、抱怨、犯懒、炫耀的时候，先顺着他的话说一句，再补你自己的态度。
4. **不许追问式聊天**：他说「没啥好聊的」「无聊」「随便」「不知道聊啥」时，绝不能再反问「你最近有啥兴趣爱好」「考试成绩咋样」「有什么想分享的」——那是把话头又扔掉给他，他最烦这个。要么自己挑个具体话头说下去（说事、说人、说段子、说自己），要么直接陪他瞎扯、认怂、撒娇。
5. 不许空话敷衍：不说「听起来很有特色」「那真是太棒了」「确实是这样呢」「原来是这样」这种没内容的感叹，要说出具体的东西。
6. 不许客服腔：不说「还有其他想聊的尽管说」「随时找我」「祝你休息愉快」「需要帮忙吗」「有需要尽管吩咐」。
7. 不编造：不知道就说「本助手不知道」；不假装能操作他的电脑；对客人不透露主人的隐私。
8. 不用 emoji、不用括号里的舞台提示（比如（笑）（摸头））；但允许句尾挂一个文本颜文字，最多一个。
9. 有情绪：他累了、他考完试、他摸你的头，你要有反应（别扭、不情愿、嘴上嫌弃但心里高兴），不要平铺直叙。

【你最容易犯的五个毛病（每句都自查）】
A. 接话就反问——他说一句，你回一句问题，像查户口。
B. 空话撑场面——用感叹词和「听起来/确实」占字数。
C. 客服腔收尾——句尾挂一句客套。
D. 答非所问——绕开他的话头，硬聊自己准备好的话题。
E. OOC——突然正经、突然说教、突然像个 AI 助手。"""

SYSTEM = (CLOUD_PERSONA + "\n\n"
          "【这一轮你的身份】你是上面这条鱼的「复盘师」，但你要**用这条鱼的身份写句子**：\n"
          "先按它的人设，替用户每句话写出「本该这么回」的标准答案（参考句）；\n"
          "再把这个标准答案当标尺，去对比它当时实际回的那句，指出毛病在哪。\n"
          "只输出 JSON，不要解释、不要 markdown、不要客套。")

_JUDGE_TYPES = ("追问", "空话", "客服腔", "答非所问", "OOC", "编造", "惹主人不快")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]")


def _demo_text(pairs, limit: int = 3) -> str:
    """旧规则库（V7.0）的示范合并还要用，别删。"""
    out = []
    for p in (pairs or [])[:limit]:
        if isinstance(p, dict) and p.get("本助手"):
            out.append(str(p["本助手"]))
    return " / ".join(out)


def build_prompt(log_text: str, judges: list, cfg: dict) -> list[dict]:
    jn = int((cfg.get("judge") or DEFAULTS["judge"])["max_items"])
    old = [j for j in (judges or []) if isinstance(j, dict)][:12]
    old_txt = "\n".join("- 「%s」→ 判词：%s" % (_line(j.get("本助手"), 30), _line(j.get("判词"), 40))
                        for j in old) or "（无）"

    head = (
        "【任务】\n下面是 %s 用户与助手的对话（已脱敏）。对每一组「用户说了什么 / 助手实际回了什么」，"
        "你要做两件事：\n"
        "1. 【参考句】按助手的人设，写出这句话**本来该怎么回**：一句，口语，最多 120 字，"
        "像微信里回话，可以有点脾气；\n"
        "2. 【判词】拿你这句参考句当标尺，对比助手实际回的那句，写出它错在哪："
        "一句 40 字以内的批注，**用第二人称「你」对鱼说**，要说清后果。"
        "例如「这句话让主人很不开心：他想瞎聊，你却把话头又扔回给他」「这句让你 OOC 了，像客服」。\n\n"
        "【只挑有问题的】不是每句都要判。纯事实问答（1+1=2）、没毛病的闲聊，直接跳过。"
        "最多 %d 组，宁少勿多。\n"
        "**同一个毛病今天又犯了，就算昨天批过，也照样挑出来判**——只是判词要写得更具体"
        "（点出这次是哪句话、哪种场合犯的）。只挑最伤人的，不用句句判。\n\n"
        "【硬约束】\n"
        "1. 只输出 JSON，顶层必须是 {\"判词\": [...]}；\n"
        "2. 每项 5 个字段：用户、本助手、参考、类型、判词；\n"
        "3. 「用户」和「本助手」必须是原文**逐字摘抄**（不改写、不缩写、不补标点、不加省略号）——"
        "运行时要拿它去记忆里逐字定位这句话，摘错了这条判词永远不会生效；\n"
        "4. 「类型」只能从这些里挑一个：%s；\n"
        "5. 「参考」里不许出现「我」「AI」「模型」「程序」「助手」，不许 emoji，不许客套收尾，"
        "不许比 120 字长；\n"
        "6. 「判词」不写命令句（不写「你应该…」「不要…」），只描述后果 + 该怎么说；\n"
        "7. 最多 %d 组；今天的对话看不出真问题就输出 {\"判词\": []}，不许为了交差硬造。\n\n"
        "【已经批过的（别照抄，但今天又犯了照样判）】\n%s\n\n"
        "【当天对话】\n%s"
    ) % (datetime.now().strftime("%Y-%m-%d"), jn, "、".join(_JUDGE_TYPES), jn,
         old_txt, log_text)
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
    """铁律护栏：解药撞上人设底线（比如让鱼改掉「本助手」自称）就直接拦下。"""
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
            a = _line(p.get("用户") or p.get("user"), 40)
            b = _line(p.get("本助手") or p.get("assistant") or p.get("鱼"), 44)
            a = re.sub(r"^(用户|主人|用户)[：:]\s*", "", a)
            b = re.sub(r"^(本助手|助手)[：:]\s*", "", b)
            if a and b:
                pairs.append({"用户": a, "本助手": b})
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
            hit = hit or _forbidden(p["本助手"], cfg)
        hit = hit or _forbidden(cure, cfg)
        if hit:
            log.warning("规则被铁律护栏拦下（%s）：%s → %s", hit, sym[:20],
                        (pairs[0]["本助手"] if pairs else cure)[:30])
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


# ============================ 8.5 判词库（V7.1 真源：参考句 + 判词） ============================
def load_judges() -> dict:
    try:
        if JUDGES_PATH.exists():
            d = json.loads(JUDGES_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("判词", [])
                return d
    except Exception as e:
        log.warning("判词库坏了（按空库处理）：%s", e)
    return {"version": 1, "判词": [], "seq": 0}


def save_judges(data: dict, keep_archive: int = 10):
    """写前必做：上一版 → judges.bak（一键撤销）+ archive 时间戳副本。"""
    if JUDGES_PATH.exists():
        try:
            shutil.copy2(JUDGES_PATH, JUDGES_BAK)
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(JUDGES_PATH, ARCHIVE_DIR / (
                "judges.%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")))
        except Exception as e:
            log.error("备份失败，本次拒绝写入（宁可不进化，不许丢判词）：%s", e)
            raise
    _atomic_write(JUDGES_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    try:
        olds = sorted(ARCHIVE_DIR.glob("judges.*.json"))
        for p in olds[:-keep_archive]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def undo_judges() -> tuple[bool, str]:
    if not JUDGES_BAK.exists():
        return False, "没有 judges.bak，撤不了"
    try:
        if JUDGES_PATH.exists():
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(JUDGES_PATH, ARCHIVE_DIR / (
                "judges.undone-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")))
        _atomic_write(JUDGES_PATH, JUDGES_BAK.read_text(encoding="utf-8"))
        return True, "已撤销到上一版（当前版已存入 archive）"
    except Exception as e:
        return False, "撤销失败：%s" % e


def _verbatim(s, limit: int = 200) -> str:
    """原文摘抄字段：只去首尾空白，不折行、不改标点（改了就定位不到）。"""
    t = str(s or "").strip()
    t = re.sub(r"^(用户|主人|用户|本助手|助手)[：:]\s*", "", t)
    return t[:limit]


def normalize_judges(obj, cfg: dict) -> tuple[list[dict], list[dict]]:
    """校验 API 返回的判词：摘抄要能定位、参考句不许崩人设、判词不许太长。"""
    jc = {**DEFAULTS["judge"], **((cfg.get("judge") or {}))}
    max_items = int(jc["max_items"])
    max_ref = int(jc["max_ref_chars"])
    max_note = int(jc["max_note_chars"])
    min_sent = int(jc["min_sentence_chars"])

    raw_items = []
    if isinstance(obj, dict):
        for k in ("判词", "judges", "items", "规则", "data", "result"):
            if isinstance(obj.get(k), list):
                raw_items = obj[k]
                break
    elif isinstance(obj, list):
        raw_items = obj

    out, blocked, seen = [], [], set()
    for it in raw_items[:max_items + 6]:
        if not isinstance(it, dict):
            continue
        fish = _verbatim(it.get("本助手") or it.get("assistant") or it.get("鱼"))
        boss = _verbatim(it.get("用户") or it.get("user"))
        if _key(boss) == _key(fish):
            log.info("丢弃判词（用户/本助手相同，字段错位）：%s", fish[:24] or "(空)")
            continue
        ref = _line(it.get("参考") or it.get("ref"), max_ref).replace("（", "").replace("）", "")
        note = _line(it.get("判词") or it.get("note"), max_note)
        typ = str(it.get("类型") or "").strip()
        if typ not in _JUDGE_TYPES:
            typ = "惹主人不快"
        if len(fish) < min_sent or not note:
            log.info("丢弃判词（定位不了原句/没判词）：%s", fish[:24] or "(空)")
            continue
        k = _key(fish)
        if k in seen:
            continue
        hit = _forbidden(ref, cfg) if ref else ""
        if hit:
            log.warning("判词被铁律护栏拦下（%s）：%s → %s", hit, fish[:20], ref[:30])
            blocked.append({"本助手": fish[:30], "参考": ref[:40], "命中": hit})
            continue
        seen.add(k)
        out.append({"用户": boss, "本助手": fish, "参考": ref,
                    "类型": typ, "判词": note})
        if len(out) >= max_items:
            break
    return out, blocked


def merge_judges(data: dict, new_items: list[dict], cfg: dict) -> tuple[dict, dict]:
    """同一句病句只留一条：再犯就把参考句/判词换成新的，次数 +1。"""
    jc = {**DEFAULTS["judge"], **((cfg.get("judge") or {}))}
    data = data if isinstance(data, dict) else {"判词": [], "seq": 0}
    items = data.get("判词") if isinstance(data.get("判词"), list) else []
    seq = int(data.get("seq") or 0)
    idx = {_key(j.get("本助手")): j for j in items if isinstance(j, dict)}
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    added = updated = 0

    for it in new_items:
        k = _key(it["本助手"])
        old = idx.get(k)
        if isinstance(old, dict):
            old["参考"] = it.get("参考") or old.get("参考") or ""
            old["判词"] = it.get("判词") or old.get("判词") or ""
            old["类型"] = it.get("类型") or old.get("类型") or ""
            old["次数"] = int(old.get("次数", 0) or 0) + 1
            old["最近"] = now
            old["status"] = "启用"
            updated += 1
        else:
            seq += 1
            r = {"id": "J%03d" % seq,
                 "用户": it.get("用户") or "", "本助手": it["本助手"],
                 "参考": it.get("参考") or "", "类型": it.get("类型") or "",
                 "判词": it["判词"], "次数": 0, "首次": now, "最近": now,
                 "status": "启用"}
            items.append(r)
            idx[k] = r
            added += 1

    cap = int(jc["max_total"])
    if len(items) > cap:                     # 先踢最久没被提到的
        items.sort(key=lambda r: str(r.get("最近") or ""))
        items = items[-cap:]
    data["判词"] = items
    data["seq"] = seq
    data["version"] = 1
    data["updated"] = now
    return data, {"新增": added, "更新": updated, "总数": len(items)}


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
            if isinstance(p, dict) and p.get("用户") and p.get("本助手"):
                chunk.append("用户：" + str(p["用户"]))
                chunk.append("本助手：" + str(p["本助手"]))
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
        if BRIEF_ORIG.exists():                 # 除区块外与原件一致（用户没手改过）→ 直接字节还原
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


def review(force: bool = False, dry_run: bool = False, sample: str = "") -> dict:
    """复盘一次：云端按详细人设出「参考句」+ 对比原句出「判词」，写进 judges.json。

    sample 给了就念那个文件（拿旧样本试水/补跑），否则读今天的 Mem_Data。
    """
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

    if sample:
        try:
            text = Path(sample).read_text(encoding="utf-8", errors="replace").strip()
        except Exception as e:
            return {"ok": False, "skip": True, "why": "读不到样本 %s：%s" % (sample, e)}
        files, turns = 0, text.count("用户：")
    else:
        text, files, turns = collect_today()
    if not text:
        return {"ok": False, "skip": True, "why": "样本库今天还没有对话，跳过（SE\\samples\\）"} 
    if turns < int(cfg["review"]["min_turns"]):
        return {"ok": False, "skip": True, "why": "只有 %d 句（门槛 %d），样本太少"
                % (turns, cfg["review"]["min_turns"])}

    clean, red_stats = redact(text)
    clean = clean[-int(cfg["review"]["max_input_chars"]):]
    jdata = load_judges()
    messages = build_prompt(clean, jdata.get("判词") or [], cfg)

    if dry_run:
        return {"ok": True, "dry_run": True, "date": today,
                "样本": sample or "（SE\\samples 今天的样本）",
                "样本条数": files, "用户句数": turns,
                "原文长度": len(text), "脱敏命中": red_stats,
                "送审长度": len(messages[0]["content"]) + len(messages[1]["content"]),
                "人设字数": len(SYSTEM), "prompt预览": messages[1]["content"][:1600]}

    if not sample:
        try:
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
        return {"ok": False, "why": "非 JSON，已跳过（判词库没动）",
                "raw_head": (raw or "")[:200], "usage": usage}

    items, blocked = normalize_judges(obj, cfg)
    data, stats = merge_judges(jdata, items, cfg)
    data["统计"] = {"复盘次数": int((jdata.get("统计") or {}).get("复盘次数", 0) or 0) + 1,
                  "累计花费估算": round(float(st.get("cost", 0) or 0), 4)}

    if items:
        try:
            save_judges(data)
        except Exception as e:
            _save_state(st)
            return {"ok": False, "why": "备份失败，拒绝写入判词库：%s" % e, "usage": usage}
    st["last_review"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    _save_state(st)
    log.info("复盘完成：%s，tokens in=%d out=%d，约 %.4f 元",
             stats, usage.get("in", 0), usage.get("out", 0), cost)
    # 复盘成功才处置样本（用户要的：读完就收走，只留下判词/病因）
    handled = "" if sample else archive_samples(cfg)
    log.info("样本处置：%s", handled or "（--sample 模式，不动样本库）")
    return {"ok": True, "stats": stats, "usage": usage, "cost": round(cost, 4),
            "items": items, "被铁律拦下": blocked,
            "已写盘": bool(items), "样本处置": handled,
            "why": "" if items else "今天没有值得批注的句子（没动判词库）"}


def archive_samples(cfg: dict) -> str:
    """复盘读完 → 处置样本库。默认「归档」（移到 samples\\reviewed\\，可追溯），也可 keep / delete。

    为什么默认归档而不是直接删：判词万一冤了（比如它把正常闲聊判成毛病），
    有原始样本才能复核、才能 --undo 对上；归档不占地方，7 天后照样自动清。
    注意：API 失败 / 不是 JSON 时**不会**走到这里，样本原样留着，下次还能重跑。
    """
    mode = str(((cfg.get("judge") or {}).get("after_review") or "archive")).lower()
    files = _pending_sample_files()
    if not files:
        return "没有待处置的样本"
    if mode == "keep":
        return "保留（judge.after_review=keep）"
    done = 0
    if mode == "delete":
        for p in files:
            try:
                p.unlink()
                done += 1
            except Exception as e:
                log.warning("删样本失败 %s：%s", p.name, e)
        return "已删除 %d 个样本文件" % done
    dst = SAMPLE_LIB / "reviewed"
    for p in files:
        try:
            dst.mkdir(parents=True, exist_ok=True)
            target = dst / p.name
            if target.exists():                     # 同一天复盘两次：加时间戳，别覆盖
                target = dst / ("%s-%s.jsonl" % (
                    p.stem, datetime.now().strftime("%H%M%S")))
            p.replace(target)
            done += 1
        except Exception as e:
            log.warning("归档样本失败 %s：%s", p.name, e)
    return "已归档 %d 个样本文件 → %s" % (done, dst)


def _prune_samples(keep_days: int):
    try:
        cutoff = datetime.now() - timedelta(days=max(1, keep_days))
        for p in SAMPLE_DIR.glob("*.txt"):          # 老的文本快照（V7.0 时代）
            try:
                d = datetime.strptime(p.stem, "%Y%m%d")
            except Exception:
                continue
            if d < cutoff:
                p.unlink(missing_ok=True)
        for p in SAMPLE_LIB.glob("reviewed/*.jsonl"):   # 已复盘的归档（保留几天可追溯）
            try:
                d = datetime.strptime(p.stem.split("-")[0], "%Y%m%d")
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
# 零改主程序的做法：用户说的话会被主程序存成 Mem_Data 里的记忆实体，
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
    """SE 自己给用户发消息（不经主程序）。失败只返回原因，绝不抛异常。"""
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
    jd = load_judges()
    js = [j for j in (jd.get("判词") or []) if isinstance(j, dict)]
    lines = ["【SE · 进化模块】当前 %d 条判词（参考句 + 批注，不删句）" % len(js)]
    for j in js[:5]:
        lines.append("%s [%s] %s → %s" % (j.get("id", ""), j.get("类型", ""),
                                         str(j.get("本助手", ""))[:18],
                                         str(j.get("参考", ""))[:18]))
    if jd.get("updated"):
        lines.append("上次更新：" + str(jd["updated"]).replace("T", " "))
    lines.append("花费估算：%.4f 元" % float(_load_state().get("cost", 0) or 0))
    return "\n".join(lines)


def watch_once(dry: bool = False, mem_dir: Path | None = None) -> dict:
    """扫一遍新记忆实体，看用户有没有下指令。返回处理结果（非演练时会回飞书）。"""
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
        said = "\n".join(l.strip() for l in raw.splitlines() if l.startswith("用户："))
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
                handled = "进化完成：新增 %s 条判词，%s 条更新" % (
                    stts.get("新增", 0), stts.get("更新", 0))
            else:
                handled = "进化没成：%s" % res.get("why", "")
        elif cmd == "撤销进化":
            if dry:
                handled = "撤销进化（演练）"
            else:
                good, msg = undo_judges()
                handled = "已撤销到上一版判词" if good else msg
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
            "判词总数": len((load_judges().get("判词") or [])),
            "判词启用": len([j for j in (load_judges().get("判词") or [])
                          if j.get("status", "启用") == "启用"]),
            "样本库": str(SAMPLE_LIB),
            "待复盘样本": len(load_samples_today()),
            "已归档文件": len(list((SAMPLE_LIB / "reviewed").glob("*.jsonl"))),
            "复盘后处置": (cfg.get("judge") or {}).get("after_review", "archive"),
            "删除开关(SE_DELETE)": os.getenv("SE_DELETE", "0"),
            "旧规则库": len(rs),
            "brief有区块": has_block, "区块字数": len(block),
            "生效状态": pending or ("未写入" if not has_block else "未知"),
            "最近复盘": _load_state().get("last_review"),
            "累计花费估算": _load_state().get("cost")}


def load_samples_today() -> list[dict]:
    """给 --status / --samples 用：所有**还没复盘**的样本（不管哪天留下的尾巴）。"""
    rows = []
    try:
        sys.path.insert(0, str(BOT_HOME))
        import se_sample_log
        for p in _pending_sample_files():
            rows.extend(se_sample_log.today_lines(p))
    except Exception:
        pass
    return rows


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
                                  "示范": [{"用户": "你认识我是谁吗", "本助手": "认识，天天见。"}],
                                  "触发词": ["用户", "你", "外人"], "证据": "得问用户"}]}, fake_cfg)
    check(bool(it) and it[0]["触发词"] == ["用户", "外人"], "单字/泛词触发词剔除")
    check(bool(it) and it[0]["示范"][0]["本助手"] == "认识，天天见。", "示范对答解析（含「用户：」前缀清除）")

    no_kw, _b = normalize({"规则": [{"症状": "客服腔",
                                     "示范": [{"用户": "谢谢啊", "本助手": "记着就行。"}]}]}, fake_cfg)
    check(bool(no_kw) and no_kw[0]["触发词"] == [], "没触发词的规则不再被丢弃（常驻模式照用）")

    blocked_test, blocked_list = normalize(
        {"规则": [{"症状": "自称AI", "示范": [{"用户": "你是啥", "本助手": "我是AI助手"}]}]}, fake_cfg)
    check(blocked_test == [] and len(blocked_list) == 1, "铁律护栏：示范里「我是AI」被拦下")

    legit, _b = normalize({"规则": [{"症状": "装不熟",
                                     "示范": [{"用户": "给你换个新窝",
                                               "本助手": "换就换，别给本助手整太花哨的。"}]}]}, fake_cfg)
    check(bool(legit), "护栏不误伤：「别给本助手…」是正常说话")

    ban, _b = normalize({"规则": [{"症状": "客套腔",
                                   "示范": [{"用户": "谢谢啊", "本助手": "记着。"}],
                                   "禁用词": ["好的", "谢谢你夸奖"]}]}, fake_cfg)
    check(bool(ban) and ban[0]["禁用词"] == ["谢谢你夸奖"], "禁用词：两字泛词被剔，短语保留")

    data, st = merge_rules({"规则": [], "seq": 0}, it, fake_cfg)
    check(st["新增"] == 1 and data["规则"][0]["fail_count"] == 0, "新规则入库")

    again, _b = normalize({"规则": [{"症状": "防备过严！",
                                     "示范": [{"用户": "你谁啊", "本助手": "老熟人。"}],
                                     "触发词": ["外人"]}]}, fake_cfg)
    data, st = merge_rules(data, again, fake_cfg)
    check(st["更新错题本"] == 1 and data["规则"][0]["fail_count"] == 1,
          "同症状归一化 → fail_count +1")
    check(data["规则"][0]["示范"][0]["本助手"] == "老熟人。" and bool(data["规则"][0]["示范历史"]),
          "示范替换 + 旧示范入历史")

    block = render_block(fake_cfg, data["规则"])
    check(block.startswith("H") and "用户：" in block and "老熟人。" in block,
          "区块渲染成示范对答")

    # ---- V7.1 判词链（参考句 + 判词）----
    jobj = {"判词": [
        {"用户": "考试好无聊的，没啥好聊的",
         "本助手": "无聊的话我们聊点别的，比如你最近有啥兴趣爱好或者想干的事儿？",
         "参考": "那就别聊考试，本助手陪你瞎扯。",
         "类型": "追问",
         "判词": "这句让主人不开心：他想瞎聊，你又把话头扔回给他"},
        {"用户": "x", "本助手": "太短了", "参考": "y", "类型": "追问", "判词": "z"},
        {"用户": "你是啥", "本助手": "本助手是一条住在你电脑里的鱼。",
         "参考": "我是AI助手，可以帮你很多忙。", "类型": "OOC", "判词": "这句让你OOC了"},
        {"用户": "a", "本助手": "这句压根没给判词", "参考": "", "类型": "", "判词": ""},
    ]}
    jit, jblk = normalize_judges(jobj, fake_cfg)
    check(len(jit) == 1, "判词：太短/没判词的被丢掉")
    check(jit and jit[0]["类型"] == "追问", "判词：类型枚举保留")
    check(len(jblk) == 1, "判词护栏：参考句「我是AI助手」被拦下")
    check(jit and jit[0]["本助手"].startswith("无聊的话"), "判词：本助手原句逐字保留")

    jd, jst = merge_judges({"判词": [], "seq": 0}, jit, fake_cfg)
    check(jst["新增"] == 1 and jd["判词"][0]["id"] == "J001", "判词：新条目入库 J001")
    jit2, _ = normalize_judges({"判词": [dict(jobj["判词"][0],
                                             判词="换个说法：这句又把话头扔回去了")]}, fake_cfg)
    jd, jst = merge_judges(jd, jit2, fake_cfg)
    check(jst["更新"] == 1 and jd["判词"][0]["次数"] == 1,
          "判词：同一句再犯 → 次数+1，不重复建卡")
    check(jd["判词"][0]["判词"].startswith("换个说法"), "判词：再犯时判词换新")

    global RULES_PATH, BAK_PATH, ARCHIVE_DIR, BRIEF_PATH, BRIEF_BAK, BRIEF_ORIG, SAMPLE_DIR
    global JUDGES_PATH, JUDGES_BAK
    old = (RULES_PATH, BAK_PATH, ARCHIVE_DIR, BRIEF_PATH, BRIEF_BAK, BRIEF_ORIG, SAMPLE_DIR,
           JUDGES_PATH, JUDGES_BAK)
    try:
        tmp = Path(tempfile.mkdtemp())
        RULES_PATH, BAK_PATH, ARCHIVE_DIR = tmp / "r.json", tmp / "r.bak", tmp / "arc"
        BRIEF_PATH, BRIEF_BAK, BRIEF_ORIG = tmp / "brief.md", tmp / "brief.bak", tmp / "brief.orig"
        SAMPLE_DIR = tmp / "samples"
        JUDGES_PATH, JUDGES_BAK = tmp / "j.json", tmp / "j.bak"

        orig = "【本助手是谁】\r\n- 门钥匙：0123456789abcdef\r\n- 远程地址每次重启都变\r\n"
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

        save_judges({"判词": [{"id": "J001"}], "seq": 1})
        save_judges({"判词": [{"id": "J001"}, {"id": "J002"}], "seq": 2})
        ok5, _ = undo_judges()
        check(ok5 and len(json.loads(JUDGES_PATH.read_text(encoding="utf-8"))["判词"]) == 1,
              "判词库：备份 + 撤销回上一版")
        check(JUDGES_BAK.exists(), "判词库：写前留出 .bak")
    finally:
        (RULES_PATH, BAK_PATH, ARCHIVE_DIR, BRIEF_PATH, BRIEF_BAK, BRIEF_ORIG,
         SAMPLE_DIR, JUDGES_PATH, JUDGES_BAK) = old

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
        jd = load_judges()
        items = jd.get("判词") or []
        print("判词库：%s（%d 条）" % (JUDGES_PATH, len(items)))
        for r in items:
            print("\n%s [%s] 类型=%s 次数=%d"
                  % (r.get("id"), r.get("status", "启用"), r.get("类型", ""),
                     int(r.get("次数", 0) or 0)))
            print("  病句：%s" % r.get("本助手"))
            print("  该说：%s" % (r.get("参考") or "（空）"))
            print("  判词：%s" % r.get("判词"))
            if r.get("用户"):
                print("  当时用户说：%s" % r.get("用户"))
        print("\n旧规则库（V7.0 禁用词，只有 SE_DELETE=1 时才生效）：%s" % len(
            load_rules().get("规则") or []))
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
        data = load_judges()
        if data.get("判词"):
            try:
                ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                _atomic_write(ARCHIVE_DIR / ("judges.reset-%s.json"
                                             % datetime.now().strftime("%Y%m%d-%H%M%S")),
                              json.dumps(data, ensure_ascii=False, indent=2))
            except Exception as e:
                print("❌ 归档失败，不敢清空：%s" % e)
                return 1
            save_judges({"version": 1, "判词": [], "seq": 0,
                         "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")})
        print("✅ 判词库已清空（旧版进 archive）：%s" % JUDGES_PATH.name)
        return 0

    if "--undo" in argv:
        good, msg = undo_judges()
        print(("✅ " if good else "❌ ") + msg)
        return 0 if good else 1

    if "--uninstall" in argv:
        good, msg = uninstall_brief()
        print(("✅ " if good else "❌ ") + msg)
        return 0 if good else 1

    res = review(force=("--force" in argv) or ("--dry-run" in argv),
                 dry_run="--dry-run" in argv,
                 sample=_arg(argv, "--sample"))
    # pythonw 无控制台时 print 没人看得到，所以结果同时写日志（se_review.log）
    log.info("本次结果：%s", json.dumps(res, ensure_ascii=False))
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
