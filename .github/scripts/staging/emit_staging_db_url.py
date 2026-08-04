"""Print the staging database URL from the oddish-staging-db Modal secret.

The Supabase API redacts branch passwords, and the Modal CLI cannot read a
secret's value back, so a Modal function that mounts the secret is the one
way to recover the live URL after a password reset. Run from backend/:

    uv run modal run --env staging ../.github/scripts/staging/emit_staging_db_url.py

Only the URL goes to stdout. Pipe it into
`gh secret set STAGING_DATABASE_URL --env staging` and dispatch the
Staging Deploy workflow; the bootstrap job summary prints the exact recipe.
"""

import modal

app = modal.App("emit-staging-db-url")
image = modal.Image.debian_slim()


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("oddish-staging-db", environment_name="staging")],
)
def emit() -> str:
    import os

    return os.environ["ODDISH_DATABASE_URL"]


@app.local_entrypoint()
def main() -> None:
    print(emit.remote())
