"""
LINEにレシートの写真を送ると、Claude API(Vision機能)で内容を読み取り、
Googleスプレッドシートに家計簿として自動記録するBot

【仕組み】
1. LINEプラットフォームからのメッセージをWebhook(POST /callback)で受信するFlaskサーバー
2. 画像メッセージを受信したら、LINEのContent APIで画像データを取得
3. 取得した画像をClaude API(Vision機能)に渡し、日付・店名・品目ごとの内訳(品目名・金額・
   カテゴリ)・合計金額をJSON形式で抽出
4. 抽出した内容を、品目ごとに1行ずつGoogleスプレッドシートに追記
   (列: 日付, 店名, 品目, 金額, カテゴリ)
5. 記録が完了したら、LINEに記録内容の要約を返信する
6. 画像以外のテキストメッセージが届いた場合は、レシート送付を案内する

【事前準備】
1. ライブラリをインストール:
     pip install flask requests anthropic google-auth-oauthlib google-api-python-client google-auth

2. credentials.json（OAuthクライアントのデスクトップアプリ用JSON、rakuten_to_sheets.py などと
   同じもの）を、このスクリプトと同じフォルダに置いておくこと
   （初回実行時はブラウザが開き、Googleアカウントでのログイン・許可が必要です。
     token_sheets.json に認証情報が保存され、次回以降は自動で使われます）

3. 環境変数を設定（すべて必須。コードには直接書き込まない）:
   ・LINE_CHANNEL_ACCESS_TOKEN … LINE Developersコンソールで発行したChannel access token
   ・LINE_CHANNEL_SECRET       … LINE Developersコンソールで発行したChannel secret
                                  （Webhookの署名検証に使用。なりすましリクエストを防ぐため必須）
   ・ANTHROPIC_API_KEY         … Claude APIキー
   ・SPREADSHEET_ID            … 記録先のGoogleスプレッドシートID

   Windows(PowerShell)の例:
       $env:LINE_CHANNEL_ACCESS_TOKEN="xxxx"
       $env:LINE_CHANNEL_SECRET="xxxx"
       $env:ANTHROPIC_API_KEY="xxxx"
       $env:SPREADSHEET_ID="xxxx"

4. LINE Developersコンソールで、Webhook URLに
   「https://（公開URL）/callback」を設定し、Webhookの利用をONにする
   （ローカルで試す場合は ngrok 等でこのサーバーを外部公開する）

【使い方】
   python line_receipt_to_sheets.py

   起動するとWebhookサーバーがバックグラウンドで待ち受けを開始します。
   終了するには、ターミナルで 'exit' または 'quit' と入力してください。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
from datetime import datetime

import anthropic
import requests
from flask import Flask, abort, request
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

try:
    from zoneinfo import ZoneInfo

    JST = ZoneInfo("Asia/Tokyo")
except Exception:
    JST = None

# 実行環境によっては標準出力・入力が自動でUTF-8にならず、
# 日本語が文字化けすることがあるため明示的に指定する
sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("line_receipt_to_sheets")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_CONTENT_URL = "https://api-data.line.me/v2/bot/message/{message_id}/content"
CLAUDE_MODEL = "claude-sonnet-5"

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token_sheets.json"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER_ROW = ["日付", "店名", "品目", "金額", "カテゴリ"]
PREFERRED_SHEET_NAME = "シート1"

CATEGORIES = ("食費", "日用品", "外食", "その他")

GUIDE_MESSAGE = "レシートの写真を送ってください。"
READ_FAILURE_MESSAGE = "レシートの内容をうまく読み取れませんでした。もう一度送ってください。"
IMAGE_FETCH_FAILURE_MESSAGE = "画像の取得に失敗しました。もう一度送ってください。"
SHEET_WRITE_FAILURE_MESSAGE = "家計簿への記録に失敗しました。時間をおいてもう一度お試しください。"

# Googleスプレッドシートへの書き込みは、main()で初期化してから使う
sheets_service = None
sheet_name = None
sheets_lock = threading.Lock()


def check_required_env_vars():
    required = {
        "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
        "LINE_CHANNEL_SECRET": LINE_CHANNEL_SECRET,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "SPREADSHEET_ID": SPREADSHEET_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        logger.error(
            "以下の環境変数が設定されていません: %s。"
            "スクリプト冒頭のコメントを参照し、環境変数を設定してから再実行してください。",
            ", ".join(missing),
        )
        sys.exit(1)

    if not os.path.exists(CREDENTIALS_FILE):
        logger.error(
            "%s が見つかりません。GoogleのOAuthクライアント用JSON（デスクトップアプリ）を、"
            "このスクリプトと同じフォルダに配置してください。",
            CREDENTIALS_FILE,
        )
        sys.exit(1)


check_required_env_vars()

app = Flask(__name__)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def verify_line_signature(body: bytes, signature: str) -> bool:
    """LINEプラットフォームから送られたリクエストであることを署名で検証する"""
    if not signature:
        return False
    digest = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


def get_sheets_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SHEETS_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SHEETS_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("sheets", "v4", credentials=creds)


def resolve_sheet_name(service, spreadsheet_id):
    spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = spreadsheet.get("sheets", [])
    titles = [s["properties"]["title"] for s in sheets]

    if PREFERRED_SHEET_NAME in titles:
        return PREFERRED_SHEET_NAME
    return titles[0]


def ensure_header_row(service, spreadsheet_id, name):
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{name}!A1:E1")
        .execute()
    )
    values = result.get("values", [])

    if not values or not values[0]:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{name}!A1:E1",
            valueInputOption="USER_ENTERED",
            body={"values": [HEADER_ROW]},
        ).execute()


def append_rows(rows):
    """品目ごとの行をスプレッドシートに追記する。複数スレッドからの同時書き込みを避けるためlockする"""
    with sheets_lock:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{sheet_name}!A:E",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()


def fetch_line_image_content(message_id: str):
    """LINEのContent APIで、受信した画像メッセージの画像データを取得する"""
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    response = requests.get(
        LINE_CONTENT_URL.format(message_id=message_id), headers=headers, timeout=15
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "image/jpeg")
    return response.content, content_type


def build_receipt_prompt(received_date_str: str) -> str:
    category_list = "」「".join(CATEGORIES)
    return f"""この画像はレシートです。内容を解析し、次のJSON形式のみを出力してください。
説明文やマークダウンのコードブロックなど、JSON以外の文字列は一切出力しないでください。

出力形式:
{{
  "date": "YYYY-MM-DD形式の日付文字列。レシートに日付の記載が無ければ \\"{received_date_str}\\"",
  "store": "店名の文字列",
  "items": [
    {{"name": "品目名", "price": 品目の金額（円単位の整数）, "category": "「{category_list}」のいずれか1つ"}}
  ],
  "total": 合計金額（円単位の整数）
}}

注意事項:
- カテゴリは、それぞれの品目の内容から「{category_list}」のいずれか1つに判断して分類してください。
- 金額の数値には、カンマや円記号を含めないでください。
- 画像がレシートでない場合や、金額・品目をほとんど読み取れない場合は、
  {{"error": "読み取れませんでした"}} という形式のJSONのみを出力してください。
"""


def parse_receipt_json(raw_text: str):
    """Claudeの応答テキストからJSONを取り出す（マークダウンのコードブロックで囲まれていても対応する）"""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def extract_receipt_with_claude(image_bytes: bytes, media_type: str, received_date_str: str):
    """レシート画像をClaude APIに渡し、日付・店名・品目・合計を抽出する。読み取り失敗時はNoneを返す"""
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = build_receipt_prompt(received_date_str)

    message = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    text_blocks = [block.text for block in message.content if block.type == "text"]
    raw_text = "".join(text_blocks).strip()

    data = parse_receipt_json(raw_text)

    if not isinstance(data, dict) or "error" in data:
        return None

    store = str(data.get("store") or "").strip()
    items = data.get("items")
    total = data.get("total")

    if not store or not isinstance(items, list) or not items or total is None:
        return None

    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or "").strip()
        price = item.get("price")
        category = item.get("category") if item.get("category") in CATEGORIES else "その他"
        if not name or price is None:
            return None
        try:
            price = int(round(float(price)))
        except (TypeError, ValueError):
            return None
        normalized_items.append({"name": name, "price": price, "category": category})

    try:
        total = int(round(float(total)))
    except (TypeError, ValueError):
        return None

    date_str = str(data.get("date") or "").strip() or received_date_str

    return {"date": date_str, "store": store, "items": normalized_items, "total": total}


def build_summary_message(receipt: dict) -> str:
    lines = [
        "家計簿に記録しました!",
        "",
        f"店名: {receipt['store']}",
        f"日付: {receipt['date']}",
        f"合計: {receipt['total']:,}円",
        f"品目数: {len(receipt['items'])}件",
        "",
        "内訳:",
    ]
    for item in receipt["items"]:
        lines.append(f"・{item['name']} - {item['price']:,}円 ({item['category']})")
    return "\n".join(lines)


def reply_to_line(reply_token: str, text: str):
    """LINEのreply APIでユーザーにメッセージを送信する"""
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        }
        payload = {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text[:5000]}],
        }
        response = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error("LINEへの返信に失敗しました: %s", e)


def handle_image_message(event: dict):
    reply_token = event.get("replyToken")
    message_id = event.get("message", {}).get("id")

    try:
        image_bytes, media_type = fetch_line_image_content(message_id)
    except Exception as e:
        logger.error("LINEからの画像取得に失敗しました: %s", e)
        reply_to_line(reply_token, IMAGE_FETCH_FAILURE_MESSAGE)
        return

    now = datetime.now(JST) if JST else datetime.now()
    received_date_str = now.strftime("%Y-%m-%d")

    try:
        receipt = extract_receipt_with_claude(image_bytes, media_type, received_date_str)
    except Exception as e:
        logger.error("Claude APIによるレシート解析に失敗しました: %s", e)
        receipt = None

    if receipt is None:
        reply_to_line(reply_token, READ_FAILURE_MESSAGE)
        return

    rows = [
        [receipt["date"], receipt["store"], item["name"], item["price"], item["category"]]
        for item in receipt["items"]
    ]

    try:
        append_rows(rows)
    except Exception as e:
        logger.error("Googleスプレッドシートへの書き込みに失敗しました: %s", e)
        reply_to_line(reply_token, SHEET_WRITE_FAILURE_MESSAGE)
        return

    reply_to_line(reply_token, build_summary_message(receipt))


def handle_text_message(event: dict):
    reply_token = event.get("replyToken")
    reply_to_line(reply_token, GUIDE_MESSAGE)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_line_signature(body, signature):
        logger.error("Webhookの署名検証に失敗しました。不正なリクエストの可能性があるため拒否します。")
        abort(400)

    try:
        events = request.get_json(force=True).get("events", [])
    except Exception as e:
        logger.error("Webhookボディの解析に失敗しました: %s", e)
        return "OK", 200

    for event in events:
        try:
            if event.get("type") != "message":
                continue
            message_type = event.get("message", {}).get("type")
            if message_type == "image":
                handle_image_message(event)
            elif message_type == "text":
                handle_text_message(event)
        except Exception as e:
            logger.error("イベント処理中にエラーが発生しました: %s", e)

    return "OK", 200


def run_server():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)


def main():
    global sheets_service, sheet_name

    try:
        sheets_service = get_sheets_service()
        sheet_name = resolve_sheet_name(sheets_service, SPREADSHEET_ID)
        ensure_header_row(sheets_service, SPREADSHEET_ID, sheet_name)
    except Exception as e:
        logger.error("Googleスプレッドシートへの接続に失敗しました: %s", e)
        sys.exit(1)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    port = int(os.environ.get("PORT", 5000))
    logger.info("Webhookサーバーを起動しました（ポート: %s、エンドポイント: /callback）", port)
    logger.info("終了するには 'exit' または 'quit' と入力してください。")

    while True:
        try:
            command = input().strip().lower()
        except EOFError:
            break
        if command in ("exit", "quit"):
            logger.info("サーバーを終了します。")
            break


if __name__ == "__main__":
    main()
