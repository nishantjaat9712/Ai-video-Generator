# Deployment boundary

The verified target is a single-user Windows localhost studio. No public deployment has been performed.

The API uses validated request sizes, upload type checks, server-owned paths, encrypted local credentials, same-origin checks, host restrictions, job quotas and rate limits. Generation runs outside the web server in a durable worker. Long-running remote requests retain their receipts across retries/restarts.

Public deployment requires an authenticated TLS reverse proxy, an explicit origin/host allowlist, access control and user isolation, backup/restore procedures, storage limits, log monitoring, abuse controls and dependency updates. STORYORBIT_PUBLIC=1 requires STORYORBIT_API_TOKEN; that shared API token is not a complete multi-user login system. Never expose the database, credential key, .env or data directory through static hosting.

PostgreSQL is configurable through DATABASE_URL. Schema creation currently uses SQLAlchemy create_all; production schema migrations must be added before evolving a deployed schema. Redis/Celery dependencies may be available, but this build uses its database-backed worker. Evaluate supported distributed queues and transactional locking before scaling. Use a single worker for this local installation.

Cloud keys and Google refresh tokens remain server-side. OAuth uses state/cookie validation and PKCE. YouTube drafts snapshot an output asset and require explicit approval; a future publish time is sent to YouTube as a scheduled public release. This integration has only mocked protocol tests, not a live authorized upload. Account quotas and API behavior must be checked during acceptance testing.

Do not redistribute reference media, model weights or third-party runtimes without checking the applicable licenses. See the original MIT notice for the bundled YouTube utilities.

