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

```bash
python -m oddish.db init
python -m oddish.db setup
```
