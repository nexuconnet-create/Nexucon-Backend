#!/bin/bash

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput || true

# Apply database migrations
echo "Applying database migrations..."
python manage.py migrate || true

# Start Gunicorn
echo "Starting Gunicorn on 0.0.0.0:8000..."
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --timeout 120 --workers 3
