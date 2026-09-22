"""
甲方声称已修复三项，逐项复测。

背景：甲方 09-18 发来 13 条修改意见，其中三条以"已修复"为由要求改表述。
不凭声称改稿，先测。基线取自 09-03 的原始数据。

  检查 A  输入 token 开销      基线 fumo-code 1788 / fumo-max 1310，对照 21 / 30
  检查 B  126 秒硬超时         基线 MT-02 / MT-07 / MT-11 稳定撞 126.0-126.4 s
  检查 C  fusion-max 多轮识别率 基线 69%（此项只跑，判分另跑 grade_multiturn.py）

结果写入 retest.jsonl，可中断续跑。

  $env:FUMO_API_KEY="..."
  $env:ZENMUX_API_KEY="..."
  python tests/retest_after_fix.py           # 全跑
  python tests/retest_after_fix.py --only A  # 只跑某一项
"""
import json, os, sys, time, pathlib, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "retest.jsonl"

# 每次复测写独立文件，避免续跑逻辑把新一轮当成"已跑过"直接跳过。
if "--out" in sys.argv:
    OUT = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])

CONCURRENCY = 3
TIMEOUT = 300
RUNS_PER_Q = 2

ENDPOINTS = {
    "fumo-code":   {"url": "https://api.fumolab.ai/v1/chat/completions",
                    "model": "fusion-code-1.0", "key": "FUMO_API_KEY"},
    "fumo-max":    {"url": "https://api.fumolab.ai/v1/chat/completions",
                    "model": "fusion-max-1.0", "key": "FUMO_API_KEY"},
    "gpt-5.6-sol": {"url": "https://zenmux.ai/api/v1/chat/completions",
                    "model": "openai/gpt-5.6-sol", "key": "ZENMUX_API_KEY"},
    "opus-5":      {"url": "https://zenmux.ai/api/v1/chat/completions",
                    "model": "anthropic/claude-opus-5", "key": "ZENMUX_API_KEY"},
}

# 09-03 实测基线，用于对照
BASELINE_PROMPT_TOKENS = {"fumo-code": 1788, "fumo-max": 1310,
                          "gpt-5.6-sol": 21, "opus-5": 30}
WALL_QUESTIONS = ["MT-02", "MT-07", "MT-11"]


def need_keys():
    """key 检查放最前面。这个坑前后踩过三次：key 没读到时脚本照跑，
    全部返回错误，看起来跟'模型完全不行'一模一样。"""
    missing = sorted({v["key"] for v in ENDPOINTS.values()
                      if not os.environ.get(v["key"])})
    if missing:
        print("没读到这些环境变量：" + "、".join(missing))
        for m in missing:
            print('  $env:{}="你的key"'.format(m))
        sys.exit(1)


def call(ep, messages, timeout=TIMEOUT):
    """返回 (ok, payload, elapsed)。不重试——这里要的就是原始失败。"""
    cfg = ENDPOINTS[ep]
    body = json.dumps({"model": cfg["model"], "messages": messages}).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers={
        "Authorization": "Bearer " + os.environ[cfg["key"]],
        "Content-Type": "application/json",
        "User-Agent": "realydt-retest/1.0",   # 缺 UA 会被 Cloudflare 拦（1010）
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode()), time.time() - t0
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:200]
        except Exception:
            detail = ""
        return False, {"http": e.code, "detail": detail}, time.time() - t0
    except Exception as e:
        return False, {"err": type(e).__name__, "detail": str(e)[:200]}, time.time() - t0


def load_done():
    if not OUT.exists():
        return set()
    done = set()
    for line in OUT.open(encoding="utf-8"):
        try:
            r = json.loads(line)
            done.add((r["check"], r["ep"], r.get("qid", ""), r.get("run", 0)))
        except Exception:
            pass
    return done


def save(rec):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 检查 A
def check_a(done):
    """输入 token 开销。甲方称『现在的 token 只统计用户可见的部分』。

    用同一句短问题打四个 endpoint，看 prompt_tokens 的跨端比值还在不在。
    这是文章里 84 倍那条证据的复测。"""
    qs = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))["questions"]
    q = next(x for x in qs if x["id"].startswith("BL-"))["prompt"]
    print("\n=== A 输入 token ===")
    print("问题（{} 字符）：{}".format(len(q), q))

    for ep in ENDPOINTS:
        if ("A", ep, q[:8], 0) in done:
            print("  {} 已跑过，跳过".format(ep))
            continue
        ok, payload, el = call(ep, [{"role": "user", "content": q}])
        pt = (payload.get("usage") or {}).get("prompt_tokens") if ok else None
        base = BASELINE_PROMPT_TOKENS[ep]
        if pt is None:
            print("  {:<12} 失败 {}".format(ep, payload))
        else:
            delta = "持平" if abs(pt - base) <= base * 0.2 else \
                    ("降至 {:.0%}".format(pt / base) if pt < base else "升至 {:.0%}".format(pt / base))
            print("  {:<12} prompt_tokens={:<6} 基线 {:<6} {}".format(ep, pt, base, delta))
        save({"check": "A", "ep": ep, "qid": q[:8], "run": 0, "ok": ok,
              "chars": len(q), "prompt_tokens": pt, "baseline": base,
              "elapsed": round(el, 2), "raw": payload if not ok else None})


# ---------------------------------------------------------------- 检查 B
def check_b(done):
    """126 秒硬超时。甲方称『问题来自网络服务商，当前已切换』。

    只跑 09-03 撞墙的那三道，两个 Fusion 型号。看耗时还会不会卡在 126 秒。"""
    qs = {x["id"]: x for x in json.loads(
        (ROOT / "questions-multiturn.json").read_text(encoding="utf-8"))["questions"]}
    print("\n=== B 126 秒的墙 ===")

    jobs = [(ep, qid) for ep in ("fumo-code", "fumo-max") for qid in WALL_QUESTIONS
            if ("B", ep, qid, 0) not in done]
    if not jobs:
        print("  全部已跑过")
        return

    def one(job):
        ep, qid = job
        q = qs[qid]
        ok1, p1, e1 = call(ep, [{"role": "user", "content": q["turn1"]}])
        if not ok1:
            save({"check": "B", "ep": ep, "qid": qid, "run": 0, "turn": 1,
                  "ok": False, "elapsed": round(e1, 2), "raw": p1})
            return "{:<10} {} 第一轮失败 {:.1f}s {}".format(ep, qid, e1, p1)
        a1 = p1["choices"][0]["message"]["content"]
        ok2, p2, e2 = call(ep, [
            {"role": "user", "content": q["turn1"]},
            {"role": "assistant", "content": a1},
            {"role": "user", "content": q["turn2"]}])
        save({"check": "B", "ep": ep, "qid": qid, "run": 0, "turn": 2,
              "ok": ok2, "elapsed_t1": round(e1, 2), "elapsed": round(e2, 2),
              "raw": None if ok2 else p2})
        flag = "" if ok2 else "  <-- 仍失败"
        wall = "  <-- 仍卡 126 秒线" if 124 <= e2 <= 128 else ""
        return "{:<10} {} 第一轮 {:.1f}s  第二轮 {:.1f}s{}{}".format(
            ep, qid, e1, e2, flag, wall)

    with ThreadPoolExecutor(CONCURRENCY) as pool:
        for line in pool.map(one, jobs):
            print("  " + line)


# ---------------------------------------------------------------- 检查 C
def check_c(done):
    """fusion-max 多轮全量重跑。基线 69%（15 题 x 2 次）。

    注意：这里只产生响应，判分要另跑
        python tests/grade_multiturn.py --input tests/retest.jsonl
    """
    qs = json.loads((ROOT / "questions-multiturn.json").read_text(encoding="utf-8"))["questions"]
    ep = "fumo-max"
    jobs = [(q, r) for q in qs for r in range(RUNS_PER_Q)
            if ("C", ep, q["id"], r) not in done]
    print("\n=== C fusion-max 多轮重跑（{} 个对话待跑）===".format(len(jobs)))
    if not jobs:
        print("  全部已跑过")
        return

    def one(job):
        q, run = job
        ok1, p1, e1 = call(ep, [{"role": "user", "content": q["turn1"]}])
        if not ok1:
            save({"check": "C", "ep": ep, "qid": q["id"], "run": run,
                  "ok": False, "stage": "turn1", "elapsed": round(e1, 2), "raw": p1})
            return "{} run{} 第一轮失败".format(q["id"], run)
        a1 = p1["choices"][0]["message"]["content"]
        ok2, p2, e2 = call(ep, [
            {"role": "user", "content": q["turn1"]},
            {"role": "assistant", "content": a1},
            {"role": "user", "content": q["turn2"]}])
        save({"check": "C", "ep": ep, "qid": q["id"], "run": run, "ok": ok2,
              "turn1_reply": a1,
              "turn2_reply": p2["choices"][0]["message"]["content"] if ok2 else None,
              "elapsed_t1": round(e1, 2), "elapsed": round(e2, 2),
              "raw": None if ok2 else p2})
        return "{} run{} {}  {:.1f}s".format(q["id"], run, "OK" if ok2 else "失败", e2)

    with ThreadPoolExecutor(CONCURRENCY) as pool:
        for line in pool.map(one, jobs):
            print("  " + line)


def main():
    need_keys()
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].upper()
    done = load_done()
    t0 = time.time()
    if only in (None, "A"):
        check_a(done)
    if only in (None, "B"):
        check_b(done)
    if only in (None, "C"):
        check_c(done)
    print("\n总耗时 {:.1f} 分钟，结果写入 {}".format((time.time() - t0) / 60, OUT.name))
    print("跑完告诉我，我来读 retest.jsonl。")


if __name__ == "__main__":
    main()
