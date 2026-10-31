"""下载 AliMeeting4MUG 的 Dev/Test1 标注 CSV（不下载音频）。

前置：
    pip install modelscope requests
    eval/.env 里设置 MODELSCOPE_TOKEN（只读权限即可）
用法：
    python -m evaluation.adapters.fetch_mug --out /data/mug
"""

from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path

import requests


def download(out_dir: Path, keys: list[str]) -> None:
    token = os.getenv("MODELSCOPE_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 MODELSCOPE_TOKEN；请只写入本地 eval/.env，不要提交")
    try:
        from modelscope.hub.api import HubApi
    except ImportError as error:
        raise SystemExit("请先 pip install modelscope") from error

    api = HubApi()
    api.login(token)
    objects = api.list_oss_dataset_objects(
        dataset_name="Alimeeting4MUG",
        namespace="modelscope",
        max_limit=100,
        is_recursive=True,
        is_filter_dir=False,
        revision="master",
        token=token,
    )
    index = {item.get("Key"): item for item in objects}
    out_dir.mkdir(parents=True, exist_ok=True)
    for key in keys:
        item = index.get(key)
        if not item:
            print(f"skip missing: {key}")
            continue
        target = out_dir / key
        with requests.get(item["Url"], stream=True, timeout=300) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_content(1 << 16):
                    handle.write(chunk)
        extract_dir = out_dir / key.replace(".zip", "")
        with zipfile.ZipFile(target) as archive:
            archive.extractall(extract_dir)
        print(f"downloaded {key} -> {extract_dir} ({target.stat().st_size} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description="下载 AliMeeting4MUG 标注（需 MODELSCOPE_TOKEN）")
    parser.add_argument("--out", default="/tmp/mug", help="输出目录")
    parser.add_argument("--keys", default="dev.zip,except_TS_test1.zip", help="逗号分隔的 zip 文件名")
    args = parser.parse_args()
    download(Path(args.out), [key.strip() for key in args.keys.split(",") if key.strip()])


if __name__ == "__main__":
    main()
