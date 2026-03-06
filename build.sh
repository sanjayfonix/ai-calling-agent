#!/usr/bin/env bash
# Render Build Script
# This runs during each deploy on Render

set -o errexit  # Exit on error

echo "=== Installing Python dependencies ==="
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Running database migrations ==="
alembic upgrade head

echo "=== Creating database tables ==="
python -c "
import asyncio
from app.database import init_db, close_db

async def setup():
    await init_db()
    print('Database tables created/verified')
    await close_db()

asyncio.run(setup())
"

echo "=== Build complete ==="
