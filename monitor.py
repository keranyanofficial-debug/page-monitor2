import os, csv, json, time, hashlib
import requests
from bs4 import BeautifulSoup

TARGETS_CSV = "targets.csv"
SNAPSHOT_JSON = "snapshots.json"

def load_targets():
    targets = []
    with open(TARGETS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 必須: id, url
            if not row.get("id") or not row.get("url"):
                continue
            row["selector"] = (row.get("selector") or "").strip()
            row["name"] = (row.get("name") or row["id"]).strip()
            targets.append(row)
    return targets

def normalize_text(s: str) -> str:
    return " ".join((s or "").split())

def fetch_page(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "PageMonitorBot/1.0"}, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text

def extract_value(html: str, selector: str) -> str:
    # selectorが空なら「ページ全体のテキスト」を監視（A）
    soup = BeautifulSoup(html, "html.parser")
    if not selector:
        return normalize_text(soup.get_text(" ", strip=True))

    # selectorがあるなら「その要素だけ」を監視（B）
    el = soup.select_one(selector)
    if not el:
        return ""  # 要素が取れない場合は空（＝変化として検知しやすい）
    return normalize_text(el.get_text(" ", strip=True))

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def load_snapshots():
    if not os.path.exists(SNAPSHOT_JSON):
        return {}
    with open(SNAPSHOT_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def save_snapshots(data):
    with open(SNAPSHOT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def discord_post(webhook_url: str, text: str):
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL empty; skip notify")
        return
    # Discordはcontentで送る。長すぎると失敗するので分割
    chunks = []
    while text:
        chunks.append(text[:1800])
        text = text[1800:]
    for c in chunks:
        res = requests.post(
            webhook_url,
            data=json.dumps({"content": c}),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        print("discord status:", res.status_code)

def main():
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    targets = load_targets()
    snapshots = load_snapshots()

    if not targets:
        print("No targets found in targets.csv")
        return

    changes = []
    now = int(time.time())

    for t in targets:
        tid = t["id"]
        name = t["name"]
        url = t["url"]
        selector = t["selector"]

        try:
            html = fetch_page(url)
            value = extract_value(html, selector)
            h = sha256(value)
        except Exception as e:
            changes.append(f"⚠️ 取得失敗: {name}\n{url}\n{type(e).__name__}: {e}")
            continue

        prev = snapshots.get(tid)
        if not prev:
            # 初回登録
            snapshots[tid] = {
                "name": name,
                "url": url,
                "selector": selector,
                "hash": h,
                "value_preview": value[:200],
                "updated_at": now,
            }
            print(f"First seen: {tid}")
            continue

        if prev.get("hash") != h:
            old_preview = (prev.get("value_preview") or "")[:200]
            new_preview = value[:200]
            changes.append(
                "🚨 更新検知\n"
                f"- {name}\n"
                f"- {url}\n"
                + (f"- selector: `{selector}`\n" if selector else "- selector: (page text)\n")
                + f"- before: {old_preview}\n"
                + f"- after : {new_preview}\n"
            )

            snapshots[tid].update({
                "name": name,
                "url": url,
                "selector": selector,
                "hash": h,
                "value_preview": new_preview,
                "updated_at": now,
            })

        time.sleep(1)

    # 保存（次回比較用）
    save_snapshots(snapshots)
    print(f"updated {SNAPSHOT_JSON}")

    if changes:
        discord_post(webhook_url, "\n\n".join(changes))
    else:
        print("No changes. Skip notify.")

if __name__ == "__main__":
    main()
