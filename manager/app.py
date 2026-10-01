import asyncio, json, os, re, secrets
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

API = os.getenv("MEDIAMTX_API", "http://mediamtx:9997").rstrip("/")
DATA = Path(os.getenv("DATA_DIR", "/data")); DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "cameras.json"
USER, PASS = os.getenv("MANAGER_USER", "admin"), os.getenv("MANAGER_PASSWORD", "")
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "")
PORTS = {k: os.getenv(f"{k.upper()}_PORT", d) for k, d in (("rtsp", "8554"), ("hls", "8888"), ("webrtc", "8889"))}
INTERVAL = int(os.getenv("RECONCILE_INTERVAL", "30"))

app = FastAPI(title="MediaMTX Manager")
security = HTTPBasic(auto_error=False)
lock = asyncio.Lock()
sync_state = {"ok": None, "error": ""}


def auth(c: HTTPBasicCredentials = Depends(security)):
    if not PASS:
        return
    ok = c and secrets.compare_digest(c.username.encode(), USER.encode()) \
        and secrets.compare_digest(c.password.encode(), PASS.encode())
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


def load() -> dict:
    try:
        return json.loads(DB.read_text("utf-8"))
    except Exception:
        return {}


def save(d: dict):
    tmp = DB.with_suffix(".tmp"); tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8"); tmp.replace(DB)


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(s).strip()).strip("-")
    if not s: raise HTTPException(400, "Tên path không hợp lệ")
    return s


def mask(url: str) -> str:
    if "://" not in url: return url
    scheme, rest = url.split("://", 1)
    i = rest.find("/"); auth_part = rest if i < 0 else rest[:i]
    if "@" not in auth_part: return url
    return f"{scheme}://***@{auth_part.rsplit('@', 1)[1]}{'' if i < 0 else rest[i:]}"


def normalize(url: str) -> str:
    """Mã hóa ký tự đặc biệt (vd @) trong user:pass để link RTSP hợp lệ."""
    if "://" not in url: return url
    scheme, rest = url.split("://", 1)
    i = rest.find("/"); a = rest if i < 0 else rest[:i]; tail = "" if i < 0 else rest[i:]
    if "@" not in a: return url
    ui, host = a.rsplit("@", 1)
    user, sep, pw = ui.partition(":")
    ui = quote(unquote(user), safe="") + (":" + quote(unquote(pw), safe="") if sep else "")
    return f"{scheme}://{ui}@{host}{tail}"


def build(name: str, cam: dict):
    """Trả về (paths cần có, paths cần xóa) cho 1 camera.
    delay > 0: kéo nguồn vào <name>_src, rồi dùng ffmpeg FIFO queue muxer với timeshift
    đệm luồng trong bộ nhớ và phát trễ D giây ra <name>, giúp chống giật và xé hình cho RTSP."""
    if not cam.get("enabled", True):
        return {}, [name, name + "_src"]
    src = normalize(cam["source"])
    base = {"source": src, "sourceOnDemand": bool(cam.get("on_demand", False))}
    if src.startswith("rtsp"):
        base["rtspTransport"] = cam.get("transport", "tcp")
    delay = int(cam.get("delay", 0) or 0)
    if delay <= 0:
        return {name: {**base, "runOnInit": "", "runOnInitRestart": False}}, [name + "_src"]
    queue = max(1000, delay * 250)
    cmd = (f"ffmpeg -hide_banner -loglevel warning -rtsp_transport tcp -i rtsp://127.0.0.1:$RTSP_PORT/{name}_src "
           f"-map 0 -c copy -f fifo -fifo_format rtsp -format_opts rtsp_transport=tcp "
           f"-timeshift {delay} -queue_size {queue} -drop_pkts_on_overflow 1 -attempt_recovery 1 "
           f"-restart_with_keyframe 1 rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH")
    return ({name + "_src": {**base, "sourceOnDemand": False},
             name: {"source": "publisher", "runOnInit": cmd, "runOnInitRestart": True}}, [])


async def reconcile():
    """Đưa trạng thái MediaMTX khớp với cameras.json (thêm/sửa/xóa path)."""
    async with lock, httpx.AsyncClient(base_url=API, timeout=8) as c:
        try:
            r = await c.get("/v3/config/paths/list", params={"itemsPerPage": 1000}); r.raise_for_status()
            have = {p["name"]: p for p in r.json().get("items", [])}
            for name, cam in load().items():
                want, stale = build(name, cam)
                for p in stale:
                    if p in have: await c.delete(f"/v3/config/paths/delete/{p}")
                for p, w in want.items():
                    cur = have.get(p)
                    if not cur:
                        (await c.post(f"/v3/config/paths/add/{p}", json=w)).raise_for_status()
                    elif any(cur.get(k) != v for k, v in w.items()):
                        (await c.post(f"/v3/config/paths/replace/{p}", json=w)).raise_for_status()
            sync_state.update(ok=True, error="")
        except Exception as e:
            sync_state.update(ok=False, error=str(e)[:200])


async def loop():
    while True:
        await reconcile()
        await asyncio.sleep(INTERVAL)


@app.on_event("startup")
async def startup():
    asyncio.create_task(loop())


def upsert(d: dict, body: dict):
    name = slug(body.get("name", ""))
    old = d.get(name, {})
    src = normalize((body.get("source") or "").strip()) or old.get("source", "")
    if not src: raise HTTPException(400, "Thiếu source (link RTSP)")
    d[name] = {
        "name": name, "title": body.get("title", old.get("title", name)), "source": src,
        "enabled": body.get("enabled", old.get("enabled", True)),
        "on_demand": body.get("on_demand", old.get("on_demand", False)),
        "transport": body.get("transport", old.get("transport", "tcp")),
        "delay": max(0, min(120, int(body.get("delay", old.get("delay", 15)) or 0))),
        **{k: body[k] for k in ("lat", "lng") if k in body},
    }
    return name


@app.get("/api/cameras", dependencies=[Depends(auth)])
async def cameras(request: Request):
    status = {}
    try:
        async with httpx.AsyncClient(base_url=API, timeout=5) as c:
            r = await c.get("/v3/paths/list", params={"itemsPerPage": 1000})
            status = {p["name"]: p for p in r.json().get("items", [])}
    except Exception:
        pass
    out = []
    for name, cam in load().items():
        st = status.get(name, {})
        out.append({**{k: v for k, v in cam.items() if k != "source"}, "source": mask(cam["source"]),
                    "ready": bool(st.get("ready")), "readers": len(st.get("readers") or []),
                    "bytes": st.get("bytesReceived", 0)})
    return {"host": PUBLIC_HOST or request.url.hostname, "ports": PORTS, "sync": sync_state, "cameras": out}


@app.post("/api/cameras", dependencies=[Depends(auth)])
async def save_camera(body: dict):
    d = load(); name = upsert(d, body); save(d); await reconcile(); return {"name": name}


@app.delete("/api/cameras/{name}", dependencies=[Depends(auth)])
async def delete_camera(name: str):
    d = load()
    if name not in d: raise HTTPException(404)
    del d[name]; save(d)
    async with httpx.AsyncClient(base_url=API, timeout=8) as c:
        for p in (name, name + "_src"):
            try: await c.delete(f"/v3/config/paths/delete/{p}")
            except Exception: pass
    return {"ok": True}


@app.post("/api/cameras/{name}/toggle", dependencies=[Depends(auth)])
async def toggle(name: str):
    d = load()
    if name not in d: raise HTTPException(404)
    d[name]["enabled"] = not d[name].get("enabled", True); save(d); await reconcile()
    return {"enabled": d[name]["enabled"]}


@app.post("/api/import", dependencies=[Depends(auth)])
async def import_json(request: Request):
    """Nhận JSON từ app Đắk Lắk Số 3.0 ({data:[{ID,TEN,STSPLINK,...}]}) hoặc list tương tự."""
    body = await request.json()
    items = body.get("data", []) if isinstance(body, dict) else body
    d, n = load(), 0
    for it in items:
        src = (it.get("STSPLINK") or "").strip()
        if not src: continue
        upsert(d, {"name": f"cam{it['ID']}", "title": (it.get("TEN") or "").strip(), "source": src,
                   "lat": (it.get("LAT") or "").strip(), "lng": (it.get("LNG") or "").strip(), "delay": 15}); n += 1
    save(d); await reconcile()
    return {"imported": n}


@app.post("/api/sync", dependencies=[Depends(auth)])
async def sync():
    await reconcile(); return sync_state


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
async def index():
    return PAGE


PAGE = """<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MediaMTX Manager</title>
<style>
:root{--bg:#fff;--fg:#1d2733;--mut:#6b7785;--bd:#dde3ea;--ac:#1f6feb;--ok:#1a7f37;--er:#cf222e}
@media(prefers-color-scheme:dark){:root{--bg:#10151b;--fg:#e6edf3;--mut:#8b98a6;--bd:#2a323c;--ac:#58a6ff;--ok:#3fb950;--er:#f85149}}
body{font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:16px;max-width:1200px;margin:auto}
h1{font-size:20px}input,select,textarea,button{font:inherit;color:inherit;background:transparent;border:1px solid var(--bd);border-radius:6px;padding:6px 8px}
button{cursor:pointer}button:hover{border-color:var(--ac)}.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
table{width:100%;border-collapse:collapse;display:block;overflow-x:auto}td,th{padding:6px 8px;border-bottom:1px solid var(--bd);text-align:left;white-space:nowrap}
.on{color:var(--ok)}.off{color:var(--mut)}.err{color:var(--er)}.mut{color:var(--mut);font-size:12px}details{margin:12px 0}
</style>
<h1>MediaMTX Manager</h1><div id="sync" class="mut"></div>
<details open><summary>Thêm / sửa camera</summary><div class="row">
<input id="name" placeholder="path (vd cam33)"><input id="title" placeholder="Tên hiển thị" size="28">
<input id="source" placeholder="rtsp://user:pass@host:port/path (trống = giữ nguyên khi sửa)" size="60">
<select id="transport"><option>tcp</option><option>udp</option><option>automatic</option></select>
<input id="delay" type="number" min="0" max="120" value="15" style="width:80px" title="Đệm chống giật RTSP (giây). 0 = tắt"> <span class="mut">delay (giây)</span>
<label><input type="checkbox" id="ondemand"> chỉ kéo khi có người xem</label><button onclick="saveCam()">Lưu</button></div></details>
<details><summary>Nhập JSON từ app Đắk Lắk Số 3.0</summary><textarea id="imp" rows="5" style="width:100%" placeholder='{"success":true,"data":[...]}'></textarea>
<div class="row"><button onclick="imp()">Nhập</button></div></details>
<div class="row"><button onclick="sync()">Đồng bộ lại MediaMTX</button><button onclick="load()">Làm mới</button></div>
<table><thead><tr><th>Path</th><th>Tên</th><th>Nguồn</th><th>Delay</th><th>Trạng thái</th><th>Người xem</th><th>Liên kết</th><th></th></tr></thead><tbody id="rows"></tbody></table>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,o){const r=await fetch(p,Object.assign({headers:{'Content-Type':'application/json'}},o||{}));if(!r.ok)throw new Error(await r.text());return r.json()}
let CAMS={};
async function load(){try{const d=await api('/api/cameras');CAMS={};
$('#sync').innerHTML=d.sync.ok===false?'<span class=err>Không nối được MediaMTX: '+esc(d.sync.error)+'</span>':'Đã đồng bộ với MediaMTX';
$('#rows').innerHTML=d.cameras.map(c=>{CAMS[c.name]=c;const u=p=>`http://${d.host}:${d.ports[p]}/${c.name}`,rtsp=`rtsp://${d.host}:${d.ports.rtsp}/${c.name}`;
const st=!c.enabled?'<span class=off>tắt</span>':c.ready?'<span class=on>● đang chạy</span>':'<span class=err>○ chưa có dữ liệu</span>';
return `<tr><td><b>${esc(c.name)}</b></td><td>${esc(c.title||'')}</td><td class=mut>${esc(c.source)}</td><td>${c.delay?c.delay+'s':'-'}</td><td>${st}</td><td>${c.readers}</td>
<td><a href="${u('webrtc')}" target=_blank>Xem (WebRTC)</a> · <a href="${u('hls')}" target=_blank>HLS</a> · <a href="#" onclick="navigator.clipboard.writeText('${rtsp}');return false">Copy RTSP</a></td>
<td><button onclick="edit('${esc(c.name)}')">Sửa</button> <button onclick="tog('${esc(c.name)}')">${c.enabled?'Tắt':'Bật'}</button> <button onclick="del('${esc(c.name)}')">Xóa</button></td></tr>`}).join('')}catch(e){$('#sync').textContent=e.message}}
function edit(n){const c=CAMS[n];$('#name').value=c.name;$('#title').value=c.title||'';$('#source').value='';$('#transport').value=c.transport||'tcp';$('#ondemand').checked=!!c.on_demand;$('#delay').value=c.delay!==undefined?c.delay:15;scrollTo(0,0)}
async function saveCam(){try{await api('/api/cameras',{method:'POST',body:JSON.stringify({name:$('#name').value,title:$('#title').value,source:$('#source').value,transport:$('#transport').value,on_demand:$('#ondemand').checked,delay:+$('#delay').value||0})});$('#source').value='';load()}catch(e){alert(e.message)}}
async function tog(n){await api(`/api/cameras/${n}/toggle`,{method:'POST'});load()}
async function del(n){if(confirm('Xóa '+n+'?')){await api('/api/cameras/'+n,{method:'DELETE'});load()}}
async function imp(){try{const r=await api('/api/import',{method:'POST',body:$('#imp').value});alert('Đã nhập '+r.imported+' camera');$('#imp').value='';load()}catch(e){alert(e.message)}}
async function sync(){await api('/api/sync',{method:'POST'});load()}
load();setInterval(load,5000);
</script></html>"""
