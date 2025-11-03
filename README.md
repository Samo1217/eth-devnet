# Ethereum Devnet on Kubernetes

A self-contained Ethereum development network with full observability and configurable load generation.

**Stack includes:**
- 🟢 **Geth** (Ethereum client in `--dev` mode)
- ⚙️ **Python Load Generator** (`loadgen/`)
- 🧠 **FastAPI Controller UI** (`controller/`)
- 📈 **Prometheus + Grafana** (monitoring and dashboards)
- 🔁 **GitHub Actions CI/CD** (lint + build + push to GHCR)

---

## 🚀 1. Prerequisites

- Kubernetes cluster (Docker Desktop, k3s, or minikube)
- `kubectl` ≥ 1.27  
- `helm` ≥ 3.13  
- Docker with access to GHCR (`ghcr.io`)

Your GHCR should contain:

ghcr.io/samo1217/eth-devnet-loadgen:latest
ghcr.io/samo1217/eth-devnet-loadctl:latest

These images are built and pushed automatically via CI.

---

## 🧩 2. Deployment Steps

> Run all commands from the repository root.

### 🧱 Create Namespace

```
kubectl create namespace eth-devnet
```

### ⛓️ Deploy Geth (Ethereum Node)
```
helm upgrade --install geth-devnet ./geth-devnet -n eth-devnet --wait
```

### 📊 Deploy Prometheus
```
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install prometheus prometheus-community/prometheus \
  -n eth-devnet -f prom-values.yaml --wait
```

### 📈 Deploy Grafana
```
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
helm upgrade --install grafana grafana/grafana \
  -n eth-devnet -f grafana-values.yaml --wait
```

### 🧠 Import Dashboard
```
kubectl apply -f dashboard.yaml -n eth-devnet
```

### ⚙️ Deploy Load Generator + Controller UI

These are built from Dockerfiles and hosted on GHCR.
Update image references if needed, then apply:
```
kubectl apply -f loadgen.yaml -n eth-devnet
kubectl apply -f controller.yaml -n eth-devnet
```
If you’re running directly from GHCR images, no local build is needed.

---

## 🌐 3. Access Points

| Component       | URL / Access | Notes |
|-----------------|---------------|--------|
| **Grafana**     | [http://localhost:30300](http://localhost:30300) | Login: `admin / admin123` |
| **Controller UI** | [http://localhost:32080](http://localhost:32080) | Adjust TPS, concurrency, and load mix |
| **Prometheus**  | Port-forward: `kubectl port-forward deploy/prometheus-server 9090:9090` → [http://localhost:9090](http://localhost:9090) |  |
| **Geth JSON-RPC** | Internal: `geth-devnet:8545` | Used by loadgen and controller |

If NodePorts aren’t directly reachable, forward manually:
```
kubectl -n eth-devnet port-forward deploy/grafana 30300:3000
kubectl -n eth-devnet port-forward deploy/loadctl 32080:8080
```
Then open:

	•	Grafana → http://localhost:30300
	•	Controller → http://localhost:32080

---

## 📊 4. Verification

### 🔹 Check Geth Block Production
```
kubectl -n eth-devnet exec deploy/geth-devnet -- \
  wget -qO- http://localhost:8545 \
  --header 'content-type: application/json' \
  --post-data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
```
Run twice (6 seconds apart) — block number should increase.


### 🔹 Check Prometheus Targets
```
kubectl -n eth-devnet port-forward deploy/prometheus-server 9090:9090
```
Then open http://localhost:9090 → Status → Targets

You should see:

	•	geth (:6060) → UP
	•	loadgen (:9100) → UP


### 🔹 Check Grafana Dashboard

Open Grafana → Loadgen Metrics

You should see metrics for:

	•	Achieved TPS / RPS
	•	MGas/s
	•	RPC Latency (p50/p90/p99)
	•	Failure rate

---

## ⚙️ 5. Adjusting Load

Open the Controller UI (http://localhost:32080):

	1.	Select Even or Write-heavy preset
	2.	Adjust TPS and Concurrency
	3.	Click Apply — the loadgen deployment updates instantly

### 🧠 Controller UI Guide

The Controller is a lightweight **FastAPI + HTML dashboard** for dynamically tuning the load generator without redeploying pods.  
You can open it at [http://localhost:32080](http://localhost:32080).

### Interface Overview

| Control | Description |
|----------|--------------|
| **TPS (Transactions per Second)** | Defines how many transactions the loadgen attempts to send every second. A higher value means more pressure on the node’s mempool and RPC throughput. |
| **Concurrency** | Number of concurrent async workers sending transactions. Use this to simulate parallel clients. <br>⚠️ Too high values may cause RPC saturation. |
| **Mix Preset** | Two preconfigured request mixes:<br>• **Even** — evenly splits 20% across five methods (`eth_blockNumber`, `eth_call`, `eth_getBalance`, `eth_maxPriorityFeePerGas`, `eth_sendTransaction`).<br>• **Write-heavy** — emphasizes transaction-type calls (≈35% `eth_sendTransaction`, 35% `eth_maxPriorityFeePerGas`, 10% each for the remaining reads). |
| **Method RPS Inputs** | Fine-tune per-method request rate (Requests Per Second). These control how often non-transaction RPCs are sent, e.g. read-only `eth_call` or `eth_blockNumber`. |
| **Apply / Start Button** | Applies your settings instantly. The controller updates the `loadgen` Deployment’s environment variables in Kubernetes — no restart required. |
| **Stop / Reset** | Stops the generator or resets to default parameters. |
| **Status Panel** | Displays the currently applied configuration (TPS, concurrency, mix). This confirms the backend accepted the change. |

### Typical Usage Scenarios

| Goal | Suggested Settings |
|------|--------------------|
| Light functional test | TPS = 10 – 20, Concurrency = 20, Preset = Even |
| Stress / performance test | TPS = 100 – 300, Concurrency = 200 – 500, Preset = Write-heavy |

Metrics in Prometheus and Grafana update within seconds after you click **Apply**.

---

## 🔁 6. CI/CD Workflow Summary

File: .github/workflows/ci.yml

GitHub Actions handles:

	•	YAML + Python linting (yamllint, ruff, black)
	•	Docker image builds for:
	  •	eth-devnet-loadgen
	  •	eth-devnet-loadctl
	•	Automatic push to GHCR on:
	  •	main branch
	•	Tags like v*.*.*
	•	Dynamic lowercasing of GHCR owner handled automatically

---

## 🧹 7. Stop, Resume, or Cleanup

### ⏸️ Pause (keep PVCs)
```
kubectl -n eth-devnet scale deploy --all --replicas=0
```
### ▶️ Resume
```
kubectl -n eth-devnet scale deploy --all --replicas=1
```
### 🗑️ Remove Everything
```
helm uninstall geth-devnet -n eth-devnet
helm uninstall prometheus -n eth-devnet
helm uninstall grafana -n eth-devnet
kubectl delete namespace eth-devnet
```
