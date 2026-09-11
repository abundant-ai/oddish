# Delivery refresh browser tests

Run `pnpm test:e2e:delivery` from `frontend/`. The dedicated Playwright config
starts this local Next.js app on port 3109. It imports the production delivery
board, SWR provider, styles, and controls. Only Clerk authentication is replaced
with a test admin; production middleware and routes are unchanged. No credentials,
backend service, analysis workers, or paid providers are used.

The app also imports the production delivery route's loading boundary.
`?seed=slow` delays the server snapshot by three seconds to verify that the
placeholder streams before the board and that hydration adds no board request.

Playwright intercepts the API responses and advances the browser clock by the
production 15-second interval. The review-completion test fails on the original
board because expanded history keeps displaying `qa (running)` after the board
refresh, despite the available response containing `qa (success)`.

For an inline browser demonstration, start
`pnpm exec next dev e2e/delivery-app --webpack -p 3109` and open
`http://localhost:3109/?task=task-a`. The local API provides fixture responses.
While leaving the page open, POST JSON to `/api/control`:

- `{"version":7,"latest":7,"status":"running"}` resets the scenario.
- `{"latest":8}` creates a non-default version.
- `{"version":8}` selects v8.
- `{"status":"success"}` completes the existing v7 review.
- `{"failure":"deliveries"}` or `{"failure":"qa-history"}` makes that read fail.
- `{"failure":""}` restores reads; use the adjacent retry button.

These controls change only in-memory fixtures in this test app. They are not
production API endpoints. The tests in `oddish/tests/test_delivery_refresh_api.py`
separately exercise the real HTTP routes with PostgreSQL and no worker lifespan.
Use `ODDISH_DATABASE_URL` pointing at a disposable local database with the core
schema initialized, as for the existing delivery tests. Each test rolls back its
outer transaction after exercising request commits in separate sessions.
