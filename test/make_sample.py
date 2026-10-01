"""テスト用の架空GTFS（サンプル交通）と、GTFS-RT（遅延情報）を作る"""
import io, zipfile, struct, sys, time

stops = [  # id, name, kana(None=読み無し), pole
    ("S1", "市役所前", "しやくしょまえ", "1"), ("S1b", "市役所前", "しやくしょまえ", "2"),
    ("S2", "中央駅", "ちゅうおうえき", "3"), ("S3", "桜町", None, ""),
    ("S4", "青葉台", "あおばだい", ""), ("S5", "緑ヶ丘", "みどりがおか", ""),
    ("S6", "かもめ公園", "かもめこうえん", ""), ("S7", "総合病院", "そうごうびょういん", ""),
    ("S8", "港南車庫", "こうなんしゃこ", ""), ("S9", "わかば台", "わかばだい", ""),
]
routes = [("R1", "1", "中央駅〜港南車庫", ["S2", "S1", "S3", "S4", "S8"], 12, "港南車庫"),
          ("R2", "2", "中央駅〜総合病院", ["S2", "S1b", "S5", "S6", "S7"], 20, "総合病院"),
          ("R3", "3", "わかば台循環", ["S9", "S4", "S6", "S2"], 30, "中央駅")]

def csv(rows):
    return "\n".join(",".join(map(str, r)) for r in rows) + "\n"

def build():
    st = [["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]]
    tr = [["route_id", "service_id", "trip_id", "trip_headsign"]]
    for rid, short, long_, seq, every, head in routes:
        t = 5 * 3600 + 30 * 60
        n = 0
        while t < 25 * 3600:
            tid = f"{rid}_{n}"
            tr.append([rid, "ALL", tid, head])
            for i, s in enumerate(seq):
                tt = t + i * 4 * 60
                hh = f"{tt//3600:02d}:{tt%3600//60:02d}:00"
                st.append([tid, hh, hh, s, i + 1])
            t += every * 60; n += 1
    files = {
        "agency.txt": csv([["agency_id", "agency_name", "agency_url", "agency_timezone"],
                           ["A", "サンプル交通", "https://example.com", "Asia/Tokyo"]]),
        "stops.txt": csv([["stop_id", "stop_name", "stop_lat", "stop_lon", "platform_code"]] +
                         [[i, n, 35.0, 139.0, p] for i, n, k, p in stops]),
        "routes.txt": csv([["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"]] +
                          [[r[0], "A", r[1], r[2], 3] for r in routes]),
        "trips.txt": csv(tr), "stop_times.txt": csv(st),
        "calendar.txt": csv([["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                              "sunday", "start_date", "end_date"], ["ALL", 1, 1, 1, 1, 1, 1, 1, 20260101, 20271231]]),
        "translations.txt": csv([["trans_id", "lang", "translation"]] +
                                [[n, "ja-Hrkt", k] for i, n, k, p in stops if k]),
    }
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, ("﻿" + v).encode("utf-8"))
    return b.getvalue()

# ---- 最小のprotobufエンコーダ（GTFS-RT TripUpdate）
def varint(n):
    out = b""
    n &= (1 << 64) - 1
    while True:
        b7 = n & 0x7F; n >>= 7
        if n: out += bytes([b7 | 0x80])
        else: return out + bytes([b7])
def fld(no, wt, payload):
    key = varint((no << 3) | wt)
    if wt == 0: return key + varint(payload)
    return key + varint(len(payload)) + payload
def s(no, txt): return fld(no, 2, txt.encode())

def rt(updates):
    """updates: list of (trip_id, stop_seq, delay_sec)"""
    hdr = s(1, "2.0") + fld(3, 0, int(time.time()))
    msg = fld(1, 2, hdr)
    for i, (tid, seq, delay) in enumerate(updates):
        stu = fld(1, 0, seq) + fld(2, 2, fld(1, 0, delay))
        tu = fld(1, 2, s(1, tid)) + fld(2, 2, stu)
        msg += fld(2, 2, s(1, f"e{i}") + fld(3, 2, tu))
    return msg

if __name__ == "__main__":
    open(sys.argv[1], "wb").write(build())
    if len(sys.argv) > 2:
        # 全便に対し 1番目の停留所で +180秒(3分遅れ) を入れる
        ups = [(f"R1_{n}", 1, 180) for n in range(200)]
        open(sys.argv[2], "wb").write(rt(ups))


def vp(entries):
    """entries: list of (trip_id, current_stop_sequence, status, timestamp)  status 1=STOPPED_AT 2=IN_TRANSIT_TO"""
    msg = fld(1, 2, s(1, "2.0") + fld(3, 0, int(time.time())))
    for i, (tid, seq, st, ts) in enumerate(entries):
        v = fld(1, 2, s(1, tid)) + fld(3, 0, seq) + fld(4, 0, st) + fld(5, 0, ts)
        msg += fld(2, 2, s(1, f"v{i}") + fld(4, 2, v))
    return msg
