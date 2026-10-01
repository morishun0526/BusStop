#!/usr/bin/env python3
"""
全国のバス時刻表データ（GTFSデータリポジトリ https://gtfs-data.jp ）を取得し、
アプリが読む小さなJSONファイル群に変換します。

使い方:
  python scripts/build_data.py --out site/data              # 全国分を取得して変換
  python scripts/build_data.py --out site/data --pref 13 17 # 都道府県を絞る（テスト用）
  python scripts/build_data.py --out site/data --local test/sample.zip  # 手元のGTFSで試す

出力:
  index.json                    都道府県 → バス会社一覧
  c/<会社ID>/stops.json         バス停一覧（五十音順）
  c/<会社ID>/cal.json           運行日カレンダー
  c/<会社ID>/s/<番号>.json      バス停ごとの発車時刻
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

API = "https://api.gtfs-data.jp/v2"
UA = "bus-arrival-pwa/1.0 (+https://github.com/)"
JST = timezone(timedelta(hours=9))

PREFS = ["北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県",
         "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
         "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
         "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
         "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県"]

try:
    import pykakasi  # 漢字→ひらがな（読みが無いバス停の並べ替え用）
    _KKS = pykakasi.kakasi()
except Exception:  # pragma: no cover
    _KKS = None


# ---------------------------------------------------------------- 文字列ユーティリティ
def to_hiragana(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def reading_of(name: str, given: str) -> str:
    r = to_hiragana(given).strip()
    if r:
        return r
    if _KKS:
        try:
            return "".join(item["hira"] for item in _KKS.convert(name))
        except Exception:
            pass
    return to_hiragana(name)


GYO = [("あ", "あいうえおぁぃぅぇぉゔ"), ("か", "かきくけこがぎぐげご"), ("さ", "さしすせそざじずぜぞ"),
       ("た", "たちつてとだぢづでどっ"), ("な", "なにぬねの"), ("は", "はひふへほばびぶべぼぱぴぷぺぽ"),
       ("ま", "まみむめも"), ("や", "やゆよゃゅょ"), ("ら", "らりるれろ"), ("わ", "わをんゎ")]


def gyo_of(reading: str) -> str:
    c = reading[:1]
    for head, chars in GYO:
        if c in chars:
            return head
    if c.isascii() and c.isalpha():
        return "A"
    return "#"


def to_sec(t: str):
    if not t:
        return None
    try:
        h, m, s = t.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except ValueError:
        return None


def safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


# ---------------------------------------------------------------- GTFS 読み込み
def read_table(zf: zipfile.ZipFile, name: str):
    target = None
    for n in zf.namelist():
        if n.split("/")[-1] == name:
            target = n
            break
    if not target:
        return []
    raw = zf.read(target)
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    rows = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def stop_readings(zf, stops):
    """translations.txt から ja-Hrkt（よみがな）を取り出す。GTFS-JP 旧形式・新形式の両対応。"""
    by_id, by_name = {}, {}
    for r in read_table(zf, "translations.txt"):
        lang = r.get("language") or r.get("lang") or ""
        if lang.lower() != "ja-hrkt":
            continue
        tr = r.get("translation", "")
        if "table_name" in r:  # 新形式（GTFS 標準）
            if r.get("table_name") != "stops" or r.get("field_name") not in ("stop_name", ""):
                continue
            if r.get("record_id"):
                by_id[r["record_id"]] = tr
            elif r.get("field_value"):
                by_name[r["field_value"]] = tr
        elif r.get("trans_id"):  # 旧形式（GTFS-JP v2）
            by_name[r["trans_id"]] = tr
    out = {}
    for s in stops:
        out[s["stop_id"]] = by_id.get(s["stop_id"]) or by_name.get(s.get("stop_name", ""), "")
    return out


def convert_feed(zip_bytes: bytes, out_dir: str):
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    stops = read_table(zf, "stops.txt")
    routes = {r["route_id"]: r for r in read_table(zf, "routes.txt")}
    trips = {t["trip_id"]: t for t in read_table(zf, "trips.txt")}
    readings = stop_readings(zf, stops)

    # バス停を名前でまとめる（同じ名前の「のりば」違いを1つのバス停として扱う）
    groups = {}  # name -> {"ids": [], "reading": str}
    stop_info = {}
    for s in stops:
        if s.get("location_type") not in ("", "0", None):
            continue
        name = s.get("stop_name", "").strip()
        if not name:
            continue
        g = groups.setdefault(name, {"ids": [], "reading": ""})
        g["ids"].append(s["stop_id"])
        if not g["reading"] and readings.get(s["stop_id"]):
            g["reading"] = readings[s["stop_id"]]
        stop_info[s["stop_id"]] = s.get("platform_code") or ""  # のりば番号

    # 便ごとの最終停留所（終点では乗れないので表示しない）
    st_rows = read_table(zf, "stop_times.txt")
    last_seq = {}
    for r in st_rows:
        try:
            q = int(r.get("stop_sequence") or 0)
        except ValueError:
            q = 0
        r["_seq"] = q
        if q >= last_seq.get(r["trip_id"], -1):
            last_seq[r["trip_id"]] = q

    stop_to_group = {}
    ordered = sorted(groups.items(), key=lambda kv: (reading_of(kv[0], kv[1]["reading"]), kv[0]))
    stop_list = []
    for idx, (name, g) in enumerate(ordered):
        rd = reading_of(name, g["reading"])
        key = hashlib.md5(name.encode("utf-8")).hexdigest()[:10]  # データ更新しても変わらないID
        stop_list.append({"k": key, "n": name, "y": rd, "g": gyo_of(rd)})
        for sid in g["ids"]:
            stop_to_group[sid] = idx

    deps = {i: [] for i in range(len(stop_list))}
    for r in st_rows:
        gi = stop_to_group.get(r.get("stop_id"))
        trip = trips.get(r.get("trip_id"))
        if gi is None or not trip:
            continue
        if r["_seq"] == last_seq.get(r["trip_id"]) or r.get("pickup_type") == "1":
            continue
        sec = to_sec(r.get("departure_time")) or to_sec(r.get("arrival_time"))
        if sec is None:
            continue
        rt = routes.get(trip.get("route_id"), {})
        route_name = rt.get("route_short_name") or rt.get("route_long_name") or ""
        headsign = r.get("stop_headsign") or trip.get("trip_headsign") or rt.get("route_long_name") or ""
        deps[gi].append([sec, route_name, headsign, trip.get("service_id", ""), r["trip_id"],
                         r["_seq"], r["stop_id"]])

    # 運行日カレンダー
    cal = {"w": {}, "x": {}}
    for c in read_table(zf, "calendar.txt"):
        days = "".join(c.get(d, "0") for d in
                       ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))
        cal["w"][c["service_id"]] = [days, c.get("start_date", ""), c.get("end_date", "")]
    for c in read_table(zf, "calendar_dates.txt"):
        cal["x"].setdefault(c["service_id"], {})[c["date"]] = int(c.get("exception_type") or 0)

    os.makedirs(os.path.join(out_dir, "s"), exist_ok=True)
    dump(os.path.join(out_dir, "stops.json"), stop_list)
    dump(os.path.join(out_dir, "cal.json"), cal)
    for gi, rows in deps.items():
        rows.sort(key=lambda x: x[0])
        poles = {sid: stop_info[sid] for sid in ordered[gi][1]["ids"] if sid in stop_info}
        dump(os.path.join(out_dir, "s", f"{stop_list[gi]['k']}.json"), {"n": stop_list[gi]["n"], "p": poles, "d": rows})
    return len(stop_list)


def dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- 取得
def http_get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as res:
                return res.read()
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (i + 1))
    raise last


def company_name(f):
    org, feed = f.get("organization_name", ""), f.get("feed_name", "")
    if not feed or feed == org or feed in org:
        return org
    if org in feed:
        return feed
    return f"{org}（{feed}）"


def build_remote(out, prefs_filter, workers):
    feeds = json.loads(http_get(f"{API}/feeds"))["body"]
    feeds = [f for f in feeds if not f.get("feed_is_discontinued")]
    if prefs_filter:
        feeds = [f for f in feeds if int(f.get("feed_pref_id") or 0) in prefs_filter]
    print(f"{len(feeds)} feeds", flush=True)

    def job(f):
        cid = safe_id(f"{f['organization_id']}--{f['feed_id']}")
        url = f"{API}/organizations/{f['organization_id']}/feeds/{f['feed_id']}/files/feed.zip"
        n = convert_feed(http_get(url), os.path.join(out, "c", cid))
        return f, cid, n

    companies = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(job, f) for f in feeds]
        for fu in as_completed(futs):
            try:
                f, cid, n = fu.result()
            except Exception as e:
                print("  skip:", e, flush=True)
                continue
            if n == 0:
                continue
            rt = (f.get("real_time") or {})
            companies.append({
                "id": cid, "name": company_name(f), "pref": int(f.get("feed_pref_id") or 0),
                "rt": rt.get("trip_update_url") or "",
                "lic": f.get("feed_license") or "", "src": f.get("organization_web_url") or "",
            })
            print(f"  ok {cid} ({n} stops)", flush=True)
    return companies


def build_local(out, zip_path):
    cid = "sample--demo"
    with open(zip_path, "rb") as fh:
        n = convert_feed(fh.read(), os.path.join(out, "c", cid))
    return [{"id": cid, "name": "サンプル交通（デモ）", "pref": 13, "rt": os.environ.get("SAMPLE_RT", ""),
             "lic": "CC0", "src": ""}]


def pick(c):
    d = {k: c[k] for k in ("id", "name", "rt", "lic", "src")}
    d["y"] = reading_of(c["name"], "")  # よみがな（ひらがな検索用）
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--pref", type=int, nargs="*")
    ap.add_argument("--local")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    if os.path.isdir(a.out):
        shutil.rmtree(a.out)
    os.makedirs(a.out)
    companies = build_local(a.out, a.local) if a.local else build_remote(a.out, set(a.pref or []), a.workers)

    prefs = []
    for code, pname in enumerate(PREFS, start=1):
        cs = sorted([c for c in companies if c["pref"] == code], key=lambda c: reading_of(c["name"], ""))
        if cs:
            prefs.append({"code": code, "name": pname,
                          "companies": [pick(c) for c in cs]})
    others = [c for c in companies if not 1 <= c["pref"] <= 47]
    if others:
        prefs.append({"code": 99, "name": "その他", "companies":
                      [pick(c) for c in others]})
    dump(os.path.join(a.out, "index.json"),
         {"updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"), "prefs": prefs})
    print(f"done: {len(companies)} companies", flush=True)
    if not companies:
        sys.exit(1)


if __name__ == "__main__":
    main()
