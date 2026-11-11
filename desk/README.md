# 客服工作台

## FB／IG 私訊與留言：瀏覽器收發＋逐則審稿（2026-11-11）

工作台本身**不連 Meta、不呼叫 Graph API、不送任何 FB／IG 訊息**。收訊與送出都由排程在已登入的 Meta 收件匣（瀏覽器）操作，工作台只負責擬稿、讓 Wayne 逐則決定、記錄結果。

流程：

1. 排程從 Meta 收件匣讀到需要回覆的對話，用本機工具寫入：`python -m desk.tools.fb_browser push <json 檔>`。
2. 背景擬稿（與 LINE 同一個 `Engine`、口吻規則、知識庫、月預算、個資遮蔽）。
3. Wayne 在 `/desk/fb` 逐則選：
   - **發送**：確認框顯示對象與最終文字（留言需另勾「公開」確認），建立一筆核准快照。
   - **修改**：寫下要改哪裡，背景依指示重擬；勾選「以後都照這樣寫」時存進 `desk_edit_rules`，之後 LINE／FB／IG 每次擬稿都附上（最新 40 條、可停用）。
   - **不發送**：標成不回覆；客人再傳新訊息會重新進來。
4. 排程下一輪用 `pending` 取得已核准清單，在瀏覽器照快照文字送出，再用 `result <attempt_id> sent|failed [原因]` 回報。

防呆：

- 核准綁定對話版本。核准後客人又傳新訊息，`pending` 不會列出，核准自動失效並重新擬稿。
- 同事已在 Meta 後台回覆（push 進來的最後一則是粉專訊息）→ 自動移出待審，未送的核准一併取消。
- 同一則對話同時只有一筆有效核准；`result` 可重複回報，不會產生第二筆送出紀錄。
- 只有可審核帳號（目前為 Wayne）能核准、取消、標不回覆、寫入修改指示；其他帳號可看草稿、請 AI 重寫。

工具子命令（輸入輸出皆 UTF-8 JSON，不輸出連線資訊）：

| 子命令 | 作用 |
| --- | --- |
| `push <檔>` | 寫入對話。每筆含 `channel`（`fb_message`／`ig_message`／`fb_comment`／`ig_comment`）、`key`、`name`、`url`、`context`、`messages[{role:user|page,text,at,note}]`，同內容重複寫入不會重複新增。 |
| `status` | `waiting_draft`、`awaiting_review`、`approved_unsent`、`send_failed` 四個數字。 |
| `pending` | 已核准、等送出的清單（含 `attempt_id`、`content`、`last_customer_text`、`is_public`）。 |
| `result <attempt_id> sent|failed [原因]` | 回報送出結果。 |

資料表：`desk_fb_threads`、`desk_fb_events`、`desk_fb_attempts`、`desk_edit_rules`（`desk/fb_schema.py`，由 `Store.initialize()` 建立）。測試全部使用合成資料與交易回滾。

`desk` 是選擇性 Flask blueprint，掛在 `/desk`。只有設定 `DESK_SECRET_KEY` 才載入。載入失敗由主程式隔離，既有 webhook 與查詢路由保持原行為。此 repo 公開，請勿加入知識庫、歷史問答、客人資料、實際帳密、截圖或執行紀錄。

客服後台雲端擬稿（line-webhook /desk）：Wayne 2026-11-09 核准使用 OpenAI API，月上限 US$30。本機程式已加入 Responses API adapter，仍須 Claude 審核後才 push／部署。失敗、金鑰未設定或額度不足時可人工填寫與確認送出。

## 本機資料準備

在 repo 根目錄以 Python 執行：

```powershell
.venv/Scripts/python.exe -m desk.tools.upload_kb
.venv/Scripts/python.exe -m desk.tools.upload_qa
.venv/Scripts/python.exe -m desk.tools.create_users
```

三支腳本讀取 `~/Desktop/Claude工作區/secrets/line_webhook_db.env`；只新增／更新 `desk_` 表。歷史問答 SQLite 以唯讀模式讀取，限定 2026-01-01 起，遮蔽已知客人姓名、電話、Email、車牌、LINE ID，沒有上傳姓名欄位。此為規則遮蔽，非保證自由文字去識別零遺漏；啟用任何外部 AI 前需另作資料範圍審核。

既有資料已匯入時，只補資料表可執行 `.venv/Scripts/python.exe -c "from desk.tools.common import store; store()"`，不會重匯知識庫或重設密碼。

建立 `Wayne`、`馨若`，使用 werkzeug 密碼 hash。隨機密碼只寫入 `~/Desktop/Claude工作區/secrets/cs_desk_users.env`。重跑只驗證既有帳號，不重設密碼；帳號檔缺失或不一致會停下。

## 審核後才設定 Render

| 變數 | 用途 |
| --- | --- |
| `DATABASE_URL` | 現有 PostgreSQL；沿用 |
| `LINE_CHANNEL_ACCESS_TOKEN` | 公司客服 OA 的既有 LINE token；沿用，勿改用徐助理 token |
| `DESK_SECRET_KEY` | 新增高熵隨機 session 簽章密鑰；至少 32 隨機位元組 |
| `DESK_WORKER_ENABLED` | 設成 `1` 才啟動背景收訊及價格更新；預設不啟動 |
| `DESK_SEND_ENABLED` | 設成 `1` 才允許登入員工確認後真正發送 LINE；預設不送 |
| `DESK_MEDIA_BASE_URL` | 附件公開 HTTPS 站台根網址，例如 `https://你的服務網域`，不可含路徑；未設定時拒絕附檔送出，文字送出不受影響 |
| `OPENAI_API_KEY` | 經核准的客服擬稿 API 金鑰；只從環境讀取，不寫 log |
| `DESK_MODEL` | 預設 `gpt-6.1-sol`；變更其他模型前須在 provider 設定其官方計價，未知價格會停擬稿 |
| `DESK_MONTHLY_USD` | 客服擬稿每月美元額度，預設 `30`；`0` 停擬稿，格式錯誤也停擬稿 |

保留 `gunicorn app:app`；模組安裝沒有網路與 DB 初始化動作。資料表由本機工具初始化；續作只新增 `desk_ai_usage` 表與索引，不重跑資料匯入或帳號建立。Wayne 已自行在 Render Save only 設定 `OPENAI_API_KEY` 與 `DESK_SECRET_KEY`，本次沒有查看或改動 Render，也未驗證雲端設定。禁止在審核前 push／部署／啟用上述開關。

## 擬稿資料範圍與費用

- 對話頁仍保留完整紀錄。背景擬稿只組裝該客人最近 24 小時（含邊界）的文字與我方成功送出的回覆，排除未來時間；不傳 userId、display_name、員工姓名或附件 metadata。圖片／影片改成 `[image]`／`[video]`，已送圖片只留標記。
- 僅傳系統口吻規則、車型清單及 DB 知識檢索結果。所有請求內容（包含知識庫與問答）在 HTTP 邊界再次遮蔽電話、車牌、Email、身分證、居留證、LINE 識別碼與已知格式的姓名／地址。規則遮蔽無法保證非標準自由文字個資零遺漏；不會上傳媒體檔案。
- 固定官方 `https://api.openai.com/v1/responses`，禁止 redirect，不帶 user metadata、不使用外部工具、不沿用 previous_response_id；`store=false`，輸出上限 3,000 tokens，單次輸入最多 80,000 UTF-8 bytes，超過就轉人工。此設定不等同供應商零資料保留政策。
- 模型使用標準計價。2026-11-09 官方每百萬 tokens 為輸入 US$2、快取輸入 US$0.10、輸出 US$10，參見 [官方模型計價](https://developers.openai.com/api/docs/models/gpt-6.1-sol)。含推理的 output_tokens 只算一次。抽取與回覆兩次請求分別記帳，格式不符但有 usage 仍記實際費用。
- `desk_ai_usage` 只存隨機請求 ID、台灣月份、模型、tokens、USD 與 settled／reserved 狀態，不存對話或金鑰。台灣時間每月 1 日自動換月，僅因上月額度而 blocked 的對話會自動重回等待；其他擬稿錯誤由人工重新擬稿。
- 呼叫前在 DB advisory lock 內確認月額度並持久保存保守預留（UTF-8 bytes 加 framing 緩衝、輸入按較高 cache-write 價 US$2.50、輸出取最大量）；成功後改記 usage 實際估算。所有 worker 共用鎖與帳本。無足夠預留空間便提早停止，不等最後一筆超支。預留會計入門檻，80% 頂部標黃，100% 顯示「本月 AI 額度已用完，請手動回覆」。
- 逾時、非 200、usage 不明、程序中斷均保留預留，避免重啟後遺失費用；無自動 HTTP 重試。核對供應商帳單後才能人工處理未結預留，程式不擅自歸零。僅計本客服後台的請求；同一金鑰供其他系統使用的費用不在本帳本內。金額是標準 token 估算，不是供應商整個帳戶的硬性花費上限。

## 行為與維運

### 對話知識送審（本機待審核）

- 每個對話（包含已處理）都有選用的「存成知識」。預填尚未處理的客人文字與對應的最近成功回覆；沒有對應回覆時保留草稿框內容。送出後重開已處理對話，也能取回這次回答的問題。表單可修改，含必填的一句話重點及選填車款／產品；不會傳 LINE、不改對話狀態、不阻擋原回覆流程。
- 知識存在新表 `desk_knowledge`，初始 `pending`。只有既有且啟用的精確帳號 `Wayne` 看得到 `/desk/knowledge`，後端也檢查相同權限；帳號、密碼與 session 行為不變。Wayne 可改文字後核准、退回待審項目、撤回已核准項目。版本檢查與行鎖防止舊畫面覆蓋新審核；提交／核准／退回／撤回的文字快照、操作者與時間另存 `desk_knowledge_events`。
- 每次新擬稿即時讀取 `approved` 項目。重點置於參考資料最前面，含適用範圍合計最多 3,000 字；超過依新問題的雙字詞重疊排序。問答取有重疊的前 3 筆，標示 Wayne 核准且優先於歷史問答。金額仍以原資料做程式檢查，知識金額不擴充可接受金額集合。AI HTTP 邊界沿用原有個資遮蔽。
- 退回／撤回後的新擬稿不再包含該筆；已產生的草稿不自動覆寫，需人工按重新擬稿。送審前請移除無關個資，知識只做內部參考。
- 部署前由 Claude 審查程式及測試證據，並透過既有資料表初始化程序新增這兩張表與索引（`Store.initialize()`／上述只補表命令）；沒有初始化時會顯示原有的暫時無法處理訊息。本次未操作正式 DB schema、push 或 Render。套用新版程式前須先補表，避免擬稿查詢缺表。
- 還原：先還原程式版本，保留新表及稽核紀錄即可；不要刪除正式知識。舊版本不會讀取這兩張表。

- 首次收訊只接手最近 3 小時，之後用來源 ID 分頁追蹤，`messages` 僅 SELECT。每 30 秒收訊，靜默 60 秒才擬稿；來源新進線會使舊草稿失效。不同執行緒處理擬稿及收訊，DB advisory lock 協調多 worker。
- 重啟時在擬稿鎖內回復中斷的擬稿狀態；背景失敗不影響 webhook，頁面顯示背景收訊逾時。
- `time_str` 按 UTC 解讀，介面呈現台灣時間。完整對話串接客人原訊息與 `desk_outgoing` 成功紀錄；LINE 後台人工回覆只能標記，無法還原該後台未提供的文字。
- 每日從官網 `branding` 的 `script#wlf-data` 更新價格，檢查 schema 後才覆寫 DB；失敗保留原資料並標黃。所有私有知識存 DB。
- Session 為簽章 cookie＋DB session，有效 12 小時（登入時勾「這台裝置 30 天內不用再登入」則為 30 天）、Secure／HttpOnly／SameSite=Lax、路徑 `/desk`。5 次失敗鎖 15 分鐘；登入表單和登入後 API 都檢查 CSRF。桌機／手機採同一介面。
- 只有人工 POST 確認才傳送。先保存 intent，再由 `desk.notifier.LineNotifier` 集中呼叫 LINE push；成功才寫 `desk_outgoing`。所有嘗試包含員工、時間、內容與 LINE HTTP 狀態；失敗保留草稿。
- 每位客人用 DB 鎖串行傳送，10 秒限制；每個確認使用 UUID retry key。逾時／5xx 保留原 key，禁止換 key 另送。23 小時後拒絕自動重試，需人工到 LINE 後台查核。409 僅在含 `x-line-accepted-request-id` 時視為已接受。
- 「已在 LINE 後台回覆（不送）」會留下人工處理紀錄，不捏造已送訊息。這也可結束經人工查核的未知傳送狀態。
- 附件由員工在電腦／手機按「加入圖片／影片」自行選取，可混用 JPEG、PNG、MP4，最多 4 個檔案，順序為一則文字再依選取順序附檔。手機使用原生檔案選取器，可依裝置提供的相簿／相機選項選檔；不強制開啟相機。AI 只產生文字，不再回傳商品圖建議；舊 catalog 程式保留供回復，背景不再更新商品圖片索引，也不再保存 branding 圖片索引。每日價格更新維持原行為。制式文字中的影片連結維持文字。

### 員工上傳媒體（本機待 Claude 審核）

- 新增 `desk_media` 一張表及兩個索引，DDL 在 `desk/media.py:SCHEMA` 並納入 `desk/db.py:SCHEMA`。部署前由 Claude 以既有補表流程套用；本次未建正式表、未操作 Render、未 push。新依賴 `Pillow==13.0.0` 用於驗證圖片及預覽圖，須隨 requirements 安裝。
- 每個檔案與其 JPEG 預覽各有獨立的 32 位十六進位隨機 token（128 bits）。表內保存原檔 BYTEA、預覽 BYTEA、MIME、兩者大小、檔名、上傳員工、對應客人、上傳／送出／移除／清空時間、最後一次 retry key。原檔／預覽皆經 `GET`／`HEAD /desk/media/<token>` 公開讀取，免登入；無目錄、無公開寫入，正確回傳 Content-Type、Content-Length，支援 Range。失效檔案回 410、未知 token 回 404，均 no-store、nosniff；只有這個確切讀取端點放行，其餘後台仍需原本登入與 CSRF。
- 上傳採登入員工的 header CSRF，每次一個原檔加一個 JPEG 預覽；後端驗證真實圖片內容／尺寸、MIME、MP4 容器基本結構，不接受 SVG、GIF、WebP、HEIC 或偽造 MIME。圖片上限 10 MB；超過時瀏覽器縮至長邊 2048 px 內，以 JPEG 多階品質壓縮至上限，壓不下就拒收。預覽圖由瀏覽器產生，長邊最多 240 px；後端驗證後重編碼移除 metadata。伺服器拒收超過 4,000 萬像素及多影格圖片，以限制記憶體使用。
- MP4 上限 50 MB，超過顯示「影片太大，請改傳 YouTube 連結」。預覽由瀏覽器讀取第一格畫面產生 JPEG，不上傳到任何 AI。影片不做伺服器轉檔；裝置無法解碼／擷取第一格時拒收並建議相容 MP4 或 YouTube 連結。MP4 容器有效不代表所有 LINE 用戶端都支援其編碼；建議 H.264／AAC，真機與實際 LINE 播放仍待 Claude 部署審核後驗收。
- 上傳後立即顯示縮圖與檔名，可逐一移除；重新進入同一對話會載入自己尚未使用的上傳。附件與客人、員工綁定，送出端只接受 token，拒絕外部 URL、其他員工／客人的附件、重複 token、過期／已移除／已送出附件。確認框保存文字、收件人、順序、token 及 retry key；後端將可信的公開 URL 與預覽 URL 固定於原 `desk_send_attempts.images` JSONB，成功後同樣寫入 `desk_outgoing.images`，時間軸顯示圖片／影片縮圖。舊 URL 字串紀錄仍可顯示，舊待確認傳送只准依原快照重試，不能建立新的商品圖傳送。
- 傳送中／結果不明的附件不可移除；沿用原 10 秒間隔、23 小時安全重試、同一 UUID 與相同媒體 URL。改網域設定也不會改掉既有 retry 的快照。LINE image／video 均使用各自原檔與 JPEG 預覽網址；依[官方媒體訊息規格](https://developers.line.biz/en/reference/messaging-api/nojs/#image-message)保留圖片 10MB、預覽 1MB 限制，影片採交辦要求較低的 50MB 上限。
- 附件內容上傳滿 30 天失效並自動清空原檔與預覽，保留紀錄。沿用既有 reader 每 30 秒執行清理，另在上傳時清理，未新增或修改任何排程；worker 停用時仍會拒絕讀取過期檔案，實體內容待下一次上傳／worker 恢復時清空。
- 原檔加預覽的邏輯容量合計上限 500 MB。上傳、送出宣告與清理共用 Postgres transaction advisory lock，避免多 worker 同時超額；不足時依送出時間先清空最舊已送出檔案，未送出或待查核附件受保護。無法騰出足夠空間回 507，請移除自己的未使用附件或改傳連結。自動清理只 UPDATE `desk_media` 內容欄位，保留紀錄，不刪客人、來源訊息或舊送出資料。此為活躍內容容量限制，Postgres 實體磁碟回收仍由 VACUUM 管理。
- 測試僅使用交易內 TEMP TABLE、最後 rollback，既有來源 messages 僅唯讀；LINE 使用 FakeNotifier，全域拒絕外部 HTTP。瀏覽器測試只連本機 Flask 與測試圖片。測試用 ffmpeg 僅產生合成影片，正式服務不需要 ffmpeg。
- 還原時用交辦證據中的 `source_before.zip` 還原對應程式，保留 `desk_media` 及其稽核紀錄即可，不需刪表。不要讓舊版程式重試新版的媒體快照；先查核所有 sending／unknown 傳送，或保持新版 service/notifier/routes/worker 讀取相容層，直到待確認項目結清。

## 驗證

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest tests -q
```

完整 DB 測試需以私密載入方式設定 `DESK_TEST_DATABASE_URL`，與部署 DB 相同即可。測試的 `desk_` 樣本只存在交易內，結束全部 rollback；來源表唯讀，LINE／AI 全部 mock。未提供測試 DB 時資料庫測試會 skip，不代表完整驗證通過。

實際執行結果與六張以上截圖保存在本次交辦的 `outputs/ca_cs_desk_cloud/` 時戳目錄，請見交辦 `.result.md`。沒有任何正式客人發送測試，亦未驗證 Render 上線行為。

## 回復

未 push、未部署時，現有 Render 服務沒有變動。停止載入客服後台可移除 `DESK_SECRET_KEY` 並維持兩個開關不啟用；程式回復可還原本次備份的 `app.py`、`.gitignore`。保留 `desk_` 表與帳號檔，無須刪除資料。若要刪除任何多筆資料／檔案，先列出清單取得 Wayne 確認。

參考： [Flask 安全說明](https://flask.palletsprojects.com/en/stable/web-security/)、[LINE 重試規則](https://developers.line.biz/en/docs/messaging-api/retrying-api-request/)、[LINE Messaging API](https://developers.line.biz/en/reference/messaging-api/)。

大小單位採保守的十進位 MB（1 MB＝1,000,000 bytes）；前後端一致，避免邊界超過上限。
