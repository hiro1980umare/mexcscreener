# STEP4b: データ修正 → v1をdevだけ再実行 → 修正前後を比較

ルール(config/rules_v1.yaml)は変更しません。sha256が凍結時と一致しないと実行が止まります。

1. 退避:  cp -r results results_before ; mv data data_before
2. 新コードを上書き: unzip -o us_stock_bot_datafix.zip
3. mcapキャッシュ再利用: mkdir data ; cp data_before/mcap.csv data/
4. 取得:  python fetch_data.py          -> data/fetch_report.md を確認
5. 監査:  python audit_data.py          -> data/audit/audit_report.md を確認（データは変更されない）
6. 再実行: python run_backtest.py --rules config/rules_v1.yaml --period dev --label datafix1 --reason "データ修正によるv1再実行"
7. 比較:  python compare_runs.py --before results_before/v1_dev --after results/v1_dev_datafix1

異常値は自動で除外・補正されません。原因確認後に data/audit/approved_fixes.csv を書いた場合のみ適用されます。
final期間は再実行できません（ロック済み）。
