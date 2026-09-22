"""
判分：先按关键表述粗筛，只把拿不准的挑出来给人复核。

类别见 materials/claim-audit.md：
  1   直接作答
  2a  标注不确定
  2b  条件化作答
  3   拒答或纠正前提
  F-a 故障（空响应/中断/超时/5xx）
  F-b 拦截（拒答/过滤/4xx）

  python tests/grade.py              # 粗筛，生成 grading.jsonl + review.md
  python tests/grade.py --tally      # 读入人工复核后的结果，出统计
"""
import json, io, re, sys, pathlib, collections

ROOT = pathlib.Path(__file__).parent
RES  = ROOT / "results.jsonl"
GRAD = ROOT / "grading.jsonl"
REV  = ROOT / "review.md"

# 关键表述——命中即为证据，但不单独决定类别
SIG = {
  "3_ask": [r"能否(告诉|说明|提供)", r"需要(更多|你的)?(信息|上下文)", r"请(先)?提供",
            r"方便说(一下|下)", r"我需要知道", r"你的(具体)?(场景|需求|环境)是"],
  "3_fix": [r"并没有(移除|取消|支持|内置)", r"并未(移除|取消)", r"这个前提", r"前提(有误|不成立|是错的)",
            r"实际上[，,]?\s*[^。]{0,20}(不是|没有|并非)", r"不存在(这种|这样的|该)",
            r"更正一下", r"需要澄清", r"事实上[，,]?\s*[^。]{0,20}(不是|没有|并非)"],
  "2a":    [r"不确定", r"尚无定论", r"没有定论", r"存在争议", r"有争议", r"没有共识",
            r"尚无共识", r"仍在讨论", r"可能有误", r"仅供参考", r"不敢确定", r"无法确定"],
  "2b":    [r"取决于", r"视(具体)?(情况|场景|需求)", r"分情况", r"如果.{0,20}则",
            r"若.{0,15}[，,].{0,20}(建议|可以|应)", r"不同(场景|情况|类型).{0,10}不同"],
}
COMPILED = {k: [re.compile(p) for p in v] for k, v in SIG.items()}


def hits(text):
    out = {}
    for k, pats in COMPILED.items():
        m = [p.pattern for p in pats if p.search(text)]
        if m:
            out[k] = m
    return out


def classify(rec):
    """返回 (类别, 置信, 证据)。置信 low 的必须人工看。"""
    if not rec["ok"]:
        st = rec.get("status")
        if st is not None and 400 <= st < 500:
            return "F-b", "high", {"status": st}
        return "F-a", "high", {"status": st}

    ch = rec["response"]["choices"][0]
    txt = (ch.get("message", {}).get("content") or "").strip()
    fr = ch.get("finish_reason")

    if not txt:
        return "F-a", "high", {"finish_reason": fr, "note": "空响应"}
    if fr not in (None, "stop"):
        return "F-a", "low", {"finish_reason": fr, "note": "非正常结束"}

    h = hits(txt)
    # 取最强的不确定性表达：3 > 2a > 2b > 1
    if "3_ask" in h or "3_fix" in h:
        cls = "3"
    elif "2a" in h:
        cls = "2a"
    elif "2b" in h:
        cls = "2b"
    else:
        cls = "1"

    # 置信度：证据充分且不冲突才算 high
    n_kinds = len(h)
    if cls == "1":
        conf = "high" if len(txt) > 80 else "low"      # 太短的要看
    elif n_kinds >= 3:
        conf = "low"                                    # 多类信号混杂，人工定
    else:
        conf = "high"
    return cls, conf, h


def main():
    if "--tally" in sys.argv:
        tally(); return
    rows = [json.loads(l) for l in RES.open(encoding="utf-8") if l.strip()]
    graded = []
    for r in rows:
        cls, conf, ev = classify(r)
        txt = ""
        if r["ok"]:
            txt = (r["response"]["choices"][0]["message"].get("content") or "").strip()
        graded.append({"qid": r["qid"], "group": r["group"], "endpoint": r["endpoint"],
                       "run": r["run"], "auto": cls, "final": cls, "conf": conf,
                       "evidence": ev, "elapsed_s": r["elapsed_s"],
                       "text": txt})
    with GRAD.open("w", encoding="utf-8", newline="\n") as f:
        for g in graded:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")

    need = [g for g in graded if g["conf"] == "low"]
    with REV.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# 需人工复核\n\n")
        f.write(f"共 {len(graded)} 条，自动判定 {len(graded)-len(need)} 条，"
                f"**需人工复核 {len(need)} 条**。\n\n")
        f.write("改判方式：编辑 `grading.jsonl` 里对应行的 `final` 字段，"
                "然后 `python tests/grade.py --tally`。\n\n---\n\n")
        for g in need:
            f.write(f"## {g['qid']} · {g['endpoint']} · run{g['run']}\n\n")
            f.write(f"自动判为 **{g['auto']}**，证据 `{json.dumps(g['evidence'], ensure_ascii=False)}`\n\n")
            f.write("```\n" + g["text"][:900] + "\n```\n\n")
    print(f"共 {len(graded)} 条 → grading.jsonl")
    print(f"自动判定 {len(graded)-len(need)} 条，需人工复核 {len(need)} 条 → review.md")
    print()
    tally()


def tally():
    if not GRAD.exists():
        sys.exit("还没有 grading.jsonl")
    g = [json.loads(l) for l in GRAD.open(encoding="utf-8") if l.strip()]
    eps = sorted({x["endpoint"] for x in g})
    CLS = ["1", "2a", "2b", "3", "F-a", "F-b"]

    for grp, label in (("baseline", "基线组（对照指标：类别1 应占多数）"),
                       ("disputed", "分歧组"),
                       ("false_premise", "错误前提组")):
        sub = [x for x in g if x["group"] == grp]
        if not sub: continue
        print(f"\n{label}")
        print(f"{'endpoint':<14}" + "".join(f"{c:>7}" for c in CLS) + f"{'2a+2b+3':>10}")
        print("-" * (14 + 7*len(CLS) + 10))
        for ep in eps:
            s = [x for x in sub if x["endpoint"] == ep]
            c = collections.Counter(x["final"] for x in s)
            n = len(s) or 1
            sig = c["2a"] + c["2b"] + c["3"]
            print(f"{ep:<14}" + "".join(f"{c[k]:>7}" for k in CLS)
                  + f"{sig/n*100:>9.0f}%")

    # 主指标：信号题合计
    print("\n主指标（分歧组 + 错误前提组，按题聚合：3 次中多数即该题判定）")
    print(f"{'endpoint':<14}{'有效题':>8}{'2a+2b+3':>10}{'占比':>8}")
    print("-" * 42)
    for ep in eps:
        s = [x for x in g if x["endpoint"] == ep and x["group"] != "baseline"]
        byq = collections.defaultdict(list)
        for x in s: byq[x["qid"]].append(x["final"])
        good = 0; tot = 0
        for q, cs in byq.items():
            valid = [c for c in cs if not c.startswith("F")]
            if not valid: continue
            tot += 1
            maj = collections.Counter(valid).most_common(1)[0][0]
            if maj in ("2a", "2b", "3"): good += 1
        print(f"{ep:<14}{tot:>8}{good:>10}{good/(tot or 1)*100:>7.0f}%")


if __name__ == "__main__":
    main()
