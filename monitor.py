import os, csv, json, time, hashlib
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

TARGETS_CSV = "targets.csv"
SNAPSHOT_JSON = "snapshots.json"

JST = ZoneInfo("Asia/Tokyo")

# 動作チューニング（GitHub Actions向け）
MAX_ATOM_ITEMS = int(os.getenv("MAX_ATOM_ITEMS", "5"))
MAX_HTML_LINKS = int(os.getenv("MAX_HTML_LINKS", "8"))
SLEEP_SEC = float(os.getenv("SLEEP_SEC", "1"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))

# 初回でも通知したい時（デモ用）
NOTIFY_FIRST_SEEN = os.getenv("NOTIFY_FIRST_SEEN", "0") == "1"

# Discordリンク埋め込みを抑制（通知が“わかってる感”でスッキリする）
DISCORD_SUPPRESS_EMBEDS = os.getenv("DISCORD_SUPPRESS_EMBEDS", "1") == "1"

def now_jst_str():
    return datetime.now(timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")

def normalize_text(s: str) -> str:
    return " ".join((s or "").split())

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

# ----------------------------
# keyword: include/exclude 対応
# ----------------------------
def parse_keywords(keyword: str):
    """
    keyword例（OR）:
      '地震|津波|特別警報'
    除外（!）:
      '地震|津波|!火山|!噴火'
    """
    k = (keyword or "").strip()
    if not k:
        return None

    parts = [p.strip() for p in k.split("|") if p.strip()]
    if not parts:
        return None

    includes, excludes = [], []
    for p in parts:
        if p.startswith("!"):
            x = p[1:].strip().lower()
            if x:
                excludes.append(x)
        else:
            includes.append(p.lower())

    return {"includes": includes, "excludes": excludes}

def match_any(text: str, kws):
    if not kws:
        return True
    t = (text or "").lower()

    if any(x in t for x in kws.get("excludes", [])):
        return False

    inc = kws.get("includes", [])
    if not inc:
        return True
    return any(k in t for k in inc)

# ----------------------------
# CSV / snapshots
# ----------------------------
def load_targets():
    targets = []
    with open(TARGETS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("id") or not row.get("url"):
                continue
            row["id"] = row["id"].strip()
            row["url"] = row["url"].strip()
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

# ----------------------------
# Fetch with caching headers (ETag/Last-Modified)
# ----------------------------
def fetch(url: str, prev_meta: dict | None = None):
    headers = {"User-Agent": "PageMonitorBot/1.0"}
    if prev_meta:
        etag = prev_meta.get("etag") or ""
        last_mod = prev_meta.get("last_modified") or ""
        if etag:
            headers["If-None-Match"] = etag
        if last_mod:
            headers["If-Modified-Since"] = last_mod

    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    # 304 = 更新なし（差分比較すら不要）
    if r.status_code == 304:
        return None, (r.headers.get("Content-Type") or "").lower(), r.headers.get("ETag"), r.headers.get("Last-Modified"), 304

    r.raise_for_status()
    r.encoding = "utf-8"
    ctype = (r.headers.get("Content-Type") or "").lower()
    return r.text, ctype, r.headers.get("ETag"), r.headers.get("Last-Modified"), r.status_code

# ----------------------------
# Atom(XML) 解析
# ----------------------------
def parse_atom(xml_text: str, base_url: str, kws):
    root = ET.fromstring(xml_text)

    def local(tag):
        return tag.split("}", 1)[-1] if "}" in tag else tag

    # Atom feedかざっくり判定（feed要素 or entryが出る想定）
    root_name = local(root.tag).lower()
    if root_name != "feed":
        # feedじゃないXMLはAtomとして扱わない（汎用XMLへ）
        raise ValueError("not_atom_feed")

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

    hash_src = "\n".join([f"{x.get('id')}|{x.get('updated')}|{x.get('title')}|{x.get('link')}" for x in entries])
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

# ----------------------------
# 汎用XML（AtomじゃないXML向け）
# ----------------------------
def parse_xml_generic(xml_text: str, kws):
    # XML全文はデカくなりがちなので、要点っぽいテキストだけ抽出して安定化
    try:
        root = ET.fromstring(xml_text)
        texts = []
        # 先頭側のテキストを少し拾う（大きくなりすぎ防止）
        for el in list(root.iter())[:300]:
            if el.text:
                t = normalize_text(el.text)
                if t:
                    texts.append(t)
            if len(texts) >= 50:
                break
        joined = "\n".join(texts)
    except Exception:
        joined = normalize_text(xml_text)[:5000]

    if not match_any(joined, kws):
        joined = ""

    hash_src = joined
    preview = (joined[:300] if joined else "(no keyword match / empty)")
    lines = [f"- xml: {preview}"]
    return hash_src, preview, lines

# ----------------------------
# HTML 解析
# ----------------------------
def parse_html(html: str, base_url: str, selector: str, kws):
    soup = BeautifulSoup(html, "html.parser")

    # selector指定：その部分のテキストだけ監視
    if selector:
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

    lines = []
    lines.append(f"- title: {title}" if title else "- title: (none)")
    if links:
        lines.append("- matched links:")
        for t, u in links:
            lines.append(f"  • {t}")
            lines.append(f"    {u}")
    else:
        lines.append("- matched links: (none)")
    return hash_src, preview, lines

# ----------------------------
# JSON API 解析
# ----------------------------
def _pick_first_list(obj):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ["items", "results", "data", "laws", "list"]:
            v = obj.get(k)
            if isinstance(v, list):
                return v
    return None

def _flatten_dict(d, keys):
    out = []
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        out.append(f"{k}={normalize_text(str(v))}")
    return " ".join(out)

def parse_json_api(json_text: str, kws, selector: str = ""):
    obj = json.loads(json_text)

    # selector: "key1,key2,key3"（CSVではダブルクォート必須）
    keys = [x.strip() for x in (selector or "").split(",") if x.strip()]

    lines = []
    hash_lines = []

    lst = _pick_first_list(obj)

    # 1) list[dict] の場合：行化して安定化
    if lst is not None and len(lst) > 0 and all(isinstance(x, dict) for x in lst):
        for item in lst:
            if keys:
                row = _flatten_dict(item, keys)
            else:
                prefer = [
                    "id", "lawId", "lawID", "lawNum", "lawNo", "lawTitle", "title", "name",
                    "updated", "updateDate", "date", "published_at", "promulgationDate"
                ]
                use = [k for k in prefer if k in item]
                if not use:
                    use = list(item.keys())[:6]
                row = _flatten_dict(item, use)

            if not row:
                continue
            if not match_any(row, kws):
                continue

            lines.append(f"- {row}")
            hash_lines.append(row)

        hash_lines = sorted(hash_lines)
        hash_src = "\n".join(hash_lines)
        preview = (hash_lines[0] if hash_lines else "(no match)")[:300]
        return hash_src, preview, lines[:MAX_ATOM_ITEMS]

    # 2) list[primitive] の場合：先頭N件だけで差分を見る（デモ/実務で便利）
    if isinstance(lst, list) and (len(lst) == 0 or not all(isinstance(x, dict) for x in lst)):
        pick = [normalize_text(str(x)) for x in lst[: max(10, MAX_ATOM_ITEMS * 2)]]
        joined = "\n".join(pick)
        if not match_any(joined, kws):
            joined = ""
        hash_src = joined
        preview = (pick[0] if pick else "(empty)")[:300]
        lines = [f"- {x}" for x in pick] if pick else ["- (empty list)"]
        return hash_src, preview, lines[:MAX_ATOM_ITEMS]

    # 3) dict など：正規化して監視（最終手段）
    canon = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not match_any(canon, kws):
        canon = ""
    hash_src = canon
    preview = canon[:300] if canon else "(no keyword match / empty)"
    lines = [f"- json: {preview}"]
    return hash_src, preview, lines

# ----------------------------
# 形式判定 → 抽出
# ----------------------------
def extract_observation(url: str, body: str, content_type: str, selector: str, keyword: str):
    kws = parse_keywords(keyword)
    ct = (content_type or "").lower()

    is_json = ("json" in ct) or url.lower().endswith(".json")
    is_xml = url.lower().endswith(".xml") or ("xml" in ct)

    if is_json:
        return parse_json_api(body, kws, selector)

    if is_xml:
        # Atomをまず試す → ダメなら汎用XML
        try:
            return parse_atom(body, url, kws)
        except Exception:
            return parse_xml_generic(body, kws)

    return parse_html(body, url, selector, kws)

# ----------------------------
# Discord通知
# ----------------------------
def discord_post(webhook_url: str, text: str):
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL empty; skip notify")
        return

    chunks = []
    while text:
        chunks.append(text[:1800])
        text = text[1800:]

    for c in chunks:
        payload = {"content": c}
        if DISCORD_SUPPRESS_EMBEDS:
            payload["flags"] = 4  # SUPPRESS_EMBEDS

        res = requests.post(
            webhook_url,
            json=payload,
            timeout=HTTP_TIMEOUT
        )
        print("discord status:", res.status_code)

def main():
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    targets = load_targets()
    snapshots = load_snapshots()

    if not targets:
        print("No targets found in targets.csv")
        return

    ts = now_jst_str()
    changes_msgs = []

    for t in targets:
        tid = t["id"]
        name = t["name"]
        url = t["url"]
        selector = t["selector"]
        keyword = t.get("keyword", "")

        prev = snapshots.get(tid) or {}
        prev_meta = {"etag": prev.get("etag"), "last_modified": prev.get("last_modified")}

        try:
            body, ctype, etag, last_modified, status = fetch(url, prev_meta=prev_meta if prev else None)
        except Exception as e:
            changes_msgs.append(f"⚠️ 取得失敗 [{name}]\n🕘 {ts}\n{url}\n{type(e).__name__}: {e}")
            time.sleep(SLEEP_SEC)
            continue

        # 304：更新なし
        if status == 304:
            # metaだけ更新（任意）
            if prev:
                prev["updated_at_jst"] = ts
                snapshots[tid] = prev
            time.sleep(SLEEP_SEC)
            continue

        # 通常：観測値を生成
        try:
            hash_src, new_preview, new_lines = extract_observation(url, body, ctype, selector, keyword)
            new_hash = sha256(hash_src)
        except Exception as e:
            changes_msgs.append(f"⚠️ 解析失敗 [{name}]\n🕘 {ts}\n{url}\n{type(e).__name__}: {e}")
            time.sleep(SLEEP_SEC)
            continue

        # 初回
        if not prev:
            snapshots[tid] = {
                "name": name, "url": url, "selector": selector, "keyword": keyword,
                "hash": new_hash, "preview": new_preview, "updated_at_jst": ts,
                "etag": etag, "last_modified": last_modified
            }
            print(f"First seen: {tid}")

            if NOTIFY_FIRST_SEEN:
                header = f"🆕 初回登録 [{name}]\n🕘 {ts}\n{url}"
                if selector:
                    header += f"\nselector: {selector}"
                if keyword:
                    header += f"\nkeyword: {keyword}"
                msg = header + f"\npreview: {new_preview[:300]}"
                if new_lines:
                    msg += "\n\n最新の内容（抜粋）\n" + "\n".join(new_lines[:40])
                changes_msgs.append(msg)

            time.sleep(SLEEP_SEC)
            continue

        # 更新検知
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
                "hash": new_hash, "preview": new_preview, "updated_at_jst": ts,
                "etag": etag, "last_modified": last_modified
            }
        else:
            # 変化なしでもmetaは更新しておく（負荷抑制の精度が上がる）
            prev["etag"] = etag or prev.get("etag")
            prev["last_modified"] = last_modified or prev.get("last_modified")
            prev["updated_at_jst"] = ts
            snapshots[tid] = prev

        time.sleep(SLEEP_SEC)

    save_snapshots(snapshots)
    print(f"updated {SNAPSHOT_JSON}")

    if changes_msgs:
        discord_post(webhook_url, "\n\n".join(changes_msgs))
    else:
        print("No changes. Skip notify.")

if __name__ == "__main__":
    main()
