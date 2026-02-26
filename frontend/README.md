# Oddish Frontend

Next.js dashboard for the Oddish evaluation platform with Clerk authentication.

## Overview

The frontend provides:

- **Experiment dashboard** - View evaluations, progress, and results
- **Task + trial details** - Logs, Harbor stages, result.json viewer
- **Public sharing** - Tokenized experiment share pages
- **Public datasets** - Browse all published experiments at `/datasets`
- **Settings** - API key management
- **Clerk auth** - Organization-based user management
- **Backend proxying** - Route handlers that forward requests to local or Modal backend

## Quick Start

### 1. Install dependencies

```bash
pnpm install
```

### 2. Configure environment

Copy the example env file and configure:

```bash
cp env.example .env.local
```

Required variables:

```bash
# Clerk (get from clerk.com dashboard)
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_...
CLERK_SECRET_KEY=sk_...
CLERK_JWT_TEMPLATE=oddish # optional, recommended for org claims

# Backend type (local or modal)
NEXT_PUBLIC_BACKEND_TYPE=modal

# For local backend
FASTAPI_URL=http://localhost:8000

# For Modal backend (defaults to Oddish Cloud if not set)
NEXT_PUBLIC_MODAL_BASE_URL=https://abundant-ai
```

### 3. Start development server

```bash
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000).

## Architecture

The frontend is a Next.js App Router dashboard that talks to Oddish backend via server-side route handlers (`src/app/api/*`). Browser clients call internal routes; handlers resolve backend URLs and forward auth headers.

### Request flow

```
Browser UI
  │
  ▼
Next.js app routes (`src/app/(app)/*`)
  │
  ▼
Next.js route handlers (`src/app/api/*`)
  │  - resolve backend URL
  │  - attach auth headers
  ▼
Oddish backend (local FastAPI or Modal API)
```

## Backend Switching

The dashboard can connect to either a local FastAPI backend or the Modal deployment.

### Quick switch

```bash
pnpm dev:local   # Use local backend (localhost:8000)
pnpm dev:modal   # Use Modal backend
```

### Manual configuration

Set `NEXT_PUBLIC_BACKEND_TYPE` in `.env.local`:

```bash
# Use local FastAPI
NEXT_PUBLIC_BACKEND_TYPE=local
FASTAPI_URL=http://localhost:8000

# Use Modal deployment (defaults to Oddish Cloud if not set)
NEXT_PUBLIC_BACKEND_TYPE=modal
NEXT_PUBLIC_MODAL_BASE_URL=https://abundant-ai
```

### How it works

The `src/lib/backend-config.ts` module provides centralized URL management:

```typescript
import { getBackendUrl } from "@/lib/backend-config";

// Returns the correct URL based on NEXT_PUBLIC_BACKEND_TYPE
const url = getBackendUrl("tasks");
```

Modal endpoint resolution:

- `NEXT_PUBLIC_MODAL_ENV` (e.g. `dev` adds `-dev` suffixes)
- `NEXT_PUBLIC_MODAL_API_URL` (full API override)
- `NEXT_PUBLIC_MODAL_${ENDPOINT_NAME}_URL` (per-endpoint override)

## Project Structure

```
frontend/
├── src/
│   ├── app/
│   │   ├── (app)/
│   │   │   ├── dashboard/
│   │   │   │   └── page.tsx      # Dashboard
│   │   │   ├── experiments/
│   │   │   │   ├── page.tsx      # Experiments landing
│   │   │   │   └── [experiment]/ # Experiment detail + panels
│   │   │   ├── settings/
│   │   │   │   └── page.tsx      # API key management
│   │   │   └── admin/
│   │   │       └── page.tsx      # Admin tooling
│   │   ├── api/                   # Proxy route handlers to backend
│   │   ├── share/
│   │   │   └── [token]/
│   │   │       └── page.tsx      # Public experiment share
│   │   ├── datasets/
│   │   │   ├── page.tsx          # Public datasets landing
│   │   │   └── [token]/page.tsx  # Public dataset detail
│   │   ├── page.tsx              # Home (redirects to dashboard)
│   │   └── layout.tsx            # Root layout with Clerk
│   ├── components/
│   │   ├── nav.tsx               # Navigation bar
│   │   ├── status-badge.tsx      # Trial status badges
│   │   ├── harbor-stage-*.tsx    # Harbor stage components
│   │   └── ui/                   # shadcn/ui components
│   ├── lib/
│   │   ├── api.ts                # API client helpers
│   │   ├── backend-config.ts     # Backend URL management
│   │   ├── types.ts              # TypeScript types
│   │   └── utils.ts              # Utility functions
│   └── middleware.ts             # Clerk auth middleware
├── public/
│   └── oddish.jpg                # Logo
└── tailwind.config.ts            # Tailwind configuration
```

## API Routes

The frontend proxies requests to the backend through Next.js route handlers:

| Frontend Route                                  | Method(s)     | Backend Route                               |
| ----------------------------------------------- | ------------- | ------------------------------------------- |
| `/api/health`                                   | GET           | `/health`                                   |
| `/api/dashboard`                                | GET           | `/dashboard`                                |
| `/api/tasks`                                    | GET           | `/tasks`                                    |
| `/api/tasks/[task_id]`                          | GET, DELETE   | `/tasks/{task_id}`                          |
| `/api/tasks/[task_id]/trials`                   | GET           | `/tasks/{task_id}/trials`                   |
| `/api/tasks/[task_id]/trials/[index]`           | GET           | `/tasks/{task_id}/trials/{index}`           |
| `/api/tasks/[task_id]/files`                    | GET           | `/tasks/{task_id}/files`                    |
| `/api/tasks/[task_id]/files/[...path]`          | GET           | `/tasks/{task_id}/files/{path}`             |
| `/api/trials/[trial_id]/retry`                  | POST          | `/trials/{trial_id}/retry`                  |
| `/api/trials/[trial_id]/logs`                   | GET           | `/trials/{trial_id}/logs`                   |
| `/api/trials/[trial_id]/logs/structured`        | GET           | `/trials/{trial_id}/logs/structured`        |
| `/api/trials/[trial_id]/trajectory`             | GET           | `/trials/{trial_id}/trajectory`             |
| `/api/trials/[trial_id]/result`                 | GET           | `/trials/{trial_id}/result`                 |
| `/api/trials/[trial_id]/files`                  | GET           | `/trials/{trial_id}/files`                  |
| `/api/trials/[trial_id]/files/[...path]`        | GET           | `/trials/{trial_id}/files/{path}`           |
| `/api/experiments/[experiment]/tasks`           | GET           | `/tasks?experiment_id={id}`                 |
| `/api/experiments/[experiment]`                 | PATCH, DELETE | `/experiments/{id}`                         |
| `/api/experiments/[experiment]/share`           | GET           | `/experiments/{id}/share`                   |
| `/api/experiments/[experiment]/publish`         | POST          | `/experiments/{id}/publish`                 |
| `/api/experiments/[experiment]/unpublish`       | POST          | `/experiments/{id}/unpublish`               |
| `/api/settings/api-keys`                        | GET, POST     | `/api-keys`                                 |
| `/api/settings/api-keys/[key_id]`               | DELETE        | `/api-keys/{key_id}`                        |
| `/api/admin/slots`                              | GET           | `/admin/slots`                              |
| `/api/admin/pgqueuer`                           | GET           | `/admin/pgqueuer`                           |
| `/api/public/experiments`                       | GET           | `/public/experiments`                       |
| `/api/public/experiments/[token]`               | GET           | `/public/experiments/{token}`               |
| `/api/public/experiments/[token]/tasks`         | GET           | `/public/experiments/{token}/tasks`         |
| `/api/public/trials/[trial_id]/logs`            | GET           | `/public/trials/{trial_id}/logs`            |
| `/api/public/trials/[trial_id]/logs/structured` | GET           | `/public/trials/{trial_id}/logs/structured` |
| `/api/public/trials/[trial_id]/trajectory`      | GET           | `/public/trials/{trial_id}/trajectory`      |
| `/api/public/trials/[trial_id]/result`          | GET           | `/public/trials/{trial_id}/result`          |
| `/api/public/trials/[trial_id]/files`           | GET           | `/public/trials/{trial_id}/files`           |
| `/api/public/trials/[trial_id]/files/[...path]` | GET           | `/public/trials/{trial_id}/files/{path}`    |
| `/api/public/tasks/[task_id]/files`             | GET           | `/public/tasks/{task_id}/files`             |
| `/api/public/tasks/[task_id]/files/[...path]`   | GET           | `/public/tasks/{task_id}/files/{path}`      |

### Route handler pattern

Each route handler resolves the backend URL and forwards the current auth token:

```typescript
export async function GET() {
  const { getToken } = await auth();
  const token = await getClerkToken(getToken);

  const response = await fetch(getBackendUrl("tasks"), {
    headers: getAuthHeaders(token),
  });

  return Response.json(await response.json());
}
```

## Authentication

The frontend uses [Clerk](https://clerk.com) for authentication:

1. Users sign in via Clerk
2. API requests include Clerk session token
3. Backend validates token and auto-provisions orgs/users

The frontend forwards auth through both `Authorization` and `X-Clerk-Authorization` compatible header paths used by backend auth parsing.

### JWT template

Oddish expects Clerk JWTs to include org context and role. Create a JWT template
in Clerk with these custom claims and set `CLERK_JWT_TEMPLATE` in the frontend
environment:

```json
{
  "email": "{{user.primary_email_address}}",
  "org_id": "{{org.id}}",
  "org_role": "{{org.role}}"
}
```

### Clerk webhooks

Configure Clerk webhooks to keep org/user roles in sync. Use this endpoint:

```
https://abundant-ai--api.modal.run/webhooks/clerk
```

## Development

### Local backend workflow

```bash
# Terminal 1: Start backend
cd ../oddish
docker compose up -d db
uv run python -m oddish.api

# Terminal 2: Start frontend
cd ../frontend
pnpm dev:local
```

### Modal backend workflow

```bash
# Terminal 1: Start Modal API
cd ../backend
modal serve deploy.py

# Terminal 2: Start frontend (explicit API URL override)
export NEXT_PUBLIC_MODAL_API_URL="https://username--api-dev.modal.run"
pnpm dev:modal
```

Equivalent modal workflow without explicit override:

```bash
# frontend pointed at default modal URL derivation
cd ../frontend
pnpm dev:modal
```

### Use Clerk production keys locally (optional)

Clerk recommends using development keys for normal local work. If you need to
debug production-only auth behavior, use the helper script in this folder.

1. Add a local hosts entry:

```bash
echo "127.0.0.1 local.oddish.app" | sudo tee -a /etc/hosts
```

2. Set `.env.local` to production Clerk keys and app URL:

```bash
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_...
CLERK_SECRET_KEY=sk_live_...
NEXT_PUBLIC_APP_URL=https://local.oddish.app
```

3. Start HTTPS dev server on port 443:

```bash
./run-prod-clerk-local.sh
```

The script checks `/etc/hosts`, generates local certs via `mkcert`, and starts
Next.js with `https://local.oddish.app` so Clerk production origin checks pass.

### Build for production

```bash
pnpm build
pnpm start
```

## Styling

The frontend uses:

- **Tailwind CSS** for styling
- **shadcn/ui** for components
- **Lucide** for icons

To add a new shadcn/ui component:

```bash
pnpm dlx shadcn@latest add button
```

## Troubleshooting

### "Failed to fetch" errors

Check that the backend is running and the URL is correct:

```bash
# Test backend health
curl http://localhost:8000/health              # local
curl https://abundant-ai--api.modal.run/health  # modal
```

### Clerk authentication errors

1. Verify Clerk keys in `.env.local`
2. Check browser console for detailed errors

### CORS errors

The backend allows all origins by default. If you see CORS errors:

1. Check that you're using the route handlers (not calling backend directly from browser)
2. Verify `FASTAPI_URL` / `NEXT_PUBLIC_MODAL_BASE_URL` is set correctly

### "Organization not found" after login

1. Confirm your Clerk JWT template includes `org_id`
2. Ensure the backend has processed Clerk webhook events
3. Verify the frontend is pointed at the intended backend environment
