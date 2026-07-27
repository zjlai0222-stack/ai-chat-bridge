---
name: ai-chat-bridge
description: Ask other AI chat websites (Gemini verified; ChatGPT/Qwen pending) from the user's logged-in browser via the local AI Chat Bridge. Use when the user wants a second opinion from another AI, cross-model verification, or says 問 Gemini / 問 ChatGPT / 問其他 AI.
---

# AI Chat Bridge

本機橋接器,透過使用者已登入的 Chrome 分頁操作 AI 聊天網站(送 prompt、等回答、取回文字)。
**目前只有 Gemini 完整驗證可用;chatgpt / qwen 的站台選擇器尚未校準,預設只用 `gemini`。**

## 前提(缺一不可)

1. Bridge server 在跑:
   ```bash
   python C:\Users\laido\Documents\kimi\workspace\ai-chat-bridge\bridge_server.py
   ```
   檢查:`python C:\Users\laido\Documents\kimi\workspace\ai-chat-bridge\bridge_cli.py health`
   需看到 `"extension_connected": true`。
2. Chrome 已開、AI Chat Bridge 擴充套件已載入並連線、Gemini 分頁已登入。
   若 health 顯示未連線,提醒使用者處理,不要自己猜測重試。

## 用法

```bash
# 簡單問題(快,約 10–20 秒)
python C:\Users\laido\Documents\kimi\workspace\ai-chat-bridge\bridge_cli.py ask gemini "<prompt>" --fresh --model "3.6 Flash"

# 複雜問題(慢但強,約 1–4 分鐘)
python C:\Users\laido\Documents\kimi\workspace\ai-chat-bridge\bridge_cli.py ask gemini "<prompt>" --fresh --model 延伸思考

# 讀取最後一則回答(逾時補救;逾時 ≠ 失敗,回答會在分頁繼續生成)
python C:\Users\laido\Documents\kimi\workspace\ai-chat-bridge\bridge_cli.py read gemini

# 狀態檢查 / 關掉重開分頁
python C:\Users\laido\Documents\kimi\workspace\ai-chat-bridge\bridge_cli.py status gemini
python C:\Users\laido\Documents\kimi\workspace\ai-chat-bridge\bridge_cli.py restart gemini
```

## 規則

- `ask` 一律帶 `--fresh`(先關再開,避免拿到舊對話的回覆)。
- `--timeout` 只是安全上限(預設 600 秒),回答一生成就立刻返回;送出失敗會在 20 秒內報錯。
- 等待期間不要操作使用者的 Gemini 分頁。
- prompt 含大量文字時用 `--file <路徑>` 傳入,避免命令列跳脫問題。
- 失敗時先看錯誤訊息:`extension not connected` → 請使用者開 Chrome/確認擴充套件;`composer not found` → Gemini 未登入,請使用者登入。
