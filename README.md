# 排球比賽查詢網頁

每日自動抓取中華民國排球協會（CTVBA）官網公告的國內賽事資訊，做成可查詢的網頁。

目前狀態：MVP 試作，以「永信杯」為測試對象。賽事基本資訊（名稱、日期、地點、報名資訊、附件PDF連結）抓取可靠；「總賽程表」PDF 的自動解析邏輯已經寫好，但**還沒有在有真正網路存取的環境跑過驗證**，第一次執行後請檢查 `docs/data/events.json` 裡每個賽事的 `matches` 和 `parse_warnings` 欄位，確認解析得準不準。

## 目錄結構

```
.
├── scraper/
│   └── scrape_ctvba.py       # 爬蟲主程式
├── docs/                      # GitHub Pages 網站根目錄
│   ├── index.html             # 查詢頁面
│   └── data/events.json       # 爬蟲輸出的資料（種子資料，會被排程覆蓋）
├── .github/workflows/
│   └── daily-scrape.yml       # 每日排程：跑爬蟲 + commit 更新
└── requirements.txt
```

repo 是透過 GitHub 網頁介面直接建立、上傳檔案的（idotla/volleyball-schedule），不是用 `git push` 指令推上去的。之後如果要在本機用 git 操作這個 repo，可以：

```bash
git clone https://github.com/idotla/volleyball-schedule.git
```

## 建置步驟（第一次設定）

1. **建立 GitHub repo**：已完成（idotla/volleyball-schedule，public）。

2. **開啟 GitHub Pages**：repo 頁面 → Settings → Pages → Source 選 `Deploy from a branch` → Branch 選 `main` / `docs`資料夾 → Save。存好之後會給你一個網址（通常是 `https://<帳號>.github.io/<repo名稱>/`），這就是查詢網頁的正式網址。

3. **確認 Actions 權限**：repo 頁面 → Settings → Actions → General → 往下捲到 "Workflow permissions"，選 **Read and write permissions**（排程需要把抓到的資料 commit 回 repo，沒開這個權限會 push 失敗）。

4. **手動觸發一次，確認爬蟲真的能跑**：repo 頁面 → Actions → 選「每日抓取排球協會賽事資料」→ Run workflow，手動跑一次，看執行紀錄：
   - 如果失敗，把錯誤訊息複製給 Claude，一起除錯（多半是 HTML 結構跟預期的不一樣，需要調整 `scrape_ctvba.py` 裡的選擇器）
   - 如果成功，檢查 `docs/data/events.json` 有沒有正確更新，網站重新整理後資料應該會換成最新的

5. 之後就會照 `.github/workflows/daily-scrape.yml` 裡設定的時間（台灣時間每天早上6點）自動跑，不用再手動操作。

## 本機測試（有網路的電腦上）

```bash
pip install -r requirements.txt

# 抓全部賽事列表 + 基本資訊 + 嘗試解析賽程PDF
python scraper/scrape_ctvba.py --out docs/data/events.json

# 只抓單一賽事(用文章ID，可以從網址 /article/{id}/ 看到)
python scraper/scrape_ctvba.py --event-id 1950 --out docs/data/events.json

# 只要基本資訊、跳過PDF解析(比較快，先確認HTML解析邏輯對不對)
python scraper/scrape_ctvba.py --skip-pdf --out docs/data/events.json
```

跑完打開 `docs/index.html`（或用 `python -m http.server` 在 docs 資料夾裡開個本機伺服器）就能預覽。

## 已知限制

- CTVBA 官網沒有公開 API，一切靠爬 HTML + PDF，網站改版就可能讓爬蟲失效，需要不定期維護
- 「總賽程表」PDF 格式因賽事、年度不同而有差異，`parse_schedule_pdf()` 目前用「儲存格裡有 vs / 對 字樣」的通用邏輯抓，不保證每個賽事都能抓到完整賽程；抓不到的會保留 `parse_warnings` 並讓使用者自行點附件PDF查看
- 目前只涵蓋官網「國內賽事」頁面列出的盃賽，不含 TVL 企業排球聯賽、TPVL 職業排球聯盟（如果之後想擴充，這兩個網站的賽程頁是結構化HTML，比PDF好抓很多）
