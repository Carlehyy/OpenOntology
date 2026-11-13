#!/bin/sh
# Derive /tmp/alembic.ini (script_location -> /work/alembic) then run the
# Alembic CLI against the fixture DATABASE_URL supplied by the container env.
sed 's|^script_location = .*|script_location = /work/alembic|' /work/alembic.ini > /tmp/alembic.ini
exec python3 -m alembic -c /tmp/alembic.ini "$@"
