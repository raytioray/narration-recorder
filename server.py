"""逐句錄音（narration-recorder）：貼逐字稿 → 斷句 → 一句一句錄音。
執行：python3 server.py  → 瀏覽器打開 http://localhost:8780
設定（環境變數，都可以不設）：
  NR_DATA  錄音存放資料夾（預設：這個檔案旁邊的 recordings/）
  NR_PORT  連接埠（預設 8780）
  NR_OPEN  設成 0 就不自動打開瀏覽器
  NR_HOSTS 額外允許的網址主機名稱，逗號分隔（預設只允許 localhost、127.0.0.1）
只聽本機 127.0.0.1；錄音只存在你自己的電腦。
安全：只接受網址主機是 localhost 的請求（擋 DNS 綁架）；跨網站請求一律拒絕；
寫入要帶 X-NR 標頭（別的網站的網頁送不出這個標頭）；上傳上限 200MB。
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.expanduser(os.environ.get("NR_DATA") or os.path.join(HERE, "recordings")))
PORT = int(os.environ.get("NR_PORT") or 8780)
FFMPEG = shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), None)
GAP = 0.35  # 合併時句與句之間的停頓（秒）
MAX_UPLOAD = 200 * 1024 * 1024
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"} | {
    h.strip().lower() for h in os.environ.get("NR_HOSTS", "").split(",") if h.strip()}
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' blob:; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")


def host_of(value):
    v = (value or "").strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1].split("/", 1)[0]
    return v.split("]")[0] + "]" if v.startswith("[") else v.rsplit(":", 1)[0]


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def export(slug, kind, gap=GAP):
    d = pdir(slug)
    p = load(slug)
    done = [x for x in p["sentences"] if x.get("file")]
    if not done:
        raise ValueError("還沒有錄好的句子")
    if kind == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for x in done:
                z.write(os.path.join(d, x["file"]), x["file"])
            z.writestr("句子.txt", "\n".join(f"{x['file']}\t{x['text']}" for x in done))
        return buf.getvalue(), "application/zip", ".zip"
    if kind == "srt":
        t, out = 0.0, []
        for i, x in enumerate(done, 1):
            out.append(f"{i}\n{srt_time(t)} --> {srt_time(t + x['dur'])}\n{x['text']}\n")
            t += x["dur"] + gap
        return "\n".join(out).encode("utf-8"), "application/x-subrip", ".srt"
    # 合併 wav：依序接起來、中間留停頓，再用 ffmpeg 拉到 -16 LUFS
    raw = io.BytesIO()
    with wave.open(raw, "wb") as w:
        for i, x in enumerate(done):
            with wave.open(os.path.join(d, x["file"]), "rb") as r:
                if i == 0:
                    w.setparams(r.getparams())
                    rate, width = r.getframerate(), r.getsampwidth()
                w.writeframes(r.readframes(r.getnframes()))
            if i < len(done) - 1:
                w.writeframes(b"\x00" * int(rate * gap) * width)
    if not FFMPEG:  # 沒裝 ffmpeg：直接給接好的原始音量檔
        return raw.getvalue(), "audio/wav", ".wav"
    res = subprocess.run([FFMPEG, "-v", "error", "-f", "wav", "-i", "pipe:0", "-af", "loudnorm=I=-16:TP=-1.5",
                          "-ar", "48000", "-f", "wav", "pipe:1"], input=raw.getvalue(), capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode()[-300:])
    return res.stdout, "audio/wav", ".wav"


def safe(name):
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())
    return name[:60] or time.strftime("未命名_%Y%m%d_%H%M%S")


def pdir(slug):
    d = os.path.realpath(os.path.join(DATA, slug))
    if not d.startswith(os.path.realpath(DATA) + os.sep):
        raise ValueError("bad project")
    return d


def load(slug):
    with open(os.path.join(pdir(slug), "project.json"), encoding="utf-8") as f:
        return json.load(f)


def save(slug, p):
    d = pdir(slug)
    tmp = os.path.join(d, "project.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(d, "project.json"))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def guard(self, write):
        """擋掉別的網站的請求。回傳錯誤訊息（擋下）或 None（放行）。"""
        if host_of(self.headers.get("Host")) not in ALLOWED_HOSTS:
            return "網址不對：請用 http://localhost 開"
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return "不接受其他網站的請求"
        origin = self.headers.get("Origin")
        if origin and host_of(origin) not in ALLOWED_HOSTS:
            return "不接受其他網站的請求"
        if write and self.headers.get("X-NR") != "1":
            return "缺少 X-NR 標頭"
        return None

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.security_headers()
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_UPLOAD:
            raise ValueError("檔案太大")
        return self.rfile.read(n)

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        if path not in ("/", "/index.html"):
            err = self.guard(write=False)
            if err:
                return self.send(403, {"error": err})
        elif host_of(self.headers.get("Host")) not in ALLOWED_HOSTS:
            return self.send(403, {"error": "網址不對：請用 http://localhost 開"})
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/projects":
                if not os.path.isdir(DATA):
                    return self.send(503, {"error": f"找不到錄音資料夾：{DATA}"})
                out = []
                for s in sorted(os.listdir(DATA)):
                    try:
                        p = load(s)
                    except Exception:
                        continue
                    done = sum(1 for x in p["sentences"] if x.get("file"))
                    out.append({"slug": s, "title": p["title"], "created": p["created"],
                                "total": len(p["sentences"]), "done": done})
                out.sort(key=lambda x: x["created"], reverse=True)
                return self.send(200, out)
            m = re.match(r"^/api/project/([^/]+)$", path)
            if m:
                return self.send(200, load(m.group(1)))
            m = re.match(r"^/api/project/([^/]+)/export$", path)
            if m:
                q = parse_qs(u.query)
                gap = min(2.0, max(0.0, float(q.get("gap", [GAP])[0])))
                data, ctype, ext = export(m.group(1), q.get("type", ["wav"])[0], gap)
                name = load(m.group(1))["title"] + ("" if ext != ".wav" else "_旁白") + ext
                from urllib.parse import quote
                self.send_response(200)
                self.security_headers()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
                self.end_headers()
                self.wfile.write(data)
                return
            m = re.match(r"^/audio/([^/]+)/([0-9]+\.wav)$", path)
            if m:
                with open(os.path.join(pdir(m.group(1)), m.group(2)), "rb") as f:
                    return self.send(200, f.read(), "audio/wav")
            self.send(404, {"error": "not found"})
        except FileNotFoundError:
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        err = self.guard(write=True)
        if err:
            return self.send(403, {"error": err})
        try:
            if path == "/api/projects":
                req = json.loads(self.body())
                title = req["title"].strip() or "未命名"
                slug = time.strftime("%Y%m%d-%H%M_") + safe(title)
                os.makedirs(pdir(slug))
                sents = [{"id": i + 1, "text": t, "file": None, "dur": 0}
                         for i, t in enumerate(x.strip() for x in req["sentences"]) if t]
                p = {"title": title, "created": time.strftime("%Y-%m-%d %H:%M"),
                     "script": req.get("script", ""), "sentences": sents, "next_id": len(sents) + 1}
                save(slug, p)
                return self.send(200, {"slug": slug})
            m = re.match(r"^/api/project/([^/]+)/audio$", path)
            if m:
                slug, sid = m.group(1), int(q["id"][0])
                data = self.body()
                if data[:4] != b"RIFF":
                    return self.send(400, {"error": "不是 WAV"})
                p = load(slug)
                s = next(x for x in p["sentences"] if x["id"] == sid)
                fn = f"{sid:03d}.wav"
                if s.get("file"):  # 重錄：舊的先移到 _刪除/，不直接蓋掉
                    trash(slug, s)
                with open(os.path.join(pdir(slug), fn + ".tmp"), "wb") as f:
                    f.write(data)
                os.replace(os.path.join(pdir(slug), fn + ".tmp"), os.path.join(pdir(slug), fn))
                s["file"], s["dur"] = fn, round(float(q.get("dur", ["0"])[0]), 2)
                s["recorded"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save(slug, p)
                return self.send(200, s)
            m = re.match(r"^/api/project/([^/]+)/sentences$", path)
            if m:  # 改句子文字：改過的句子錄音作廢
                slug = m.group(1)
                req = json.loads(self.body())
                p = load(slug)
                s = next(x for x in p["sentences"] if x["id"] == int(req["id"]))
                if s["text"] != req["text"].strip():
                    s["text"] = req["text"].strip()
                    if s.get("file"):
                        trash(slug, s)
                save(slug, p)
                return self.send(200, s)
            m = re.match(r"^/api/project/([^/]+)/restructure$", path)
            if m:  # 調整斷句：整份重排，文字沒變的句子保留錄音
                slug = m.group(1)
                lines = [x.strip() for x in json.loads(self.body())["lines"] if x.strip()]
                p = load(slug)
                pool = {}
                for x in p["sentences"]:
                    pool.setdefault(x["text"], []).append(x)
                new = []
                for t in lines:
                    if pool.get(t):
                        new.append(pool[t].pop(0))
                    else:
                        new.append({"id": p["next_id"], "text": t, "file": None, "dur": 0})
                        p["next_id"] += 1
                for left in pool.values():
                    for x in left:
                        trash(slug, x)
                p["sentences"] = new
                save(slug, p)
                return self.send(200, p)
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})

    def do_DELETE(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        err = self.guard(write=True)
        if err:
            return self.send(403, {"error": err})
        try:
            m = re.match(r"^/api/project/([^/]+)/audio$", path)
            if m:
                slug = m.group(1)
                p = load(slug)
                s = next(x for x in p["sentences"] if x["id"] == int(q["id"][0]))
                trash(slug, s)
                save(slug, p)
                return self.send(200, s)
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})


def trash(slug, s):
    """刪除的錄音移到 _刪除/，不真的刪，誤刪救得回來。"""
    d = pdir(slug)
    if s.get("file") and os.path.exists(os.path.join(d, s["file"])):
        os.makedirs(os.path.join(d, "_刪除"), exist_ok=True)
        shutil.move(os.path.join(d, s["file"]),
                    os.path.join(d, "_刪除", time.strftime("%H%M%S_") + s["file"]))
    s["file"], s["dur"] = None, 0


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        sys.exit(f"連接埠 {PORT} 已經有人在用。可能已經開過了：直接打開 http://localhost:{PORT}")
    url = f"http://localhost:{PORT}"
    print(f"逐句錄音已啟動：{url}\n錄音存在：{DATA}\n" + ("" if FFMPEG else "（沒找到 ffmpeg：合併音檔不會自動調音量，其他功能正常）\n") + "要關掉請按 Ctrl+C", flush=True)
    if os.environ.get("NR_OPEN", "1") != "0":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已關閉")
