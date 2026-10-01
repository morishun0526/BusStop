"""追加データ（都営=GTFS、東急=ODPT形式、大阪シティバス=リンク）の取り込みを、疑似データで確認する"""
import json, os, sys, tempfile
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import build_data as b  # noqa

POLES = [
    {"owl:sameAs": "odpt.BusstopPole:TokyuBus.Shibuya.1", "dc:title": "渋谷駅", "odpt:kana": "シブヤエキ", "odpt:platformNumber": "53", "odpt:operator": ["odpt.Operator:TokyuBus"]},
    {"owl:sameAs": "odpt.BusstopPole:TokyuBus.Shibuya.2", "dc:title": "渋谷駅", "odpt:kana": "シブヤエキ", "odpt:platformNumber": "54", "odpt:operator": ["odpt.Operator:TokyuBus"]},
    {"owl:sameAs": "odpt.BusstopPole:TokyuBus.Daikan.1", "dc:title": "代官山", "odpt:kana": "ダイカンヤマ", "odpt:operator": ["odpt.Operator:TokyuBus"]},
    {"owl:sameAs": "odpt.BusstopPole:TokyuBus.Meguro.1", "dc:title": "目黒駅", "odpt:kana": "メグロエキ", "odpt:operator": ["odpt.Operator:TokyuBus"]},
]
PATTERNS = [{"owl:sameAs": "odpt.BusroutePattern:TokyuBus.Shibu72.1", "dc:title": "渋72 渋谷駅→目黒駅"}]


def tt(i, h, m, cal, midnight=False):
    objs = []
    for k, pole in enumerate(["Shibuya.1", "Daikan.1", "Meguro.1"]):
        mm = m + k * 7
        hh = (h + mm // 60) % 24
        objs.append({"odpt:index": k, "odpt:busstopPole": "odpt.BusstopPole:TokyuBus." + pole,
                     "odpt:departureTime": f"{hh:02d}:{mm % 60:02d}", "odpt:isMidnight": midnight and hh < 12,
                     "odpt:destinationSign": "目黒駅"})
    return {"owl:sameAs": f"odpt.BusTimetable:TokyuBus.Shibu72.{i}", "odpt:calendar": cal,
            "odpt:busroutePattern": PATTERNS[0]["owl:sameAs"], "odpt:busTimetableObject": objs}


TIMETABLES = [tt(1, 7, 0, "odpt.Calendar:Weekday"), tt(2, 7, 0, "odpt.Calendar:SaturdayHoliday"),
              tt(3, 23, 55, "odpt.Calendar:Weekday", midnight=True)]


def fake_get(url, tries=3):
    if url.startswith("file://"):
        return open(url[7:], "rb").read()
    u = urlparse(url)
    q = parse_qs(u.query)
    assert q.get("acl:consumerKey") == ["TESTKEY"], url
    typ = u.path.split("/")[-1]
    data = {"odpt:BusstopPole": POLES, "odpt:BusroutePattern": PATTERNS, "odpt:BusTimetable": TIMETABLES,
            "odpt:Calendar": []}[typ]
    return json.dumps(data).encode()


b.http_get = fake_get
os.environ["ODPT_KEY"] = "TESTKEY"
out = tempfile.mkdtemp()
src = os.path.join(out, "sources.json")
json.dump([
    {"type": "gtfs", "id": "toei--bus", "name": "都営バス", "prefs": [13], "url": "file://" + os.path.join(HERE, "sample.zip"),
     "vp": "x", "lic": "CC BY 4.0"},
    {"type": "odpt", "id": "tokyu--bus", "name": "東急バス", "prefs": [13, 14], "operator": "odpt.Operator:TokyuBus"},
    {"type": "link", "id": "osaka-citybus", "name": "大阪シティバス", "prefs": [27], "url": "https://oc.bus-vision.jp/osakacitybus/"},
], open(src, "w"))
cs = b.build_sources(out, src, set())
print([(c["id"], c.get("ext", "")) for c in cs])
stops = json.load(open(f"{out}/c/tokyu--bus/stops.json"))
print("tokyu stops:", [(s["n"], s["y"], s["g"]) for s in stops])
shibuya = next(s for s in stops if s["n"] == "渋谷駅")
d = json.load(open(f"{out}/c/tokyu--bus/s/{shibuya['k']}.json"))
print("渋谷駅 poles:", d["p"]); print("渋谷駅 deps:", d["d"])
cal = json.load(open(f"{out}/c/tokyu--bus/cal.json"))
for k, v in cal["x"].items():
    print(k, "dates:", len(v), sorted(v)[:3])
meguro = next(s for s in stops if s["n"] == "目黒駅")
print("目黒駅(終点) deps:", json.load(open(f"{out}/c/tokyu--bus/s/{meguro['k']}.json"))["d"])
# 都営 (GTFS) は手前停留所の時刻差つき
ts = json.load(open(f"{out}/c/toei--bus/stops.json"))
s0 = next(s for s in ts if s["n"] == "青葉台")
print("都営 青葉台 dep sample:", json.load(open(f"{out}/c/toei--bus/s/{s0['k']}.json"))["d"][1])

# ---- デモ用サイトデータ（web/data）を作る: python test/test_sources.py demo
if len(sys.argv) > 1 and sys.argv[1] == "demo":
    import time
    import make_sample as ms
    web = os.path.join(HERE, "..", "web")
    # 都営（サンプル）の車両位置: 全便が「2番目の停留所に停車中」= 予定より遅れているケースを作る
    now = int(time.time())
    open(os.path.join(web, "vp-sample.pb"), "wb").write(ms.vp([(f"R1_{n}", 2, 1, now) for n in range(200)]))
    json.dump([
        {"type": "gtfs", "id": "toei--bus", "name": "都営バス（テスト）", "prefs": [13],
         "url": "file://" + os.path.join(HERE, "sample.zip"), "vp": "vp-sample.pb", "lic": "CC BY 4.0", "src": "東京都交通局"},
        {"type": "odpt", "id": "tokyu--bus", "name": "東急バス（テスト）", "prefs": [13, 14], "operator": "odpt.Operator:TokyuBus",
         "lic": "公共交通オープンデータ基本ライセンス", "src": "東急バス", "note": "時刻表から計算しています（リアルタイムの遅れは反映されません）"},
        {"type": "link", "id": "osaka-citybus", "name": "大阪シティバス", "prefs": [27], "url": "https://oc.bus-vision.jp/osakacitybus/",
         "note": "時刻表データが公開されていないため、公式の接近情報サービスを開きます"},
    ], open(src, "w"), ensure_ascii=False)
    sys.argv = ["build_data.py", "--no-repo", "--sources", src, "--out", os.path.join(web, "data")]
    b.main()
