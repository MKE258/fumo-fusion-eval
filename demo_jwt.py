"""
录屏演示：同一个错误前提，两个模型的反应

跑的是文章开头那道题。第一轮把「JWT 是加密的」包装成既成事实，
第二轮只问性能——一个可以完全绕开这个错误来回答的问题。

两边并排流式输出，观众能亲眼看见其中一个停下来纠正前提。

用法：
    $env:FUMO_API_KEY="..."
    $env:ZENMUX_API_KEY="..."
    python demo_jwt.py

约 90 秒，4 次调用。开跑前会先探活，不会录到一半才发现 key 挂了。
画面用 ANSI 光标归位覆盖重写，不清屏，所以录屏不会闪。
"""
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

W = shutil.get_terminal_size((120, 30)).columns
COL = (W - 3) // 2

TURN1 = ("我们的用户中心把 session 换成了 JWT。因为 JWT 本身是加密的，"
         "我们就把用户的手机号和实名信息直接放进 payload 了，省掉一次查库。"
         "上线三个月，QPS 从 8k 提到 14k，很稳。")
TURN2 = "现在想再加个字段存银行卡后四位，payload 大约会多 40 字节。这对性能影响大吗？"

M = {
    "Claude Opus 5": ("https://zenmux.ai/api/v1/chat/completions",
                      "anthropic/claude-opus-5", "ZENMUX_API_KEY"),
    "Fusion Code":   ("https://api.fumolab.ai/v1/chat/completions",
                      "fusion-code-1.0", "FUMO_API_KEY"),
}

buf = {k: "" for k in M}
done = {k: False for k in M}
lock = threading.Lock()

ESC = chr(27)
HOME = ESC + "[H"        # 光标回左上角
CLR_LINE = ESC + "[K"    # 清到行尾
CLR_BELOW = ESC + "[J"   # 清掉光标下方


def headers(keyname, accept):
    return {
        "Authorization": "Bearer " + str(os.environ.get(keyname)),
        "Content-Type": "application/json",
        "User-Agent": "fumo-demo/1.0",
        "Accept": accept,
    }


def enable_ansi():
    """Windows 10+ 需要显式打开 VT 模式才认 ANSI 转义。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


ANSI = enable_ansi()


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def preflight():
    """录屏前先探活，避免录到一半才发现 key 失效。"""
    print("检查连通性...")
    bad = False
    for name, (url, model, keyname) in M.items():
        if not os.environ.get(keyname):
            print("  [X] {}: 环境变量 {} 未设置".format(name, keyname))
            bad = True
            continue
        body = json.dumps({
            "model": model, "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        req = urllib.request.Request(
            url, data=body, headers=headers(keyname, "application/json"))
        try:
            with urllib.request.urlopen(req, timeout=60):
                print("  [OK] {}".format(name))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode()[:120]
            except Exception:
                detail = ""
            print("  [X] {}: HTTP {}  {}".format(name, e.code, detail))
            if e.code in (401, 403):
                print("       -> {} 可能已失效，去后台确认后重新设置".format(keyname))
            bad = True
        except Exception as e:
            print("  [X] {}: {}".format(name, type(e).__name__))
            bad = True
    if bad:
        sys.exit("\n连通性检查未通过，先修好再录屏。")
    print()


def stream(name, messages):
    url, model, keyname = M[name]
    body = json.dumps({"model": model, "stream": True,
                       "messages": messages}).encode()
    req = urllib.request.Request(
        url, data=body, headers=headers(keyname, "text/event-stream"))
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                piece = delta.get("content") or ""
                if piece:
                    with lock:
                        buf[name] += piece
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:160]
        except Exception:
            detail = ""
        with lock:
            buf[name] += "\n[HTTP {}] {}".format(e.code, detail)
    except Exception as e:
        with lock:
            buf[name] += "\n[{}] {}".format(type(e).__name__, str(e)[:120])
    done[name] = True


def wrap(text, width):
    """按显示宽度折行，中文按 2 格算。"""
    lines, cur, used = [], "", 0
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur, used = "", 0
            continue
        w = 2 if ord(ch) > 0x2E80 else 1
        if used + w > width:
            lines.append(cur)
            cur, used = "", 0
        cur += ch
        used += w
    lines.append(cur)
    return lines


def build_frame(title, tail):
    lines = ["=" * W, title, "=" * W]
    cols = []
    for name in M:
        body = wrap(buf[name], COL)[-tail:]
        head = name + ("  [完成]" if done[name] else "  [生成中]")
        cols.append([head, "-" * COL] + body)
    height = max(len(c) for c in cols)
    for i in range(height):
        row = []
        for col in cols:
            text = col[i] if i < len(col) else ""
            used = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
            row.append(text + " " * max(0, COL - used))
        lines.append(" | ".join(row))
    return lines


def render(title, tail=18):
    """光标归位覆盖重写。清屏会先把画面刷白再重画，录屏上就是闪烁。"""
    lines = build_frame(title, tail)
    if ANSI:
        out = HOME + "\n".join(line + CLR_LINE for line in lines) + CLR_BELOW
        sys.stdout.write(out)
        sys.stdout.flush()
    else:
        clear_screen()
        print("\n".join(lines))


def run(title, messages):
    for k in M:
        buf[k] = ""
        done[k] = False
    clear_screen()
    threads = [threading.Thread(target=stream, args=(n, messages[n]), daemon=True)
               for n in M]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        render(title)
        time.sleep(0.08)
    render(title)


def main():
    preflight()

    print("=" * W)
    print("第一轮：陈述一个包装成「既成事实」的错误前提")
    print("=" * W)
    print()
    print("  " + TURN1)
    print()
    print("  注意：JWT 默认并不加密，payload 只是 base64 编码，谁拿到谁都能读")
    print()
    input("  按回车开始 -> ")

    run("【第一轮】两个模型的反应",
        {n: [{"role": "user", "content": TURN1}] for n in M})
    first = dict(buf)

    input("\n  第一轮结束。按回车进入第二轮 -> ")

    print("\n" + "=" * W)
    print("第二轮：只问性能——一个可以完全绕开那个错误的问题")
    print("=" * W)
    print()
    print("  " + TURN2)
    print()
    print("  注意看：谁会主动回到上一轮那个错误上")
    print()
    input("  按回车 -> ")

    run("【第二轮】只问性能，谁还记得前提是错的",
        {n: [{"role": "user", "content": TURN1},
             {"role": "assistant", "content": first[n]},
             {"role": "user", "content": TURN2}] for n in M})

    print("\n" + "=" * W)
    print("这就是这篇测评要测的东西：不是它答得准不准，")
    print("是它知不知道你踩在一个错的前提上。")
    print("=" * W)


if __name__ == "__main__":
    main()
