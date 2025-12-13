# Page Monitor (GitHub Actions + Discord)

Webページ / Atom(XML)フィードの更新を定期チェックして、差分があればDiscordに通知します。  
監視対象は `targets.csv` に追記するだけで増やせます（HTMLはCSSセレクタで“特定部分だけ監視”も可能）。

## Demo
![Discord通知](discord-demo.png)

## Features
- GitHub Actionsで自動実行（スケジュール / 手動実行）
- 複数URL監視（`targets.csv`）
- 変化があった時だけDiscord通知（ノイズ削減）
- Atom(XML)は「最新のタイトル＋リンク」を通知
- HTMLは「ページの要約（タイトル＋主要リンク）」または「selector指定の部分監視」

## Quick Start（5分で動く）
### 1) Discord Webhookを用意
Discordで通知したいチャンネル → Webhook URL を作成してコピー  
※このURLは **READMEやコードに書かない**（Secretsに入れる）

### 2) GitHub Secretsに登録（重要）
リポジトリ → **Settings → Secrets and variables → Actions**  
→ **New repository secret**  
- Name: `DISCORD_WEBHOOK_URL`  
- Secret: さっきのWebhook URL

### 3) 監視対象を設定
`targets.csv` を編集して監視したいURLを追加します。

### 4) 実行（テスト）
**Actions → Page Monitor → Run workflow**  
- 1回目：初回登録（First seen）でスナップショット作成  
- 2回目以降：差分があればDiscord通知

## targets.csv の書き方
- `selector` が空：ページの要約（タイトル＋リンク）やAtomの最新項目で監視
- `selector` がある：その要素だけ監視（HTML向け）

例：
```csv
id,name,url,selector
jma_extra,気象庁 防災情報(随時/毎分),https://www.data.jma.go.jp/developer/xml/feed/extra.xml,
egov_news,e-Gov お知らせ,https://laws.e-gov.go.jp/news/,
