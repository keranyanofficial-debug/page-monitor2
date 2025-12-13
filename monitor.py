import os
import re
import time
import json
import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://books.toscrape.com/"
START = urljoin(BASE, "catalogue/page-1.html")
SNAPSHOT_FILE = "snapshot_latest.csv"

def normalize_price(s):
    if pd.isna(s):
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(s))
    return float(m.group(1)) if m else None

def scrape_all_books():
    url = START
    rows = []
    while url:
        r = requests.get(url, headers={"User-Agent":"PageMonitorBot/1.0"}, timeout=30)
        r.raise_for_status()
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")

        for a in soup.select("article.product_pod h3 a"):
            title = a.get("title")
            rel = a.get("href")
            product_url = urljoin(url, rel)

            pod = a.find_parent("article")
            price = pod.select_one(".price_color").get_text(strip=True)
            stock = pod.select_one(".instock.availability").get_text(" ", strip=True)

            rows.append({"product_url": product_url, "title": title, "price": price, "stock": stock})

        next_a = soup.select_one("li.next a")
        url = urljoin(url, next_a["href"]) if next_a else None
        time.sleep(1)

    df = pd.DataFrame(rows)
    df["price_num"] = df["price"].apply(normalize_price)
    df = df.sort_values("product_url").reset_index(drop=True)
    return df[["product_url","title","price","price_num","stock"]]

def diff(old_df, new_df):
    old_key = old_df.drop_duplicates(subset=["product_url"]).set_index("product_url")
    new_key = new_df.drop_duplicates(subset=["product_url"]).set_index("product_url")

    added = new_key.loc[new_key.index.difference(old_key.index)].reset_index()
    removed = old_key.loc[old_key.index.difference(new_key.index)].reset_index()
    common = new_key.index.intersection(old_key.index)

    merged = pd.DataFrame(index=common)
    merged["old_price"] = old_key.loc[common, "price_num"]
    merged["new_price"] = new_key.loc[common, "price_num"]
    merged["old_stock"] = old_key.loc[common, "stock"]
    merged["new_stock"] = new_key.loc[common, "stock"]

    changed_price = merged[merged["old_price"] != merged["new_price"]].reset_index()
    changed_stock = merged[merged["old_stock"] != merged["new_stock"]].reset_index()

    return added, removed, changed_price, changed_stock, new_key

def notify_discord(webhook_url, added, removed, changed_price, changed_stock, new_key):
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL empty")
        return
    if len(added)==0 and len(removed)==0 and len(changed_price)==0 and len(changed_stock)==0:
        print("No changes. Skip notify.")
        return

    lines = ["🚨 PageMonitor: 更新検知（Books to Scrape）"]

    if len(changed_price) > 0:
        lines.append(f"💰 価格変更: {len(changed_price)}件（上位3件）")
        for _, r in changed_price.head(3).iterrows():
            u = r["product_url"]
            title = new_key.loc[u, "title"]
            lines.append(f"- {title}: {r['old_price']} → {r['new_price']}")
            lines.append(f"  {u}")

    if len(changed_stock) > 0:
        lines.append(f"📦 在庫変更: {len(changed_stock)}件（上位3件）")
        for _, r in changed_stock.head(3).iterrows():
            u = r["product_url"]
            title = new_key.loc[u, "title"]
            lines.append(f"- {title}: {r['old_stock']} → {r['new_stock']}")
            lines.append(f"  {u}")

    msg = "\n".join(lines)
    res = requests.post(webhook_url, data=json.dumps({"content": msg}),
                        headers={"Content-Type":"application/json"}, timeout=30)
    print("discord status:", res.status_code)

def main():
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL","")
    new_df = scrape_all_books()

    if not os.path.exists(SNAPSHOT_FILE):
        new_df.to_csv(SNAPSHOT_FILE, index=False, encoding="utf-8")
        print(f"First run: created {SNAPSHOT_FILE}")
        return

    old_df = pd.read_csv(SNAPSHOT_FILE)
    added, removed, changed_price, changed_stock, new_key = diff(old_df, new_df)

    print("added:", len(added), "removed:", len(removed),
          "price changed:", len(changed_price), "stock changed:", len(changed_stock))

    notify_discord(webhook_url, added, removed, changed_price, changed_stock, new_key)

    new_df.to_csv(SNAPSHOT_FILE, index=False, encoding="utf-8")
    print(f"updated {SNAPSHOT_FILE}")

if __name__ == "__main__":
    main()
