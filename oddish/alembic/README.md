# Database Migrations

Alembic migrations for Oddish live here.

## Common commands

```bash
alembic upgrade head
alembic downgrade -1
alembic revision --autogenerate -m "describe changes"
```

## Notes

- Always review auto-generated migrations before applying them

## Helper CLI

With `ODDISH_DATABASE_URL` set (for example in `oddish/.env`):

```bash
python -m oddish.db init    # run migrations only
python -m oddish.db setup   # alias for init
```
