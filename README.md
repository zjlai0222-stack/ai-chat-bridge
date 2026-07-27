# AI Chat Bridge

用本機橋接器控制 **ChatGPT / Gemini / Qwen** 網頁版:送出 prompt、等待回答、取回文字。
架構參考 Kimi WebBridge(Chrome Extension + chrome.debugger/CDP),但改為**免建置、純標準庫**,方便直接修改。

```
你的程式 / CLI ──HTTP──> bridge_server.py ──WebSocket──> Chrome 擴充套件 ──CDP──> AI 網站分頁
                :8788                      :8765/ws
```

## 檔案結構

```
ai-chat-bridge/
├── extension/            # Chrome 擴充套件(免建置,直接載入)
│   ├── manifest.json
│   ├── background.js     # WebSocket 客戶端 + 各站 DOM 適配器(選擇器在此調整)
│   ├── popup.html / popup.js
│   └── icon/
├── bridge_server.py      # 本機伺服器(Python 標準庫,無需 pip install)
├── bridge_cli.py         # 命令列客戶端
└── bridge_token.txt      # 首次啟動自動產生的 API token
```

## 安裝與啟動(約 2 分鐘)

1. **啟動伺服器**
   ```bash
   python bridge_server.py
   ```
   看到 `WebSocket listening on ws://127.0.0.1:8765/ws` 即就緒。

2. **載入擴充套件**
   - Chrome 打開 `chrome://extensions`,開啟右上角「開發人員模式」
   - 按「載入未封裝項目」,選擇 `ai-chat-bridge/extension` 資料夾
   - 擴充套件會自動連上本機伺服器;點工具列圖示,popup 顯示「已連線」即成功

3. **登入各 AI 網站**
   - 在 popup 點 ChatGPT / Gemini / Qwen 按鈕開啟分頁,**手動登入一次**
   - Bridge 只操作你已登入的分頁,不經手帳密

## 使用方式

### CLI

```bash
python bridge_cli.py health                     # 檢查擴充套件是否連線
python bridge_cli.py status gemini              # 分頁狀態(是否登入、訊息數)
python bridge_cli.py ask gemini "用三句話解釋量子糾纏" --fresh
python bridge_cli.py ask gemini "複雜問題" --fresh --model 延伸思考
python bridge_cli.py read gemini                # 讀取最後一則回答
python bridge_cli.py restart gemini             # 關掉該站所有分頁,開全新分頁
```

**常用參數(ask):**

| 參數 | 說明 |
|---|---|
| `--fresh` | 先關再開全新分頁(乾淨對話,避免拿到舊回覆) |
| `--model 延伸思考` | 送題前切換模型(Gemini 選單:3.5 Flash-Lite / 3.6 Flash / 3.1 Pro / 延伸思考) |
| `--timeout 秒` | 只是安全上限(預設 600),回答一生成就立刻返回;逾時也能用 `read` 拿回 |

**輪詢策略:** 前 90 秒每秒查一次(搶快),之後每 5 秒查一次(長思考不中斷)。

**回應時間實測參考:** Flash 約 10–20 秒;延伸思考通常 30–90 秒,偶爾 2–4 分鐘。

目前 **Gemini 已完整驗證**;ChatGPT / Qwen 選擇器待校準(可用 `eval` 指令 + `--selectors` 覆寫除錯,不用改程式)。

### HTTP API(任何語言都能接)

```bash
curl -X POST http://127.0.0.1:8788/ask \
  -H "Content-Type: application/json" \
  -H "X-Bridge-Token: <bridge_token.txt 內容>" \
  -d '{"site":"chatgpt","prompt":"你好","timeoutSec":240}'
```

回應:`{"ok": true, "result": {"text": "...", "completed": true, ...}}`

### Python 整合

```python
import json, urllib.request
req = urllib.request.Request(
    "http://127.0.0.1:8788/ask",
    data=json.dumps({"site": "qwen", "prompt": "..."}).encode(),
    headers={"Content-Type": "application/json", "X-Bridge-Token": TOKEN},
)
resp = json.loads(urllib.request.urlopen(req, timeout=360).read())
print(resp["result"]["text"])
```

## 給 Kilo Code 使用

### 方式 A(推薦):MCP server

1. 確認 `python bridge_server.py` 已在執行、Chrome 擴充套件已連線。
2. Kilo Code 面板 → **MCP Servers** 圖示 → **Edit MCP Settings**,加入:

```json
{
  "mcpServers": {
    "ai-chat-bridge": {
      "command": "C:\\Users\\laido\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["C:\\Users\\laido\\Documents\\kimi\\workspace\\ai-chat-bridge\\mcp_bridge_server.py"]
    }
  }
}
```

3. 儲存後 Kilo 會出現 4 個工具:`ask_ai`、`read_last_reply`、`ai_tab_status`、`new_ai_chat`。
4. 直接對 Kilo 說:「用 ask_ai 問 chatgpt:……」即可。

注意:`ask_ai` 預設最長等 240 秒;若 Kilo 的 MCP 工具逾時較短,請在 Kilo 設定調大,或把問題拆小。

### 方式 B:自訂指令(不用 MCP)

在專案放 `.kilocode/rules/ai-bridge.md`(或 Kilo 全域 custom instructions)寫:

```
當需要諮詢其他 AI(ChatGPT/Gemini/Qwen)時,用 terminal 執行:
python C:\Users\laido\Documents\kimi\workspace\ai-chat-bridge\bridge_cli.py ask <site> "<prompt>"
站名:chatgpt | gemini | qwen。若回報 server 未啟動,先執行 python bridge_server.py。
```

## 常見問題

- **「extension not connected」** → 確認 Chrome 有開、擴充套件已載入、popup 顯示已連線。
- **「composer not found」** → 該站未登入,或網站改版導致選擇器失效。
- **選擇器失效(網站改版)** → 打開 `extension/background.js` 最上方的 `SITES` 設定,更新該站的
  `composer` / `sendBtn` / `messages` 等 CSS 選擇器即可,改完在 `chrome://extensions` 按重新載入。
- **回答被截斷/逾時** → `ask` 預設等 240 秒;長回答請加大 `--timeout`。逾時時仍會回傳目前已產生的文字(`completed: false`)。
- **分頁上方出現「正在偵錯」橫幅** → 這是 chrome.debugger(CDP)的正常提示,命令執行完會自動消失。

## 支援的命令

| action     | 說明                                   |
|------------|----------------------------------------|
| `ask`      | 送出 prompt 並等待回答完成,回傳文字   |
| `status`   | 分頁狀態:URL、是否找到輸入框、訊息數   |
| `read`     | 讀取最後一則 AI 回答                   |
| `new_chat` | 開新對話(找不到按鈕時回首頁)          |
| `open`     | 開啟/切到該站分頁                      |
| `navigate` | 開新分頁到任意 URL                     |

## 注意事項

- 僅綁定 `127.0.0.1`,不對外開放;HTTP API 需要 `bridge_token.txt` 的 token。
- 透過操作你自己的瀏覽器分頁運作,使用時請遵守各服務的使用條款。
- 三站 UI 改版頻繁,選擇器集中放在 `background.js` 的 `SITES`,方便維護。
