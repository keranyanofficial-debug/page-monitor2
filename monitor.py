import os, csv, json, time, hashlib
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
import re

TARGETS_CSV = "targets.csv"
SNAPSHOT_JSON = "snapshots.json"

MAX_ATOM_ITEMS = int(os.getenv("MAX_ATOM_ITEMS", "5"))
MAX_HTML_LINKS = int(os.getenv("MAX_HTML_LINKS", "8"))
JST = ZoneInfo("Asia/Tokyo")

def now_jst_str():
    return datetime.now(timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")

def normalize_text(s: str) -> str:
    return " ".join((s or "").split())

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def parse_keywords(keyword: str):
    """keyword: '地震|津波|特別警報' みたいに OR 条件。空ならNone"""
    k = (keyword or "").strip()
    if not k:
        return None
    parts = [p.strip() for p in k.split("|") if p.strip()]
    if not parts:
        return None
    # 大文字小文字を気にしない検索
    return [p.lower() for p in parts]

def match_any(text: str, kws):
    if not kws:
        return True  # フィルタ無しは常にマッチ
    t = (text or "").lower()
    return any(k in t for k in kws)

def load_targets():
    targets = []
    with open(TARGETS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("id") or not row.get("url"):
                continue
            row["selector"] = (row.get("selector") or "").strip()
            row["name"] = (row.get("name") or row["id"]).strip()
            row["keyword"] = (row.get("keyword") or "").strip()
            targets.append(row)
    return targets

def load_snapshots():
    if not os.path.exists(SNAPSHOT_JSON):
        return {}
    with open(SNAPSHOT_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def save_snapshots(data):
    with open(SNAPSHOT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def fetch(url: str):
    r = requests.get(url, headers={"User-Agent":"PageMonitorBot/1.0"}, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    ctype = (r.headers.get("Content-Type") or "").lower()
    return r.text, ctype

def parse_atom(xml_text: str, base_url: str, kws):
    root = ET.fromstring(xml_text)

    def local(tag):
        return tag.split("}", 1)[-1] if "}" in tag else tag

    entries = []
    for e in root.iter():
        if local(e.tag) != "entry":
            continue
        title = ""
        link = ""
        updated = ""
        eid = ""
        for ch in list(e):
            t = local(ch.tag)
            if t == "title":
                title = normalize_text(ch.text or "")
            elif t == "updated":
                updated = normalize_text(ch.text or "")
            elif t == "id":
                eid = normalize_text(ch.text or "")
            elif t == "link":
                href = ch.attrib.get("href") or ""
                if href:
                    link = urljoin(base_url, href)

        # キーワードフィルタ（タイトル＋URLも対象にする）
        if not match_any(title + " " + link, kws):
            continue

        if title or link:
            entries.append({"title": title, "link": link, "updated": updated, "id": eid})

    entries = entries[:MAX_ATOM_ITEMS]

    # フィルタ後の内容だけでハッシュ（＝関係ない更新では通知しない）
    hash_src = "\n".join([f"{x.get('id')}|{x.get('updated')}|{x.get('title')}|{x.get('link')}" for x in entries])
    preview = " / ".join([x.get("title","") for x in entries])[:300]

    lines = []
    for x in entries:
        t = x.get("title") or "(no title)"
        u = x.get("link") or ""
        up = x.get("updated") or ""
        if up:
            lines.append(f"- {t} ({up})")
        else:
            lines.append(f"- {t}")
        if u:
            lines.append(f"  {u}")
        lines.append("")  # 見やすく空行

    return hash_src, preview, lines

def parse_html(html: str, base_url: str, selector: str, kws):
    soup = BeautifulSoup(html, "html.parser")

    # selector指定：その部分のテキストだけ監視
    if selector:
        el = soup.select_one(selector)
        text = normalize_text(el.get_text(" ", strip=True)) if el else ""
        # キーワードがある時は、マッチしないなら「空」として扱う（通知しない）
        if not match_any(text, kws):
            text = ""
        hash_src = text
        preview = text[:300]
        lines = [f"- value: {preview}"] if preview else ["- value: (no keyword match / empty)"]
        return hash_src, preview, lines

    # selector無し：タイトル＋主要リンクを抽出（要約）
    title = normalize_text(soup.title.get_text(strip=True)) if soup.title else ""
    main = soup.find("main") or soup.body or soup

    links = []
    for a in main.select("a[href]"):
        txt = normalize_text(a.get_text(" ", strip=True))
        href = (a.get("href") or "").strip()
        if not txt or len(txt) < 2:
            continue
        if href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        absu = urljoin(base_url, href)

        # キーワードフィルタ（リンク文言＋URL）
        if not match_any(txt + " " + absu, kws):
            continue

        links.append((txt, absu))
        if len(links) >= MAX_HTML_LINKS:
            break

    # フィルタ後リンクだけで比較（＝関係ない更新は通知しない）
    hash_src = title + "\n" + "\n".join([f"{t}|{u}" for t,u in links])
    preview = (title or (links[0][0] if links else ""))[:300]

    lines = []
    lines.append(f"- title: {title}" if title else "- title: (none)")
    if links:
        lines.append("- matched links:")
        for t,u in links:
            lines.append(f"  • {t}")
            lines.append(f"    {u}")
    else:
        lines.append("- matched links: (none)")
    return hash_src, preview, lines

def extract_observation(url: str, body: str, content_type: str, selector: str, keyword: str):
    kws = parse_keywords(keyword)
    is_xml = url.lower().endswith(".xml") or ("xml" in content_type)
    if is_xml:
        return parse_atom(body, url, kws)
    return parse_html(body, url, selector, kws)

def discord_post(webhook_url: str, text: str):
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL empty; skip notify")
        return
    chunks = []
    while text:
        chunks.append(text[:1800])
        text = text[1800:]
    for c in chunks:
        res = requests.post(
            webhook_url,
            data=json.dumps({"content": c}),
            headers={"Content-Type":"application/json"},
            timeout=30
        )
        print("discord status:", res.status_code)

def main():
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL","")
    targets = load_targets()
    snapshots = load_snapshots()

    if not targets:
        print("No targets found in targets.csv")
        return

    changes_msgs = []
    ts = now_jst_str()

    for t in targets:
        tid = t["id"]
        name = t["name"]
        url = t["url"]
        selector = t["selector"]
        keyword = t.get("keyword","")

        try:
            body, ctype = fetch(url)
            hash_src, new_preview, new_lines = extract_observation(url, body, ctype, selector, keyword)
            new_hash = sha256(hash_src)
        except Exception as e:
            changes_msgs.append(f"⚠️ 取得失敗 [{name}]\n🕘 {ts}\n{url}\n{type(e).__name__}: {e}")
            continue

        prev = snapshots.get(tid)

        # 初回は登録だけ（通知しない）
        if not prev:
            snapshots[tid] = {
                "name": name, "url": url, "selector": selector, "keyword": keyword,
                "hash": new_hash, "preview": new_preview, "updated_at_jst": ts
            }
            print(f"First seen: {tid}")
            time.sleep(1)
            continue

        if prev.get("hash") != new_hash:
            old_preview = (prev.get("preview") or "")[:300]
            header = f"🚨 更新検知 [{name}]\n🕘 {ts}\n{url}"
            if selector:
                header += f"\nselector: {selector}"
            if keyword:
                header += f"\nkeyword: {keyword}"

            msg = header + f"\nbefore: {old_preview}\nafter : {new_preview[:300]}"
            if new_lines:
                msg += "\n\n最新の内容（抜粋）\n" + "\n".join(new_lines[:40])

            changes_msgs.append(msg)

            snapshots[tid] = {
                "name": name, "url": url, "selector": selector, "keyword": keyword,
                "hash": new_hash, "preview": new_preview, "updated_at_jst": ts
            }

        time.sleep(1)

    save_snapshots(snapshots)
    print(f"updated {SNAPSHOT_JSON}")

    if changes_msgs:
        discord_post(webhook_url, "\n\n".join(changes_msgs))
    else:
        print("No changes. Skip notify.")

if __name__ == "__main__":
    main()
