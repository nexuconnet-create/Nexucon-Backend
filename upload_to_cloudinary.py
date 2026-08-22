import os
import django
import hashlib
import time
import urllib.request
import json

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from apps.monitoring.models import DailySiteUpdate
from apps.projects.models import Project

CLOUD_NAME = 'fspyt1uw'
API_KEY = '226324943154255'
API_SECRET = 'xEYPJUsx6Gih1uxUBZhiQ9K7VK0'
FOLDER = 'nexucon/daily_updates'

# High-res construction site photos to upload to the Cloudinary account
SOURCE_IMAGES = [
    {
        'tag': 'eko_atlantic_piling',
        'url': 'https://images.unsplash.com/photo-1541888946425-d0fbb180c5f2?w=1200&q=80'
    },
    {
        'tag': 'eko_atlantic_concrete',
        'url': 'https://images.unsplash.com/photo-1504307651254-35680f356dfd?w=1200&q=80'
    },
    {
        'tag': 'victoria_island_facade',
        'url': 'https://images.unsplash.com/photo-1503387762-592deb58ef4e?w=1200&q=80'
    },
    {
        'tag': 'victoria_island_glass',
        'url': 'https://images.unsplash.com/photo-1590486803833-1c5dc8ddd4c8?w=1200&q=80'
    },
    {
        'tag': 'lekki_drone_survey',
        'url': 'https://images.unsplash.com/photo-1581094794329-c8112a89af12?w=1200&q=80'
    },
    {
        'tag': 'ikoyi_hvac_mep',
        'url': 'https://images.unsplash.com/photo-1513694203232-719a280e022f?w=1200&q=80'
    }
]

def upload_image_to_cloudinary(image_url):
    timestamp = int(time.time())
    params_to_sign = f"folder={FOLDER}&timestamp={timestamp}{API_SECRET}"
    signature = hashlib.sha1(params_to_sign.encode('utf-8')).hexdigest()

    try:
        req = urllib.request.Request(image_url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'})
        with urllib.request.urlopen(req, timeout=15) as response:
            img_bytes = response.read()
    except Exception as e:
        print(f"Failed to download source image {image_url}: {e}")
        return None

    boundary = f"----WebKitFormBoundary{int(time.time()*1000)}"
    body_parts = []
    
    fields = {
        'api_key': API_KEY,
        'timestamp': str(timestamp),
        'signature': signature,
        'folder': FOLDER
    }
    for k, v in fields.items():
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode('utf-8'))
    
    file_header = f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"site_image.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode('utf-8')
    body = b"".join(body_parts) + file_header + img_bytes + f"\r\n--{boundary}--\r\n".encode('utf-8')

    upload_url = f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}/image/upload"
    req_upload = urllib.request.Request(
        upload_url,
        data=body,
        headers={
            'Content-Type': f"multipart/form-data; boundary={boundary}",
            'User-Agent': 'Nexucon-Migration/1.0'
        }
    )

    try:
        with urllib.request.urlopen(req_upload, timeout=30) as upload_resp:
            result = json.loads(upload_resp.read().decode('utf-8'))
            secure_url = result.get('secure_url')
            print(f"Uploaded successfully to Cloudinary ({CLOUD_NAME}): {secure_url}")
            return secure_url
    except Exception as e:
        print(f"Cloudinary upload error: {e}")
        return None

def main():
    print(f"Starting Cloudinary migration to account: {CLOUD_NAME} (Key: {API_KEY})...")
    cloudinary_urls = []

    for item in SOURCE_IMAGES:
        url = upload_image_to_cloudinary(item['url'])
        if url:
            cloudinary_urls.append(url)

    print(f"\nTotal successfully uploaded to Cloudinary: {len(cloudinary_urls)}")

    if not cloudinary_urls:
        print("No images were uploaded.")
        return

    # Update DailySiteUpdate records in database
    updates = list(DailySiteUpdate.objects.all().order_by('id'))
    if updates:
        for idx, u in enumerate(updates):
            if idx == 0:
                u.photos = [cloudinary_urls[0], cloudinary_urls[1]] if len(cloudinary_urls) > 1 else cloudinary_urls[:1]
            elif idx == 1:
                u.photos = [cloudinary_urls[2], cloudinary_urls[3]] if len(cloudinary_urls) > 3 else cloudinary_urls[:1]
            elif idx == 2:
                u.photos = [cloudinary_urls[4]] if len(cloudinary_urls) > 4 else cloudinary_urls[:1]
            elif idx == 3:
                u.photos = [cloudinary_urls[5]] if len(cloudinary_urls) > 5 else cloudinary_urls[:1]
            else:
                u.photos = [cloudinary_urls[idx % len(cloudinary_urls)]]
            u.save()
            print(f"Updated DailySiteUpdate {u.update_reference} with Cloudinary URLs: {u.photos}")
    else:
        print("No existing updates found in database.")

    print("\nCloudinary image migration completed successfully!")

if __name__ == '__main__':
    main()
