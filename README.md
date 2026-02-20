# nara_minutes
奈良市議会の議事録を取得する

## 概要

[奈良市議会議事録](https://ssp.kaigiroku.net/tenant/narashi/SpTop.html) から議事録を取得するPythonスクリプトです。

このサイトはJavaScriptで動的にコンテンツを生成しており、単純なHTTPリクエストでは各会議へのリンクを取得できません。そのため、[Playwright](https://playwright.dev/python/) を使用してブラウザを自動操作しています。

## インストール

```bash
pip install -r requirements.txt
playwright install chromium
```

## 使い方

```bash
# 最新5件の議事録を取得（デフォルト）
python fetch_nara_minutes.py

# 最新10件を取得
python fetch_nara_minutes.py --max 10

# すべての議事録を取得
python fetch_nara_minutes.py --all

# 出力先ディレクトリを指定
python fetch_nara_minutes.py --output ./data

# ブラウザを表示して実行（デバッグ用）
python fetch_nara_minutes.py --no-headless
```

## 出力

- `nara_minutes.json` : 取得した議事録データ（JSON形式）
- `nara_minutes.csv` : 会議一覧（CSV形式、Excel対応）
- `minutes_<council_id>_<schedule_id>_<会議名>.txt` : 各会議の議事録テキスト

## 依存ライブラリ

- `playwright` : ブラウザ自動操作（JavaScript対応サイトのスクレイピングに使用）
- `beautifulsoup4` : HTMLの解析と発言者ごとの内容抽出
