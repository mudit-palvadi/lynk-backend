# Lynk — Cross-Device File Transfer API (Backend)

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Python_3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Redis-DC382D?style=for-the-badge&logo=redis&logoColor=white" alt="Redis" />
  <img src="https://img.shields.io/badge/Cloudflare_R2-F38020?style=for-the-badge&logo=cloudflare&logoColor=white" alt="R2" />
</p>

This is the FastAPI backend service for **Lynk**, a minimal, login-free, cross-device file transfer app.

The service coordinates the transfer of files by generating short-lived, presigned upload/download URLs directly to Cloudflare R2 and storing transfer session metadata in Redis. No file bytes are proxied through this server, ensuring high throughput and minimal memory overhead.

---

## Architecture & Core Features

* **Lifespan Pool Management**: Uses FastAPI's modern `@asynccontextmanager` lifespan handlers to manage connection pools for Redis cleanly.
* **Non-Blocking I/O**: Offloads synchronous S3 `boto3` calls (used for R2 bucket interactions) to background worker threads using `anyio.to_thread.run_sync`.
* **Zero-Knowledge Pathing**: Generates random UUID/cryptographic identifiers for files and transfers. Files are saved in private R2 folders without exposing original names.
* **RFC 5987 Header Sanitation**: Filenames are URL-encoded before generation of download URLs, ensuring emojis and non-ASCII character sets work seamlessly without header injection vulnerabilities.
* **IP-Based Rate Limiting**: Enforces request limits (5 requests per 60s) using a custom Redis-based FastAPI dependency to protect write endpoints from bot abuse.
* **R2 Storage Cap Guard**: Employs a self-cleaning Redis Sorted Set (ZSET) to track total active file storage size in real-time, blocking new uploads if the active data would exceed the 9.5 GB free-tier limit.
* **TTL Preservation Rules**: Redis updates are executed dynamically, retrieving and preserving the remaining seconds of a session's lifetime to prevent lifetime extensions.

---

## API Endpoints

### File Transfers (`/api/v1/transfers`)
* **`POST /transfers`**: Initiates a transfer session, checks file limits, and returns presigned upload URLs.
* **`POST /transfers/{transfer_id}/files/{file_id}/complete`**: Confirms that a file has been successfully uploaded to Cloudflare R2 and updates status.
* **`GET /transfers/{transfer_id}`**: Retrieves transfer metadata (file list, sizes, status, and time-to-live).
* **`POST /transfers/{transfer_id}/downloads`**: Generates presigned download URLs for the files.
* **`DELETE /transfers/{transfer_id}`**: Cancels the transfer and removes files from storage immediately.
* **`POST /transfers/{transfer_id}/extend`**: Extends the session TTL to 30 minutes. It does not check that an ad was watched.
* **`GET /transfers/{transfer_id}/turn-credentials`**: Short-lived HMAC TURN credentials. Returns `enabled: false` when TURN is not configured. Does not check that the transfer exists.

### Signaling
* **`WS /ws/signaling/{session_id}`**: In-memory two-peer relay for SDP and ICE. Requires a single Uvicorn worker. Session validation is off unless `set_session_validator` is registered; `main.py` does not register it.

### Receiver Sessions (`/api/v1/receiver-sessions`)
* **`POST /receiver-sessions`**: Creates a temporary receiver session (status `"waiting"`) and returns a session ID.
* **`GET /receiver-sessions/{session_id}`**: Polls for session changes (tracks if a transfer has been attached).
* **`POST /receiver-sessions/{session_id}/attach-transfer`**: Attaches a transfer ID to a receiver session (triggers status change to `"attached"`).
* **`DELETE /receiver-sessions/{session_id}`**: Cancels and removes a receiver session.

---

## Development Setup

### Prerequisites
* Python 3.10+
* Redis Server (running locally on port `6379`)

### Installation
1. Navigate to the backend directory:
   ```bash
   cd backend
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   # Windows Command Prompt:
   .venv\Scripts\activate
   # Windows PowerShell:
   .venv\Scripts\Activate.ps1
   ```
3. Install all dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Local Environment Config
Create a `.env` file in the root of the `backend` folder:
```env
ENVIRONMENT=development
PORT=8000
HOST=0.0.0.0
REDIS_URL=redis://localhost:6379/0

# Optional: Add your actual R2 settings here for real uploads.
# Leaving them blank defaults to local mock configurations.
R2_ACCOUNT_ID=your-cloudflare-account-id
R2_ACCESS_KEY_ID=your-access-key-id
R2_SECRET_ACCESS_KEY=your-secret-access-key
R2_BUCKET_NAME=your-bucket-name
```

---

## Running & Testing

* **Start the Server (Reload Mode)**:
  ```bash
  uvicorn app.main:app --reload
  ```
  Explore the interactive API docs at **`http://localhost:8000/docs`**.

* **Run the test suite**:
  ```bash
  .venv\Scripts\python -m pytest
  ```
  Tests live in `backend/tests` (transfers, receiver sessions, signaling, TURN, limits). Do not treat an old "17 tests" or "47 tests" note as the current count.

---

## Production & Cloud Deployment Notes

When deploying to environments like **Oracle Cloud (OCI)**:
1. **Network Configuration**: The API is the only thing Cloudflare talks to, so the OCI Ingress Security Rules must allow the Cloudflare IP ranges (and nothing else) to reach the published container port. Do **not** open the API port to `0.0.0.0/0`. See [DOCKER-USER hardening](#docker-user-hardening-oci) for the host-level second layer.
2. **Process Management**: Run the app as a background service using **systemd** (`systemctl`) or containerize it via **Docker** to ensure persistent operation and crash recovery.
3. **TLS**: Cloudflare terminates TLS (orange-cloud proxy) and connects straight to the origin's published port over plain HTTP — there is no reverse proxy on the VM. Use Caddy or nginx with Let's Encrypt only if the Cloudflare proxy is not in front of the origin.

### DOCKER-USER hardening (OCI)

`docker-compose.yml` publishes the API as `8000:8000`, so the container's port is reachable on **every** host interface. Docker's `nat` table DNATs that port to the container, which means the traffic is *forwarded* and can be filtered with the `DOCKER-USER` chain — the hook Docker itself provides for exactly this.

`deploy/docker_user_chain.sh` installs a chain that allows only the Cloudflare edge ranges to reach `:8000` and drops everything else, without touching the host's own listeners (SSH, and the TURN server on `3478` tcp/udp, live in the `INPUT` chain):

```
Cloudflare edge CIDRs        -> RETURN   (the only allowed clients)
-i br-<compose bridge>       -> RETURN   (container egress, container<->container, hairpin)
-p tcp --dport 8000          -> DROP     (everything else to the API port)
-j RETURN                              (all other forwarded traffic untouched)
```

Install it on the backend VM (it needs root):

```bash
sudo bash deploy/install_docker_user_chain.sh
```

That copies the installer to `/usr/local/sbin/lynk-docker-user.sh`, enables `lynk-docker-user.service` (runs after `docker.service`, so it also survives reboots) and `lynk-docker-user.timer` (re-applies the chain hourly), then verifies that `https://api.lynkshare.app/` still answers `200` — rolling back automatically if it does not.

Notes:
- The chain is idempotent: it compares the rendered rules and does nothing when they already match, so the hourly timer never leaves the chain empty.
- The timer deliberately does **not** run the verification/auto-rollback. A brief Cloudflare outage must never flush the chain and re-open the API port to the internet; rolling back is a human decision.
- `netfilter-persistent` must not be used to restore these rules: it runs *before* `docker.service` and would blank the chain on boot.
- Rollback: `iptables -F DOCKER-USER && ip6tables -F DOCKER-USER` plus `systemctl disable --now lynk-docker-user.service lynk-docker-user.timer`.
