"""Разведка сайта, который не открывается из сети разработчика: скачать список адресов тем же
вежливым клиентом, что и сборщик (robots.txt, 1 запрос/с, честный User-Agent), и сложить ответы
в recon/ — их забирает workflow «Recon» как артефакт.

    python scripts/recon.py "https://example.ru/" "https://example.ru/funds/"
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from pif.http import FetchError, Fetcher, Manifest

OUT = Path("recon")


def main(urls: list[str]) -> None:
    OUT.mkdir(exist_ok=True)
    http = Fetcher(Manifest(OUT / "manifest.json"), raw_dir=OUT / "raw", fresh_hours=0)
    index = []
    for n, line in enumerate(u for chunk in urls for u in chunk.split()):
        method, url = ("POST", line[5:]) if line.startswith("POST:") else ("GET", line)
        name = f"{n:02d}_" + re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("://", 1)[-1])[:120]
        try:
            doc = http.get(url, fmt="bin", method=method, data={} if method == "POST" else None,
                           allow_stale=False)
            shutil.copy(doc.path, OUT / name)
            index.append({"url": url, "file": name, "bytes": doc.path.stat().st_size, "ok": True})
        except FetchError as e:
            index.append({"url": url, "ok": False, "error": e.reason})
        print(index[-1], flush=True)
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
    http.close()


if __name__ == "__main__":
    main(sys.argv[1:])
