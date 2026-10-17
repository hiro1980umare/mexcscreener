# STEP4 実行手順（V1）

0. `pip install -r requirements.txt`
1. 【Finnhub再テスト】`python step4_finnhub_recheck.py --n 60`（要 FINNHUB_API_KEY）
2. 【データ取得】`python fetch_data.py --max-tickers 300`（お試し） → 問題なければ `python fetch_data.py`
3. 【テスト】`python tests/test_synthetic.py`
4. 【凍結】`git add config/rules_v1.yaml && git commit -m "freeze rules_v1" && git push`
5. 【devで動作確認】`python run_backtest.py --rules config/rules_v1.yaml --period dev`
6. 【最終検証・1回だけ】`python run_backtest.py --rules config/rules_v1.yaml --period final`
7. `results/v1_dev/report.md` と `results/v1_final/report.md` を貼る

ルールを変えたくなったら rules_v1.yaml は触らず rules_v2.yaml を作る。
