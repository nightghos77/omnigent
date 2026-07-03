# Deploying this fork to Render (source-controlled) + Modal managed hosts

Everything about the deploy lives in this repo:

| File | Role |
|---|---|
| `render.yaml` | Render Blueprint: web service (overlay build) + managed Postgres + 10GB disk + env |
| `deploy/render/Dockerfile` | Thin overlay: pinned base image + baked config + `OMNIGENT_CONFIG` |
| `deploy/render/server-config.yaml` | Non-secret server config — the Modal `sandbox:` block |

Auth is built-in **`accounts`** (multi-user, invite-only). Agents execute in
disposable **Modal** sandboxes, one per session — no machine stays online.

Nothing secret is committed. Secrets are set in the Render dashboard
(`sync: false`) and in Modal.

---

## One-time prerequisites

1. **Modal account** — sign up at https://modal.com.
2. **Modal API token** — https://modal.com/settings/tokens → create one.
   Note the `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`.
3. **Modal LLM secret** — holds the model keys injected into every sandbox:
   ```bash
   pip install modal && modal token new          # logs the CLI in
   modal secret create omnigent-llm \
     OMNIGENT_ANTHROPIC_API_KEY=sk-ant-...        # + OPENAI_API_KEY=... etc. if used
   ```
   The name `omnigent-llm` must match `modal.secrets` in
   `deploy/render/server-config.yaml`.

---

## Deploy

### 1. Connect the Blueprint
Render dashboard → **New → Blueprint** → connect **github.com/nightghos77/omnigent**
→ Render reads `render.yaml`, provisions the web service + Postgres + disk, and
builds the overlay image. First build takes a few minutes.

### 2. Set the Modal token secrets
Service → **Environment** → fill in the two `sync:false` vars:
`MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`.

### 3. Pin `server_url`, then push
After the first deploy Render assigns a URL (e.g. `https://omnigent.onrender.com`,
or attach a custom domain). Put it in `deploy/render/server-config.yaml`:
```yaml
sandbox:
  server_url: https://omnigent.onrender.com   # your real URL
```
Commit + push → Render rebuilds automatically. (Managed sandboxes need this to
dial back to the server. The blueprint's `accounts` auth base URL is
auto-detected and does not depend on this value.)

### 4. Get the admin login
Service → **Logs** on first boot prints the generated `admin` password. It is
persisted at `/data/admin-credentials` (survives redeploys). Sign in at the
service URL as `admin`.

---

## Invite your team (non-technical friendly)

Web UI → sign in as `admin` → **Admin → Members → Invite** → creates a
single-use link (no email server). Send it; the teammate opens it, sets a
password, and they're in. Signup is invite-only.

> Switch to Google/GitHub SSO later: set `OMNIGENT_AUTH_PROVIDER=oidc` +
> `OMNIGENT_OIDC_*` vars in the dashboard. See `deploy/render/README.md`.

---

## Run agents

Web UI → **New Chat** → pick a **managed** host → the server spins up a Modal
sandbox and runs the agent (Claude Code, Codex, …) there. To work on a GitHub
repo, have the agent clone it inside the session; put a `GIT_TOKEN` in the
`omnigent-llm` Modal secret (and list it under `modal.secrets`) if the repo is
private.

> Modal caps sandbox lifetime at 24h; a new session gets a fresh sandbox.

---

## Governance

Cost budgets, tool-call approvals, and access controls are enforced at the
meta-harness layer (policies), not via prompts — useful when non-technical
teammates drive agents. See the policies docs / `examples/`.

---

## Maintenance

- **Upgrade the server**: refresh the digest in `deploy/render/Dockerfile`
  (`ghcr.io` token → `HEAD /v2/omnigent-ai/omnigent-server/manifests/latest` →
  copy `Docker-Content-Digest`), commit, push. Reproducible, tracked bumps.
- **Sync upstream**: `git fetch upstream && git merge upstream/main`.
- **More memory**: bump `plan: starter` → `standard` in `render.yaml` if the
  server OOM-loops (it idles ~275MB; starter is 512MB).
