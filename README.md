# 🧾 LINEレシート家計簿Bot

LINEに送ったレシートの写真をClaude API(Vision機能)が読み取り、日付・店名・品目・金額・カテゴリをGoogleスプレッドシートに自動で家計簿として記録するLINE Botです。

## 📋 概要

LINEに画像メッセージを送るだけで、以下の流れを自動化します。

1. LINEプラットフォームからのWebhook(`POST /callback`)を受信
2. LINEのContent APIでレシート画像を取得
3. Claude API(Vision機能)が画像を解析し、日付・店名・品目ごとの内訳(品目名・金額・カテゴリ)・合計金額をJSON形式で抽出
4. 抽出結果を、品目ごとに1行ずつGoogleスプレッドシートに追記
5. 記録が完了したら、LINEに記録内容の要約を返信

## ✨ 主な機能

- 📷 **レシート画像の自動解析**: Claude Vision APIが画像から日付・店名・品目・金額・合計金額を読み取り
- 🏷️ **カテゴリ自動分類**: 各品目を「食費」「日用品」「外食」「その他」のいずれかにAIが自動分類
- 📊 **Googleスプレッドシート連携**: 品目ごとに1行ずつ(日付・店名・品目・金額・カテゴリ)を自動追記、ヘッダー行が無ければ自動作成
- 💬 **LINEへの結果返信**: 記録した店名・日付・合計金額・品目数と内訳をLINEに返信
- 🔒 **Webhook署名検証**: HMAC-SHA256でLINEプラットフォームからの正規リクエストのみを受け付け
- ⚠️ **エラーハンドリング**: 画像取得・AI解析・スプレッドシート書き込みをそれぞれ個別にエラー処理し、失敗時も日本語で分かりやすく案内
- 🔁 **常時稼働**: Webhookサーバーはバックグラウンドで待ち受け、ターミナルで `exit`/`quit` を入力するまで稼働し続ける

## 🔑 必要な環境変数

| 環境変数 | 説明 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developersコンソールで発行したChannel access token |
| `LINE_CHANNEL_SECRET` | LINE Developersコンソールで発行したChannel secret(Webhook署名検証用) |
| `ANTHROPIC_API_KEY` | Claude APIキー |
| `SPREADSHEET_ID` | 記録先のGoogleスプレッドシートID |

このほか、Googleスプレッドシート用のOAuthクライアントJSON(`credentials.json`)をスクリプトと同じフォルダに配置してください。初回実行時にブラウザ認証が行われ、`token_sheets.json` が自動生成されます。

**`credentials.json` / `token_sheets.json` などの認証情報ファイルは、このリポジトリには含まれていません。** 各自で用意し、絶対にリポジトリにコミットしないでください。

## ⚙️ セットアップ手順

1. 必要なパッケージをインストール
   ```bash
   pip install flask requests anthropic google-auth-oauthlib google-api-python-client google-auth
   ```

2. GoogleのOAuthクライアントJSON(デスクトップアプリ用)を `credentials.json` としてスクリプトと同じフォルダに配置

3. 環境変数を設定

   **Windows (PowerShell)**
   ```powershell
   $env:LINE_CHANNEL_ACCESS_TOKEN = "xxxx"
   $env:LINE_CHANNEL_SECRET = "xxxx"
   $env:ANTHROPIC_API_KEY = "xxxx"
   $env:SPREADSHEET_ID = "xxxx"
   ```

   **macOS / Linux**
   ```bash
   export LINE_CHANNEL_ACCESS_TOKEN="xxxx"
   export LINE_CHANNEL_SECRET="xxxx"
   export ANTHROPIC_API_KEY="xxxx"
   export SPREADSHEET_ID="xxxx"
   ```

4. LINE Developersコンソールで、Webhook URLに `https://(公開URL)/callback` を設定し、Webhookの利用をON
   (ローカルで試す場合は ngrok 等でこのサーバーを外部公開する)

## 💻 使い方

1. スクリプトを実行
   ```bash
   python line_receipt_to_sheets.py
   ```
   初回実行時はブラウザが開き、Googleアカウントでの認証が必要です。

2. Webhookサーバーがバックグラウンドで起動し、待ち受けを開始します

3. LINEでBotにレシートの写真を送ると、以下のように自動記録されます

   ```
   家計簿に記録しました!

   店名: ○○スーパー
   日付: 2026-08-20
   合計: 3,450円
   品目数: 5件

   内訳:
   ・牛乳 - 250円 (食費)
   ・卵 - 200円 (食費)
   ・洗剤 - 480円 (日用品)
   ```

4. 画像以外のテキストメッセージを送ると、「レシートの写真を送ってください。」と案内が返信されます

5. 終了する場合は、ターミナルで `exit` または `quit` と入力

## ⚠️ 注意事項

- レシートの内容をうまく読み取れなかった場合は、その旨をLINEに返信し、スプレッドシートへの記録は行いません
- レシートに日付の記載が無い場合は、画像を受信した日付が代わりに使用されます
- 個人利用を想定したシンプルな構成のため、Webhookサーバーの公開には ngrok や適切なホスティング環境を別途用意してください
