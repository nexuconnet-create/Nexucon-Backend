import os
import django
import sys

# Setup Django
sys.path.append(r'C:\Users\USER\OneDrive\Desktop\coding\nexucon\Nexucon_backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from django.apps import apps
from django.conf import settings

for app_config in apps.get_app_configs():
    # Only process our local apps 
    # They might be in apps.* or just processing, scans, etc.
    # Let's filter by checking if the app path is within Nexucon_backend\apps
    if 'Nexucon_backend' not in app_config.path:
        continue
    
    models = app_config.get_models()
    model_names = [model.__name__ for model in models if not model._meta.abstract and not model._meta.proxy]
    
    if not model_names:
        continue
        
    admin_file_path = os.path.join(app_config.path, 'admin.py')
    
    existing_content = ""
    if os.path.exists(admin_file_path):
        with open(admin_file_path, 'r', encoding='utf-8') as f:
            existing_content = f.read()
            
    models_to_register = []
    for model_name in model_names:
        if f'register({model_name}' not in existing_content and f'register({model_name})' not in existing_content:
            models_to_register.append(model_name)
            
    if not models_to_register:
        continue
        
    new_content = ""
    if 'from django.contrib import admin' not in existing_content:
        new_content += "from django.contrib import admin\n"
        
    # Import the models. (Using * is easier to avoid import conflicts or duplicate imports, but let's just explicitly import what's needed)
    # We will do from .models import * just to make sure everything works without import errors if it was already imported partially.
    if 'from .models import' not in existing_content and 'from . import models' not in existing_content:
        new_content += f"from .models import {', '.join(models_to_register)}\n\n"
    else:
        new_content += f"from .models import {', '.join(models_to_register)}\n\n"
    
    for model_name in models_to_register:
        new_content += f"@admin.register({model_name})\n"
        new_content += f"class {model_name}Admin(admin.ModelAdmin):\n"
        new_content += f"    pass\n\n"
        
    with open(admin_file_path, 'a', encoding='utf-8') as f:
        if existing_content and not existing_content.endswith('\n'):
            f.write('\n')
        f.write(new_content)
        
    print(f"Registered {len(models_to_register)} models in {app_config.name}")
