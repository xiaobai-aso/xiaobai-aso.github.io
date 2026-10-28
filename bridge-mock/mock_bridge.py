#!/usr/bin/env python3
"""ASO Bridge 模拟服务:按 docs/bridge-api.md 实现全部接口,用来调试网页的「一键发布」。

不会连接 App Store Connect;收到的发布包会解压检查后模拟上传过程。

    python3 bridge-mock/mock_bridge.py            # 默认端口 47321
    python3 bridge-mock/mock_bridge.py --port 47400 --no-accounts
"""
import argparse
import io
import json
import secrets
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.0-mock"
DEV_LABEL = {"IPHONE_67": "iPhone 6.9″", "IPHONE_65": "iPhone 6.5″", "IPHONE_61": "iPhone 6.3″", "IPAD_PRO_3GEN_129": "iPad 13″"}

state = {
    "pair_code": f"{secrets.randbelow(10**6):06d}",
    "tokens": set(),
    "accounts": [{"id": "acc_1", "name": "示例公司(模拟)", "issuerId": "69a6de7e-0000-47e3-e053-5b8c7c11a4d1"}],
    "apps": {
        "acc_1": [
            {"id": "6470000001", "name": "记账本", "bundleId": "com.example.ledger"},
            {"id": "6470000002", "name": "番茄专注", "bundleId": "com.example.focus"},
        ]
    },
    "versions": {
        "6470000001": [
            {"id": "ver_a", "versionString": "1.3.0", "platform": "IOS", "state": "PREPARE_FOR_SUBMISSION", "stateLabel": "准备提交", "editable": True},
            {"id": "ver_b", "versionString": "1.2.0", "platform": "IOS", "state": "READY_FOR_SALE", "stateLabel": "可供销售", "editable": False},
        ],
        "6470000002": [
            {"id": "ver_c", "versionString": "1.0", "platform": "IOS", "state": "PREPARE_FOR_SUBMISSION", "stateLabel": "准备提交", "editable": True},
        ],
    },
    "jobs": {},
}
lock = threading.Lock()

SAMPLE_METADATA = {
    "locales": {
        "zh-Hans": {
            "name": "记账本 - 预算与记账",
            "subtitle": "每天三秒记一笔",
            "privacyPolicyUrl": "https://example.com/privacy",
            "keywords": "账本,存钱,理财,支出,收入,报销,家庭,信用卡",
            "promotionalText": "新版支持多账本和 iCloud 同步。",
            "description": "最简单的记账 App。\n\n· 三秒记一笔\n· 自动分类统计\n· 多账本与家人共享",
            "whatsNew": "· 新增多账本\n· 修复若干问题",
            "supportUrl": "https://example.com/help",
            "marketingUrl": "https://example.com",
        },
        "en-US": {
            "name": "Ledger - Budget & Expense",
            "subtitle": "Track money in 3 seconds",
            "privacyPolicyUrl": "https://example.com/privacy",
            "keywords": "finance,spending,bills,family,receipt,saving",
            "promotionalText": "Now with multiple ledgers and iCloud sync.",
            "description": "The simplest budget app.\n\n- Log in 3 seconds\n- Smart categories",
            "whatsNew": "- Multiple ledgers\n- Bug fixes",
            "supportUrl": "https://example.com/help",
        },
    },
    "review": {
        "contactFirstName": "小明", "contactLastName": "王", "contactPhone": "+86 13800000000",
        "contactEmail": "dev@example.com", "demoAccountRequired": True,
        "demoAccountName": "review@example.com", "demoAccountPassword": "Review2026", "notes": "登录后点右下角 + 记一笔。",
    },
}


class Job:
    def __init__(self):
        self.events = []
        self.finished = False
        self.cond = threading.Condition()

    def emit(self, **ev):
        with self.cond:
            self.events.append(ev)
            if ev["type"] in ("done", "error"):
                self.finished = True
            self.cond.notify_all()


def run_job(job, data, version, delay):
    def step(sid, label, secs=delay):
        job.emit(type="step", id=sid, label=label, status="run")
        time.sleep(secs)
        job.emit(type="step", id=sid, label=label, status="done")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        mpath = next((n for n in names if n.endswith("/manifest.json") and n.count("/") == 1), None)
        if not mpath:
            raise ValueError("发布包里没有 manifest.json")
        root = mpath.rsplit("/", 1)[0]
        m = json.loads(zf.read(mpath))
        if m.get("format") != "aso-release/1":
            raise ValueError(f"不认识的发布包格式:{m.get('format')}")
        if m["app"]["version"] != version["versionString"]:
            raise ValueError("发布包里的版本号和所选版本不一致")
        if not version.get("editable", True):
            raise ValueError(f"版本 {version['versionString']} 当前状态是「{version['stateLabel']}」,不能修改")

        step("find", f"找到版本 {version['versionString']}(模拟)", delay / 2)
        parts, locales = m["parts"], m["locales"]
        if parts.get("shots"):
            groups = [(d, l) for d in m["screenshots"] for l in locales]
            is_img = lambda n: n.lower().endswith((".png", ".jpg", ".jpeg"))
            total = sum(1 for n in names if n.startswith(f"{root}/screenshots/") and is_img(n))
            sent = 0
            for d, l in groups:
                pngs = sorted(n for n in names if n.startswith(f"{root}/screenshots/{d}/{l}/") and is_img(n))
                if not pngs:
                    job.emit(type="log", msg=f"{d} / {l} 没有截图,跳过")
                    continue
                sid, label = f"shots-{d}-{l}", f"上传截图 {DEV_LABEL.get(d, d)} · {l}({len(pngs)} 张)"
                job.emit(type="step", id=sid, label=label, status="run")
                if m.get("replaceScreenshots"):
                    job.emit(type="log", msg=f"删除 {d} / {l} 的旧截图(模拟)")
                for p in pngs:
                    head = zf.read(p)[:8]
                    if head != b"\x89PNG\r\n\x1a\n" and head[:3] != b"\xff\xd8\xff":
                        raise ValueError(f"{p} 不是 PNG / JPG")
                    time.sleep(delay / 4)
                    sent += 1
                    job.emit(type="progress", pct=round(sent / max(total, 1) * 60), msg=f"正在上传截图 {sent} / {total}")
                job.emit(type="step", id=sid, label=label, status="done")
        if parts.get("meta"):
            for l in locales:
                meta = m["metadata"].get(l)
                if not meta:
                    job.emit(type="log", msg=f"{l} 没填文案,沿用后台现有的")
                    continue
                over = [k for k, lim in (("name", 30), ("subtitle", 30), ("keywords", 100), ("promotionalText", 170)) if len(meta.get(k, "")) > lim]
                if over:
                    raise ValueError(f"{l} 的 {', '.join(over)} 超出字数")
                step(f"meta-{l}", f"更新商店文案 · {l}({'、'.join(meta)})")
            job.emit(type="progress", pct=85, msg="商店文案已更新")
        if parts.get("review"):
            r = {k: v for k, v in (m.get("review") or {}).items() if v not in ("", None)}
            if r:
                keys = [k for k in r if k not in ("demoAccountName", "demoAccountPassword")]
                step("review", f"更新审核信息 · {'、'.join(keys)}")
            else:
                job.emit(type="log", msg="审核信息没填,沿用后台现有的")
        step("doctor", "检查提交条件", delay / 2)
        job.emit(type="progress", pct=100, msg="完成")
        job.emit(type="done", msg="全部上传完成(模拟,没有真的改动 App Store Connect)")
    except Exception as e:  # noqa: BLE001
        job.emit(type="error", msg=str(e))


SETUP_HTML = """<!doctype html><meta charset="utf-8"><title>ASO Bridge 设置(模拟)</title>
<style>body{font:15px/1.6 -apple-system,sans-serif;max-width:560px;margin:48px auto;padding:0 20px;color:#222}
input{display:block;width:100%;padding:8px;margin:4px 0 12px;box-sizing:border-box}button{padding:8px 18px}
li{margin:4px 0}</style>
<h2>ASO Bridge · API 密钥</h2>
<p>模拟服务:密钥只保存在内存里,不会真的使用。正式版会把 .p8 存进系统钥匙串。</p>
<ul id=list></ul>
<form id=f><label>名称<input name=name placeholder="我的公司" required></label>
<label>Key ID<input name=keyId required></label><label>Issuer ID<input name=issuerId required></label>
<label>.p8 私钥文件<input name=p8 type=file accept=".p8"></label><button>添加</button></form>
<script>
async function load(){const r=await fetch('/setup/accounts');const a=await r.json();
list.innerHTML=a.map(x=>`<li>${x.name} <small>${x.issuerId}</small></li>`).join('')||'<li>还没有密钥</li>'}
f.onsubmit=async e=>{e.preventDefault();const d=Object.fromEntries(new FormData(f));delete d.p8;
await fetch('/setup/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});f.reset();load()};
load();
</script>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "aso-bridge-mock"

    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path.split('?')[0]} → {args[1] if len(args) > 1 else ''}")

    # ---------- 安全检查 ----------
    def host_ok(self):
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def origin_ok(self):
        o = self.headers.get("Origin")
        if o is None:
            return True  # 同源页面(/setup)或命令行
        if o in ALLOWED_ORIGINS or o == "null":  # null = 直接双击打开的本地 HTML 文件,仅开发时放行
            return True
        return o.startswith(("http://localhost:", "http://127.0.0.1:"))

    def cors(self):
        o = self.headers.get("Origin")
        if o and self.origin_ok():
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self, q):
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else (q.get("token") or [""])[0]
        return tok in state["tokens"]

    def guard(self):
        if not self.host_ok():
            self.send_json(403, {"error": "Host 不对"})
            return False
        if not self.origin_ok():
            self.send_json(403, {"error": "这个网站没有被允许调用本地服务"})
            return False
        return True

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        if not self.guard():
            return
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        if not self.guard():
            return
        u = urlparse(self.path)
        q, path = parse_qs(u.query), u.path.rstrip("/") or "/"
        if path == "/health":
            return self.send_json(200, {"name": "aso-bridge", "version": VERSION})
        if path == "/setup":
            body = SETUP_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path == "/setup/accounts":
            if self.headers.get("Origin"):
                return self.send_json(403, {"error": "只能在设置页里调用"})
            return self.send_json(200, state["accounts"])
        if not self.authed(q):
            return self.send_json(401, {"error": "未配对或配对已失效"})
        if path == "/accounts":
            return self.send_json(200, state["accounts"])
        if path == "/apps":
            acc = (q.get("account") or [""])[0]
            return self.send_json(200, state["apps"].get(acc, []))
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "apps" and parts[3] == "metadata":
            if parts[2] not in state["versions"]:
                return self.send_json(404, {"error": "找不到这个 App"})
            return self.send_json(200, SAMPLE_METADATA)
        if len(parts) == 4 and parts[1] == "apps" and parts[3] == "versions":
            return self.send_json(200, state["versions"].get(parts[2], []))
        if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "events":
            return self.stream(parts[2])
        self.send_json(404, {"error": "没有这个接口"})

    def do_POST(self):
        if not self.guard():
            return
        u = urlparse(self.path)
        q, path = parse_qs(u.query), u.path.rstrip("/")
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path == "/pair":
            try:
                code = json.loads(data or b"{}").get("code", "")
            except ValueError:
                code = ""
            if not secrets.compare_digest(str(code), state["pair_code"]):
                return self.send_json(403, {"error": "配对码不对,看本地服务的终端窗口"})
            token = secrets.token_hex(32)
            with lock:
                state["tokens"].add(token)
                state["pair_code"] = f"{secrets.randbelow(10**6):06d}"
            print(f"\n  ✓ 已配对。下一个配对码:{state['pair_code']}\n")
            return self.send_json(200, {"token": token})
        if path == "/setup/accounts":
            if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
                return self.send_json(403, {"error": "只能在设置页里调用"})
            d = json.loads(data or b"{}")
            acc = {"id": f"acc_{len(state['accounts']) + 1}", "name": d.get("name") or "未命名", "issuerId": d.get("issuerId", "")}
            with lock:
                state["accounts"].append(acc)
                state["apps"][acc["id"]] = state["apps"]["acc_1"] if "acc_1" in state["apps"] else []
            return self.send_json(200, acc)
        if not self.authed(q):
            return self.send_json(401, {"error": "未配对或配对已失效"})
        if path == "/jobs":
            app = (q.get("app") or [""])[0]
            ver = next((v for v in state["versions"].get(app, []) if v["versionString"] == (q.get("version") or [""])[0]), None)
            if not ver:
                return self.send_json(400, {"error": "找不到这个 App 或版本"})
            if not data.startswith(b"PK"):
                return self.send_json(400, {"error": "请求体不是 zip"})
            jid = "job_" + secrets.token_hex(6)
            job = state["jobs"][jid] = Job()
            print(f"  收到发布包 {len(data) / 1024:.0f} KB → {jid}")
            threading.Thread(target=run_job, args=(job, data, ver, DELAY), daemon=True).start()
            return self.send_json(200, {"id": jid})
        self.send_json(404, {"error": "没有这个接口"})

    def stream(self, jid):
        job = state["jobs"].get(jid)
        if not job:
            return self.send_json(404, {"error": "没有这个任务"})
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        i = 0
        try:
            while True:
                with job.cond:
                    while i >= len(job.events) and not job.finished:
                        job.cond.wait(15)
                        if i >= len(job.events) and not job.finished:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    batch, i = job.events[i:], len(job.events)
                    done = job.finished
                for ev in batch:
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                if done and i >= len(job.events):
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=47321)
    ap.add_argument("--delay", type=float, default=0.6, help="每一步模拟耗时(秒)")
    ap.add_argument("--no-accounts", action="store_true", help="从「没有 API 密钥」状态开始,测试设置页流程")
    ap.add_argument("--origin", action="append", default=[], help="额外允许的网页 Origin,可重复")
    a = ap.parse_args()
    PORT, DELAY = a.port, a.delay
    ALLOWED_ORIGINS = set(a.origin)
    if a.no_accounts:
        state["accounts"] = []
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ASO Bridge(模拟)已启动:http://127.0.0.1:{PORT}")
    print(f"\n  配对码:{state['pair_code']}\n")
    print("  在网页「一键发布」里输入配对码。Ctrl+C 退出。\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
