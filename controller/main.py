# controller/main.py
import os
import time
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from kubernetes import client, config

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
NAMESPACE  = os.getenv("NAMESPACE",  "eth-devnet")
DEPLOYMENT = os.getenv("DEPLOYMENT", "loadgen")
CONTAINER  = os.getenv("CONTAINER",  "loadgen")

app = FastAPI(title="Loadgen Controller")

# Try in-cluster, fall back to local kubeconfig for dev
try:
    config.load_incluster_config()
except Exception:
    config.load_kube_config()

apps = client.AppsV1Api()

# -------------------------------------------------------------------
# K8s helpers
# -------------------------------------------------------------------
def get_deploy():
    return apps.read_namespaced_deployment(DEPLOYMENT, NAMESPACE)

def _env_list(container):
    return [{"name": e.name, "value": e.value} for e in (container.env or [])]

def _set_env(env_list, name, value):
    # upsert string value
    value = "" if value is None else str(value)
    for e in env_list:
        if e["name"] == name:
            e["value"] = value
            return
    env_list.append({"name": name, "value": value})

def read_env():
    dep = get_deploy()
    containers = dep.spec.template.spec.containers
    target = next((c for c in containers if c.name == CONTAINER), containers[0])
    env = {e["name"]: e["value"] for e in _env_list(target)}
    # Provide reasonable defaults if missing
    return {
        "tps": int(float(env.get("TPS", "80"))),
        "concurrency": int(float(env.get("CONCURRENCY", "200"))),
        "rps_block": float(env.get("RPS_BLOCK", "0")),
        "rps_bal":   float(env.get("RPS_BAL",   "0")),
        "rps_call":  float(env.get("RPS_CALL",  "0")),
    }

def patch_env_simple(tps: int, conc: int, rps_block: float, rps_bal: float, rps_call: float):
    dep = get_deploy()
    containers = dep.spec.template.spec.containers
    target = next((c for c in containers if c.name == CONTAINER), containers[0])

    env_list = _env_list(target)
    _set_env(env_list, "TPS", tps)
    _set_env(env_list, "CONCURRENCY", conc)
    _set_env(env_list, "RPS_BLOCK", rps_block)
    _set_env(env_list, "RPS_BAL",   rps_bal)
    _set_env(env_list, "RPS_CALL",  rps_call)

    # bump an annotation to force rollout
    anns = dep.spec.template.metadata.annotations or {}
    anns["loadgen-controller/lastUpdate"] = str(time.time())

    body = {
        "spec": {
            "template": {
                "metadata": {"annotations": anns},
                "spec": {"containers": [
                    {"name": CONTAINER, "env": env_list}
                ]}
            }
        }
    }
    apps.patch_namespaced_deployment(name=DEPLOYMENT, namespace=NAMESPACE, body=body)

# -------------------------------------------------------------------
# Preset math
# -------------------------------------------------------------------
def compute_mix(total_tps: float, preset: str):
    """
    Split a TOTAL rate across 5 logical methods:
      - send (eth_sendTransaction) -> TPS (write)
      - tip  (eth_maxPriorityFeePerGas) -> implicitly ≈ TPS (handled in loadgen.py)
      - reads: block/bal/call -> env RPS_BLOCK/RPS_BAL/RPS_CALL

    Presets:
      - "even"  : 20% each  => per = total/5
      - "write" : 35% send, 10% each read (tip stays ≈ TPS implicitly)
    Returns: dict with tps, rps_block, rps_bal, rps_call
    """
    t = max(float(total_tps), 0.0)
    if preset == "even":
        per = round(t / 5.0)
        return {"tps": per, "rps_block": per, "rps_bal": per, "rps_call": per}
    # default: write-heavy
    send = round(0.35 * t)
    read = round(0.10 * t)
    return {"tps": send, "rps_block": read, "rps_bal": read, "rps_call": read}

# -------------------------------------------------------------------
# API
# -------------------------------------------------------------------
@app.get("/api/state")
def api_state():
    try:
        return read_env()
    except Exception as e:
        raise HTTPException(500, f"read failed: {e}")

@app.post("/api/set")
def api_set(tps: int = Query(..., ge=0, le=100000),
            concurrency: int = Query(..., ge=1, le=100000),
            rps_block: float = Query(0.0, ge=0, le=5000),
            rps_bal:   float = Query(0.0, ge=0, le=5000),
            rps_call:  float = Query(0.0, ge=0, le=5000)):
    try:
        patch_env_simple(tps, concurrency, rps_block, rps_bal, rps_call)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"patch failed: {e}")

@app.post("/api/set_mix")
def api_set_mix(total_tps: float = Query(..., ge=0, le=100000),
                preset: str      = Query("write", regex="^(write|even)$"),
                concurrency: int = Query(None, ge=1, le=100000)):
    """
    Accept TOTAL TPS + preset, compute per-method split server-side,
    and patch the Deployment envs (TPS, CONCURRENCY, RPS_BLOCK/BAL/CALL).
    """
    try:
        cur = read_env()
        conc = concurrency if concurrency is not None else cur["concurrency"]
        mix  = compute_mix(total_tps, preset)
        patch_env_simple(int(mix["tps"]), int(conc), mix["rps_block"], mix["rps_bal"], mix["rps_call"])
        # Note: tip RPS ~= TPS implicitly, since loadgen calls eth_maxPriorityFeePerGas inside send_tx
        return {
            "ok": True,
            "applied": {
                "preset": preset,
                "total_tps": total_tps,
                "TPS(send)": int(mix["tps"]),
                "RPS(tip≈eth_maxPriorityFeePerGas)": int(mix["tps"]),
                "RPS_BLOCK": mix["rps_block"],
                "RPS_BAL":   mix["rps_bal"],
                "RPS_CALL":  mix["rps_call"],
                "CONCURRENCY": int(conc),
            }
        }
    except Exception as e:
        raise HTTPException(500, f"patch failed: {e}")

# -------------------------------------------------------------------
# Minimal UI
# -------------------------------------------------------------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loadgen Controller</title>
<style>
body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; padding: 24px; max-width: 760px; margin: auto;}
.card { padding: 16px; border: 1px solid #e5e7eb; border-radius: 12px; box-shadow: 0 1px 2px rgba(0,0,0,.04); margin-bottom: 16px;}
label { display:flex; justify-content:space-between; margin-bottom: 8px; font-weight:600;}
.row { display:flex; gap:12px; align-items:center; }
input[type=number]{ width:140px; padding:8px; border:1px solid #e5e7eb; border-radius:10px; }
select { padding:8px; border:1px solid #e5e7eb; border-radius:10px; }
button { background:#111827; color:#fff; padding:10px 16px; border:none; border-radius:10px; cursor:pointer;}
button:hover{ background:#0b1220;}
.small{ color:#6b7280; font-size:12px;}
.kv { display:grid; grid-template-columns: 200px 1fr; gap: 6px 12px; }
.kv div:nth-child(odd){ color:#6b7280; }
</style>
<script>
function clamp(n, lo, hi){ n = Number(n); if (isNaN(n)) return lo; return Math.max(lo, Math.min(hi, n)); }

async function loadState(){
  const r = await fetch('/api/state'); const s = await r.json();
  document.getElementById('cur').innerHTML = `
    <div class="kv">
      <div>TPS (send)</div><div>${s.tps}</div>
      <div>CONCURRENCY</div><div>${s.concurrency}</div>
      <div>RPS_BLOCK</div><div>${s.rps_block}</div>
      <div>RPS_BAL</div><div>${s.rps_bal}</div>
      <div>RPS_CALL</div><div>${s.rps_call}</div>
    </div>`;
}

async function applyPreset(){
  const tot  = clamp(document.getElementById('totalTps').value, 0, 100000);
  const conc = clamp(document.getElementById('conc').value, 1, 100000);
  const mix  = document.getElementById('mix').value;
  const u = `/api/set_mix?total_tps=${tot}&preset=${mix}&concurrency=${conc}`;
  const r = await fetch(u, {method:'POST'});
  if (r.ok){ alert('Preset applied'); loadState(); }
  else { alert('Error: ' + await r.text()); }
}

async function saveExplicit(){
  // Optional explicit mode if you want to set exact numbers
  const tps  = clamp(document.getElementById('tps').value, 0, 100000);
  const conc = clamp(document.getElementById('conc2').value, 1, 100000);
  const rb = clamp(document.getElementById('rb').value, 0, 5000);
  const rg = clamp(document.getElementById('rg').value, 0, 5000);
  const rc = clamp(document.getElementById('rc').value, 0, 5000);
  const u = `/api/set?tps=${tps}&concurrency=${conc}&rps_block=${rb}&rps_bal=${rg}&rps_call=${rc}`;
  const r = await fetch(u, {method:'POST'});
  if (r.ok){ alert('Updated'); loadState(); }
  else { alert('Error: ' + await r.text()); }
}

window.addEventListener('DOMContentLoaded', loadState);
</script>
</head>
<body>
<h1>Ethereum Loadgen Controller</h1>

<div class="card">
  <label>Current values</label>
  <div id="cur" class="small">Loading…</div>
</div>

<div class="card">
  <label>Preset apply</label>
  <div class="row" style="margin-bottom:8px;">
    <span class="small">Preset:</span>
    <select id="mix">
      <option value="write" selected>Write-heavy (35% send, reads 10/10/10)</option>
      <option value="even">Even (20% each)</option>
    </select>
  </div>
  <div class="row" style="margin-bottom:8px;">
    <span class="small">Total TPS (across 5 methods)</span>
    <input id="totalTps" type="number" min="0" max="100000" step="1" value="100" inputmode="numeric" />
    <span class="small">Concurrency</span>
    <input id="conc" type="number" min="1" max="100000" step="1" value="200" inputmode="numeric" />
  </div>
  <button onclick="applyPreset()">Apply Preset</button>
</div>

<div class="card">
  <label>Advanced (explicit values)</label>
  <div class="row" style="margin-bottom:8px;">
    <span class="small">TPS (send)</span>
    <input id="tps" type="number" min="0" max="100000" step="1" value="80" inputmode="numeric" />
    <span class="small">Concurrency</span>
    <input id="conc2" type="number" min="1" max="100000" step="1" value="200" inputmode="numeric" />
  </div>
  <div class="row">
    <span class="small">RPS_BLOCK</span>
    <input id="rb" type="number" min="0" max="5000" step="1" value="0" inputmode="numeric" />
    <span class="small">RPS_BAL</span>
    <input id="rg" type="number" min="0" max="5000" step="1" value="0" inputmode="numeric" />
    <span class="small">RPS_CALL</span>
    <input id="rc" type="number" min="0" max="5000" step="1" value="0" inputmode="numeric" />
  </div>
  <div style="margin-top:8px;">
    <button onclick="saveExplicit()">Save Explicit</button>
  </div>
</div>

</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML