FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Install GDAL and PostGIS dependencies for GeoDjango
RUN apt-get update && apt-get install -y     binutils     libproj-dev     gdal-bin     libgdal-dev     && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements /app/requirements
RUN pip install --no-cache-dir -r requirements/development.txt

COPY . /app/
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
