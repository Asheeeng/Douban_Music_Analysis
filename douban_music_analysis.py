# -*- coding: utf-8 -*-
"""豆瓣音乐 Top 250：实时爬取、清洗、绘图与缺失名次核查。

Python 3.11；依赖 requests、beautifulsoup4、pandas、matplotlib。
运行：.venv/bin/python douban_music_analysis.py
不读取旧 CSV，不补造缺失唱片；遇到访问限制立即停止，临时连接故障仅重试一次。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

# 使用无需图形窗口的后端，缓存放入可写的临时目录。
os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "douban_music_matplotlib"))
import matplotlib

matplotlib.use("Agg")
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
from matplotlib import font_manager
import pandas as pd
import requests
from bs4 import BeautifulSoup


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "output"
BASE_URL = "https://music.douban.com/top250"
PAGE_STARTS = tuple(range(0, 250, 25))
PAGE_SIZE = 25
REQUEST_DELAY = 3.0
RETRY_DELAY = 5.0  # 仅网络连接中断或超时时，等待后重试一次。
USE_ENV_PROXY = False  # 默认正常直连；本机代理存在 TLS 故障。确需环境代理时可改为 True。
TIMEOUT = (10, 25)  # 分别为建立连接、读取响应的超时秒数。
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
COLUMNS = [
    "榜单名次", "专辑ID", "专辑名称", "专辑别名", "歌手", "发行日期", "发行年份",
    "唱片类型", "载体", "音乐流派", "豆瓣评分", "评价人数", "专辑链接",
    "原始简介", "字段说明", "来源页偏移", "抓取时间",
]
CHART_NAMES = [
    "01_音乐流派分布.png", "02_年代分布.png", "03_评分分布.png",
    "04_评分与评价人数.png", "05_歌手上榜数量.png",
]
LOGGER = logging.getLogger(__name__)


def now() -> datetime:
    """统一用北京时间标记采集时间，避免系统时区造成歧义。"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def text_of(value: str) -> str:
    return " ".join(value.split())


def subject_id(href: str) -> str | None:
    """只接受豆瓣音乐的数字唱片 ID；查询参数不参与去重。"""
    url = urlsplit(urljoin(BASE_URL, href))
    match = re.fullmatch(r"/subject/(\d+)/?", url.path)
    if url.scheme in {"http", "https"} and url.netloc == "music.douban.com" and match:
        return match.group(1)
    return None


def parse_metadata(metadata: str) -> tuple[dict, list[str]]:
    """斜杠两侧都有空格才是分隔符，保留 R&B、Live 和日期中的斜杠。"""
    parts = [part.strip() for part in re.split(r"(?<=\s)/(?=\s)", metadata)]
    result = {"歌手": "", "发行日期": "", "发行年份": None,
              "唱片类型": "", "载体": "", "音乐流派": ""}
    notes = []
    date_positions = [i for i, part in enumerate(parts)
                      if re.match(r"^(?:18|19|20)\d{2}(?:$|[-/.年])", part)]
    if len(parts) == 5 and (not date_positions or date_positions == [1]):
        # 标准格式保留空字段，不删除空串后再向左移动其他字段。
        for key, value in zip(("歌手", "发行日期", "唱片类型", "载体", "音乐流派"), parts):
            result[key] = value
    elif len(date_positions) == 1:
        date_index = date_positions[0]
        result["歌手"] = " / ".join(parts[:date_index])
        result["发行日期"] = parts[date_index]
        tail = parts[date_index + 1:]
        if len(tail) >= 3:
            # 类型可能有多个标签，如 Import / Soundtrack；载体和流派位于末尾。
            result["唱片类型"] = " / ".join(tail[:-2])
            result["载体"], result["音乐流派"] = tail[-2:]
        elif len(tail) == 2 and re.fullmatch(
            r"(?:Audio\s+CD|音乐CD|CD(?:[+＋ /]DVD)?|DVD|数字(?:\(Digital\))?|Digital|磁带|黑胶|Vinyl)",
            tail[0], re.IGNORECASE,
        ):
            # 例如“王菲 / 1996 / Audio CD / 摇滚”没有唱片类型。
            result["载体"], result["音乐流派"] = tail
            notes.append("网页简介未提供唱片类型，留空")
        else:
            notes.append("简介字段数量异常，未能确定的字段留空，请查看原始简介")
    else:
        # 非标准简介不猜测字段位置，也不因此删除唱片。
        result["歌手"] = parts[0] if parts else ""
        notes.append("简介缺少可定位的日期或格式异常，未确定的字段留空")
    year = re.match(r"^((?:18|19|20)\d{2})(?:$|[-/.年])", result["发行日期"])
    result["发行年份"] = int(year.group(1)) if year else None
    for key in ("歌手", "发行日期", "载体", "音乐流派"):
        if not result[key]:
            notes.append(f"网页未提供可识别的{key}")
    return result, notes


def parse_music_page(html: str, start: int, collected_at: str) -> tuple[list[dict], dict]:
    """逐个解析真实 DOM 条目；身份或排名解析失败时整次停止，不静默跳过。"""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1")
    page_tag = soup.select_one(".paginator .thispage")
    if (not heading or "豆瓣音乐 Top 250" not in heading.get_text(" ", strip=True)
            or not page_tag or page_tag.get_text(strip=True) != str(start // PAGE_SIZE + 1)
            or not soup.select_one("#footer") or "</html>" not in html.lower()):
        raise ValueError(f"start={start}：榜单标题、当前页码或页尾不完整，可能是验证页或截断响应")
    rows = soup.select("tr.item")
    if not 1 <= len(rows) <= PAGE_SIZE:
        raise ValueError(f"start={start}：tr.item 数量异常：{len(rows)}")

    records, indices = [], []
    for position, row in enumerate(rows, 1):
        context = f"start={start}，DOM 第 {position} 个条目"
        links = [a for a in row.select("a[href]") if subject_id(a["href"])]
        ids = {subject_id(a["href"]) for a in links}
        if len(ids) != 1:
            raise ValueError(f"{context}：没有有效专辑链接，或同一行包含不同唱片 ID：{ids}")
        album_id = next(iter(ids))
        if row.get("id") and row["id"] != album_id:
            raise ValueError(f"{context}：行 ID 与链接 ID 不一致")
        row_indices = set()
        for link in links:
            onclick = link.get("onclick", "")
            index = re.search(r"\bi\s*:\s*['\"]?(\d+)['\"]?\s*[,}]", onclick)
            if index:
                row_indices.add(int(index.group(1)))
            click_id = re.search(r"\bsubject_id\s*:\s*['\"](\d+)['\"]", onclick)
            if click_id and click_id.group(1) != album_id:
                raise ValueError(f"{context}：onclick 中的唱片 ID 与链接不一致")
        if len(row_indices) != 1:
            raise ValueError(f"{context}：onclick 排名缺失或互相冲突：{row_indices}")
        index = next(iter(row_indices))
        rank = index + 1  # i 从 0 开始；不能用 start + 循环下标计算名次。
        if not start + 1 <= rank <= start + PAGE_SIZE:
            raise ValueError(f"{context}：网页真实名次 {rank} 超出当前页范围")
        indices.append(index)

        title_link = row.select_one(".pl2 a[href]")
        cover_link = row.select_one("a.nbg")
        name, alias = "", ""
        if title_link is not None and subject_id(title_link.get("href", "")) == album_id:
            # 标题里的 span 是别名；分开存储，避免“王妃 王妃”等重复名称。
            name = text_of(" ".join(str(node) if isinstance(node, str) else node.get_text(" ", strip=True)
                                    for node in title_link.contents if getattr(node, "name", None) != "span"))
            alias = text_of(" / ".join(span.get_text(" ", strip=True)
                                       for span in title_link.find_all("span", recursive=False)))
        if not name and cover_link is not None:
            cover_title = cover_link.get("title", "")
            if not cover_title and cover_link.select_one("img[alt]"):
                cover_title = cover_link.select_one("img[alt]")["alt"]
            name = text_of(cover_title.partition(" - ")[2])
        if not name:
            raise ValueError(f"{context}：专辑 {album_id} 缺少可识别的名称")

        metadata_tag = row.select_one("p.pl")
        metadata = metadata_tag.get_text(" ", strip=True) if metadata_tag else ""
        fields, notes = parse_metadata(metadata)
        score_tag = row.select_one(".rating_nums")
        score_text = score_tag.get_text(strip=True) if score_tag else ""
        score = None
        if score_text:
            try:
                score = float(score_text)
            except ValueError as exc:
                raise ValueError(f"{context}：无法识别评分 {score_text!r}") from exc
            if not 0 <= score <= 10:
                raise ValueError(f"{context}：评分超出 0~10")
        else:
            notes.append("网页未提供评分")
        people_match = re.search(r"([\d,，]+)\s*人评价", row.get_text(" ", strip=True))
        people = int(re.sub(r"[,，]", "", people_match.group(1))) if people_match else None
        if people is None:
            notes.append("网页未提供评价人数")
        records.append({
            "榜单名次": rank, "专辑ID": album_id, "专辑名称": name, "专辑别名": alias,
            **fields, "豆瓣评分": score, "评价人数": people,
            "专辑链接": f"https://music.douban.com/subject/{album_id}/",
            "原始简介": metadata, "字段说明": "；".join(notes),
            "来源页偏移": start, "抓取时间": collected_at,
        })

    # 检查榜单正文中是否有被 tr.item 选择器遗漏的唱片链接。
    article = soup.select_one(".article") or soup
    all_ids = {subject_id(a["href"]) for a in article.select("a[href]") if subject_id(a["href"])}
    row_ids = {record["专辑ID"] for record in records}
    if all_ids != row_ids:
        raise ValueError(f"start={start}：tr.item 之外还有专辑链接，网页结构可能改变：{all_ids - row_ids}")
    missing = sorted(set(range(start + 1, start + PAGE_SIZE + 1)) - {i + 1 for i in indices})
    return records, {
        "start": start, "页码": start // PAGE_SIZE + 1, "tr.item数量": len(rows),
        "有效专辑链接条目数": len(records), "onclick_i": indices,
        "真实名次": [i + 1 for i in indices], "网页缺失名次": missing,
        "重复专辑ID": sorted({r["专辑ID"] for r in records
                              if sum(x["专辑ID"] == r["专辑ID"] for x in records) > 1}),
        "解析失败条目数": 0, "HTML文件": f"debug_pages/page_start_{start}.html",
    }


def request_page(session: requests.Session, start: int):
    """临时连接故障最多重试一次；不重试 HTTP 错误、验证页或缺失条目。"""
    for attempt in (1, 2):
        try:
            return session.get(BASE_URL, params={"start": start}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            retryable = isinstance(exc, (requests.ConnectionError, requests.Timeout))
            # TLS 连接被意外关闭可以重试；证书校验错误直接报告，不能关闭校验。
            if isinstance(exc, requests.exceptions.SSLError):
                retryable = "UNEXPECTED_EOF_WHILE_READING" in str(exc)
            if attempt == 1 and retryable:
                LOGGER.warning("start=%s 连接失败，%s 秒后仅重试一次：%s: %s",
                               start, RETRY_DELAY, type(exc).__name__, exc)
                time.sleep(RETRY_DELAY)
            else:
                raise RuntimeError(f"start={start} 请求失败（第 {attempt} 次）：{type(exc).__name__}: {exc}") from exc


def crawl_all_pages(run_dir: Path) -> tuple[pd.DataFrame, list[dict]]:
    """从零请求十页，每页保存证据；HTTP 200 也要检查真实内容。"""
    html_dir = run_dir / "debug_pages"
    html_dir.mkdir()
    records, pages = [], []
    with requests.Session() as session:
        session.trust_env = USE_ENV_PROXY
        session.headers.update(HEADERS)
        for number, start in enumerate(PAGE_STARTS, 1):
            if number > 1:
                time.sleep(REQUEST_DELAY)
            collected_at = now().isoformat(timespec="seconds")
            LOGGER.info("[%s/10] 请求 %s?start=%s", number, BASE_URL, start)
            try:
                response = request_page(session, start)
                LOGGER.info("HTTP %s；响应 %s 字节；URL %s", response.status_code, len(response.content), response.url)
                if response.status_code in (403, 418, 429):
                    raise RuntimeError(f"start={start}：网站限制访问（HTTP {response.status_code}），停止请求")
                response.raise_for_status()
                if response.status_code != 200:
                    raise RuntimeError(f"start={start}：预期 HTTP 200，实际 {response.status_code}")
            except requests.RequestException as exc:
                raise RuntimeError(f"start={start} 请求失败：{type(exc).__name__}: {exc}") from exc
            final_url = urlsplit(response.url)
            actual_start = parse_qs(final_url.query).get("start", ["0"])
            if (final_url.netloc != "music.douban.com" or final_url.path.rstrip("/") != "/top250"
                    or actual_start != [str(start)]):
                raise RuntimeError(f"start={start}：请求被重定向到 {response.url}，停止采集")
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                raise RuntimeError(f"start={start}：响应不是 HTML")
            # utf-8 严格解码，乱码或验证页面不能被当成成功数据。
            html = response.content.decode("utf-8-sig")
            (html_dir / f"page_start_{start}.html").write_text(html, encoding="utf-8")
            page_records, diagnostic = parse_music_page(html, start, collected_at)
            diagnostic.update({"URL": response.url, "HTTP状态码": response.status_code,
                               "响应字节数": len(response.content), "采集时间": collected_at,
                               "HTML_SHA256": sha256(html.encode("utf-8")).hexdigest()})
            pages.append(diagnostic)
            records.extend(page_records)
            LOGGER.info("DOM %s 条；成功解析 %s 条；网页缺失名次 %s",
                        diagnostic["tr.item数量"], len(page_records), diagnostic["网页缺失名次"])
    return pd.DataFrame(records, columns=COLUMNS), pages


def clean_data(raw: pd.DataFrame) -> pd.DataFrame:
    """只删除完全相同的重复记录；缺字段保留，排名或 ID 冲突则停止。"""
    business_columns = [c for c in COLUMNS if c != "抓取时间"]
    clean = raw.drop_duplicates(subset=business_columns).copy()
    for column in ("榜单名次", "专辑ID", "专辑链接"):
        if clean[column].duplicated().any():
            values = clean.loc[clean[column].duplicated(keep=False), column].tolist()
            raise ValueError(f"{column} 存在冲突重复，不能擅自删除：{values}")
    if clean.empty or not clean["榜单名次"].between(1, 250).all():
        raise ValueError("没有有效记录或名次超出 1~250")
    for column in ("榜单名次", "发行年份", "评价人数", "来源页偏移"):
        clean[column] = pd.to_numeric(clean[column], errors="raise").astype("Int64")
    clean["豆瓣评分"] = pd.to_numeric(clean["豆瓣评分"], errors="raise")
    for column in ("歌手", "音乐流派"):
        clean[column] = clean[column].fillna("").replace("", "未知")
    return clean.sort_values("榜单名次").reset_index(drop=True)


def visualize(df: pd.DataFrame, run_dir: Path) -> list[dict]:
    """五张图使用同一份清洗后数据，同时记录每张图的真实统计口径。"""
    available = {font.name for font in font_manager.fontManager.ttflist}
    chinese_font = next((font for font in ("PingFang SC", "Heiti SC", "Microsoft YaHei", "SimHei",
                                          "Noto Sans CJK SC", "Arial Unicode MS") if font in available), None)
    if not chinese_font:
        raise RuntimeError("未找到中文字体；请安装中文字体后重跑，避免生成乱码图表")
    plt.rcParams.update({"font.sans-serif": [chinese_font], "axes.unicode_minus": False})
    total, checks = len(df), []

    def save(fig, name, used, counts=None, note=""):
        fig.text(0.02, 0.02, f"CSV 共 {total} 条；本图统计 {used} 条；未参与 {total-used} 条。{note}",
                 fontsize=9, color="#555555")
        fig.tight_layout(rect=(0, 0.06, 1, 1))
        try:
            fig.savefig(run_dir / name, dpi=180)
        finally:
            plt.close(fig)
        check = {"文件": name, "CSV记录数": total, "参与记录数": used, "未参与记录数": total - used}
        if counts is not None:
            check["分组计数"] = {str(k): int(v) for k, v in counts.items()}
            if int(sum(counts.values())) != used:
                raise ValueError(f"{name}：图表分组数量与样本量不一致")
        checks.append(check)
        LOGGER.info("图表生成：%s（%s 条）", name, used)

    genres = df["音乐流派"].value_counts().sort_values()
    fig, ax = plt.subplots(figsize=(10, 5.5))
    genres.plot.barh(ax=ax, color="#4682a9")
    ax.set(title="豆瓣音乐 Top 250 · 音乐流派分布", xlabel="唱片数量", ylabel="音乐流派")
    save(fig, CHART_NAMES[0], total, genres.to_dict())

    decades = df["发行年份"].map(lambda y: f"{int(y)//10*10}年代" if pd.notna(y) else "未知")
    decade_counts = decades.value_counts().sort_index()
    fig, ax = plt.subplots(figsize=(10, 5.5))
    decade_counts.plot.bar(ax=ax, color="#6b9c83", rot=0)
    ax.set(title="上榜唱片的发行年代分布", xlabel="发行年代", ylabel="唱片数量")
    save(fig, CHART_NAMES[1], total, decade_counts.to_dict())

    scores = df["豆瓣评分"].dropna().astype(float)
    fig, ax = plt.subplots(figsize=(10, 5.5))
    heights, _, _ = ax.hist(scores, bins=12, range=(0, 10) if scores.empty else None,
                            color="#7695b2", edgecolor="white")
    ax.set(title="豆瓣评分分布", xlabel="评分", ylabel="唱片数量")
    if int(heights.sum()) != len(scores):
        raise ValueError("评分直方图计数异常")
    save(fig, CHART_NAMES[2], len(scores), note="缺少评分的记录不参与。")

    scatter = df.dropna(subset=["评价人数", "豆瓣评分"])
    scatter = scatter.loc[scatter["评价人数"] > 0]
    fig, ax = plt.subplots(figsize=(10, 5.5))
    points = ax.scatter(scatter["评价人数"].astype(float), scatter["豆瓣评分"].astype(float),
                        color="#4682a9", alpha=0.65, s=28)
    if len(points.get_offsets()) != len(scatter):
        raise ValueError("散点图数量异常")
    ax.set_xscale("log")
    ax.set(title="评价人数与豆瓣评分的关系", xlabel="评价人数（对数刻度）", ylabel="豆瓣评分")
    save(fig, CHART_NAMES[3], len(scatter), note="缺少数值或评价人数为 0 的记录不参与。")

    artists = df["歌手"].value_counts()
    top_artists = artists.head(10).to_dict()
    if len(artists) > 10:
        top_artists["其他歌手（合计）"] = int(artists.iloc[10:].sum())
    artist_counts = pd.Series(top_artists).sort_values()
    fig, ax = plt.subplots(figsize=(10, 6))
    artist_counts.plot.barh(ax=ax, color="#ad8b73")
    ax.set(title="歌手上榜数量（前 10 个署名及其他）", xlabel="唱片数量", ylabel="网页歌手署名")
    save(fig, CHART_NAMES[4], total, artist_counts.to_dict(), "组合署名作为一个整体统计。")
    return checks


def write_reports(raw, clean, pages, charts, run_dir):
    missing = sorted(set(range(1, 251)) - set(clean["榜单名次"].astype(int)))
    notes = clean.loc[clean["字段说明"].ne(""), ["榜单名次", "专辑名称", "字段说明"]]
    check = {
        "完成时间": now().isoformat(timespec="seconds"), "数据来源": "本次实时请求全部10页，无旧CSV或离线回退",
        "原始记录数": len(raw), "清洗后记录数": len(clean), "删除完全相同的重复记录数": len(raw)-len(clean),
        "缺失名次": missing, "重复名次": [], "重复专辑ID": [], "重复专辑链接": [],
        "字段说明": notes.to_dict("records"), "分页核查": pages, "图表核查": charts,
    }
    (run_dir / "运行核查.json").write_text(json.dumps(check, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        f"完成时间：{check['完成时间']}", "验证方式：本次实时请求全部 10 页；不使用旧 CSV 或历史 HTML 补数据。",
        f"实际原始记录数：{len(raw)}；清洗后记录数：{len(clean)}；完全相同重复记录：{len(raw)-len(clean)}",
        f"真实缺失名次：{missing or '无'}", "",
        "每页核查（onclick 的 i 从 0 开始，真实名次 = i + 1）：",
    ]
    for page in pages:
        report.extend([
            f"start={page['start']}：HTTP {page['HTTP状态码']}，HTML {page['响应字节数']} 字节，"
            f"tr.item={page['tr.item数量']}，有效链接条目={page['有效专辑链接条目数']}，解析失败=0，"
            f"缺失={page['网页缺失名次']}，重复ID={page['重复专辑ID']}",
            f"  onclick i：{page['onclick_i']}", f"  真实名次：{page['真实名次']}",
            f"  原始HTML：{page['HTML文件']}；SHA256：{page['HTML_SHA256']}",
        ])
    report.extend(["", "缺失原因判定："])
    if missing:
        report.extend([
            "上述名次未出现在本次完整榜单 HTML 中；所有 tr.item 均已成功解析，清洗未删除缺字段记录。",
            "因此本次返回内容的缺口来自上游榜单页面，而不是 CSS 选择器、循环下标或数据清洗。",
            "仅能确认页面未提供这些名次；网站内部是否删除、隐藏或缓存异常，无法由公开 HTML 判断。",
            "不虚构唱片，不改动真实排名，也不为凑足 250 条反复重试。",
        ])
    else:
        report.append("本次 HTML 的 1~250 名齐全。")
    report.extend(["", "字段核查："] + (notes.to_string(index=False).splitlines() if len(notes) else ["没有字段异常。 "]))
    (run_dir / "缺失名次核查报告.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    summary = [
        f"完成时间：{check['完成时间']}", "请求页数：10（全部实时成功）",
        f"原始记录数：{len(raw)}", f"去重后记录数：{len(clean)}",
        f"删除完全相同重复记录数：{len(raw)-len(clean)}", f"缺失名次：{missing or '无'}",
        f"有效评分数：{clean['豆瓣评分'].notna().sum()}", f"平均评分：{clean['豆瓣评分'].mean():.2f}",
        f"有效发行年份数：{clean['发行年份'].notna().sum()}", f"字段说明记录数：{len(notes)}",
        "", "音乐流派：", clean["音乐流派"].value_counts().to_string(), "", "图表统计口径：",
    ]
    summary.extend(f"{c['文件']}：CSV {c['CSV记录数']} 条，参与 {c['参与记录数']} 条，未参与 {c['未参与记录数']} 条"
                   for c in charts)
    summary.append("歌手图按完整署名统计前 10 个及其他；所有分组之和等于 CSV 记录数。")
    (run_dir / "统计摘要.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return check


def validate_outputs(run_dir: Path, raw: pd.DataFrame, clean: pd.DataFrame):
    """真正读回文件核对，写文件或绘图失败均不能发布正式结果。"""
    for suffix, expected in (("原始数据", raw), ("清洗后", clean)):
        saved = pd.read_csv(run_dir / f"豆瓣音乐Top250_{suffix}.csv", keep_default_na=False, dtype={"专辑ID": str})
        if len(saved) != len(expected) or list(saved.columns) != COLUMNS:
            raise ValueError(f"{suffix} CSV 行列数不一致")
        for key in ("榜单名次", "专辑ID", "专辑名称", "专辑别名", "歌手", "发行日期", "音乐流派", "专辑链接"):
            if saved[key].astype(str).tolist() != expected[key].astype(str).tolist():
                raise ValueError(f"{suffix} CSV 的{key}写入后不一致")
    for name in CHART_NAMES:
        pixels = mpimg.imread(run_dir / name)
        if min(pixels.shape[:2]) < 500 or pixels[:, :, :3].std() < 0.01:
            raise ValueError(f"{name}：图片尺寸异常或为空白图")
    check = json.loads((run_dir / "运行核查.json").read_text(encoding="utf-8"))
    if check["清洗后记录数"] != len(clean) or len(check["分页核查"]) != 10 or len(check["图表核查"]) != 5:
        raise ValueError("核查报告的记录数、页数或图数不一致")


def publish_outputs(run_dir: Path, output_dir: Path):
    """整套输出完成后才替换目录，上一份输出完整保存在 history 中。"""
    backup = output_dir.parent / "history" / f"{output_dir.name}_{now():%Y%m%d_%H%M%S_%f}"
    if output_dir.exists():
        backup.parent.mkdir(exist_ok=True)
        output_dir.rename(backup)
    try:
        run_dir.rename(output_dir)
    except OSError:
        if backup.exists():
            backup.rename(output_dir)
        raise
    if backup.exists():
        LOGGER.info("上一份完整输出已归档：%s", backup)


def run_pipeline(output_dir: Path = OUTPUT_DIR):
    # 临时结果与 output 放在同一文件系统，全部验证后再整体发布。
    run_dir = Path(tempfile.mkdtemp(prefix=".douban-run-", dir=output_dir.parent))
    try:
        raw, pages = crawl_all_pages(run_dir)
        clean = clean_data(raw)
        raw.to_csv(run_dir / "豆瓣音乐Top250_原始数据.csv", index=False, encoding="utf-8-sig")
        clean.to_csv(run_dir / "豆瓣音乐Top250_清洗后.csv", index=False, encoding="utf-8-sig")
        charts = visualize(clean, run_dir)
        check = write_reports(raw, clean, pages, charts, run_dir)
        validate_outputs(run_dir, raw, clean)
        publish_outputs(run_dir, output_dir)
    except BaseException:
        # 失败的 HTML 和半成品单独保留，不能混入上一次成功输出。
        if run_dir.exists():
            failed_dir = output_dir.parent / "logs" / f"failed_{run_dir.name.removeprefix('.douban-run-')}"
            try:
                failed_dir.parent.mkdir(exist_ok=True)
                run_dir.rename(failed_dir)
                LOGGER.error("本次失败证据保存在 %s；正式输出未替换", failed_dir)
            except OSError:
                LOGGER.error("失败证据仍保存在 %s", run_dir)
        raise
    return check


def main() -> int:
    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"运行_{now():%Y%m%d_%H%M%S_%f}.log"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")])
    LOGGER.info("开始实时抓取豆瓣音乐 Top 250；日志：%s", log_path)
    LOGGER.info("网络方式：%s；保持 TLS 证书校验", "使用环境代理设置" if USE_ENV_PROXY else "直接连接，不继承环境代理")
    try:
        check = run_pipeline()
    except KeyboardInterrupt:
        LOGGER.error("用户中断，运行未完成")
        return 130
    except Exception:
        LOGGER.exception("运行失败，未发布新结果")
        return 1
    LOGGER.info("完成：%s 条真实唱片；缺失名次 %s；输出目录 %s",
                check["清洗后记录数"], check["缺失名次"] or "无", OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
