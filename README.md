# Page Monitor (GitHub Actions + Discord)

Webページ / Atom(XML)の更新を監視して、変化があればDiscordに通知します。

## Demo
![Discord通知](discord-demo.png.png)

## Setup
1. GitHub Secrets に DISCORD_WEBHOOK_URL を登録
2. Actions から Run workflow を実行

## targets.csv
監視対象は targets.csv に追加します。
