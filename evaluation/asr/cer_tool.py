"""⑥ ASR 字错误率（CER）评分工具（2026-09-26）：中文口径 + 标点/全半角/数字归一实验。

## 为什么只有 CER 先行

AliMeeting / AISHELL-4 的完整口径（cpCER/cpWER/DER）需要说话人对齐与 MeetEval；
本工具先落**最基础也最可比**的 CER（含标点实验），因为 METRICS_RESEARCH §6.2 的
结论是「标点剥离与否会改变每一个后续数字」——这个口径必须先钉死。cpCER/DER
在音频到位后接 MeetEval（安装命令见文末）。

## 归一化口径（三个都必须报，不允许只报一个）

1. `raw`：仅去空白——标点/全半角差异全部计入错误（最严）；
2. `no_punct`：去标点符号（中文会议 ASR 语料参考转写基本无标点，
   而 DashScope 返回带标点——不剥离会把每个逗号句号算成插入错误）；
3. `normalized`：no_punct + 全角→半角 + 汉字数字→阿拉伯数字（口径最宽）。

## 用法

    PYTHONPATH=evaluation python evaluation/metrics/cer_tool.py \
        --reference ref.txt --hypothesis hyp.txt
    # 或 Python API：cer(reference, hypothesis) -> {raw, no_punct, normalized}

音频首测（尚未执行，音频不在本地）：
    # OpenSLR 119（AliMeeting）eval 集，近讲话戴做金标、远场喂产品 API
    wget https://us.openslr.org/resources/119/...Eval.tar.gz
    # cpCER/DER：pip install meeteval（CHiME-2023，MIT）
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]")
# 只转换单字数字。「十/百/千/万」参与复合数（三百、十五）时逐字替换必然出错
# （三百→3100）——这是本项目校准发现第 3 条的同款陷阱；复合数归一需要真正的
# 数词解析，属 MeetEval/meeteval 对齐层的职责，此处如实不转并记录局限。
_CN_NUM = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
           "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def _raw(text: str) -> str:
    return _WS.sub("", text)


def _no_punct(text: str) -> str:
    return _PUNCT.sub("", _raw(text))


def _fullwidth_to_half(text: str) -> str:
    return "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
        for ch in text
    )


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", _no_punct(text))
    text = _fullwidth_to_half(text)
    for cn, ar in _CN_NUM.items():
        text = text.replace(cn, ar)
    return text.lower()


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein（字符级，中文惯例）。两行滚动数组，长转写也吃得下。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1,          # 删除
                               current[j - 1] + 1,       # 插入
                               previous[j - 1] + (ca != cb)))  # 替换
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> dict[str, object]:
    """三种归一口径的 CER。分母是参考长度；参考为空时返回 None 而不是 0。"""
    out: dict[str, object] = {}
    for name, fn in (("raw", _raw), ("no_punct", _no_punct), ("normalized", _normalized)):
        r, h = fn(reference), fn(hypothesis)
        out[name] = round(_edit_distance(r, h) / len(r), 4) if r else None
    return out


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--hypothesis", type=Path, required=True)
    args = parser.parse_args()
    result = cer(_read_text(args.reference), _read_text(args.hypothesis))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
