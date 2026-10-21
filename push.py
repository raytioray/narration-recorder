#!/usr/bin/env python3
"""把斷好的稿子直接送進逐句錄音（給 Claude Code 等 AI 工具用）。
用法：python3 push.py "標題" lines.txt [--script 原稿檔]   （lines.txt 一行一句）
工具網址預設 http://127.0.0.1:8780，可用環境變數 NR_URL 改
斷句規則（給 AI）：照語意與換氣點切，一句一口氣（約 8 到 18 字）；不切開數字、英文詞、專有名詞；
拿掉〔出處〕【示意】（自算）這類標記；保留原本標點。
"""
import json, os, sys, urllib.request
title, lines_file = sys.argv[1], sys.argv[2]
script = open(sys.argv[sys.argv.index("--script") + 1], encoding="utf-8").read() if "--script" in sys.argv else ""
lines = [l.strip() for l in open(lines_file, encoding="utf-8") if l.strip()]
req = urllib.request.Request(os.environ.get("NR_URL", "http://127.0.0.1:8780").rstrip("/") + "/api/projects", method="POST",
                             data=json.dumps({"title": title, "sentences": lines, "script": script}).encode(),
                             headers={"X-NR": "1", "Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req))["slug"], len(lines), "句")
