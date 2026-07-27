#!/usr/bin/env python3
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request


def get_token() -> str:
    script_dir = Path(__file__).resolve().parent
    token_file = script_dir / "bridge_token.txt"
    if not token_file.is_file():
        print(f"錯誤: 找不到 token 檔案 {token_file}", file=sys.stderr)
        sys.exit(1)
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        print(f"錯誤: {token_file} 內容為空", file=sys.stderr)
        sys.exit(1)
    return token


def query_site(site: str, prompt: str, token: str, timeout_sec: int) -> tuple[bool, str]:
    url = "http://127.0.0.1:8788/ask"
    headers = {
        "Content-Type": "application/json",
        "X-Bridge-Token": token,
    }
    payload = {
        "site": site,
        "prompt": prompt,
        "timeoutSec": timeout_sec,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec + 15) as resp:
            resp_data = resp.read().decode("utf-8")
            res_json = json.loads(resp_data)
            if res_json.get("ok"):
                text = res_json.get("result", {}).get("text", "")
                return True, text
            else:
                err_msg = res_json.get("error", "未知錯誤")
                return False, f"API 傳回錯誤: {err_msg}"
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
            res_json = json.loads(err_body)
            err_msg = res_json.get("error", str(e))
        except Exception:
            err_msg = str(e)
        return False, f"HTTP 錯誤 ({e.code}): {err_msg}"
    except urllib.error.URLError as e:
        return False, f"網路連線失敗: {e.reason}"
    except Exception as e:
        return False, f"請求異常: {str(e)}"


def format_table_cell(text: str) -> str:
    escaped = text.replace("|", "\\|")
    return escaped.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def main():
    parser = argparse.ArgumentParser(description="多 AI 平台問答對照工具")
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="問題內容 (若指定 --file 則可省略)",
    )
    parser.add_argument(
        "--file", "-f", type=str, default=None, help="包含問題內容的文字檔案"
    )
    parser.add_argument(
        "--out",
        "-o",
        type=str,
        default="compare_result.md",
        help="輸出 Markdown 檔名 (預設: compare_result.md)",
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=600,
        help="每家等待上限秒數，回答完成會提早返回 (預設: 600)",
    )
    parser.add_argument(
        "--sites",
        type=str,
        default="",
        help="要查詢的 AI 站台，逗號分隔 (預設: chatgpt,gemini,qwen)",
    )

    args = parser.parse_args()

    prompt_text = ""
    if args.file:
        file_path = Path(args.file)
        if not file_path.is_file():
            print(f"錯誤: 找不到提示詞檔案 {args.file}", file=sys.stderr)
            sys.exit(1)
        prompt_text = file_path.read_text(encoding="utf-8").strip()
    elif args.prompt:
        prompt_text = args.prompt.strip()

    if not prompt_text:
        parser.error("請提供問題內容 (可直接傳入字串或透過 --file 指定檔案)")

    token = get_token()
    all_sites = [
        ("chatgpt", "ChatGPT"),
        ("gemini", "Gemini"),
        ("qwen", "Qwen"),
    ]
    if args.sites:
        site_ids = [s.strip().lower() for s in args.sites.split(",") if s.strip()]
        all_site_ids = {sid for sid, _ in all_sites}
        for sid in site_ids:
            if sid not in all_site_ids:
                print(f"錯誤: 不認識的站台 '{sid}'，可用: {', '.join(sorted(all_site_ids))}", file=sys.stderr)
                sys.exit(1)
        sites = [(sid, name) for sid, name in all_sites if sid in site_ids]
    else:
        sites = list(all_sites)

    results = []
    for site_id, site_name in sites:
        print(f"正在發送請求至 {site_name}...")
        ok, content = query_site(site_id, prompt_text, token, args.timeout)
        status_str = "成功" if ok else "失敗"
        results.append({
            "name": site_name,
            "status": status_str,
            "content": content,
        })

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md_lines = []
    md_lines.append("# AI 回答對照表\n")
    md_lines.append(f"- **產生時間**: {now_str}")
    md_lines.append(f"- **問題內容**:\n\n```\n{prompt_text}\n```\n")
    md_lines.append("--- \n")
    md_lines.append("| AI 站台 | 狀態 | 回答內容 |")
    md_lines.append("| --- | --- | --- |")

    for res in results:
        formatted_content = format_table_cell(res["content"])
        md_lines.append(f"| {res['name']} | {res['status']} | {formatted_content} |")

    output_path = Path(args.out)
    output_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"\n對照表已成功輸出至: {output_path.resolve()}")


if __name__ == "__main__":
    main()
