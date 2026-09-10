#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中華民國排球協會(CTVBA)國內賽事爬蟲 - MVP版本

用途：
  1. 抓取「國內賽事」列表頁 (https://www.ctvba.org.tw/article/front/inGame)，
     取得每個賽事的名稱、文章連結。
  2. 進入每篇賽事文章頁，解析賽事基本資訊(日期、地點、報名資訊、聯絡方式)
     以及所有附件(PDF)連結。
  3. 嘗試下載「總賽程表」PDF 並用 pdfplumber 解析表格，抓出場次資料。
     （賽程表格式因賽事而異，解析結果不保證完整，抓不到的會標記為
     needs_manual_review，讓使用者自己點附件連結查看。）

執行環境需求：
  - 這支程式需要「一般網際網路存取」(能連到 www.ctvba.org.tw)。
    Claude 目前所在的雲端沙盒環境的對外連線受組織政策限制，無法連到
    這個網域，所以這支程式在沙盒裡沒有被實際跑過、驗證過，是根據
    已經用 WebFetch 工具實際看過的頁面結構寫的。
    正式使用建議跑在：
      - 你自己的電腦，或
      - GitHub Actions 排程 (每天跑一次)，或
      - 任何有一般對外網路的伺服器/容器

安裝套件：
  pip install requests beautifulsoup4 pdfplumber --break-system-packages

用法：
  python scrape_ctvba.py                 # 抓全部國內賽事列表 + 各賽事基本資訊
  python scrape_ctvba.py --event-id 1950 # 只抓單一賽事(用文章ID)，並嘗試解析賽程PDF
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.ctvba.org.tw"
INGAME_LIST_URL = f"{BASE_URL}/article/front/inGame"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}
REQUEST_TIMEOUT = 20
REQUEST_DELAY_SEC = 1.0  # 對官網保持禮貌的抓取間隔，別打太快

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------

@dataclass
class Attachment:
    name: str
    url: str


@dataclass
class EventInfo:
    id: str
    name: str
    url: str
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    date_note: Optional[str] = None
    venue: Optional[str] = None
    registration_deadline: Optional[str] = None
    registration_fee: Optional[str] = None
    organizer: Optional[str] = None
    contact_phone: list[str] = field(default_factory=list)
    contact_email: Optional[str] = None
    attachments: list[Attachment] = field(default_factory=list)
    raw_text_excerpt: Optional[str] = None


# --------------------------------------------------------------------------
# HTTP 小工具
# --------------------------------------------------------------------------

def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def fetch_bytes(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


# --------------------------------------------------------------------------
# Step 1：抓賽事列表
# --------------------------------------------------------------------------

def list_events() -> list[dict]:
    """回傳 [{"id": "1950", "name": "...", "url": "..."}]"""
    html = fetch(INGAME_LIST_URL)
    soup = BeautifulSoup(html, "html.parser")

    events = []
    seen_ids = set()
    # 賽事文章連結格式觀察到的樣式為 /article/{id}/{slug}
    for a in soup.select("a[href*='/article/']"):
        href = a.get("href", "")
        m = re.search(r"/article/(\d+)/", href)
        if not m:
            continue
        event_id = m.group(1)
        if event_id in seen_ids:
            continue
        seen_ids.add(event_id)
        name = a.get_text(strip=True)
        if not name:
            continue
        events.append({
            "id": event_id,
            "name": name,
            "url": urljoin(BASE_URL, href),
        })
    return events


# --------------------------------------------------------------------------
# Step 2：解析單一賽事文章頁
# --------------------------------------------------------------------------

DATE_RANGE_RE = re.compile(
    r"(?:中華民國)?\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"\s*(?:至|~|-)\s*(?:(\d{2,3})\s*年)?\s*(\d{1,2})?\s*月?\s*(\d{1,2})\s*日"
)
SINGLE_DATE_RE = re.compile(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def roc_to_gregorian(roc_year: str, month: str, day: str) -> str:
    year = int(roc_year) + 1911
    return f"{year:04d}-{int(month):02d}-{int(day):02d}"


def parse_event_page(event_id: str, url: str) -> EventInfo:
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    # 主要內文容器：CMS 常見會放在 article / .content / .article-content 之類
    # 的區塊裡；抓不到特定 class 時，退而求其次抓整個 <article> 或 <body> 文字。
    content_node = (
        soup.select_one("article")
        or soup.select_one(".article-content")
        or soup.select_one("#content")
        or soup.body
    )
    text = content_node.get_text("\n", strip=True) if content_node else ""

    name = (soup.select_one("h1") or soup.select_one("title"))
    name_text = name.get_text(strip=True) if name else ""

    info = EventInfo(id=event_id, name=name_text, url=url)
    info.raw_text_excerpt = text[:500]

    # 日期
    m = DATE_RANGE_RE.search(text)
    if m:
        y1, mo1, d1, y2, mo2, d2 = m.groups()
        info.date_start = roc_to_gregorian(y1, mo1, d1)
        end_year = y2 or y1
        end_month = mo2 or mo1
        info.date_end = roc_to_gregorian(end_year, end_month, d2)

    # 地點：抓「比賽地點」「地點：」後面那段文字
    venue_m = re.search(r"(?:比賽地點|地點)[：:]\s*([^\n]+)", text)
    if venue_m:
        info.venue = venue_m.group(1).strip()

    # 報名截止
    deadline_m = re.search(r"報名截止[日期]*[：:]\s*([^\n]+)", text)
    if deadline_m:
        info.registration_deadline = deadline_m.group(1).strip()

    # 報名費
    fee_m = re.search(r"報名費[用]*[：:]\s*([^\n]+)", text)
    if fee_m:
        info.registration_fee = fee_m.group(1).strip()

    # 主辦單位
    org_m = re.search(r"主辦單位[：:]\s*([^\n]+)", text)
    if org_m:
        info.organizer = org_m.group(1).strip()

    # 聯絡電話 / email
    info.contact_phone = re.findall(r"\(?0\d{1,2}\)?[\d\-]{6,}", text)
    email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if email_m:
        info.contact_email = email_m.group(0)

    # 附件 (通常在 /files/articleAttr/ 路徑下)
    for a in soup.select("a[href*='/files/']"):
        href = a.get("href", "")
        if not href:
            continue
        att_name = a.get_text(strip=True) or href.split("/")[-1]
        info.attachments.append(Attachment(name=att_name, url=urljoin(BASE_URL, href)))

    return info


# --------------------------------------------------------------------------
# Step 3：嘗試解析「總賽程表」PDF
# --------------------------------------------------------------------------

@dataclass
class Match:
    day: Optional[int] = None
    date: Optional[str] = None
    venue: Optional[str] = None
    time: Optional[str] = None
    group: Optional[str] = None
    match_no: Optional[str] = None
    team_a: Optional[str] = None
    team_b: Optional[str] = None
    source_page: Optional[int] = None


def find_schedule_attachment(info: EventInfo) -> Optional[Attachment]:
    """在附件裡找「總賽程表」之類的檔案。"""
    keywords = ["總賽程表", "賽程表", "賽程"]
    for att in info.attachments:
        if any(k in att.name for k in keywords):
            return att
    return None


def parse_schedule_pdf(pdf_bytes: bytes) -> tuple[list[Match], list[str]]:
    """
    嘗試用 pdfplumber 抓表格。回傳 (成功解析的場次, 解析警告訊息)。

    注意：CTVBA 的總賽程表是「場地(列) x 時間(欄)」的網格表，且不同賽事、
    不同年度排版不一定相同，這裡先用「抓每個 cell、如果符合『隊伍 vs 隊伍』
    或包含 vs/對 字樣的就視為一場比賽』的通用邏輯來抓，抓不到規律的表格
    會回報警告，改用「請查看附件PDF」的 fallback。
    """
    import pdfplumber  # 延遲載入，避免沒安裝時整支程式打不開

    matches: list[Match] = []
    warnings: list[str] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables()
            if not tables:
                warnings.append(f"第{page_index}頁沒有偵測到表格結構")
                continue

            page_text = page.extract_text() or ""
            day_m = re.search(r"第\s*(\d+)\s*天", page_text)
            date_m = SINGLE_DATE_RE.search(page_text)
            day_no = int(day_m.group(1)) if day_m else None
            date_str = (
                roc_to_gregorian(*date_m.groups()) if date_m else None
            )

            for table in tables:
                if not table or len(table) < 2:
                    continue
                header = table[0]
                for row in table[1:]:
                    venue = row[0] if row else None
                    for col_index, cell in enumerate(row[1:], start=1):
                        if not cell:
                            continue
                        cell = unicodedata.normalize("NFKC", cell).strip()
                        if not cell or ("vs" not in cell.lower() and "對" not in cell):
                            continue
                        time_label = (
                            header[col_index] if col_index < len(header) else None
                        )
                        teams = re.split(r"vs|VS|對", cell, maxsplit=1)
                        if len(teams) != 2:
                            continue
                        matches.append(Match(
                            day=day_no,
                            date=date_str,
                            venue=venue,
                            time=time_label,
                            team_a=teams[0].strip(),
                            team_b=teams[1].strip(),
                            source_page=page_index,
                        ))

    if not matches:
        warnings.append(
            "整份PDF都沒解析出符合格式的場次，這份賽程表可能是圖片掃描檔、"
            "手繪對戰圖，或欄位格式跟預期不同，需要人工確認並視情況調整"
            "解析規則，或直接連結附件PDF給使用者自行查看。"
        )

    return matches, warnings


# --------------------------------------------------------------------------
# 主程式
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", help="只抓單一賽事(文章ID)")
    parser.add_argument("--out", default=str(DATA_DIR / "events.json"),
                         help="輸出JSON路徑（正式跑法建議指向 docs/data/events.json，"
                              "GitHub Pages 才能直接讀到）")
    parser.add_argument("--skip-pdf", action="store_true",
                         help="跳過總賽程表PDF解析，只抓賽事基本資訊(比較快)")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.event_id:
        events = [e for e in list_events() if e["id"] == args.event_id]
        if not events:
            # 允許直接指定文章ID，即使沒出現在列表頁裡
            events = [{"id": args.event_id,
                       "name": "",
                       "url": f"{BASE_URL}/article/{args.event_id}/"}]
    else:
        events = list_events()
        print(f"列表頁找到 {len(events)} 個賽事")

    results = []
    for e in events:
        print(f"抓取賽事：{e['name'] or e['id']} ({e['url']})")
        try:
            info = parse_event_page(e["id"], e["url"])
        except Exception as exc:  # noqa: BLE001
            print(f"  失敗：{exc}", file=sys.stderr)
            continue

        result = asdict(info)

        if not args.skip_pdf:
            sched_att = find_schedule_attachment(info)
            if sched_att:
                print(f"  找到賽程表附件：{sched_att.name}，嘗試下載解析...")
                try:
                    pdf_bytes = fetch_bytes(sched_att.url)
                    matches, warnings = parse_schedule_pdf(pdf_bytes)
                    result["matches"] = [asdict(m) for m in matches]
                    result["parse_warnings"] = warnings
                    print(f"  解析出 {len(matches)} 場比賽，{len(warnings)} 則警告")
                except Exception as exc:  # noqa: BLE001
                    result["matches"] = []
                    result["parse_warnings"] = [f"下載或解析失敗：{exc}"]
            else:
                result["matches"] = []
                result["parse_warnings"] = ["附件裡找不到「總賽程表」"]
        else:
            result["matches"] = []
            result["parse_warnings"] = ["已跳過PDF解析(--skip-pdf)"]

        results.append(result)
        time.sleep(REQUEST_DELAY_SEC)

    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"寫出 {len(results)} 筆賽事資料到 {out_path}")


if __name__ == "__main__":
    main()
