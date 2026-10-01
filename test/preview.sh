#!/bin/sh
# 手元でテスト用データを作り、http://localhost:8765 で確認する
cd "$(dirname "$0")/.." || exit 1
python3 test/make_sample.py test/sample.zip web/rt-sample.pb
SAMPLE_RT=rt-sample.pb python3 scripts/build_data.py --out web/data --local test/sample.zip
cd web && python3 -m http.server 8765
