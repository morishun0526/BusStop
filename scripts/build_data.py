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
import urllib.error
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


def convert_feed(zip_bytes: bytes, out_dir: str, with_prev: bool = False):
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

    # 車両位置（VehiclePosition）から遅れを推定するため、手前の停留所の時刻差を持たせる
    trip_times = {}
    if with_prev:
        for r in st_rows:
            sec = to_sec(r.get("departure_time")) or to_sec(r.get("arrival_time"))
            if sec is not None:
                trip_times.setdefault(r["trip_id"], []).append((r["_seq"], sec))
        for v in trip_times.values():
            v.sort()

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
        row = [sec, route_name, headsign, trip.get("service_id", ""), r["trip_id"], r["_seq"], r["stop_id"]]
        if with_prev:
            prev = []
            for q, t in reversed(trip_times.get(r["trip_id"], [])):
                if q < r["_seq"]:
                    prev += [q, sec - t]
                    if len(prev) >= 24:  # 手前12停留所まで
                        break
            row.append(prev)
        deps[gi].append(row)

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
class FetchError(Exception):
    pass


def _mask(url):
    return re.sub(r"(consumerKey=)[^&]+", r"\1***", url)


def http_get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read()[:200].decode("utf-8", "replace")
            except Exception:
                pass
            last = FetchError(f"HTTP {e.code} {_mask(url)} {body}")
            if e.code in (400, 401, 403, 404):  # 再試行しても変わらない
                break
        except Exception as e:  # noqa
            last = FetchError(f"{type(e).__name__}: {e} {_mask(url)}")
        time.sleep(3 * (i + 1))
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
        vp = bool((f.get("real_time") or {}).get("vehicle_position_url"))
        n = convert_feed(http_get(url), os.path.join(out, "c", cid), with_prev=vp)
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
                "id": cid, "name": company_name(f), "prefs": [int(f.get("feed_pref_id") or 0)],
                "rt": rt.get("trip_update_url") or "", "vp": rt.get("vehicle_position_url") or "",
                "lic": f.get("feed_license") or "", "src": f.get("organization_web_url") or "",
            })
            print(f"  ok {cid} ({n} stops)", flush=True)
    return companies


def build_local(out, zip_path):
    cid = "sample--demo"
    with open(zip_path, "rb") as fh:
        n = convert_feed(fh.read(), os.path.join(out, "c", cid))
    return [{"id": cid, "name": "サンプル交通（デモ）", "prefs": [13], "rt": os.environ.get("SAMPLE_RT", ""),
             "vp": "", "lic": "CC0", "src": ""}]


# ---------------------------------------------------------------- 追加データ（scripts/sources.json）
def build_sources(out, path, existing_ids):
    """都営バス（GTFS）・東急バス（ODPT形式）・公式リンクのみの会社などを追加する"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        sources = json.load(fh)
    res = []
    for src in sources:
        sid = src["id"]
        if sid in existing_ids:
            continue
        STATUS[sid] = {"name": src["name"], "ok": False, "msg": ""}
        base = {"id": sid, "name": src["name"], "prefs": src.get("prefs", []), "rt": src.get("rt", ""),
                "vp": src.get("vp", ""), "lic": src.get("lic", ""), "src": src.get("src", ""),
                "note": src.get("note", "")}
        try:
            if src["type"] == "link":  # データ非公開 → 公式の接近情報ページへのリンクだけ置く
                res.append({**base, "ext": src["url"]})
                STATUS[sid].update(ok=True, msg="公式リンク")
                print(f"  link {sid}", flush=True)
                continue
            if src["type"] == "gtfs":
                z = http_get(src["url"].replace("{ODPT_KEY}", os.environ.get("ODPT_KEY", "")))
            elif src["type"] == "odpt":
                key = os.environ.get("ODPT_KEY", "")
                if not key:
                    msg = "ODPT_KEY が未設定です（GitHub の Secrets に登録してください）"
                    STATUS[sid]["msg"] = msg
                    print(f"  skip {sid}: {msg}", flush=True)
                    continue
                z = odpt_to_gtfs(key, src["operator"])
            else:
                continue
            n = convert_feed(z, os.path.join(out, "c", sid), with_prev=bool(base["vp"]))
            if n:
                res.append(base)
                STATUS[sid].update(ok=True, msg=f"{n} バス停")
                print(f"  ok {sid} ({n} stops)", flush=True)
            else:
                STATUS[sid]["msg"] = "バス停が0件でした"
                print(f"  skip {sid}: 0 stops", flush=True)
        except Exception as e:
            STATUS[sid]["msg"] = str(e)[:300]
            print(f"  skip {sid}: {e}", flush=True)
    return res


STATUS = {}  # 追加データの取り込み結果（data/status.json に出力し、アプリにも表示）


ODPT_API = os.environ.get("ODPT_API", "https://api.odpt.org/api/v4")


def odpt_get(key, path, **params):
    from urllib.parse import urlencode
    q = urlencode({**params, "acl:consumerKey": key}, safe=":")  # ODPT の書式どおり「:」はそのまま
    return json.loads(http_get(f"{ODPT_API}/{path}?{q}"))


def _has_operator(r, operator):
    op = r.get("odpt:operator")
    return operator in (op if isinstance(op, list) else [op])


def odpt_all(key, typ, operator):
    """事業者で絞って取得。失敗・件数上限のときはダンプ（全件）から絞り込む。"""
    rows, err = [], None
    try:
        rows = odpt_get(key, typ, **{"odpt:operator": operator})
        print(f"    {typ}: {len(rows)}件", flush=True)
    except Exception as e:
        err = e
        print(f"    {typ}: 検索APIで失敗 ({e})", flush=True)
    if err or len(rows) >= 1000:
        try:
            dumped = odpt_get(key, typ + ".json")
            full = [r for r in dumped if _has_operator(r, operator)]
            print(f"    {typ}: ダンプから {len(full)}件", flush=True)
            if len(full) >= len(rows):
                rows = full
        except Exception as e:
            print(f"    {typ}: ダンプも失敗 ({e})", flush=True)
            if err:
                raise err
    return rows


def jp_holidays(years):
    try:
        import holidays
        return {d.strftime("%Y%m%d") for y in years for d in holidays.Japan(years=y)}
    except Exception:
        return set()


def odpt_calendar_dates(key, cal_ids):
    """ODPT の曜日区分（平日・土曜・休日…）を、具体的な日付の一覧にする"""
    today = datetime.now(JST).date()
    days = [today + timedelta(days=i) for i in range(-2, 400)]
    hol = jp_holidays({d.year for d in days})
    specific = {}
    if any("Specific" in c for c in cal_ids):
        try:
            for c in odpt_get(key, "odpt:Calendar"):
                if c.get("owl:sameAs") in cal_ids:
                    specific[c["owl:sameAs"]] = {d.replace("-", "") for d in c.get("odpt:day", [])}
        except Exception as e:
            print("    calendar:", e, flush=True)
    rows = []
    for cid in cal_ids:
        kind = cid.split(":")[-1]
        for d in days:
            ymd = d.strftime("%Y%m%d")
            wd, h = d.weekday(), ymd in hol or (d.month == 1 and d.day <= 3) or (d.month == 12 and d.day >= 30)
            on = {
                "Weekday": wd < 5 and not h,
                "Saturday": wd == 5 and not h,
                "Holiday": wd == 6 or h,
                "Sunday": wd == 6,
                "SaturdayHoliday": wd >= 5 or h,
                "SundayHoliday": wd == 6 or h,
                "Everyday": True,
            }.get(kind)
            if on is None:
                on = ymd in specific.get(cid, set())
            if on:
                rows.append([cid, ymd, 1])
    return rows


def odpt_to_gtfs(key, operator):
    """ODPT 形式（バス停・系統・時刻表）を GTFS の zip に組み替える"""
    poles = odpt_all(key, "odpt:BusstopPole", operator)
    patterns = odpt_all(key, "odpt:BusroutePattern", operator)
    print(f"    poles={len(poles)} patterns={len(patterns)}", flush=True)

    if not poles:
        raise FetchError("バス停（BusstopPole）が取得できませんでした")

    # 時刻表: まず事業者でまとめて取得し、上限に当たりそうなら系統ごとに取得
    timetables, fails = [], []
    try:
        timetables = odpt_get(key, "odpt:BusTimetable", **{"odpt:operator": operator})
        print(f"    BusTimetable(事業者一括): {len(timetables)}件", flush=True)
    except Exception as e:
        print(f"    BusTimetable(事業者一括): 失敗 ({e})", flush=True)
    if len(timetables) >= 1000 or not timetables:
        def fetch_tt(pid):
            try:
                return odpt_get(key, "odpt:BusTimetable", **{"odpt:busroutePattern": pid})
            except Exception as e:
                fails.append(str(e))
                return []
        by_pattern = []
        with ThreadPoolExecutor(max_workers=3) as ex:
            for rows in ex.map(fetch_tt, [p["owl:sameAs"] for p in patterns]):
                by_pattern += rows
        print(f"    BusTimetable(系統ごと): {len(by_pattern)}件、失敗 {len(fails)}系統"
              + (f"（例: {fails[0]}）" if fails else ""), flush=True)
        if len(by_pattern) >= len(timetables):
            timetables = by_pattern
    if not timetables:
        raise FetchError("時刻表（BusTimetable）が0件でした" + (f": {fails[0]}" if fails else ""))
    print(f"    timetables={len(timetables)}", flush=True)

    pole_name = {}
    stops = [["stop_id", "stop_name", "platform_code"]]
    trans = [["trans_id", "lang", "translation"]]
    seen_kana = set()
    for p in poles:
        pid, name = p["owl:sameAs"], (p.get("dc:title") or "").strip()
        if not name:
            continue
        pole_name[pid] = name
        stops.append([pid, name, p.get("odpt:platformNumber") or ""])
        kana = p.get("odpt:kana") or (p.get("odpt:busstopPoleTitle") or p.get("title") or {}).get("ja-Hrkt", "")
        if kana and name not in seen_kana:
            seen_kana.add(name)
            trans.append([name, "ja-Hrkt", kana])

    routes = [["route_id", "route_short_name", "route_long_name"]]
    for p in patterns:
        title = (p.get("dc:title") or "").strip()
        short = (p.get("odpt:busroute") or "").split(".")[-1] if not title else title.split()[0]
        routes.append([p["owl:sameAs"], short, title])

    trips = [["route_id", "service_id", "trip_id", "trip_headsign"]]
    st = [["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type"]]
    cal_ids = set()
    for t in timetables:
        objs = sorted(t.get("odpt:busTimetableObject") or [], key=lambda o: o.get("odpt:index", 0))
        if not objs:
            continue
        tid, cal = t["owl:sameAs"], t.get("odpt:calendar") or "odpt.Calendar:Everyday"
        cal_ids.add(cal)
        head = next((o.get("odpt:destinationSign") for o in objs if o.get("odpt:destinationSign")), "") \
            or pole_name.get(objs[-1].get("odpt:busstopPole"), "")
        trips.append([t.get("odpt:busroutePattern", ""), cal, tid, head])
        prev = -1
        for i, o in enumerate(objs):
            tm = o.get("odpt:departureTime") or o.get("odpt:arrivalTime")
            if not tm:
                continue
            h, m = map(int, tm.split(":")[:2])
            sec = h * 3600 + m * 60
            if o.get("odpt:isMidnight") and h < 12:
                sec += 86400
            if sec < prev:  # 日付をまたいだのにフラグが無い場合の保険
                sec += 86400
            prev = sec
            hhmmss = f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:00"
            pick_up = "1" if o.get("odpt:canGetOn") is False else "0"
            st.append([tid, hhmmss, hhmmss, o.get("odpt:busstopPole", ""), i + 1, pick_up])

    cal_dates = [["service_id", "date", "exception_type"]] + odpt_calendar_dates(key, cal_ids)

    def to_csv(rows):
        b = io.StringIO()
        csv.writer(b).writerows(rows)
        return b.getvalue()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, rows in [("stops.txt", stops), ("routes.txt", routes), ("trips.txt", trips),
                           ("stop_times.txt", st), ("calendar_dates.txt", cal_dates), ("translations.txt", trans)]:
            z.writestr(name, to_csv(rows))
    return buf.getvalue()


def build_search_index(out, companies):
    """全国のバス停をまとめた検索用ファイル（search.json）"""
    cos, rows = [], []
    for c in companies:
        if c.get("ext"):
            continue
        path = os.path.join(out, "c", c["id"], "stops.json")
        if not os.path.exists(path):
            continue
        ci = len(cos)
        pref = "・".join(PREFS[p - 1] for p in c["prefs"] if 1 <= p <= 47)
        cos.append([c["id"], c["name"], pref])
        with open(path, encoding="utf-8") as fh:
            for st in json.load(fh):
                rows.append([st["n"], st["y"], ci, st["k"]])
    rows.sort(key=lambda r: (r[1], r[0]))
    dump(os.path.join(out, "search.json"), {"c": cos, "s": rows})
    print(f"search index: {len(rows)} stops", flush=True)


def pick(c):
    d = {k: c.get(k, "") for k in ("id", "name", "rt", "vp", "lic", "src", "ext", "note")}
    d = {k: v for k, v in d.items() if v or k in ("id", "name")}
    d["y"] = reading_of(c["name"], "")  # よみがな（ひらがな検索用）
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--pref", type=int, nargs="*")
    ap.add_argument("--local")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sources", default=os.path.join(os.path.dirname(__file__), "sources.json"))
    ap.add_argument("--no-repo", action="store_true", help="GTFSデータリポジトリを取り込まない（テスト用）")
    a = ap.parse_args()

    if os.path.isdir(a.out):
        shutil.rmtree(a.out)
    os.makedirs(a.out)
    if a.local:
        companies = build_local(a.out, a.local)
    else:
        companies = [] if a.no_repo else build_remote(a.out, set(a.pref or []), a.workers)
        companies += build_sources(a.out, a.sources, {c["id"] for c in companies})

    prefs = []
    for code, pname in enumerate(PREFS, start=1):
        cs = sorted([c for c in companies if code in c["prefs"]], key=lambda c: reading_of(c["name"], ""))
        if cs:
            prefs.append({"code": code, "name": pname,
                          "companies": [pick(c) for c in cs]})
    others = [c for c in companies if not any(1 <= p <= 47 for p in c["prefs"])]
    if others:
        prefs.append({"code": 99, "name": "その他", "companies":
                      [pick(c) for c in others]})
    dump(os.path.join(a.out, "index.json"),
         {"updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"), "prefs": prefs})
    build_search_index(a.out, companies)
    dump(os.path.join(a.out, "status.json"), STATUS)
    print(f"done: {len(companies)} companies", flush=True)
    if not companies:
        sys.exit(1)


if __name__ == "__main__":
    main()
