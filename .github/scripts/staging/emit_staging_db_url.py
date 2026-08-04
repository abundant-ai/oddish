"""Print the staging database URL from the oddish-staging-db Modal secret.

The Supabase API redacts branch passwords, and the Modal CLI cannot read a
secret's value back, so a Modal function that mounts the secret is the one
way to recover the live URL after a password reset. Run from backend/:

    uv run modal run --env staging ../.github/scripts/staging/emit_staging_db_url.py

The URL is the only thing this script prints, but `modal run` writes its own
progress output around it. Select the URL line and check it is non-empty
before writing any secret: `gh secret set` stores whatever reaches its stdin,
including nothing. The bootstrap job summary prints a recipe that does both,
then dispatches the Staging Deploy workflow.
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
