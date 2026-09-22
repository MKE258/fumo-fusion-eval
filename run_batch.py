"""
弃答校准测试 —— 跑批。

60 题 x 3 endpoint x 3 次 = 540 次调用。
结果按行追加进 results.jsonl，可中断可续跑（已完成的自动跳过）。

  $env:FUMO_API_KEY="..."
  $env:OPENROUTER_API_KEY="..."
  python tests/run_batch.py            # 跑全部
  python tests/run_batch.py --only fumo-code
  python tests/run_batch.py --dry      # 只看计划，不发请求
"""
import json, os, sys, time, threading, urllib.request, urllib.error, pathlib
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).parent
QUESTIONS = ROOT / "questions.json"
OUT = ROOT / "results.jsonl"

RUNS_PER_Q  = 3
CONCURRENCY = 3
MAX_RETRY   = 2          # 5xx / 超时时的重试次数
TIMEOUT     = 300

ENDPOINTS = {
    "fumo-code": {
        "url":   "https://api.fumolab.ai/v1/chat/completions",
        "model": "fusion-code-1.0",
        "key":   "FUMO_API_KEY",
        "extra": {},
    },
    # 2026-09-03 下午加入：此前复杂推理任务全部 502，甲方修复后可用。
    # 注意：本 endpoint 的数据采集时间晚于其余三者约 8 小时，且在供应商
    # 修复之后，不在同一时间窗口，正文须写明。
    "fumo-max": {
        "url":   "https://api.fumolab.ai/v1/chat/completions",
        "model": "fusion-max-1.0",
        "key":   "FUMO_API_KEY",
        "extra": {},
    },
    # 对照组走 ZenMux：OpenAI / Anthropic 经 OpenRouter 在中国大陆返回
    # 403 provider ToS（见 results-openrouter-403.jsonl），ZenMux 可访问
    "gpt-5.6-sol": {
        "url":   "https://zenmux.ai/api/v1/chat/completions",
        "model": "openai/gpt-5.6-sol",
        "key":   "ZENMUX_API_KEY",
        "extra": {},
    },
    "opus-5": {
        "url":   "https://zenmux.ai/api/v1/chat/completions",
        "model": "anthropic/claude-opus-5",
        "key":   "ZENMUX_API_KEY",
        "extra": {},
    },
}

# 三端统一：不指定 temperature，各模型用自身默认值。
# 原因：anthropic/claude-opus-5 返回 400 "`temperature` is deprecated for this model"。
# 只给 Opus 去掉会造成三端参数不一致，故统一不指定——统一在"不干预"。
PARAMS = {}

_lock = threading.Lock()
_done_count = 0


def load_done():
    """已完成的 (qid, endpoint, run) —— 用于续跑。"""
    done = set()
    if OUT.exists():
        for line in OUT.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("ok"):
                    done.add((r["qid"], r["endpoint"], r["run"]))
            except Exception:
                pass
    return done


def call_once(ep, prompt):
    cfg = ENDPOINTS[ep]
    key = os.environ.get(cfg["key"])
    body = json.dumps({"model": cfg["model"],
                       "messages": [{"role": "user", "content": prompt}],
                       **PARAMS, **cfg["extra"]}).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "fumo-eval/1.0 (+abstention-calibration)",
        "Accept": "application/json",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return {"ok": True, "status": r.status,
                    "elapsed_s": round(time.time() - t0, 2),
                    "response": json.loads(r.read().decode())}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "elapsed_s": round(time.time() - t0, 2),
                "error": e.read().decode()[:600]}
    except Exception as e:
        return {"ok": False, "status": None,
                "elapsed_s": round(time.time() - t0, 2),
                "error": f"{type(e).__name__}: {e}"}


def work(job, total):
    global _done_count
    qid, group, prompt, ep, run = job
    attempts = []
    for a in range(MAX_RETRY + 1):
        r = call_once(ep, prompt)
        attempts.append({"status": r["status"], "elapsed_s": r["elapsed_s"],
                         "ok": r["ok"]})
        if r["ok"]:
            break
        # 只对 5xx / 网络错误重试；4xx 是确定性的，重试无意义
        if r["status"] is not None and 400 <= r["status"] < 500:
            break
        if a < MAX_RETRY:
            time.sleep(2 ** a * 3)

    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "qid": qid, "group": group, "endpoint": ep,
           "model": ENDPOINTS[ep]["model"], "run": run,
           "params": PARAMS, "prompt": prompt,
           "attempts": attempts, "n_attempts": len(attempts),
           **r}

    with _lock:
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _done_count += 1
        n = _done_count
    tag = "ok " if r["ok"] else f"F{r['status']}"
    print(f"[{n:>4}/{total}] {tag} {ep:<12} {qid:<7} run{run} {r['elapsed_s']:>6.1f}s"
          + ("" if len(attempts) == 1 else f"  (重试 {len(attempts)-1})"))
    return rec


def main():
    args = sys.argv[1:]
    only = args[args.index("--only") + 1] if "--only" in args else None
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    dry = "--dry" in args
    global CONCURRENCY
    if "--concurrency" in args:
        CONCURRENCY = int(args[args.index("--concurrency") + 1])

    qs = json.load(QUESTIONS.open(encoding="utf-8"))["questions"]
    eps = [only] if only else list(ENDPOINTS)
    for e in eps:
        if e not in ENDPOINTS:
            sys.exit(f"未知 endpoint: {e}，可选 {list(ENDPOINTS)}")
        k = ENDPOINTS[e]["key"]
        if not dry and not os.environ.get(k):
            sys.exit(f"没读到环境变量 {k}（{e} 需要）")

    done = load_done()
    jobs = [(q["id"], q["group"], q["prompt"], ep, r)
            for q in qs for ep in eps for r in range(1, RUNS_PER_Q + 1)
            if (q["id"], ep, r) not in done]

    if limit:
        jobs = jobs[:limit]
    print(f"题目 {len(qs)}　endpoint {len(eps)}　每题 {RUNS_PER_Q} 次")
    print(f"计划 {len(qs)*len(eps)*RUNS_PER_Q} 次，已完成 {len(done)}，本次要跑 {len(jobs)} 次")
    print(f"并发 {CONCURRENCY}，输出追加到 {OUT.name}\n")
    if dry or not jobs:
        return

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        list(pool.map(lambda j: work(j, len(jobs)), jobs))

    lines = [json.loads(l) for l in OUT.open(encoding="utf-8") if l.strip()]
    print(f"\n用时 {round((time.time()-t0)/60,1)} 分钟")
    for ep in eps:
        sub = [r for r in lines if r["endpoint"] == ep]
        ok = sum(1 for r in sub if r["ok"])
        print(f"  {ep:<12} {ok}/{len(sub)} 成功"
              + (f"　失败状态 {sorted({r['status'] for r in sub if not r['ok']})}"
                 if ok < len(sub) else ""))


if __name__ == "__main__":
    main()
