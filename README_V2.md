# V2 実行手順（Colab + Googleドライブ）

最終検証期間(シグナル2011-01〜2015-11)は封印されています。dev用データには2016-06以降の株価しか入れません。
最終検証用の株価は、最終候補ルールを凍結・コミットするまで取得できません。

## 評価は二段階
- Tier1(主要な成功判定): 全銘柄ベースライン(ALL_GATE)に対して、事前登録した6基準を満たすか。
- Tier2(追加判定): Tier1を満たした場合にだけ評価。RSI単独(SIMPLE_RSI)に対しても同じ基準を満たすか。
  Tier1を満たしてTier2を満たさない場合は「V2自体には優位性があるが、RSI単独を明確には上回らない」と評価する。

## 今やること（dev環境の構築 → 凍結 → dev実行）
0. zipを上書き展開 → python tests/test_v2.py と python tests/test_v2_cli.py が全部通ること
1. dev用株価(2016-06〜):  python fetch_data_v2.py          （30〜60分・途中再開可。data/mcap.csv が必要）
2. SEC財務:               python fetch_sec.py              （30〜60分・途中再開可。SEC_USER_AGENT必須）
3. 成功基準の確認 → 凍結: config/criteria_v2.yaml を読んで、変えたい所があればこの時点が最後。
                          python freeze_v2.py
                          git add config/ && git commit -m "register v2 criteria and rules"
4. dev実行:               python run_v2.py --rules config/rules_v2.yaml --period dev --label v2a
   dev結果を見てルールを変えたい時は rules_v2b.yaml を新規作成(devのみ)。成功基準は変更不可。

## 後でやること（dev期間で最終候補が決まってから）
5. python freeze_v2.py --final-candidate config/rules_v2x.yaml  →  git add config/ && git commit
6. python fetch_data_v2.py --period final                      （凍結後にだけ実行できる）
7. python run_v2.py --rules config/rules_v2x.yaml --period final   （1回きり。使用済みロックが作られる）


---
# V2b（最後の開発版・登録済みの手順）  ※詳細: docs/v2b_preregistration.md
V2b = V2のQualityを「営業利益/総資産・アクルーアル・希薄化の同日順位平均(上位1/3=HIGH)」に差し替えたもの。他は同じ。
V2の事前登録基準は変更しない。V2bはV2結果を見た後の2回目のdev試行なので、V2bのc2だけ97.5%区間にする(追加の厳格化)。

1. コードを上書き展開し、テストを通す:  python tests/test_v2b.py / tests/test_v2b_cli.py / tests/test_v2.py / tests/test_v2_cli.py
2. SEC財務の再取得(純利益を追加。V2のデータ data_v2/sec は上書きしない):
      python fetch_sec.py --sec-dir sec_v2b          （約20分・途中再開可。SEC_USER_AGENT必須）
3. V2bを登録(1回だけ。以後 rules_v2b.yaml などは1文字も変更できず、V2c(3つ目のdev版)は拒否される):
      python freeze_v2.py --register-v2b
      git add config/ docs/ && git commit -m "register v2b"
4. V2b dev実行:
      python run_v2.py --rules config/rules_v2b.yaml --period dev --label v2b
5. 最終検証に進めるのは、V2bが dev で (a)(b)(c) をすべて満たした場合だけ(コードが機械的に検査する)。満たさなければ封印のまま終了。
