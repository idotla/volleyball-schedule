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
  4. 把每篇文章的附件PDF下載到repo裡(docs/data/attachments/)，網頁可以直接
     連結本地檔案下載，不用依賴協會官網的原始連結。
  5. 用「標題文字有沒有變」當作變更偵測：跟上次抓到的標題一樣就直接沿用
     上次的完整結果(docs/data/scrape_cache.json)，不用重新抓文章內文、也
     不用重新下載附件PDF，避免每天對官網重複做一樣的抓取。

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
from datetime import date, datetime
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

# GitHub 單一檔案硬性上限是 100MB，超過會讓整個 push 被拒絕(pre-receive hook
# declined)。留一點緩衝空間，附件超過這個大小就不下載進repo，local_path維持
# None，網頁改連回協會官網的原始連結(這種通常是電子秩序冊之類的大型掃描檔)。
MAX_ATTACHMENT_BYTES = 80 * 1024 * 1024  # 80MB

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 預設只抓這幾個指定盃賽的資料（2026-09-10 使用者要求縮小範圍）。
# 用「不含杯/盃字樣的關鍵字」去比對賽事標題的子字串，因為官網同一個盃賽
# 在不同文章裡「杯」「盃」兩種寫法都會出現（例如「永信杯」vs 標題其他地方
# 可能寫成「永信盃」），拿掉那個字才能兩種寫法都比對得到。
DEFAULT_TOURNAMENT_KEYWORDS = ["永信", "媽祖", "華宗", "和家"]


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------

@dataclass
class Attachment:
    name: str
    url: str
    # 附件PDF下載到repo後的相對路徑(相對於 docs/ 目錄，例如
    # "data/attachments/1950/xxx.pdf")，讓網頁可以直接連結本地檔案下載，
    # 不用依賴協會官網的原始連結(連結未來可能失效，或檔案被置換)。
    # 下載失敗、或執行時加了 --skip-download 的話會維持 None。
    local_path: Optional[str] = None


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
    last_modified: Optional[str] = None  # 官網文章頁底部「修改日期」，ISO格式(YYYY-MM-DD)


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


def fetch_bytes_capped(url: str, max_bytes: int) -> Optional[bytes]:
    """跟 fetch_bytes 一樣，但邊下載邊檢查大小，一旦超過 max_bytes 就中止連線、
    回傳 None(而不是先整個下載完才發現太大——附件PDF可能到幾百MB，白白浪費
    流量跟時間)。"""
    with requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        content_length = resp.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    return None
            except ValueError:
                pass
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)


# --------------------------------------------------------------------------
# Step 1：抓賽事列表
# --------------------------------------------------------------------------

def list_events() -> list[dict]:
    """回傳 [{"id": "1950", "name": "...", "url": "..."}]"""
    html = fetch(INGAME_LIST_URL)
    soup = BeautifulSoup(html, "html.parser")

    events = []
    seen_ids = set()
    # 賽事列表實際上是頁面裡唯一一個 <table class="highlight striped">，
    # 一定要scope在這張表格裡面抓，否則會連同左側選單、頁尾等處的
    # /article/ 連結（例如各縣市委員會的「組織簡則」「成員名單」）一起
    # 抓進來，混進上百筆不相關的連結。
    # (2026-09-10 實跑驗證：不scope會抓到 126 個連結，scope之後剛好 30 個。)
    scope = soup.select_one("table.highlight.striped") or soup
    # 賽事文章連結格式觀察到的樣式為 /article/{id}/{slug}
    for a in scope.select("a[href*='/article/']"):
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


def filter_events_by_keywords(events: list[dict], keywords: list[str]) -> list[dict]:
    """只保留標題包含任一關鍵字的賽事。keywords 是空list就直接回傳全部(不篩選)。"""
    if not keywords:
        return events
    return [e for e in events if any(k in e["name"] for k in keywords)]


# 標題裡的年度標記，用來避免把不同屆/不同年的舊公告誤合併在一起。
# CTVBA標題幾乎都會在最前面標年度，但有的用民國年(3碼，例如「115年」)、
# 有的用西元年(4碼、20開頭，例如「2026年」)，要各自抓、再統一換算成西元年
# 比較。(?<!\d) 是為了避免「2026年」被 3碼regex 誤吃成「026年」→誤判成
# 民國26年這種離譜結果。
ROC_YEAR_IN_TITLE_RE = re.compile(r"(?<!\d)(\d{3})年")
GREGORIAN_YEAR_IN_TITLE_RE = re.compile(r"(?<!\d)(20\d{2})年")


def _extract_title_year(name: str) -> Optional[int]:
    """從賽事標題抓出「這篇公告屬於西元哪一年」，抓不到回傳None。"""
    candidates = []
    m_roc = ROC_YEAR_IN_TITLE_RE.search(name)
    if m_roc:
        candidates.append((m_roc.start(), int(m_roc.group(1)) + 1911))
    m_ad = GREGORIAN_YEAR_IN_TITLE_RE.search(name)
    if m_ad:
        candidates.append((m_ad.start(), int(m_ad.group(1))))
    if not candidates:
        return None
    # 標題裡如果兩種年份標記都出現，用「先出現」的那個，比較符合標題語意
    # (年度通常寫在標題最前面)。
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def pick_latest_year_events(events: list[dict], keywords: list[str]) -> list[dict]:
    """在還沒抓文章內文、還沒下載附件之前，先只用列表頁標題判斷「這個關鍵字
    底下最新一屆是哪一年」，只留下那個年度的文章，把其他舊年度的文章直接
    丟掉，不進到後面「抓內文+下載附件」的迴圈。

    背景(2026-09-10)：merge_events_by_tournament() 本來就只會在最後把非
    最新年度的文章丟掉、不輸出，但「丟掉」是在抓完內文、下載完附件PDF
    之後才發生的，等於白白對官網多抓了好幾篇舊公告、下載了好幾份舊PDF
    (例如114年/去年那屆已經打完的「華宗盃」成績公告跟賽程表)——實際跑
    GitHub Actions時就因為這樣一次要commit的資料量變太大，push還逾時
    失敗過一次。提早在這裡濾掉，可以避免這些用不到的網路請求，也讓
    repo不會塞進一堆網站上其實不會顯示的舊年度附件。

    分組邏輯跟merge_events_by_tournament()一致(用同一套「先比對到的
    關鍵字」分組、「年度最大」優先，抓不到年度的視為最舊)，這樣這裡濾掉
    的文章，一定也是merge最後才會丟掉的那些，不會提早濾掉不該濾的。
    """
    if not keywords:
        return events

    by_kw_year: dict[str, dict[Optional[int], list[dict]]] = {}
    unmatched: list[dict] = []
    for e in events:
        matched_kw = next((k for k in keywords if k in (e.get("name") or "")), None)
        if matched_kw is None:
            unmatched.append(e)
            continue
        year = _extract_title_year(e.get("name") or "")
        by_kw_year.setdefault(matched_kw, {}).setdefault(year, []).append(e)

    kept: list[dict] = []
    for year_groups in by_kw_year.values():
        best_year = max(year_groups.keys(), key=lambda y: (y is not None, y or 0))
        kept.extend(year_groups[best_year])

    return unmatched + kept


def merge_events_by_tournament(results: list[dict], keywords: list[str]) -> list[dict]:
    """
    把同一個盃賽底下抓到的多篇公告文章合併成一筆賽事資料。

    背景：CTVBA官網一個盃賽通常會有好幾篇獨立公告文章(競賽規程、成績公告、
    住宿資訊、會議紀錄、即時比分連結...)，用關鍵字篩選(--tournaments)只是
    把「不相關的其他賽事」濾掉，同一個盃賽底下這些文章本身還是各自一筆，
    直接輸出到events.json的話，網頁上的「Coming Soon」清單一個盃賽會重複
    出現好幾行，使用者要看的其實是「這個盃賽」而不是「這篇公告」。

    **重要(2026-09-10 修過的bug)**：同一個關鍵字(例如「華宗」)底下，不只
    有「今年這一屆」的公告，官網列表也會留著「去年那一屆已經打完」的舊公告
    (成績一覽表、即時成績、舊賽程表...)。原本的合併邏輯只看關鍵字、不看
    年度/屆數，結果把「115年第47屆華宗盃競賽規程」(今年、還沒開打、也還
    沒有總賽程表附件)跟「114年第46屆華宗盃...」(去年、已經打完的舊公告，
    其中一篇有一份真的總賽程表PDF附件)合併成同一筆，導致解析出587場「去年
    的」比賽場次、卻顯示在「今年」這筆賽事底下，看起來像是今年已經有完整
    賽程一樣——這是使用者直接發現並回報的錯誤資料。
    修法：合併前先用 `_extract_title_year()` 抓出每篇文章標題裡的年度，
    同一個關鍵字底下再依年度細分；每個關鍵字最後只保留「年度最新」的那一組
    來合併，年度較舊的公告不會被合併進來、也不會另外顯示成一筆(反正使用者
    要的是「這幾個盃賽最新一屆」的資訊，不是歷屆舊資料)。抓不到年度的文章
    視為最舊、優先度最低。

    只有在有指定關鍵字篩選時才合併——沒有 --tournaments 篩選(要看全部賽事
    原始清單)的情況下不合併，避免在關鍵字不明確時把不相關的賽事誤合併。

    合併規則(同一個關鍵字+同一年度那組內)：
    - 每組挑一篇「主要文章」代表這個盃賽的name/url/日期/地點等metadata：
      優先選標題含「競賽規程」的(通常是最完整、最正式的官方公告，賽事全名、
      日期、地點、報名費都寫得最清楚)，其次選有解析出matches的，都沒有就
      選第一篇。matches本身是合併整組所有文章解析出來的結果(見下)，跟
      「主要文章」的選擇無關，就算「總賽程表」那篇不是主要文章，它的
      matches還是會被合併進來。
    - 純量欄位(日期、地點、主辦單位、報名截止、報名費、聯絡email、修改日期)
      以主要文章為主，缺漏的用同組其他文章裡第一個非空值補上。
    - contact_phone / attachments：合併同組所有文章的，並去重。
    - matches：合併同組所有文章解析出來的matches(正常情況下只有「總賽程表」
      那篇文章有解析出東西)，用(day,time,venue,team_a,team_b)去重，並依
      day/time排序。
    - parse_warnings：只保留主要文章的，其他文章的parse_warnings(通常只是
      「這篇公告的附件裡沒有總賽程表」這種對其他公告本來就正常的訊息)沒必要
      顯示出來混淆使用者。
    - source_articles：新增欄位，記錄同組所有來源文章的{name,url}，讓使用者
      需要時可以自己點進去看原始公告全文(例如完整競賽規程)。
    """
    if not keywords:
        return results

    by_kw_year: dict[str, dict[Optional[int], list[dict]]] = {}
    unmatched: list[dict] = []
    for r in results:
        matched_kw = next((k for k in keywords if k in (r.get("name") or "")), None)
        if matched_kw is None:
            unmatched.append(r)
            continue
        year = _extract_title_year(r.get("name") or "")
        by_kw_year.setdefault(matched_kw, {}).setdefault(year, []).append(r)

    merged: list[dict] = []
    for year_groups in by_kw_year.values():
        # 同一個關鍵字底下可能混到不同年度的舊公告，只留「年度最新」的
        # 那一組合併；年度是None(抓不到)的視為最舊、優先度最低。
        best_year = max(year_groups.keys(), key=lambda y: (y is not None, y or 0))
        group = year_groups[best_year]

        primary = (
            next((r for r in group if "競賽規程" in (r.get("name") or "")), None)
            or next((r for r in group if r.get("matches")), None)
            or group[0]
        )
        out = dict(primary)

        scalar_fields = [
            "date_start", "date_end", "date_note", "venue", "organizer",
            "registration_deadline", "registration_fee", "contact_email",
            "last_modified",
        ]
        for f in scalar_fields:
            if not out.get(f):
                for r in group:
                    if r.get(f):
                        out[f] = r[f]
                        break

        phones: list[str] = []
        for r in group:
            for p in (r.get("contact_phone") or []):
                if p not in phones:
                    phones.append(p)
        out["contact_phone"] = phones

        atts: list[dict] = []
        seen_urls: set = set()
        for r in group:
            for a in (r.get("attachments") or []):
                if a.get("url") in seen_urls:
                    continue
                seen_urls.add(a.get("url"))
                atts.append(a)
        out["attachments"] = atts

        seen_match_keys: set = set()
        all_matches: list[dict] = []
        for r in group:
            for m in (r.get("matches") or []):
                key = (m.get("day"), m.get("time"), m.get("venue"),
                       m.get("team_a"), m.get("team_b"))
                if key in seen_match_keys:
                    continue
                seen_match_keys.add(key)
                all_matches.append(m)
        all_matches.sort(key=lambda m: (
            m.get("day") if m.get("day") is not None else 999,
            m.get("time") or "",
            m.get("venue") or "",
        ))
        out["matches"] = all_matches
        if all_matches:
            # 已經從同組某篇文章(通常是總賽程表)合併出實際場次資料了，
            # 「主要文章」(可能是競賽規程，本來就不含附件PDF場次)自己的
            # parse_warnings(例如「附件裡找不到總賽程表」)在這裡已經不成立，
            # 不該顯示出來誤導使用者以為沒有場次資料。
            out["parse_warnings"] = []
        else:
            seen_warnings: set = set()
            merged_warnings: list[str] = []
            for r in group:
                for w in (r.get("parse_warnings") or []):
                    if w in seen_warnings:
                        continue
                    seen_warnings.add(w)
                    merged_warnings.append(w)
            out["parse_warnings"] = merged_warnings

        out["source_articles"] = [
            {"name": r.get("name"), "url": r.get("url")} for r in group
        ]

        merged.append(out)

    return unmatched + merged


# --------------------------------------------------------------------------
# Step 2：解析單一賽事文章頁
# --------------------------------------------------------------------------

# 日期範圍正則，處理過程中發現兩個容易誤判的實際案例，寫法特別針對它們調整過：
#   1. (2026-09-10 event 1909) 結束日沒有重複「月」，例如「7月16日至21日」，
#      如果月份用 (\d{1,2})?\s*月?\s*(\d{1,2}) 這種寫法，「21」會被貪婪地
#      拆成「月=2、日=1」這種離譜結果。修法：把「月」這個字設為月份數字的
#      必要條件（用 (?:(\d{1,2})\s*月\s*)? 包住），沒有「月」字就不吃這個
#      數字，讓它完整留給「日」。
#   2. (event 1950) 日期後面常常緊接著「(六)」「(二)」這種星期幾附註，例如
#      「9月19日(六)至9月22日(二)」，如果沒有處理這段插入文字，
#      \s*(?:至|~|-) 會因為前面多了「(六)」而配對不到，導致整個 range 都
#      抓不到，退化成只抓到起始日。修法：在日期後面加一個可選的
#      「(...)」群組，把星期幾附註吃掉再繼續比對。
DATE_RANGE_RE = re.compile(
    r"(?:中華民國)?\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*[\(（][^)）]*[\)）])?"
    r"\s*(?:至|~|-)\s*(?:(\d{2,3})\s*年\s*)?(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日"
    r"(?:\s*[\(（][^)）]*[\)）])?"
)
SINGLE_DATE_RE = re.compile(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")

# 總賽程表PDF每頁最上面會有「1150919(六)」這種「民國年3碼+月2碼+日2碼」
# 沒有分隔符號、緊接著星期幾(括號)的日期格式，跟文章內文的「115年9月19日」
# 完全不同格式，所以另外開一個regex，不能共用SINGLE_DATE_RE。
PDF_HEADER_DATE_RE = re.compile(r"(\d{3})(\d{2})(\d{2})\(")

# 文章頁最下面通常有「修改日期：2026/08/28 12:27」這種西元格式的時間戳，
# 用來判斷這篇文章是不是「最近才更新過」的。跟上面比賽日期用民國年不同，
# 這裡官網本身就是西元年，不用轉換。
LAST_MODIFIED_RE = re.compile(r"修改日期[：:]\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})")


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

    # 賽事標題：實際觀察到的DOM結構是 <article> 裡面的 <h4>，頁面沒有 <h1>，
    # <title> 則是全站共用的「中華民國排球協會」，不能拿來當標題退路。
    # (2026-09-10 用瀏覽器實測 DOM 才發現這點。)
    name_node = None
    if content_node:
        name_node = (
            content_node.select_one("h4")
            or content_node.select_one("h1")
            or content_node.select_one("h2")
        )
    if not name_node:
        name_node = soup.select_one("h1")
    name_text = name_node.get_text(strip=True) if name_node else ""

    info = EventInfo(id=event_id, name=name_text, url=url)
    info.raw_text_excerpt = text[:500]

    # 日期：不要直接對整篇文章文字(text)跑正則！
    # text 是把所有段落用 \n 接起來的長字串，DATE_RANGE_RE 裡的 \s* 會吃掉
    # \n，導致 regex 在「比賽日期」那行沒有乾淨符合時，往下backtrack、
    # 跨段落配對到頁面下方完全無關的日期文字，抓出離譜的結果(例如
    # date_end 比 date_start 還早)。
    # (2026-09-10 用真實爬到的 event 1909 資料root-cause：原文是「比賽日期：
    # 115 年 7 月 16 日至 21 日」，結束日沒有重複年月，照理該用 date_start
    # 的年月補上，但跨段落誤配對蓋掉了正確結果。)
    # 修法：先抓出「比賽日期：...」那一行(用 [^\n]+ 限制在同一行內)，
    # 只對這個子字串跑日期正則，避免跨段落誤配對。
    date_line_m = re.search(r"比賽日期[：:]\s*([^\n]+)", text)
    date_search_text = date_line_m.group(1) if date_line_m else text
    if date_line_m:
        # date_note 保留原始文字(例如「中華民國115年9月19日(六)至9月22日(二)，共4天」)，
        # 之前這個欄位從來沒被賦值過，網頁上一直顯示空白。
        info.date_note = date_search_text.strip()
    m = DATE_RANGE_RE.search(date_search_text)
    if not m:
        m = SINGLE_DATE_RE.search(date_search_text)
        if m:
            y1, mo1, d1 = m.groups()
            info.date_start = roc_to_gregorian(y1, mo1, d1)
            info.date_end = info.date_start
            m = None  # 已經在這裡手動處理過了，避免下面的 range 邏輯重複跑
    if m:
        y1, mo1, d1, y2, mo2, d2 = m.groups()
        info.date_start = roc_to_gregorian(y1, mo1, d1)
        end_year = y2 or y1
        end_month = mo2 or mo1
        info.date_end = roc_to_gregorian(end_year, end_month, d2)

    # 地點：不同賽事委員會用詞不太一樣，實測看過「比賽地點」「競賽場地」兩種，
    # 都收進來抓；並把行尾常見的句點（。/.）去掉，避免資料尾巴多一個符號。
    venue_m = re.search(r"(?:比賽地點|競賽場地|地點)[：:]\s*([^\n]+)", text)
    if venue_m:
        info.venue = venue_m.group(1).strip().rstrip("。.")

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

    # 修改日期(西元年)，用來判斷文章是不是最近才更新的
    mod_m = LAST_MODIFIED_RE.search(text)
    if mod_m:
        y, mo, d = mod_m.groups()
        info.last_modified = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    # 附件 (通常在 /files/articleAttr/ 路徑下)
    for a in soup.select("a[href*='/files/']"):
        href = a.get("href", "")
        if not href:
            continue
        att_name = a.get_text(strip=True) or href.split("/")[-1]
        info.attachments.append(Attachment(name=att_name, url=urljoin(BASE_URL, href)))

    return info


# --------------------------------------------------------------------------
# Step 2.5：把附件PDF下載到repo裡，並記錄相對路徑
# --------------------------------------------------------------------------

def _sanitize_attachment_filename(name: str) -> str:
    """把附件原始檔名清成安全的檔案系統路徑片段。

    官網附件檔名本身通常已經是正常檔名(例如「115_53rd永信杯競賽規程0701.pdf」)，
    這裡只是防呆：拿掉路徑分隔符號(避免不小心跳出目標資料夾)、拿掉控制字元，
    檔名太長就截斷(保留副檔名)，抓不到檔名就給預設值。
    """
    name = (name or "").replace("/", "_").replace("\\", "_").strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = name.lstrip(".")  # 避免變成隱藏檔或 ".."
    if not name:
        name = "attachment"
    if len(name) > 150:
        if "." in name:
            stem, ext = name.rsplit(".", 1)
            name = stem[:140] + "." + ext
        else:
            name = name[:150]
    return name


def download_attachments(info: EventInfo, attachments_dir: Path) -> None:
    """把這個賽事所有附件PDF下載到本地repo裡(attachments_dir/賽事id/檔名)，
    並把下載後的相對路徑寫回每個 Attachment.local_path(相對於 docs/ 目錄，
    例如 "data/attachments/1950/xxx.pdf")。

    背景(2026-09-10 使用者要求)：原本網頁上完全沒有附件下載連結，使用者要看
    競賽規程/總賽程表這些PDF只能自己去協會官網找，體驗不好，而且協會官網的
    連結未來也可能失效或被置換。改成爬蟲直接把PDF抓下來存進repo、網頁直接
    連本地檔案。

    已經下載過、檔案存在且大小 > 0 的附件不會重新下載——公告發布後附件內容
    幾乎不會再變(如果真的置換了新檔案，官網那篇文章的檔名通常也會跟著換，
    此時會被當成新檔名重新下載，不會沿用舊內容)，這樣可以避免每天重複下載
    同樣的檔案，節省網路流量跟執行時間。
    """
    event_dir = attachments_dir / info.id
    for att in info.attachments:
        safe_name = _sanitize_attachment_filename(att.name)
        dest = event_dir / safe_name
        rel_path = f"data/attachments/{info.id}/{safe_name}"
        if dest.exists() and dest.stat().st_size > 0:
            att.local_path = rel_path
            continue
        try:
            content = fetch_bytes_capped(att.url, MAX_ATTACHMENT_BYTES)
        except Exception as exc:  # noqa: BLE001
            print(f"    附件下載失敗：{att.name} ({exc})", file=sys.stderr)
            continue
        if content is None:
            print(
                f"    附件超過 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB，"
                f"不下載進repo，網頁將改連協會官網原始連結：{att.name}",
                file=sys.stderr,
            )
            continue
        if not content:
            continue
        event_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        att.local_path = rel_path
        print(f"    已下載附件：{att.name} ({len(content)} bytes)")
        time.sleep(REQUEST_DELAY_SEC)


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


# 表格cell裡場次標籤的格式是「組別 輪次 M數字」，例如「社男 ⼩組賽D M1」，
# 數字前面的M是場次編號，用來把「組別/輪次」跟「場次編號」拆開存。
MATCH_LABEL_RE = re.compile(r"^(.*?)(?:\s+(M\.?\d+))?$")

# 總賽程表PDF的內嵌字型有些字會被錯誤對應到「部首」符號而不是正常漢字
# (例如「三民國中」被抽出來變成「三⺠國中」，「長春國小」變成「⻑春國小」)，
# 這是PDF本身字型編碼的問題，不是我們解析邏輯的bug，NFKC normalize也修不了
# 這幾個(它們屬於CJK Radicals Supplement，NFKC不會分解回本字)。
# 目前只發現這兩個字有這個問題，用簡單對照表修正；未來如果發現更多字有
# 同樣狀況，在這裡繼續加。
PDF_GLYPH_FIXES = {
    "⺠": "民",  # CJK RADICAL CIVILIAN -> 民
    "⻑": "長",  # CJK RADICAL LONG ONE -> 長
}


def _fix_pdf_glyphs(s: str) -> str:
    for bad, good in PDF_GLYPH_FIXES.items():
        s = s.replace(bad, good)
    return s


def parse_schedule_pdf(pdf_bytes: bytes) -> tuple[list[Match], list[str]]:
    """
    嘗試用 pdfplumber 抓表格。回傳 (成功解析的場次, 解析警告訊息)。

    2026-09-10 用真實下載的「115年第53屆永信盃」總賽程表PDF實測後，
    確認CTVBA的總賽程表是「時間(列) x 場地(欄)」的網格表(不是原本猜測的
    「場地(列) x 時間(欄)」，欄列相反)，每個有比賽的cell內容是「組別/輪次
    標籤 \n 隊伍A \n 隊伍B」三行文字堆疊在一起，不是「隊伍A vs 隊伍B」單行
    字串——舊版邏輯找 "vs"/"對" 字樣，實際PDF裡完全不會出現這兩個字，
    所以永遠抓不到東西。改用「每個cell用換行拆成多行，抓得到3行以上的才
    當作一場比賽(第1行是組別/輪次標籤，第2、3行是兩隊隊名)」的邏輯。

    另外要注意：
    - 表頭(header)第0欄是日期(例如「1150919(六)」，用PDF_HEADER_DATE_RE另外
      解析，跟文章內文日期格式不同)，其餘欄位是「場地 01」「場地 02」...。
    - 每個row第0欄是時間(例如「09:00」)，用來判斷這一列是不是真的賽程列
      (跳過像「09:00 開幕典禮」這種只有單行文字、沒有隊伍資訊的列)。
    - 複賽/淘汰賽輪次(通常在最後一天)會出現「隊伍還沒決定，只有前一場
      勝負代號(如 W21/L21)」的cell，只有2行文字、沒有真正隊名，這種cell
      沒辦法抓出隊名，直接跳過(不算解析失敗，只是那場還沒對到真正隊伍)。
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

            # 「第X天」有時候會被拆成「第 天\n X」(X跑到天後面)，兩種格式都要試。
            day_m = re.search(r"第\s*(\d+)\s*天", page_text)
            if not day_m:
                day_m = re.search(r"天\s*\n\s*(\d+)", page_text)
            day_no = int(day_m.group(1)) if day_m else None

            date_m = PDF_HEADER_DATE_RE.search(page_text)
            date_str = (
                roc_to_gregorian(*date_m.groups()) if date_m else None
            )

            for table in tables:
                if not table or len(table) < 2:
                    continue
                # 表頭列(日期+場地欄位)不一定是table的第0列——有時候PDF會把
                # 賽事標題(跨欄合併儲存格)獨立佔一整列排在最前面(例如場地
                # 12~19那個table)，這種情況下table[0]其實是標題列，不是
                # 表頭，要往下找「有欄位以『場地』開頭」的那一列才是真表頭。
                header = None
                header_idx = None
                for idx, row in enumerate(table):
                    if row and any(
                        c and unicodedata.normalize("NFKC", c).strip().startswith("場地")
                        for c in row[1:]
                        if c
                    ):
                        header = row
                        header_idx = idx
                        break
                if header is None:
                    warnings.append(
                        f"第{page_index}頁的表格找不到「場地」欄位表頭，跳過"
                    )
                    continue
                venues = header[1:]
                for row in table[header_idx + 1:]:
                    if not row or not row[0]:
                        continue
                    time_label = unicodedata.normalize("NFKC", row[0]).strip()
                    if not re.match(r"^\d{1,2}:\d{2}$", time_label):
                        # 例如表尾空白列，或不是「時間」開頭的列，跳過。
                        continue
                    for col_index, cell in enumerate(row[1:], start=1):
                        if not cell:
                            continue
                        cell = _fix_pdf_glyphs(unicodedata.normalize("NFKC", cell)).strip()
                        lines = [l.strip() for l in cell.split("\n") if l.strip()]
                        if len(lines) < 3:
                            # 少於3行代表這格不是「標籤+兩隊」的完整比賽資訊
                            # (例如「開幕典禮」只有1行，或淘汰賽還沒決定隊伍
                            # 只有勝負代號的2行)，抓不出隊名，跳過。
                            continue
                        label, team_a, team_b = lines[0], lines[1], lines[2]
                        label_m = MATCH_LABEL_RE.match(label)
                        group = label_m.group(1).strip() if label_m else label
                        match_no = label_m.group(2) if label_m else None
                        venue_raw = (
                            venues[col_index - 1]
                            if col_index - 1 < len(venues)
                            else None
                        )
                        venue = (
                            _fix_pdf_glyphs(unicodedata.normalize("NFKC", venue_raw)).strip()
                            if venue_raw
                            else None
                        )
                        matches.append(Match(
                            day=day_no,
                            date=date_str,
                            venue=venue,
                            time=time_label,
                            group=group,
                            match_no=match_no,
                            team_a=team_a,
                            team_b=team_b,
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
# 變更偵測快取：標題沒變就沿用上次抓到的完整結果，不用重新抓文章+附件
# --------------------------------------------------------------------------

# 2026-09-10 使用者要求：避免每天重複抓取同樣的資料。CTVBA官網公告文章如果
# 內容有更新，標題通常會自己補上「幾月幾號更新」(例如本來叫「115年第53屆
# 「永信杯」全國排球錦標賽 競賽規程」，加了分組表之後標題變成「...競賽規程
# (8/２８更新分組賽製圖及總賽程表)」)，所以「標題文字有沒有變」是判斷「這篇
# 公告內容是不是更新過」很好用的訊號，比較文章列表頁的標題比重新抓文章內文
# 再比對修改日期簡單、成本也低(只需要抓一次列表頁，不用每篇都進去)。
# 快取檔存在 docs/data/scrape_cache.json，跟events.json一起commit回repo。
def load_cache(cache_path: Path) -> dict:
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"讀取快取檔失敗，當作沒有快取繼續跑：{exc}", file=sys.stderr)
        return {}


def save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
    parser.add_argument("--skip-download", action="store_true",
                         help="跳過附件PDF下載(只記錄協會官網的原始連結，不存進repo)")
    parser.add_argument("--no-cache", action="store_true",
                         help="不使用/更新變更偵測快取，每篇公告都強制重新抓取"
                              "(除錯或想確保拿到最新資料時用)")
    parser.add_argument(
        "--tournaments",
        default=",".join(DEFAULT_TOURNAMENT_KEYWORDS),
        help="只抓標題包含這些關鍵字的盃賽，多個關鍵字用逗號分隔"
             f"(預設：{','.join(DEFAULT_TOURNAMENT_KEYWORDS)})。"
             "傳空字串 --tournaments \"\" 表示不篩選、抓全部賽事。"
             "用 --event-id 直接指定單一賽事時不受此篩選影響。",
    )
    parser.add_argument(
        "--hide-past-days",
        type=int,
        default=0,
        help="用「比賽日期」(不是文章修改日期)判斷賽事是不是已經過期："
             "比賽結束日(date_end)超過N天前就濾掉，預設0天表示比賽一結束"
             "就濾掉，還在進行中或未來才開打的都保留。想讓剛結束的賽事"
             "還留著一陣子方便回顧，可以調大這個數字，例如30。"
             "(2026-09-10 原本是用「修改日期」判斷「文章是不是最近更新過」，"
             "但這樣會把『比賽還沒開打、公告內容其實還有效，只是官網很早"
             "就發布、後來沒再更新』的賽事也濾掉——例如永信盃的總賽程表"
             "8/28發布後就沒再變過，但比賽是9/19~9/22，用修改日期判斷會"
             "誤判成『太舊』而濾掉，明明比賽根本還沒開打。改用比賽日期判斷"
             "才是真正符合『這個網站是查賽程』的用途。)"
             "文章抓不到比賽日期時，保守起見還是會保留(無法判斷是否已經"
             "過期，不代表它已經過期)。"
             "用 --event-id 直接指定單一賽事時不受此篩選影響。",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    attachments_dir = out_path.parent / "attachments"
    cache_path = out_path.parent / "scrape_cache.json"

    keywords: list[str] = []
    if args.event_id:
        # 直接指定文章ID時，視為使用者明確要抓這一篇，不套用盃賽關鍵字篩選
        events = [e for e in list_events() if e["id"] == args.event_id]
        if not events:
            # 允許直接指定文章ID，即使沒出現在列表頁裡
            events = [{"id": args.event_id,
                       "name": "",
                       "url": f"{BASE_URL}/article/{args.event_id}/"}]
    else:
        events = list_events()
        print(f"列表頁找到 {len(events)} 個賽事")
        keywords = [k.strip() for k in args.tournaments.split(",") if k.strip()]
        if keywords:
            events = filter_events_by_keywords(events, keywords)
            print(f"依關鍵字 {keywords} 篩選後剩 {len(events)} 個賽事")
            events = pick_latest_year_events(events, keywords)
            print(f"只保留每個盃賽最新年度的公告後剩 {len(events)} 個賽事"
                  "(避免抓/下載舊年度已經用不到的公告跟附件)")

    cache: dict = {} if (args.event_id or args.no_cache) else load_cache(cache_path)
    new_cache: dict = {}

    results = []
    for e in events:
        cached = cache.get(e["id"])
        if cached and cached.get("name") == e["name"]:
            # 標題跟上次抓到的一模一樣，視為內容沒更新，直接沿用快取的完整
            # 結果(已經含matches、附件local_path等)，不用重新抓文章內文、
            # 也不用重新下載附件PDF。
            print(f"  標題未變，沿用快取：{e['name'] or e['id']}")
            result = cached
        else:
            print(f"抓取賽事：{e['name'] or e['id']} ({e['url']})")
            try:
                info = parse_event_page(e["id"], e["url"])
            except Exception as exc:  # noqa: BLE001
                print(f"  失敗：{exc}", file=sys.stderr)
                continue

            if not args.skip_download:
                download_attachments(info, attachments_dir)

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

        if not args.event_id:
            # 不管這筆賽事等一下會不會被下面的hide-past-days濾掉，都先存進
            # 快取——這樣比賽結束、首頁不再顯示之後，只要標題沒再變，下次
            # 還是能命中快取，不用每天重新抓一次已經打完、內容不會再變的
            # 舊公告。
            new_cache[e["id"]] = result

        if not args.event_id and result.get("date_end"):
            comp_end = datetime.strptime(result["date_end"], "%Y-%m-%d").date()
            days_since_end = (date.today() - comp_end).days
            if days_since_end > args.hide_past_days:
                print(f"  略過(比賽日期 {result['date_end']} 已結束，{days_since_end} "
                      f"天前，超過 {args.hide_past_days} 天門檻)")
                continue

        results.append(result)
        time.sleep(REQUEST_DELAY_SEC)

    if not args.event_id and keywords:
        before = len(results)
        results = merge_events_by_tournament(results, keywords)
        print(f"依盃賽合併：{before} 篇公告文章合併成 {len(results)} 筆賽事資料")

    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"寫出 {len(results)} 筆賽事資料到 {out_path}")

    if not args.event_id:
        save_cache(cache_path, new_cache)
        print(f"寫出 {len(new_cache)} 筆快取到 {cache_path}")


if __name__ == "__main__":
    main()
