#!/usr/bin/env python3
"""
永信盃即時比分爬蟲 (vbg.yungshingroup.com)。

跟 scrape_ctvba.py 完全獨立、資料來源不同：這裡抓的是永信盃主辦單位另外架設
的「即時計分網站」(https://vbg.yungshingroup.com/tw/event/time?day=YYYY-MM-DD)，
不是協會官網(ctvba.org.tw)的總賽程表PDF。伺服器端渲染的HTML，沒有JSON API，
用 requests + BeautifulSoup 直接抓即可，不需要瀏覽器渲染。

頁面上有兩個 <table>：第一個是社會組六人排球(場地1-11)，第二個是國小組
(場地12-19)。兩個表格結構相同，都是「場地(列) x 時間(欄)」的網格：
  - 第一列(header row)是時間刻度(欄)
  - 每一列(row)第一格是場地名稱(可能混雜好幾個組別名稱擠在一起，只取開頭
    「場地N」這段用正則抓出來，不理會後面附加的組別列表文字)
  - 每個儲存格(cell)如果有比賽，會有 .new-team-title(組別+比賽代號，兩個
    <span>) 跟 .game-result(兩個 <ol>，每隊一個，每個 <ol> 5 個 <li>：
    [0]隊名(可能有種子序號前綴如「(11)」)、[1]獲勝局數(獲勝方有 class
    "clred")、[2..4]各局比分(缺賽/未打的局是空字串))

status(finished/in_progress/upcoming)判斷邏輯：
  - 雙方完全沒有任何局數/比分資料 -> upcoming(還沒開打)
  - 任一方的獲勝局數有 clred(標記獲勝) -> finished(已經打完、有明確贏家)
  - 有部分比分資料、但還沒有明確贏家 -> in_progress(比賽正在進行中)
這個網站截至2026-09-19的實測資料裡沒出現過 in_progress 的例子(要嘛還沒開打、
要嘛已經打完有明確贏家)，但邏輯上保留這個分支，供之後比賽真的打到一半時使用。

輸出的 JSON schema 跟 2026-09-19 手動抓取、已經上線驗證過的
docs/data/live_scores_1950.json 完全一致(欄位名稱、型別)，這樣前端
(docs/index.html 的 findLiveMatch/renderInlineScore)不需要跟著改。
"""
import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

TZ_TW = timezone(timedelta(hours=8))


def fetch(url: str) -> str:
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    # 這個網站的HTTP回應有正確宣告UTF-8，比照 scrape_ctvba.py 的教訓(見專案
    # 文件「重大bug」段落)，固定用 utf-8、不要用 apparent_encoding 猜測。
    resp.encoding = "utf-8"
    return resp.text


def parse_venue(raw: str) -> str:
    # 儲存格第一欄的文字常常是「場地1                        社男組高男組」這種
    # 場地名稱後面直接黏著好幾個組別名稱，只取「場地N」這一段。
    m = re.match(r"場地\s*(\d+)", raw.strip())
    return f"場地{m.group(1)}" if m else raw.strip()


def parse_cell(cell, venue: str, time_label: str):
    title = cell.select_one(".new-team-title")
    result = cell.select_one(".game-result")
    if not title or not result:
        return None

    spans = title.find_all("span")
    group = spans[0].get_text(strip=True) if len(spans) > 0 else ""
    match_code = spans[1].get_text(strip=True) if len(spans) > 1 else ""

    ols = result.find_all("ol")
    if len(ols) != 2:
        return None

    sides = []
    for ol in ols:
        lis = ol.find_all("li")
        name = lis[0].get_text(strip=True) if len(lis) > 0 else ""
        sets_won_text = lis[1].get_text(strip=True) if len(lis) > 1 else ""
        is_winner = "clred" in (lis[1].get("class") or []) if len(lis) > 1 else False
        set_texts = [li.get_text(strip=True) for li in lis[2:]]
        sides.append(
            {
                "name": name,
                "sets_won": sets_won_text or None,
                "is_winner": is_winner,
                "set_texts": set_texts,
            }
        )

    a, b = sides
    both_empty = (
        not a["sets_won"]
        and not any(a["set_texts"])
        and not b["sets_won"]
        and not any(b["set_texts"])
    )
    has_winner = a["is_winner"] or b["is_winner"]
    if both_empty:
        status = "upcoming"
    elif has_winner:
        status = "finished"
    else:
        status = "in_progress"

    max_sets = max(len(a["set_texts"]), len(b["set_texts"]))
    set_scores = []
    for i in range(max_sets):
        sa = a["set_texts"][i] if i < len(a["set_texts"]) else ""
        sb = b["set_texts"][i] if i < len(b["set_texts"]) else ""
        if not sa and not sb:
            continue
        set_scores.append([sa or None, sb or None])

    winner = "team_a" if a["is_winner"] else ("team_b" if b["is_winner"] else None)

    if not a["name"] or not b["name"]:
        return None

    return {
        "venue": venue,
        "time": time_label,
        "group": group,
        "match_code": match_code,
        "team_a": a["name"],
        "team_b": b["name"],
        "team_a_sets_won": a["sets_won"],
        "team_b_sets_won": b["sets_won"],
        "winner": winner,
        "set_scores": set_scores,
        "status": status,
    }


def parse_table(table):
    rows = table.find_all("tr")
    if not rows:
        return []
    header_cells = rows[0].find_all(["th", "td"])
    times = [c.get_text(strip=True) for c in header_cells[1:]]

    matches = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if not cells:
            continue
        venue = parse_venue(cells[0].get_text(" ", strip=True))
        for i, cell in enumerate(cells[1:]):
            if i >= len(times):
                break
            m = parse_cell(cell, venue, times[i])
            if m:
                matches.append(m)
    return matches


def scrape_day(day: str, event_id: str, event_name: str) -> dict:
    url = f"https://vbg.yungshingroup.com/tw/event/time?day={day}"
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    matches = []
    for table in soup.find_all("table"):
        matches.extend(parse_table(table))

    # 用(場地, 時間, 隊伍A, 隊伍B)去重，理論上不該有重複，保守起見還是做一次。
    seen = set()
    unique = []
    for m in matches:
        key = (m["venue"], m["time"], m["team_a"], m["team_b"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)

    for i, m in enumerate(unique):
        m["date"] = day
        m["id"] = f"live-{event_id}-{i}"

    now = datetime.now(TZ_TW)
    return {
        "event_id": event_id,
        "event_name": event_name,
        "day": day,
        "source_url": url,
        "updated_at": now.strftime("%Y/%m/%d %H:%M"),
        "matches": unique,
    }


def merge_matches(existing_matches, new_matches, day, event_id):
    # 2026-09-20 使用者要求：比分要累加保留，不要每天互相覆蓋——之前的做法
    # 是整份檔案直接蓋掉，前一天已經打完、有比分的比賽，過了那一天之後就會
    # 從這個檔案裡消失(前端也就不會再顯示比分了)。這裡只把「今天」這個 day
    # 的舊資料換成新抓到的，其他天的資料照原樣保留、疊加上去。
    # 舊資料裡若有沒有 date 欄位的(這次加 date 欄位之前已經存在的舊資料)，一律當作與今天同一天、一起換掉，避免與新抓到、已經有 date 欄位的今天資料重複。
    kept = [m for m in existing_matches if m.get("date") not in (None, day)]
    merged = kept + new_matches
    merged.sort(key=lambda m: (m.get("date") or "", m.get("venue") or "", m.get("time") or ""))
    for i, m in enumerate(merged):
        m["id"] = f"live-{event_id}-{i}"
    return merged


def main():
    ap = argparse.ArgumentParser(description="抓取vbg.yungshingroup.com指定日期的即時比分")
    ap.add_argument("--day", required=True, help="YYYY-MM-DD，要抓哪一天的比分")
    ap.add_argument("--event-id", default="1950", help="對應主賽程events.json裡的賽事id")
    ap.add_argument("--event-name", default="115年第53屆永信盃")
    ap.add_argument("--out", default=None, help="輸出路徑，預設 docs/data/live_scores_{event-id}.json")
    args = ap.parse_args()

    data = scrape_day(args.day, args.event_id, args.event_name)
    out_path = Path(args.out or f"docs/data/live_scores_{args.event_id}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing_matches = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing_matches = existing.get("matches", [])
        except (json.JSONDecodeError, OSError):
            existing_matches = []

    merged_matches = merge_matches(existing_matches, data["matches"], args.day, args.event_id)

    result = {
        "event_id": args.event_id,
        "event_name": args.event_name,
        "day": args.day,
        "source_url": data["source_url"],
        "updated_at": data["updated_at"],
        "matches": merged_matches,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(merged_matches)} matches total ({len(data['matches'])} for day={args.day}) to {out_path}")


if __name__ == "__main__":
    main()
