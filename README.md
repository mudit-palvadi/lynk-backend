<div align="center">

# LynkShare Backend

**Self-hostable API and real-time service for LynkShare**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Redis](https://img.shields.io/badge/Redis-DC382D?style=flat-square&logo=redis&logoColor=white)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com)
[![Cloudflare R2](https://img.shields.io/badge/Cloudflare_R2-F38020?style=flat-square&logo=cloudflare&logoColor=white)](https://www.cloudflare.com/developer-platform/r2/)

</div>

---

LynkShare Backend coordinates encrypted file transfers between the Android app
and the web app. It creates short-lived transfer and receiver sessions, helps
two devices connect directly when possible, and provides temporary access to
encrypted cloud storage when a direct path is not available. The service never
receives plaintext file contents or the file-decryption key.

This repository is part of [LynkShare](https://github.com/mudit-palvadi/LynkShare).
The mobile app lives in
[lynkshare-mobile](https://github.com/mudit-palvadi/lynkshare-mobile), and the
browser app lives in
[lynkshare-web](https://github.com/mudit-palvadi/lynkshare-web).

## What it does

- Creates short-lived transfer and receiver sessions.
- Coordinates direct connections between two devices.
- Provides temporary access to encrypted objects in Cloudflare R2.
- Optionally issues short-lived TURN credentials for restrictive networks.
- Keeps session metadata in Redis.

## Quick start

### Docker Compose (recommended)

You need Docker with Compose and a Cloudflare R2 bucket for cloud transfers.

```sh
git clone https://github.com/mudit-palvadi/lynkshare-backend.git
cd lynkshare-backend
cp .env.example .env
```

Open `.env` and set:

- `REDIS_PASSWORD` to a long random value.
- `REDIS_URL` to use the same password.
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and
  `R2_BUCKET_NAME` to your own bucket.
- `ALLOWED_ORIGINS` to include the address where you serve the web app.

Then start the stack:

```sh
docker compose up -d
```

The API listens on `http://localhost:8000`. The Compose stack requires
`REDIS_PASSWORD` and refuses to start without it. If you serve the web app
locally on port `3000`, the default `ALLOWED_ORIGINS` already includes it.

### Manual setup

You need Python 3.10+, Redis 7+, and an R2 bucket for cloud transfers.

```sh
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux:       source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Set `REDIS_PASSWORD` in your shell, then start Redis with the same password and
without persistence:

```sh
redis-server --requirepass "$REDIS_PASSWORD" --save "" --appendonly no
```

Create `.env` from `.env.example`, set the same password in `REDIS_PASSWORD`
and `REDIS_URL`, add your R2 credentials, then start the API:

```sh
uvicorn app.main:app --reload
```

## Configuration

Copy `.env.example` to `.env` and fill in your own values. Do not commit
`.env`.

| Variable | What it does |
|---|---|
| `REDIS_PASSWORD` | Required. Password for Redis. Use the same value in `REDIS_URL`. |
| `REDIS_URL` | Redis connection string. |
| `R2_ACCOUNT_ID` | Cloudflare account ID. |
| `R2_ACCESS_KEY_ID` | R2 API token access key. |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret. |
| `R2_BUCKET_NAME` | Bucket for encrypted cloud transfers. |
| `MAX_FILES_PER_TRANSFER` | Maximum number of files in one transfer. |
| `MAX_TOTAL_TRANSFER_SIZE` | Maximum combined size of one transfer. |
| `MAX_INDIVIDUAL_FILE_SIZE` | Maximum size of one file. |
| `TURN_URLS` | Optional TURN server URLs for restrictive networks. |
| `TURN_SECRET` | Optional TURN shared secret. |
| `ALLOWED_ORIGINS` | Browser origins allowed to call the API. |

Cloud transfers need Cloudflare R2. Direct transfers do not upload file bytes.

## Testing

```sh
python -m pytest
python -m ruff check .
```

The suite uses a Redis double, so no running Redis is needed for tests.

## Security

- Session data is short-lived and stored in Redis.
- Files are encrypted before they reach the service; the service does not
  receive the file-decryption key.
- R2 credentials stay on the server and are never bundled into the mobile or
  web app.
- Redis requires a password and keeps data in memory only.
- The container runs as a non-root user with all Linux capabilities dropped.
- TURN credentials are short-lived when TURN is configured.
- Do not commit `.env` or expose Redis to the public internet.

For private vulnerability reporting, use the
[LynkShare security policy](https://github.com/mudit-palvadi/lynkshare-mobile/blob/main/SECURITY.md).

## Contributing

Issues and pull requests are welcome in the
[lynkshare-backend repository](https://github.com/mudit-palvadi/lynkshare-backend).
Run the tests and linter before opening a pull request.

## License

No license is currently declared for this repository. Check with the maintainer
before redistributing or reusing the code.

---

<div align="center">

Part of **[LynkShare](https://github.com/mudit-palvadi/LynkShare)**

</div>
