#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
華宗盃即時比分爬蟲（Google試算表，2026-10-17新增）。

跟 scrape_live_scores.py（永信盃，vbg.yungshingroup.com）完全獨立、資料來源
不同：這裡抓的是主辦單位開放給現場志工直接打分的 Google 試算表
（https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid=0，使用者傳的
短網址 https://reurl.cc/zOGGMV 展開後就是這份表），不是 vbg 網站、也不是協會
官網(ctvba.org.tw)的總賽程表PDF。

跟永信盃那份抓法最大的不同：vbg網站每次只能查「某一天」的比分、過了那天
資料就不在網站上了，所以scrape_live_scores.py要靠--day參數＋merge_matches()
自己把每天的結果累加保留下來。這份Google試算表相反：**一整個賽會(10/17~
10/21全部5天)的比分都在同一份試算表、同一個分頁(工作表1, gid=0)裡**，新的
一天開始時試算表會直接往下新增一段新的「場地欄位」標頭(例如「1018(日)」)
再接著當天的場次，前面幾天的資料不會被清掉、也不會被覆蓋。所以這裡**不需要**
像永信盃那樣額外做「累加保留」的merge邏輯，每次執行直接重新抓整份試算表、
重新解析出完整的全部場次清單，整份覆蓋輸出的JSON即可——資料本身的『跨天
累加』已經由試算表自己負責了。

版面結構（人工在Google試算表裡現場key分數，格式比vbg網站鬆散很多）：
  - 每個場地佔9欄一組，欄位依序是：
    [時間, 組別或比賽代號, 隊名, 獲勝局數, 第1局比分, 第2局比分, 第3局比分,
     結束時間, (空白分隔欄)]
  - 每一場比賽佔「兩列」：第一列是A隊(時間欄有值、第2欄是組別名稱如
    「高男甲」)，緊接著下一列是B隊(時間欄空白、第2欄是比賽代號如
    「小組賽A_M1」)。這跟一般直覺「A隊列放比賽代號」是反過來的，是這份
    表格本身的填法、不是爬蟲的bug。
  - 每個場地欄位最上面原本只有第0列(整份表格唯一的全域表頭)標示日期跟場地
    名稱，但新的一天開始時，表格中間會插入「不屬於任何一場比賽」的標頭列
    (可能只有日期、可能只有場地名稱、也可能兩者分兩列、也可能某個場地欄位
    那一列整列全空白當分隔)，人工填寫方式每個場地欄位不完全一致。這裡不試
    著去完美解析每一種標頭格式，而是用更穩健的判斷：只要「隊名」那一欄是
    空的，這一列就不算是比賽資料(不管它是標頭、分隔空白列、還是別的場地欄
    位還沒排到這一天)，直接跳過，剩下有隊名的列再兩兩配對成一場比賽——這樣
    不管標頭格式多不一致，都不會把標頭列誤判成比賽。
  - 日期則是另外單獨掃描全表(不分場地欄位、跨所有場地欄位一起看)、找「像
    日期」的文字(例如「1018(日)」這種「4位數字+括號」格式)來更新「目前是
    第幾天」的狀態，套用到當前列之後遇到的比賽列上，這樣就不需要每個場地
    欄位都自己標對日期才能正確分天。

字元編碼怪癖（實測發現，很重要）：這份試算表裡的隊名、場地名稱用到的部分
中文字，用的是「康熙部首」這個Unicode區段的相容字元(例如「⾼」U+2FBC)，
跟一般打字用的正常中文字(「高」U+9AD8)視覺上幾乎一樣、但是不同的字元碼，
如果不處理，拿去跟官方賽程(events.json，用一般正常中文字)比對隊名一定全部
比對失敗。解法：用 unicodedata.normalize("NFKC", ...) 做相容字元正規化，
正好可以把這些康熙部首字元還原成一般對應的中文字(「⾼」->「高」、
「⾵」->「風」、「⼩」->「小」等)，前端 docs/index.html 的 findLiveMatch()
本來就是用正規化後的隊名去跟官方賽程比對，兩邊才能真正對得起來。

輸出的JSON schema比照 live_scores_1950.json 的欄位名稱/型別(event_id,
event_name, day, source_url, updated_at, matches[]，每場比賽都有venue/time/
group/match_code/team_a/team_b/team_a_sets_won/team_b_sets_won/winner/
set_scores/status/date/id)，這樣前端完全不用改，直接把這個event_id(1981)
的資料也塞進同一套 LIVE_DATA_BY_EVENT 機制裡就能用。

這份試算表沒有明確的「獲勝方」標記(不像vbg網站用css class clred標記贏家)，
贏家/狀態是用「雙方獲勝局數」自己比較推算出來的(兩邊都有局數、且局數不同
就代表打完了、局數較多的那邊贏；只有部分比分、局數還沒填齊就當作「進行中」；
完全沒有任何比分資料就是「尚未開始」)，是「盡力猜」的版本，比對不到/猜不準
的場次前端就不會顯示比分，不影響主要賽程查詢功能。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TZ_TW = timezone(timedelta(hours=8))

DEFAULT_SHEET_ID = "1YFRGH2AnnrqzQkSpig5y9N-LW2Ng6jslBUSMb5Xm2MQ"
DEFAULT_GID = "0"
VENUE_STRIDE = 9  # 每個場地欄位佔9欄

# 這份試算表只服務華宗盃這一個賽會，年度直接寫死(比照scrape_ctvba.py裡
# ACADEMIC_YEAR_START的做法，不試著動態猜年度)。
TOURNAMENT_YEAR = 2026


def normalize_name(s: str) -> str:
    """NFKC正規化，把康熙部首相容字元還原成一般中文字，見上方模組docstring。"""
    return unicodedata.normalize("NFKC", (s or "").strip())


def fetch_csv(sheet_id: str, gid: str) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&gid={gid}"
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text, url


def parse_rows(csv_text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(csv_text))
    return [row for row in reader]


def cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if 0 <= idx < len(row) else ""


def parse_venue_label(raw: str) -> str:
    # 比照 scrape_live_scores.py 的 parse_venue()：只取「場地N」這一段，
    # 後面接著的球館/球場名稱跟場次編號都不要，避免前端 normVenue()
    # (把整段文字裡所有數字都抓出來比對)誤把球館名稱裡的數字也算進去。
    m = re.search(r"場地\s*(\d+)", raw)
    return f"場地{m.group(1)}" if m else raw.strip()


def mmdd_to_iso(mmdd: str) -> str:
    mm, dd = int(mmdd[:2]), int(mmdd[2:])
    return f"{TOURNAMENT_YEAR:04d}-{mm:02d}-{dd:02d}"


def compute_row_dates(rows: list[list[str]], num_venues: int) -> dict[int, str | None]:
    """掃描全表(不分場地欄位)找日期標頭，回傳每一列對應的「目前是第幾天」。"""
    current = None
    if rows:
        m0 = re.search(r"(\d{4})\(", cell(rows[0], 0))
        if m0:
            current = mmdd_to_iso(m0.group(1))
    row_date: dict[int, str | None] = {}
    date_re = re.compile(r"^(\d{4})\(")
    for r in range(1, len(rows)):
        for v in range(num_venues):
            c = v * VENUE_STRIDE
            text = cell(rows[r], c + 1)
            m = date_re.match(text)
            if m:
                current = mmdd_to_iso(m.group(1))
        row_date[r] = current
    return row_date


def determine_status(sets_a: str, sets_b: str, set_scores: list[list[str | None]]):
    has_any = bool(sets_a) or bool(sets_b) or any(s[0] or s[1] for s in set_scores)
    if not has_any:
        return "upcoming", None
    if sets_a and sets_b:
        try:
            ia, ib = int(sets_a), int(sets_b)
            if ia != ib:
                return "finished", ("team_a" if ia > ib else "team_b")
        except ValueError:
            pass
    return "in_progress", None


def parse_venue_matches(rows, row_date, venue_idx: int, venue_label: str) -> list[dict]:
    c = venue_idx * VENUE_STRIDE
    data_rows = [r for r in range(1, len(rows)) if cell(rows[r], c + 2)]

    matches = []
    i = 0
    while i + 1 < len(data_rows):
        rA, rB = data_rows[i], data_rows[i + 1]
        rowA, rowB = rows[rA], rows[rB]

        time_label = cell(rowA, c + 0)
        group = cell(rowA, c + 1)
        team_a = normalize_name(cell(rowA, c + 2))
        sets_a = cell(rowA, c + 3)
        sa = [cell(rowA, c + 4), cell(rowA, c + 5), cell(rowA, c + 6)]

        match_code = cell(rowB, c + 1)
        team_b = normalize_name(cell(rowB, c + 2))
        sets_b = cell(rowB, c + 3)
        sb = [cell(rowB, c + 4), cell(rowB, c + 5), cell(rowB, c + 6)]

        i += 2
        if not team_a or not team_b:
            continue

        set_scores = []
        for a, b in zip(sa, sb):
            if not a and not b:
                continue
            set_scores.append([a or None, b or None])

        status, winner = determine_status(sets_a, sets_b, set_scores)

        matches.append(
            {
                "venue": venue_label,
                "time": time_label,
                "group": group,
                "match_code": match_code,
                "team_a": team_a,
                "team_b": team_b,
                "team_a_sets_won": sets_a or None,
                "team_b_sets_won": sets_b or None,
                "winner": winner,
                "set_scores": set_scores,
                "status": status,
                "date": row_date.get(rA),
            }
        )
    return matches


def scrape(sheet_id: str, gid: str, event_id: str, event_name: str) -> dict:
    csv_text, source_url = fetch_csv(sheet_id, gid)
    rows = parse_rows(csv_text)
    if not rows:
        raise RuntimeError("試算表內容是空的，抓取失敗")

    num_venues = (len(rows[0]) + VENUE_STRIDE - 1) // VENUE_STRIDE
    row_date = compute_row_dates(rows, num_venues)

    all_matches = []
    for v in range(num_venues):
        venue_label = parse_venue_label(cell(rows[0], v * VENUE_STRIDE + 1))
        all_matches.extend(parse_venue_matches(rows, row_date, v, venue_label))

    # 用(date, venue, time, team_a, team_b)去重，理論上不該有重複，保守起見還是做一次。
    seen = set()
    unique = []
    for m in all_matches:
        key = (m["date"], m["venue"], m["time"], m["team_a"], m["team_b"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)

    unique.sort(key=lambda m: (m.get("date") or "", m.get("venue") or "", m.get("time") or ""))
    for i, m in enumerate(unique):
        m["id"] = f"live-{event_id}-{i}"

    now = datetime.now(TZ_TW)
    return {
        "event_id": event_id,
        "event_name": event_name,
        "day": None,  # 這份試算表一次就包含全部5天，不是單一天，見上方模組docstring
        "source_url": source_url,
        "updated_at": now.strftime("%Y/%m/%d %H:%M"),
        "matches": unique,
    }


def main():
    ap = argparse.ArgumentParser(description="抓取華宗盃Google試算表即時比分")
    ap.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--gid", default=DEFAULT_GID)
    ap.add_argument("--event-id", default="1981", help="對應主賽程events.json裡的賽事id")
    ap.add_argument("--event-name", default="115年第47屆華宗盃")
    ap.add_argument("--out", default=None, help="輸出路徑，預設 docs/data/live_scores_{event-id}.json")
    args = ap.parse_args()

    result = scrape(args.sheet_id, args.gid, args.event_id, args.event_name)
    out_path = Path(args.out or f"docs/data/live_scores_{args.event_id}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    finished = sum(1 for m in result["matches"] if m["status"] == "finished")
    in_progress = sum(1 for m in result["matches"] if m["status"] == "in_progress")
    upcoming = sum(1 for m in result["matches"] if m["status"] == "upcoming")
    print(
        f"Wrote {len(result['matches'])} matches to {out_path} "
        f"(finished={finished}, in_progress={in_progress}, upcoming={upcoming})"
    )


if __name__ == "__main__":
    main()
