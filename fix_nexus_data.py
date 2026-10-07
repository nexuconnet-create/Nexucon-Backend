# -*- coding: utf-8 -*-
import os
import sys
import django

# Setup Django
sys.path.append(r'C:\Users\USER\OneDrive\Desktop\coding\nexucon\nexucon_backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from apps.projects.models import Project

for p in Project.objects.all()[:5]:
    print(f"Project ID: {p.id}, Name: {p.name}")
