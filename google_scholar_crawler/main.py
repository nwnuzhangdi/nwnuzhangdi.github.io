"""抓取 Google Scholar 的引用数据，输出给主页徽章使用。

数据来源（按速度从快到慢依次尝试）：
1. 直接请求作者主页 HTML 解析（1 个请求，约 2~5 秒；顺带拿到每篇论文的 citation_for_view ID）
2. `scholarly` 只取总数（不逐篇抓，避免几十个请求拖到十几分钟）
3. SerpAPI 的 google_scholar_author 接口（需要 secret SERPAPI_KEY，稳定）

产物（与 acad-homepage 模板约定一致）：
- results/gs_data.json：完整数据，`publications` 以 author_pub_id 为键（供论文级引用数使用）
- results/gs_data_shieldsio.json：shields.io endpoint 格式，`message` 为总引用数
"""

import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime

SCHOLAR_ID = os.environ.get("GOOGLE_SCHOLAR_ID", "").strip()
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "").strip()
OUT_DIR = "results"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def annotate(level, message):
    """GitHub Actions 注解：失败信息会出现在 run 的 Annotations 里，方便排查。"""
    message = str(message).replace("\n", " ")[:800]
    print(f"::{level}::{message}", flush=True)


def fetch(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 来源 1：直接解析主页
def crawl_by_html(attempts=2):
    url = f"https://scholar.google.com/citations?user={SCHOLAR_ID}&hl=en&cstart=0&pagesize=100"

    last_error = None
    for i in range(1, attempts + 1):
        try:
            print(f"[html] 第 {i}/{attempts} 次请求作者主页…", flush=True)
            html = fetch(url, timeout=30, headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            })

            if "gsc_a_tr" not in html and "gsc_rsb_std" not in html:
                raise RuntimeError("页面里没有 gsc_ 结构，疑似被 Google 拦截（captcha / 403）")

            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")

            name_el = soup.select_one("#gsc_prf_in")
            name = name_el.get_text(strip=True) if name_el else None

            counts = [int(td.get_text(strip=True) or 0) for td in soup.select("td.gsc_rsb_std")]
            citedby = counts[0] if counts else None
            hindex = counts[2] if len(counts) > 2 else None

            publications = {}
            for row in soup.select("tr.gsc_a_tr"):
                link = row.select_one("a.gsc_a_at")
                if not link:
                    continue
                match = re.search(r"citation_for_view=([\w\-:]+)", link.get("href", ""))
                if not match:
                    continue
                pub_id = match.group(1)
                cite_el = row.select_one("a.gsc_a_ac")
                publications[pub_id] = {
                    "author_pub_id": pub_id,
                    "title": link.get_text(strip=True),
                    "num_citations": int(cite_el.get_text(strip=True) or 0) if cite_el else 0,
                }

            if citedby is None:
                raise RuntimeError("解析不到总引用数（td.gsc_rsb_std 为空）")

            print(f"[html] 成功：{name} / citedby={citedby} / 论文 {len(publications)} 篇", flush=True)
            return {
                "name": name,
                "citedby": citedby,
                "hindex": hindex,
                "publications": publications,
                "source": "html",
            }
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[html] 第 {i} 次失败：{type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            if i < attempts:
                time.sleep(5)

    raise RuntimeError(f"主页解析失败：{type(last_error).__name__}: {last_error}")


# ---------------------------------------------------------------- 来源 2：scholarly 取总数
def crawl_with_scholarly():
    from scholarly import scholarly

    if hasattr(scholarly, "set_timeout"):
        scholarly.set_timeout(30)

    print("[scholarly] 只取作者统计（不逐篇抓取）…", flush=True)
    author = scholarly.search_author_id(SCHOLAR_ID)
    scholarly.fill(author, sections=["basics", "indices", "counts"])
    if author.get("citedby") is None:
        raise RuntimeError("scholarly 没有返回 citedby")
    print(f"[scholarly] 成功：{author.get('name')} / citedby={author.get('citedby')}", flush=True)
    return {
        "name": author.get("name"),
        "citedby": author.get("citedby"),
        "hindex": author.get("hindex"),
        "publications": {},
        "source": "scholarly",
    }


# ---------------------------------------------------------------- 来源 3：SerpAPI
def crawl_with_serpapi():
    if not SERPAPI_KEY:
        raise RuntimeError("未配置 SERPAPI_KEY")

    params = {
        "engine": "google_scholar_author",
        "author_id": SCHOLAR_ID,
        "hl": "en",
        "api_key": SERPAPI_KEY,
    }
    print("[serpapi] 请求作者主页…", flush=True)
    payload = json.loads(fetch("https://serpapi.com/search.json?" + urllib.parse.urlencode(params), timeout=60))

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


SOURCES = (
    ("html", crawl_by_html),
    ("scholarly", crawl_with_scholarly),
    ("serpapi", crawl_with_serpapi),
)


def main():
    if not SCHOLAR_ID:
        annotate("error", "缺少 secret GOOGLE_SCHOLAR_ID，请在 Settings → Secrets and variables → Actions 添加")
        sys.exit(1)

    print(f"GOOGLE_SCHOLAR_ID={SCHOLAR_ID}", flush=True)
    print(f"SERPAPI_KEY={'已配置' if SERPAPI_KEY else '未配置'}", flush=True)

    errors = []
    data = None
    for name, crawler in SOURCES:
        try:
            data = crawler()
            if data.get("citedby") is not None:
                break
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] 失败：{exc}", flush=True)
            errors.append(f"{name}: {exc}")
            data = None

    if data is None or data.get("citedby") is None:
        annotate("error", "抓取失败（所有来源都不可用）：" + " | ".join(errors))
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

    print(
        f"完成：来源={data.get('source')}，总引用={data['citedby']}，论文 {len(data['publications'])} 篇",
        flush=True,
    )


if __name__ == "__main__":
    main()
