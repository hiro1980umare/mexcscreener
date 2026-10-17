# 通知Bot(候補リスト・監視ツール) 手順書

優位性は研究で確認されていません(docs/research_report.md)。「勝てる銘柄」ではなく、条件に該当した銘柄を根拠つきで一覧する監視ツールです。

## 実データで確認する(Colab・ネットワークあり。送信はしない)
1. 対象銘柄リスト:   python export_universe.py            → config/bot_universe.csv ができる(data_v2/coverage.csv から)
2. 凍結(1回だけ):   python freeze_bot.py
                      git add config/ docs/ && git commit -m "register bot manifest"
3. 試運転(送信しない): python run_bot.py --source dir --data-dir data_v2 --asof <データの最終日> --today <同じ日> --dry-run --no-earnings
4. 正確性の自己検査:   python bot_selfcheck.py --data-dir data_v2                  (再計算が一致するか。ネットワーク不要)
                      python bot_selfcheck.py --data-dir data_v2 --network        (Yahooの終値・決算日の2情報源と照合)
5. 決算日つきの試運転: python run_bot.py ... --dry-run   (FINNHUB_API_KEY を環境変数に入れると、2情報源で照合)

## Telegram
- @BotFather で Bot を作成 → トークンを控える。作ったBotに何かメッセージを送り、
  https://api.telegram.org/bot<トークン>/getUpdates を開いて chat の id を控える。
- 接続確認: TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python telegram_test.py
- ★トークンはコードや画面に貼らない(環境変数 / GitHub Secrets)。

## 自動実行(GitHub Actions)
- Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, FINNHUB_API_KEY, SEC_USER_AGENT
- 先に weekly_sec.yml を手動実行(SEC財務のキャッシュを作る。約20分)。次に daily_bot.yml を dry_run=true で手動実行。
- 毎日の記録(bot_records/)はリポジトリにコミットされる。

## 後から成績を確認する(月1回など)
  python track_forward.py --records bot_records           → bot_records/forward_returns.csv と review_summary.md
  ※件数が少ないうちは数字に意味はない。研究の基準は適用しない参考値。

## 守られる仕組み
- 報告書・ルール・基準・Bot仕様は freeze_bot.py のハッシュで凍結。1文字でも変わると実行しない(終了コード2)。
- 禁止表現(勝てる/高確率/おすすめ 等)が通知文に出たら送信しない(終了コード4)。
- 記録は送信より前に保存。送信に失敗しても記録は残る(終了コード5)。同じ判定日は二重に通知しない。
- 通知は銘柄ブロックの途中で切らずに分割し、2通以上のときは先頭に `[1/3]` が付く。1通目の冒頭に、実行日・候補数・データ品質の注意事項を表示する。
- 並びはTimingスコアの降順 → Quality順位(良い順。判定不能は最後) → ティッカー順。同点の銘柄には「同点N銘柄」と表示する(同じ区分の中の数)。

## 送信に失敗したとき(未送信分の再送)
- 記録 `bot_records/<判定日>/` に、送る文面(`message_parts.json`)と通ごとの送信状況(`meta.json` の `send_parts` / `send_status`)が残る。
  `send_status` は `sent`(全て送信済み)/ `partial: 1/3通送信済み; …`(一部)/ `failed: …`(1通も届いていない)。
- 未送信の確認:  python resend_bot.py --dry-run
- 未送信分だけ再送: python resend_bot.py          (送信済みの通は送らない。文面は作り直さず、保存した文面をそのまま送る)
- 同じ判定日に run_bot.py を再実行しても、再計算や二重送信はしない。未送信が残っていれば、その旨を表示して終了コード5で止まる。
- 注意: 通信エラーで「届いたか分からない」場合は、再送で同じ通が2回届くことがある(先頭の `[k/n]` で見分けられる)。
- 株価が古い(5日超)ときは通知しない(終了コード3)。取れない情報は「判定不能」「取得できず」と表示する。
