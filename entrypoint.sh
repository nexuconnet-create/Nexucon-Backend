#!/bin/bash

# Exit on error
set -o errexit

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput

# Create PostGIS extension if it doesn't exist (required for Render PostgreSQL)
echo "Ensuring PostGIS extension is enabled..."
python manage.py shell -c "from django.db import connection; cursor = connection.cursor(); cursor.execute('CREATE EXTENSION IF NOT EXISTS postgis;')" || echo "Warning: Could not create postgis extension, it may already exist or require superuser privileges."

# Apply database migrations
echo "Applying database migrations..."
python manage.py migrate

# Seed comprehensive initial data
echo "Seeding comprehensive data..."
python seed_comprehensive_data.py || echo "Seeding completed or non-fatal issue encountered."

# Start Gunicorn
echo "Starting Gunicorn..."
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --timeout 120 --workers 3
