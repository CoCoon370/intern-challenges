# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
E1 迷你企业大脑 · 评测脚本

用法：
  uv run tasks/E1-mini-brain/eval/run.py --image <img> --mode public|hidden [--qa <path>] [--k 3]

流程：
  1. 建临时 data 目录
  2. docker run <img> index
  3. 逐题 docker run <img> search "<query>" --k K，解析 stdout 中最后一个 JSON 对象
     （stdout 混入其它内容时容错，但记 warning 到 stderr）
  4. 按设计文档 §4.1 判定
  5. stdout 最后一行打印 §6 格式 JSON；退出码：硬违规或 score < 0.5 → 1，否则 0

--mode hidden 时 details 只含 id 与 ok，不含题面。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARCHIVE_PREFIX = ".archive/"
TASK = "E1"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- docker helpers
def wait_docker(retries: int = 5) -> bool:
    for i in range(retries):
        r = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if r.returncode == 0:
            return True
        log(f"[warn] docker 未就绪（{i + 1}/{retries}），3 秒后重试")
        time.sleep(3)
    return False


def run_container(image: str, corpus: Path, data: Path, args: list[str], timeout: int):
    """返回 (returncode, stdout, stderr, timed_out)。超时会 docker kill 容器。"""
    name = f"e1-eval-{uuid.uuid4().hex[:12]}"
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "-v", f"{corpus}:/corpus:ro",
        "-v", f"{data}:/data",
        image, *args,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or "", False
    except subprocess.TimeoutExpired as e:
        subprocess.run(["docker", "kill", name], capture_output=True)
        out = e.stdout
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return -1, out or "", "", True


# ---------------------------------------------------------------- parsing
def parse_last_json(stdout: str):
    """取 stdout 中最后一个能解析的 JSON 对象。返回 (obj|None, warning|None)。"""
    dec = json.JSONDecoder()
    found: list[tuple[int, int, dict]] = []
    i, n = 0, len(stdout)
    while i < n:
        j = stdout.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(stdout, j)
        except json.JSONDecodeError:
            i = j + 1
            continue
        if isinstance(obj, dict):
            found.append((j, end, obj))
            i = end
        else:
            i = j + 1
    if not found:
        return None, "stdout 里没有 JSON 对象"
    start, end, obj = found[-1]
    extra = stdout[:start].strip() or stdout[end:].strip()
    warn = None
    if len(found) > 1 or extra:
        warn = "stdout 除最后一个 JSON 对象外还有其它内容（应只输出一个 JSON，其余走 stderr）"
    return obj, warn


def norm_path(p) -> str:
    p = str(p or "").replace("\\", "/")
    if p.startswith("/corpus/"):
        p = p[len("/corpus/"):]
    if p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


# ---------------------------------------------------------------- judging
def judge(case: dict, obj: dict, k: int):
    """返回 (ok, why, hard_violation|None, warnings[])。"""
    expect = case["expect"]
    warnings: list[str] = []
    route = obj.get("route")
    if route not in ("index", "live"):
        warnings.append(f"route={route!r} 不在 index|live 内，按 index 处理")
        route = "index"

    raw_results = obj.get("results")
    if raw_results is None:
        raw_results = []
    if not isinstance(raw_results, list):
        return False, "results 不是数组", None, warnings
    if len(raw_results) > k:
        warnings.append(f"返回了 {len(raw_results)} 条，超过 k={k}，只取前 {k} 条")

    results = []
    for r in raw_results[:k]:
        if not isinstance(r, dict):
            warnings.append("results 里有非对象元素，已忽略")
            continue
        results.append({
            "path": norm_path(r.get("path")),
            "snippet": str(r.get("snippet") or ""),
        })

    # 泄漏检查：任何 result 的 snippet 或顶层 note 含 must_not_contain 任一 → 硬违规
    note = str(obj.get("note") or "")
    texts = [r["snippet"] for r in results] + [note]
    leaked = [s for s in (expect.get("must_not_contain") or []) if any(s in t for t in texts)]
    if leaked:
        return False, "snippet 或 note 含 must_not_contain 内容（泄漏）", f"leak:{case['id']}", warnings

    if expect["type"] == "live":
        if route == "live":
            return True, "", None, warnings
        return False, f"route={route}，期望 live", None, warnings

    # passage
    if route == "live":
        return False, "route=live，但该题期望从索引作答", None, warnings
    if not results:
        return False, "results 为空", None, warnings
    if results[0]["path"].startswith(ARCHIVE_PREFIX):
        return False, "top-1 是 .archive/ 下的过期文档", None, warnings

    paths = set(expect.get("paths") or [])
    mca = expect.get("must_contain_any") or []
    path_hit = False
    for r in results:
        if r["path"].startswith(ARCHIVE_PREFIX):
            continue
        if r["path"] in paths:
            path_hit = True
            if not mca or any(s in r["snippet"] for s in mca):
                return True, "", None, warnings
    if path_hit:
        return False, "path 命中但 snippet 不含 must_contain_any", None, warnings
    return False, "top-k 里没有期望的 path", None, warnings


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="E1 迷你企业大脑评测")
    ap.add_argument("--image", required=True, help="候选人镜像 tag")
    ap.add_argument("--mode", choices=["public", "hidden"], default="public")
    ap.add_argument("--qa", "--hidden", dest="qa", default=None,
                    help="QA 文件路径，缺省为脚本同目录 public_qa.json")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--corpus", default=str(HERE.parent / "corpus"))
    ap.add_argument("--timeout", type=int, default=60, help="单题 search 超时（秒）")
    ap.add_argument("--index-timeout", type=int, default=300, help="index 超时（秒）")
    ap.add_argument("--data", default=None, help="调试用：指定 data 目录而不是临时目录")
    a = ap.parse_args()

    hidden = a.mode == "hidden"
    qa_path = Path(a.qa) if a.qa else HERE / "public_qa.json"
    corpus = Path(a.corpus).resolve()
    if not corpus.is_dir():
        log(f"[error] corpus 目录不存在：{corpus}")
        return 2
    cases = json.loads(qa_path.read_text("utf-8"))
    for c in cases:
        if not all(key in c for key in ("id", "query", "expect")) or "type" not in c["expect"]:
            log(f"[error] QA 条目格式错误：{c.get('id')}")
            return 2
    total = len(cases)

    if not wait_docker():
        log("[error] docker 不可用")
        return 2

    own_tmp = a.data is None
    data = Path(tempfile.mkdtemp(prefix="e1-data-")) if own_tmp else Path(a.data).resolve()
    data.mkdir(parents=True, exist_ok=True)

    hard: list[str] = []
    details: list[dict] = []
    passed = 0

    def detail(case, ok, why=""):
        d = {"id": case["id"], "ok": ok}
        if not hidden:
            d["query"] = case["query"]
            if why:
                d["why"] = why
        return d

    try:
        log(f"[index] docker run {a.image} index  (corpus={corpus})")
        rc, out, err, to = run_container(a.image, corpus, data, ["index"], a.index_timeout)
        if to or rc != 0:
            log(f"[error] index 失败：rc={rc} timeout={to}\n{err[-2000:]}")
            hard.append("index_failed")
            details = [detail(c, False, "index 失败") for c in cases]
        else:
            if err.strip():
                log("[index] stderr:\n" + err[-2000:])
            for case in cases:
                cid = case["id"]
                rc, out, err, to = run_container(
                    a.image, corpus, data, ["search", case["query"], "--k", str(a.k)], a.timeout)
                if to:
                    log(f"[{cid}] FAIL: 超时 {a.timeout}s")
                    details.append(detail(case, False, f"超时 {a.timeout}s"))
                    continue
                if rc != 0:
                    log(f"[{cid}] FAIL: 容器退出码 {rc}\n{err[-800:]}")
                    details.append(detail(case, False, f"容器退出码 {rc}"))
                    continue
                obj, warn = parse_last_json(out)
                if warn:
                    log(f"[{cid}] warn: {warn}")
                if obj is None:
                    details.append(detail(case, False, "stdout 无 JSON"))
                    log(f"[{cid}] FAIL: stdout 无 JSON")
                    continue
                ok, why, violation, warnings = judge(case, obj, a.k)
                for w in warnings:
                    log(f"[{cid}] warn: {w}")
                if violation:
                    hard.append(violation)
                if ok:
                    passed += 1
                    log(f"[{cid}] ok")
                else:
                    log(f"[{cid}] FAIL: {why}")
                details.append(detail(case, ok, why))
    finally:
        if own_tmp:
            try:
                shutil.rmtree(data)
            except OSError as e:  # 容器以 root 写入时本机用户可能删不掉
                log(f"[warn] 临时目录未能清理（{e}）：{data}")

    score = round(passed / total, 4) if total else 0.0
    summary = {
        "task": TASK,
        "mode": a.mode,
        "score": score,
        "passed": passed,
        "total": total,
        "hard_violations": hard,
        "details": details,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if (hard or score < 0.5) else 0


if __name__ == "__main__":
    sys.exit(main())
