#!/bin/sh
# 手元でテスト用の架空データを作り、http://localhost:8765 で確認する
cd "$(dirname "$0")/.." || exit 1
python3 test/make_sample.py test/sample.zip
python3 test/test_sources.py demo
cd web && python3 -m http.server 8765
