import os, csv, json, time, hashlib
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

TARGETS_CSV = "targets.csv"
SNAPSHOT_JSON = "snapshots.json"

MAX_ATOM_ITEMS = int(os.getenv("MAX_ATOM_ITEMS", "5"))
MAX_HTML_LINKS = int(os.getenv("MAX_HTML_LINKS", "8"))
MAX_JSON_ITEMS = int(os.getenv("MAX_JSON_ITEMS", "5"))
JST = ZoneInfo("Asia/Tokyo")


def now_jst_str():
    return datetime.now(timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def normalize_text(s: str) -> str:
    return " ".join((s or "").split())


def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def parse_keywords(keyword: str):
    k = (keyword or "").strip()
    if not k:
        return None
    parts = [p.strip() for p in k.split("|") if p.strip()]
    if not parts:
        return None
    return [p.lower() for p in parts]


def match_any(text: str, kws):
    if not kws:
        return True
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
    r = requests.get(url, headers={"User-Agent": "PageMonitorBot/1.0"}, timeout=30)
    r.raise_for_status()

    # 文字化け対策：JSONはほぼUTF-8なので優先。その他はヘッダ→推定。
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        text = r.content.decode("utf-8", errors="replace")
        return text, ctype

    r.encoding = r.encoding or "utf-8"
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

        if not match_any(title + " " + link, kws):
            continue

        if title or link:
            entries.append({"title": title, "link": link, "updated": updated, "id": eid})

    entries = entries[:MAX_ATOM_ITEMS]

    hash_src = "\n".join(
        [f"{x.get('id')}|{x.get('updated')}|{x.get('title')}|{x.get('link')}" for x in entries]
    )
    preview = " / ".join([x.get("title", "") for x in entries])[:300]

    lines = []
    for x in entries:
        t = x.get("title") or "(no title)"
        u = x.get("link") or ""
        up = x.get("updated") or ""
        lines.append(f"- {t}" + (f" ({up})" if up else ""))
        if u:
            lines.append(f"  {u}")
        lines.append("")

    return hash_src, preview, lines


def parse_html(html: str, base_url: str, selector: str, kws):
    soup = BeautifulSoup(html, "html.parser")

    if selector and not selector.startswith("json:"):
        el = soup.select_one(selector)
        text = normalize_text(el.get_text(" ", strip=True)) if el else ""
        if not match_any(text, kws):
            text = ""
        hash_src = text
        preview = text[:300]
        lines = [f"- value: {preview}"] if preview else ["- value: (no keyword match / empty)"]
        return hash_src, preview, lines

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

        if not match_any(txt + " " + absu, kws):
            continue

        links.append((txt, absu))
        if len(links) >= MAX_HTML_LINKS:
            break

    hash_src = title + "\n" + "\n".join([f"{t}|{u}" for t, u in links])
    preview = (title or (links[0][0] if links else ""))[:300]

    lines = [f"- title: {title}" if title else "- title: (none)"]
    if links:
        lines.append("- matched links:")
        for t, u in links:
            lines.append(f"  • {t}")
            lines.append(f"    {u}")
    else:
        lines.append("- matched links: (none)")

    return hash_src, preview, lines


def parse_json_api(json_text: str, selector: str, kws):
    """
    selector:
      - "json:result" のように listキーを指定（jGrantsは result）
      - 空なら、result/items/data を自動推定
    """
    try:
        data = json.loads(json_text)
    except Exception:
        # JSONとして壊れてたら、文字列として比較
        src = normalize_text(json_text)
        return src, src[:300], ["- raw(json): " + src[:300]]

    list_key = None
    if (selector or "").startswith("json:"):
        list_key = (selector.split("json:", 1)[1] or "").strip() or None

    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if list_key and isinstance(data.get(list_key), list):
            items = data.get(list_key)
        else:
            for k in ["result", "items", "data"]:
                if isinstance(data.get(k), list):
                    items = data.get(k)
                    break

    if not isinstance(items, list):
        # 取得できる一覧がない場合、dict全体の一部で比較
        src = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if not match_any(src, kws):
            src = ""
        return src, src[:300], ["- json(dict)"]

    # 一覧の中身を「ID/タイトル/期限/URL」中心に整形
    picked = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = normalize_text(it.get("title") or it.get("name") or "")
        _id = normalize_text(it.get("id") or it.get("code") or it.get("name") or "")
        end = normalize_text(it.get("acceptance_end_datetime") or it.get("updated") or it.get("updated_at") or "")
        url = normalize_text(it.get("front_subsidy_detail_page_url") or it.get("url") or it.get("link") or "")

        blob = f"{_id} {title} {end} {url}"
        if not match_any(blob, kws):
            continue

        picked.append({"id": _id, "title": title, "end": end, "url": url})
        if len(picked) >= MAX_JSON_ITEMS:
            break

    # フィルタ後だけで比較（＝関係ない更新で通知しない）
    hash_src = "\n".join([f"{x['id']}|{x['title']}|{x['end']}|{x['url']}" for x in picked])
    preview = " / ".join([x["title"] for x in picked if x["title"]])[:300]

    lines = []
    for x in picked:
        t = x["title"] or "(no title)"
        lines.append(f"- {t}" + (f" ({x['end']})" if x["end"] else ""))
        if x["url"]:
            lines.append(f"  {x['url']}")
        lines.append("")

    if not lines:
        lines = ["- (no keyword match / empty)"]

    return hash_src, preview, lines


def extract_observation(url: str, body: str, content_type: str, selector: str, keyword: str):
    kws = parse_keywords(keyword)

    # JSON（公式API）優先
    if "application/json" in (content_type or "").lower() or (selector or "").startswith("json:"):
        return parse_json_api(body, selector, kws)

    # XML/Atom
    is_xml = url.lower().endswith(".xml") or ("xml" in (content_type or "").lower())
    if is_xml:
        return parse_atom(body, url, kws)

    # HTML
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

    changes_msgs = []
    ts = now_jst_str()

    for t in targets:
        tid = t["id"]
        name = t["name"]
        url = t["url"]
        selector = t["selector"]
        keyword = t.get("keyword", "")

        try:
            body, ctype = fetch(url)
            hash_src, new_preview, new_lines = extract_observation(url, body, ctype, selector, keyword)
            new_hash = sha256(hash_src)
        except Exception as e:
            changes_msgs.append(f"⚠️ 取得失敗 [{name}]\n🕘 {ts}\n{url}\n{type(e).__name__}: {e}")
            continue

        prev = snapshots.get(tid)

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
