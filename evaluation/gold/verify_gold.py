"""机械核对金标事实是否真的在**标注的那几个转写片段**里（零判定调用）。

## 为什么需要它

`qa_seed.json` 的 100 条里，73 条问题与全部意图标签是代理起草的（待人工确认）。
人还没标之前，至少可以做一件不需要人的事：**把每条"标准事实"拿回它标注的片段原文里
逐字核对**——数字必须全部出现、字符 bigram 覆盖要够。核不过的，要么事实写错了、
要么片段号标错了，两种都必须先修，否则 `context_recall` 的分母本身不可信。

这不能替代人工标注（它只证明"片段里说了这件事"，不证明"这条问题值得问"），
但它把"金标是不是抄的/编的"这个最致命的问题挡在门口。

## 判据（机械、可解释）

- **数字全部出现**：事实里的每个数字/百分比/日期都要能在片段原文里找到（这是最硬的锚点）；
- **字符 bigram 覆盖率 ≥ 0.5**：容忍改写与标点差异，但挡住"只沾一两个词"的假金标；
- 跨录音题（`also_in`）把两段来源的片段合并核对。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping

EVALUATION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_QA_SET = EVALUATION_DIR / "gold" / "qa_seed.json"
_NUMERAL = re.compile(r"\d+(?:\.\d+)?%?")
_PUNCT = re.compile(r"[\s，。、；：！？“”‘’（）()【】\[\]「」\"'—…·,.;:!?\-]+")
BIGRAM_THRESHOLD = 0.5


def _http_json(url: str, *, token: str, timeout: float = 60.0) -> dict:
    request = urllib.request.Request(url, method="GET")
    request.add_header("token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 地址来自命令行
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"GET {url} → HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GET {url} → {error.reason}") from error


def fetch_segments(base_url: str, token: str, meeting_id: int) -> dict[int, dict[int, str]]:
    """{task_id: {segment_no: text}} —— 从产品的转写接口读，不碰库、不 import 产品代码。"""
    page = _http_json(f"{base_url}/transcription/selectPage?page_num=1&page_size=50"
                      f"&meetingId={meeting_id}", token=token)
    tasks = [row for row in ((page.get("data") or {}).get("list") or [])
             if row.get("status") == "SUCCEEDED"]
    collected: dict[int, dict[int, str]] = {}
    for task in tasks:
        detail = _http_json(f"{base_url}/transcription/selectById/{task['id']}", token=token)
        segments = (detail.get("data") or {}).get("segments") or []
        collected[int(task["id"])] = {int(seg["segment_no"]): str(seg.get("content") or "")
                                      for seg in segments}
    return collected


def _normalize(text: str) -> str:
    # 去标点/空白并**统一小写**：转写里是 "my sql知识库"，金标里写 "MySQL" ——
    # 同一件事，不能因为大小写与空格被判成「片段里没说」（产品侧 normalize_source 同样小写）
    return _PUNCT.sub("", str(text or "")).lower()


def bigrams(text: str) -> set[str]:
    compact = _normalize(text)
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def check_fact(fact: str, source_text: str) -> dict[str, Any]:
    """一条事实的机械核对结果。"""
    numerals = _NUMERAL.findall(str(fact or ""))
    missing_numerals = [value for value in numerals if _normalize(value) not in _normalize(source_text)]
    fact_grams = bigrams(fact)
    source_grams = bigrams(source_text)
    covered = len(fact_grams & source_grams)
    coverage = covered / len(fact_grams) if fact_grams else 1.0
    return {
        "ok": not missing_numerals and coverage >= BIGRAM_THRESHOLD,
        "coverage": round(coverage, 3),
        "missing_numerals": missing_numerals,
    }


def verify_case(case: Mapping[str, Any], segments: Mapping[int, Mapping[int, str]]) -> dict[str, Any]:
    texts: list[str] = []
    missing_segments: list[str] = []
    for key in ("source", "also_in"):
        entry = case.get(key) or {}
        task_id = entry.get("task_id")
        for segment_no in entry.get("segments") or []:
            text = (segments.get(int(task_id or 0)) or {}).get(int(segment_no))
            if text is None:
                missing_segments.append(f"task{task_id}#{segment_no}")
            else:
                texts.append(text)
    if missing_segments:
        return {"case_id": case.get("id"), "ok": False,
                "error": "标注的片段在转写里不存在：" + "、".join(missing_segments)}
    joined = "\n".join(texts)
    results = [check_fact(fact, joined) for fact in (case.get("expected_facts") or [])]
    return {
        "case_id": case.get("id"),
        "ok": all(item["ok"] for item in results),
        "facts": results,
        "min_coverage": min((item["coverage"] for item in results), default=1.0),
        "missing_numerals": [n for item in results for n in item["missing_numerals"]],
    }


def verify(qa_set: Mapping[str, Any], *, base_url: str, token: str,
           progress=print) -> dict[str, Any]:
    meeting_ids = sorted({int((case.get("source") or {}).get("meeting_id") or 0)
                          for case in qa_set["cases"]} - {0})
    # 语料只有一场会（meeting 2）；题面里没有 meeting_id，用命令行给的
    segments: dict[int, dict[int, str]] = {}
    for meeting_id in (meeting_ids or [2]):
        segments.update(fetch_segments(base_url, token, meeting_id))
    rows = [verify_case(case, segments) for case in qa_set["cases"]
            if case.get("expected_facts")]
    passed = [row for row in rows if row["ok"]]
    progress(f"金标机械核对：{len(passed)}/{len(rows)} 条通过"
             f"（数字全出现且 bigram 覆盖 ≥ {BIGRAM_THRESHOLD}）")
    return {
        "checked": len(rows),
        "passed": len(passed),
        "failed": [row for row in rows if not row["ok"]],
        "reading": ("机械核对只证明'片段里说了这件事'，不证明'这条问题值得问'；"
                    "它挡的是抄错/编错的金标，不能替代人工标注。"),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--token", default="")
    parser.add_argument("--qa-set", type=Path, default=DEFAULT_QA_SET)
    parser.add_argument("--meeting-id", type=int, default=2)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.token:
        print("拒绝执行：--token 是登录后的 JWT（要读转写接口）", file=sys.stderr)
        return 2
    qa_set = json.loads(args.qa_set.read_text(encoding="utf-8"))
    for case in qa_set["cases"]:
        if case.get("source") and not case["source"].get("meeting_id"):
            case["source"]["meeting_id"] = args.meeting_id
    result = verify(qa_set, base_url=args.base_url, token=args.token)
    print(json.dumps({k: v for k, v in result.items() if k != "failed"}, ensure_ascii=False))
    for row in result["failed"]:
        print(f"  未通过 {row['case_id']}: {row.get('error') or ''} "
              f"coverage={row.get('min_coverage')} 缺数字={row.get('missing_numerals')}")
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
