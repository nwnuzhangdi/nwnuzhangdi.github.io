"""抓取 Google Scholar 的引用数据，输出给主页徽章使用。

数据来源（按顺序尝试）：
1. `scholarly` 直连 Google Scholar（免费，但 GitHub 运行器 IP 常被 Google 拦）
2. SerpAPI 的 google_scholar_author 接口（需要 secret `SERPAPI_KEY`，免费额度 100 次/月，稳定）

产物（与 acad-homepage 模板约定一致）：
- results/gs_data.json：完整数据，`publications` 以 author_pub_id 为键（供论文级引用数使用）
- results/gs_data_shieldsio.json：shields.io endpoint 格式，`message` 为总引用数
"""

import json
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime

SCHOLAR_ID = os.environ.get("GOOGLE_SCHOLAR_ID", "").strip()
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "").strip()
OUT_DIR = "results"


def annotate(level, message):
    """GitHub Actions 注解：失败信息会出现在 run 的 Annotations 里，方便排查。"""
    message = str(message).replace("\n", " ")[:800]
    print(f"::{level}::{message}", flush=True)


def crawl_with_scholarly(attempts=3):
    from scholarly import scholarly

    last_error = None
    for i in range(1, attempts + 1):
        try:
            print(f"[scholarly] 第 {i}/{attempts} 次尝试…", flush=True)
            author = scholarly.search_author_id(SCHOLAR_ID)
            scholarly.fill(author, sections=["basics", "indices", "counts", "publications"])
            print(f"[scholarly] 成功：{author.get('name')} / citedby={author.get('citedby')}", flush=True)
            return author
        except Exception as exc:  # noqa: BLE001 - 需要把任何异常都记录下来
            last_error = exc
            print(f"[scholarly] 第 {i} 次失败：{type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            if i < attempts:
                time.sleep(5 * i)
    raise RuntimeError(f"scholarly 尝试 {attempts} 次均失败，最后错误：{type(last_error).__name__}: {last_error}")


def crawl_with_serpapi():
    if not SERPAPI_KEY:
        raise RuntimeError("未配置 SERPAPI_KEY")

    params = {
        "engine": "google_scholar_author",
        "author_id": SCHOLAR_ID,
        "hl": "en",
        "api_key": SERPAPI_KEY,
    }
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    print("[serpapi] 请求作者主页…", flush=True)
    with urllib.request.urlopen(url, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    if payload.get("error"):
        raise RuntimeError(f"SerpAPI 返回错误：{payload['error']}")

    author_info = payload.get("author") or {}
    citedby = None
    for row in (author_info.get("cited_by") or {}).get("table") or []:
        if (row.get("citations") or {}).get("all") is not None:
            citedby = row["citations"]["all"]
            break
    if citedby is None:
        raise RuntimeError("SerpAPI 响应里找不到 cited_by.table[].citations.all")

    publications = {}
    for article in payload.get("articles") or []:
        pub_id = article.get("citation_id")
        if not pub_id:
            continue
        publications[pub_id] = {
            "author_pub_id": pub_id,
            "title": article.get("title"),
            "num_citations": (article.get("cited_by") or {}).get("value", 0),
        }

    print(f"[serpapi] 成功：{author_info.get('name')} / citedby={citedby} / 论文 {len(publications)} 篇", flush=True)
    return {
        "name": author_info.get("name"),
        "citedby": citedby,
        "publications": publications,
        "source": "serpapi",
    }


def normalize_scholarly(author):
    """把 scholarly 的返回值整理成可 JSON 序列化的结构。"""
    publications = {}
    for pub in author.get("publications") or []:
        pub_id = pub.get("author_pub_id")
        if not pub_id:
            continue
        publications[pub_id] = {
            "author_pub_id": pub_id,
            "title": pub.get("bib", {}).get("title"),
            "num_citations": pub.get("num_citations", 0),
        }

    return {
        "name": author.get("name"),
        "citedby": author.get("citedby"),
        "hindex": author.get("hindex"),
        "publications": publications,
        "source": "scholarly",
    }


def main():
    if not SCHOLAR_ID:
        annotate("error", "缺少 secret GOOGLE_SCHOLAR_ID，请在 Settings → Secrets and variables → Actions 添加")
        sys.exit(1)

    print(f"GOOGLE_SCHOLAR_ID={SCHOLAR_ID}", flush=True)
    print(f"SERPAPI_KEY={'已配置' if SERPAPI_KEY else '未配置'}", flush=True)

    errors = []
    data = None

    try:
        data = normalize_scholarly(crawl_with_scholarly())
    except Exception as exc:  # noqa: BLE001
        print(f"[scholarly] 整体失败：{exc}", flush=True)
        errors.append(f"scholarly: {exc}")

    if data is None or data.get("citedby") is None:
        try:
            data = crawl_with_serpapi()
        except Exception as exc:  # noqa: BLE001
            print(f"[serpapi] 失败：{exc}", flush=True)
            errors.append(f"serpapi: {exc}")

    if data is None or data.get("citedby") is None:
        annotate("error", "抓取失败（两种来源都不可用）：" + " | ".join(errors))
        sys.exit(1)

    data["updated"] = str(datetime.now())

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "gs_data.json"), "w", encoding="utf-8") as outfile:
        json.dump(data, outfile, ensure_ascii=False)

    shieldio_data = {
        "schemaVersion": 1,
        "label": "citations",
        "message": f"{data['citedby']}",
        "color": "9cf",
    }
    with open(os.path.join(OUT_DIR, "gs_data_shieldsio.json"), "w", encoding="utf-8") as outfile:
        json.dump(shieldio_data, outfile, ensure_ascii=False)

    print(f"完成：来源={data.get('source')}，总引用={data['citedby']}，论文 {len(data['publications'])} 篇", flush=True)


if __name__ == "__main__":
    main()
