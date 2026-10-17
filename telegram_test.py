#!/usr/bin/env python3
"""Telegramの接続確認(本番の通知とは別。銘柄の情報は含まない)。
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python telegram_test.py
"""
import os
import sys

from src.tgram import TelegramError, send_message

try:
    ids = send_message(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""),
                       "接続確認です。この通知に銘柄の情報はありません。(米国株 候補リストBot)")
    print(f"送信できました(message_id={ids})。")
except TelegramError as e:
    sys.exit(f"送信できませんでした: {e}")
