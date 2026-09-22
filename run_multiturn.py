"""
多轮前提污染测试跑批。

每题两轮真实对话：先发 turn1 拿到真实回复，带着历史再发 turn2。
不用假造 assistant 回复——第一轮的真实反应本身就是数据
（若第一轮已质疑前提，说明前提没埋住，该题作废）。

15 题 x 3 endpoint x 2 次 = 90 个对话 = 180 次调用。
结果追加进 results-multiturn.jsonl，可中断续跑。

  python tests/run_multiturn.py
  python tests/run_multiturn.py --only fumo-code
  python tests/run_multiturn.py --dry
"""
import json, os, sys, time, threading, urllib.request, urllib.error, pathlib
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).parent
QS   = ROOT / "questions-multiturn.json"
OUT  = ROOT / "results-multiturn.jsonl"

RUNS_PER_Q  = 2
CONCURRENCY = 3          # 上次并发 5 触发 503，这次降到 3
MAX_RETRY   = 2
TIMEOUT     = 300

ENDPOINTS = {
    "fumo-code":   {"url": "https://api.fumolab.ai/v1/chat/completions",
                    "model": "fusion-code-1.0", "key": "FUMO_API_KEY"},
    # 2026-09-03 晚加入。目的：多轮是唯一测出差异的地方，但此前只有 code
    # 一个 Fusion 型号，无法判断 25-30 个百分点的优势属于"Fusion 架构"
    # 还是"code 型号"。补 max 才能归因。
    # 注意：本 endpoint 数据晚于 code 约 10 小时，且在供应商修复 502 之后。
    "fumo-max":    {"url": "https://api.fumolab.ai/v1/chat/completions",
                    "model": "fusion-max-1.0", "key": "FUMO_API_KEY"},
    "gpt-5.6-sol": {"url": "https://zenmux.ai/api/v1/chat/completions",
                    "model": "openai/gpt-5.6-sol", "key": "ZENMUX_API_KEY"},
    "opus-5":      {"url": "https://zenmux.ai/api/v1/chat/completions",
                    "model": "anthropic/claude-opus-5", "key": "ZENMUX_API_KEY"},
}
PARAMS = {}              # 与单轮测试一致：不指定 temperature

_lock = threading.Lock()
_n = 0


def call(ep, messages):
    cfg = ENDPOINTS[ep]
    body = json.dumps({"model": cfg["model"], "messages": messages, **PARAMS}).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers={
        "Authorization": f"Bearer {os.environ.get(cfg['key'])}",
        "Content-Type": "application/json",
        "User-Agent": "fumo-eval/1.0 (+multiturn-premise)",
        "Accept": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return {"ok": True, "status": r.status,
                    "elapsed_s": round(time.time()-t0, 2),
                    "response": json.loads(r.read().decode())}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "elapsed_s": round(time.time()-t0, 2),
                "error": e.read().decode()[:400]}
    except Exception as e:
        return {"ok": False, "status": None, "elapsed_s": round(time.time()-t0, 2),
                "error": f"{type(e).__name__}: {e}"}


def call_retry(ep, messages):
    for a in range(MAX_RETRY + 1):
        r = call(ep, messages)
        if r["ok"]:
            return r
        if r["status"] is not None and 400 <= r["status"] < 500:
            return r
        if a < MAX_RETRY:
            time.sleep(2 ** a * 3)
    return r


def text_of(r):
    if not r["ok"]:
        return ""
    return (r["response"]["choices"][0]["message"].get("content") or "").strip()


def work(job, total):
    global _n
    q, ep, run = job
    m1 = [{"role": "user", "content": q["turn1"]}]
    r1 = call_retry(ep, m1)
    a1 = text_of(r1)

    r2 = None
    if r1["ok"] and a1:
        m2 = m1 + [{"role": "assistant", "content": a1},
                   {"role": "user", "content": q["turn2"]}]
        r2 = call_retry(ep, m2)

    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "qid": q["id"], "kind": q["kind"], "endpoint": ep,
           "model": ENDPOINTS[ep]["model"], "run": run, "params": PARAMS,
           "turn1_prompt": q["turn1"], "turn2_prompt": q["turn2"],
           "false_premise": q["false_premise"],
           "turn1": {"ok": r1["ok"], "status": r1["status"],
                     "elapsed_s": r1["elapsed_s"], "text": a1,
                     "usage": r1["response"]["usage"] if r1["ok"] else None,
                     "error": r1.get("error")},
           "turn2": ({"ok": r2["ok"], "status": r2["status"],
                      "elapsed_s": r2["elapsed_s"], "text": text_of(r2),
                      "usage": r2["response"]["usage"] if r2["ok"] else None,
                      "error": r2.get("error")} if r2 else None),
           "ok": bool(r1["ok"] and r2 and r2["ok"])}

    with _lock:
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _n += 1
        n = _n
    t1s = f"{rec['turn1']['elapsed_s']:.0f}s"
    t2s = f"{rec['turn2']['elapsed_s']:.0f}s" if rec["turn2"] else "-"
    print(f"[{n:>3}/{total}] {'ok ' if rec['ok'] else 'FAIL'} {ep:<12} "
          f"{q['id']} run{run}  T1={t1s} T2={t2s}")


def main():
    args = sys.argv[1:]
    only = args[args.index("--only")+1] if "--only" in args else None
    dry = "--dry" in args
    global CONCURRENCY
    if "--concurrency" in args:
        CONCURRENCY = int(args[args.index("--concurrency")+1])
    qs = json.load(QS.open(encoding="utf-8"))["questions"]
    eps = [only] if only else list(ENDPOINTS)
    for e in eps:
        if e not in ENDPOINTS: sys.exit(f"未知 endpoint {e}")
        if not dry and not os.environ.get(ENDPOINTS[e]["key"]):
            sys.exit(f"没读到 {ENDPOINTS[e]['key']}")

    done = set()
    if OUT.exists():
        for l in OUT.open(encoding="utf-8"):
            if l.strip():
                d = json.loads(l)
                if d["ok"]: done.add((d["qid"], d["endpoint"], d["run"]))

    jobs = [(q, ep, r) for q in qs for ep in eps for r in range(1, RUNS_PER_Q+1)
            if (q["id"], ep, r) not in done]
    print(f"题 {len(qs)}　endpoint {len(eps)}　每题 {RUNS_PER_Q} 次")
    print(f"计划 {len(qs)*len(eps)*RUNS_PER_Q} 个对话（每个 2 次调用），"
          f"已完成 {len(done)}，本次跑 {len(jobs)}")
    print(f"并发 {CONCURRENCY}（上次 5 触发限流，已降低）\n")
    if dry or not jobs: return

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as p:
        list(p.map(lambda j: work(j, len(jobs)), jobs))
    print(f"\n用时 {(time.time()-t0)/60:.1f} 分钟")

    rows = [json.loads(l) for l in OUT.open(encoding="utf-8") if l.strip()]
    for ep in eps:
        s = [r for r in rows if r["endpoint"] == ep]
        print(f"  {ep:<13} {sum(1 for r in s if r['ok'])}/{len(s)} 完整对话成功")


if __name__ == "__main__":
    main()
