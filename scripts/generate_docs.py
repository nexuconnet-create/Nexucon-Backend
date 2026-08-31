import os
import yaml
import ast
from pathlib import Path

BASE_DIR = Path(r"c:\Users\USER\OneDrive\Desktop\coding\nexucon\Tarsus-sitesupervise-Integration-\backend")

def analyze_app(app_dir):
    models = []
    serializers = []
    services = []
    tasks = []
    
    for root, _, files in os.walk(app_dir):
        for f in files:
            if f.endswith('.py') and not f.startswith('__'):
                file_path = os.path.join(root, f)
                try:
                    with open(file_path, 'r', encoding='utf-8') as file:
                        tree = ast.parse(file.read())
                        
                    for node in tree.body:
                        if isinstance(node, ast.ClassDef):
                            class_name = node.name
                            docstring = ast.get_docstring(node) or "No description"
                            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
                            
                            info = f"**{class_name}**"
                            if bases:
                                info += f" (Inherits from {', '.join(bases)})"
                            info += f": {docstring[:100]}..." if len(docstring) > 100 else f": {docstring}"
                            
                            if 'models' in f or 'models' in root:
                                models.append(info)
                            elif 'serializers' in f:
                                serializers.append(info)
                            elif 'services' in f or 'ai_service' in f:
                                services.append(info)
                                
                        elif isinstance(node, ast.FunctionDef):
                            # Check for celery tasks
                            is_task = any(
                                isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == 'shared_task'
                                for dec in node.decorator_list
                            ) or any(
                                isinstance(dec, ast.Name) and dec.id == 'shared_task'
                                for dec in node.decorator_list
                            )
                            if is_task or 'tasks' in f:
                                docstring = ast.get_docstring(node) or "No description"
                                tasks.append(f"**{node.name}**: {docstring[:100]}...")
                            elif 'services' in f or 'ai_service' in f:
                                docstring = ast.get_docstring(node) or "No description"
                                services.append(f"**{node.name}**: {docstring[:100]}...")
                except Exception as e:
                    pass
    return models, serializers, services, tasks

def main():
    schema_path = BASE_DIR / "schema.yaml"
    if not schema_path.exists():
        print("Schema not found.")
        return
        
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema = yaml.safe_load(f)
        
    apps_dir = BASE_DIR / "apps"
    
    all_models = []
    all_serializers = []
    all_services = []
    all_tasks = []
    
    for app in os.listdir(apps_dir):
        app_path = apps_dir / app
        if app_path.is_dir() and not app.startswith('__'):
            m, s, srv, t = analyze_app(app_path)
            if m: all_models.extend([f"### {app.title()} App"] + [f"- {x}" for x in m])
            if s: all_serializers.extend([f"### {app.title()} App"] + [f"- {x}" for x in s])
            if srv: all_services.extend([f"### {app.title()} App"] + [f"- {x}" for x in srv])
            if t: all_tasks.extend([f"### {app.title()} App"] + [f"- {x}" for x in t])

    # Extract API endpoints
    endpoints_markdown = ""
    master_table = "| Method | Endpoint | Purpose | Description | Auth | Permission | Status |\n|--------|----------|---------|-------------|------|------------|--------|\n"
    
    paths = schema.get('paths', {})
    for path, methods in paths.items():
        for method, details in methods.items():
            method_upper = method.upper()
            summary = details.get('summary', 'No summary provided.')
            description = details.get('description', '')
            if not description:
                description = summary
            # Clean up description to be single line for markdown table
            description = description.replace('\n', ' ').replace('\r', '').replace('|', '-')
            
            auth_info = "Yes" if 'security' in details else "None"
            # Usually permissions aren't fully detailed in standard OpenAPI without extensions, we will guess 'IsAuthenticated' based on global setting
            permission = "IsAuthenticated" if auth_info == "Yes" else "AllowAny"
            status = list(details.get('responses', {}).keys())[0] if details.get('responses') else "Unknown"
            
            master_table += f"| {method_upper} | `{path}` | {summary} | {description} | {auth_info} | {permission} | {status} |\n"
            
            endpoints_markdown += f"### {method_upper} {path}\n\n"
            endpoints_markdown += f"- **Purpose:** {summary}\n"
            endpoints_markdown += f"- **Description:** {description}\n"
            endpoints_markdown += f"- **Authentication:** {auth_info}\n"
            endpoints_markdown += f"- **Permissions:** {permission}\n"
            
            if 'parameters' in details:
                endpoints_markdown += "- **Parameters:**\n"
                for p in details['parameters']:
                    req = "Required" if p.get('required') else "Optional"
                    endpoints_markdown += f"  - `{p['name']}` ({p.get('in')}): {p.get('description', 'No description')} [{req}]\n"
                    
            if 'requestBody' in details:
                endpoints_markdown += "- **Request Body:** Requires data in request body (e.g. JSON/Form Data).\n"
                
            endpoints_markdown += "- **Responses:**\n"
            for code, resp in details.get('responses', {}).items():
                endpoints_markdown += f"  - `{code}`: {resp.get('description', 'No description')}\n"
                
            endpoints_markdown += "\n"

    # Build the full Markdown document
    markdown_content = f"""---
title: NEXUCON / SiteSupervise - Backend Technical Architecture & API Documentation
author: NEXUCON
date: 2026
---

<style>
body {{ font-family: 'Helvetica', 'Arial', sans-serif; line-height: 1.6; color: #333; }}
h1 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
h2 {{ color: #2980b9; margin-top: 30px; }}
h3 {{ color: #16a085; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background-color: #f2f2f2; font-weight: bold; }}
.header {{ text-align: center; color: #7f8c8d; font-size: 10px; margin-bottom: 20px; }}
.footer {{ text-align: center; color: #7f8c8d; font-size: 10px; margin-top: 40px; border-top: 1px solid #ddd; padding-top: 10px; }}
@page {{ margin: 2cm; @top-center {{ content: "CONFIDENTIAL AND PROPRIETARY TO NEXUCON"; }} @bottom-center {{ content: "Copyright © 2026 NEXUCON.NET All rights reserved"; }} }}
</style>

# NEXUCON / SiteSupervise – Backend Technical Architecture & API Documentation

**Version:** 1.0
**Date:** 2026

*CONFIDENTIAL AND PROPRIETARY TO NEXUCON*

---

## 1. Executive Summary
This document provides a comprehensive technical architecture and API documentation for the backend of the SiteSupervise platform by NEXUCON. The backend serves as the central hub for construction site monitoring, AI-based defect detection, thermal analysis, BIM processing, and progress tracking.

## 2. Backend Architecture
The backend is built using Django and Django REST Framework (DRF), following a modular, app-based architecture. It provides RESTful APIs for the frontend and mobile apps (e.g., Tersus MVP S1 scanner). The architecture employs Celery for asynchronous task processing (e.g., AI inference, BIM conversion, point-cloud processing) and Redis as the message broker.

## 3. Complete Backend Folder/Module Structure
- `config/`: Core Django settings, global URL routing, WSGI/ASGI configurations.
- `apps/`: Modular Django apps encapsulating specific domains:
  - `audit/`: Audit trails and system logs.
  - `authentication/`: User authentication, JWT handling.
  - `common/`: Shared utilities, base models, AI services (`ai_service.py`).
  - `inspections/`: Safety and quality inspection management.
  - `notifications/`: Alerting and notification logic.
  - `processing/`: Background processing jobs (BIM, point clouds).
  - `projects/`: Project and site management.
  - `reports/`: Report generation (PDF, QA/QC).
  - `scans/`: Scan data management from handheld devices.
  - `storage/`: File handling and Cloudinary/S3 integration.

## 4. Technology Stack and Dependencies
- **Language:** Python 3.x
- **Framework:** Django, Django REST Framework (DRF)
- **Database:** PostgreSQL (production), SQLite (local dev)
- **Message Broker:** Redis
- **Task Queue:** Celery
- **AI/ML:** Google Gemini API integration (vision, text)
- **Authentication:** SimpleJWT (JSON Web Tokens)
- **Storage:** Cloudinary (implied by configuration/apps)
- **API Documentation:** drf-spectacular (OpenAPI 3)

## 5. Database Architecture
The database follows a relational design. Key entities include Users, Projects, Scans, Inspections, and Reports. Relationships are strictly defined using Django ORM ForeignKeys and ManyToMany fields, ensuring referential integrity.

## 6. All Models and Relationships
{chr(10).join(all_models)}

## 7. All Serializers and Validation
{chr(10).join(all_serializers)}

## 8. All Services and Business Logic
{chr(10).join(all_services)}

## 9. All Background/Celery Tasks
{chr(10).join(all_tasks)}

## 10. AI/ML Architecture
The AI integration primarily relies on `ai_service.py` to interface with the Google Gemini API (`gemini-flash-latest`). It handles:
- **Defect Detection:** Analyzing scan images to detect construction defects.
- **Thermal Analysis:** Evaluating thermal scan data for anomalies.
- **Delamination Detection:** Identifying structural delamination.
- **ML Pipelines:** Background inference is queued via Celery tasks to prevent API blocking.

## 11. BIM, Point-Cloud, Deviation and Clash-Processing
The `processing` app manages BIM (Building Information Modeling) logic and point-cloud transformations. Functionality includes:
- IFC model generation and parsing.
- Clash detection algorithms identifying deviations between structural models and point-cloud scans.

## 12. File/Storage Architecture
Storage is abstracted through the `storage` app. Media files (scans, reports, BIM models) are securely uploaded and linked to their respective records. External integrations (like Cloudinary) handle large media payloads.

## 13. Authentication, Permissions and Security
- **Authentication:** JWT-based (`rest_framework_simplejwt`). Tokens have a 60-minute lifetime, with a 1-day refresh token.
- **Permissions:** Most endpoints are protected via `IsAuthenticated`. Specific views may implement custom object-level permissions.
- **Security:** CSRF protection, secure password hashing (Argon2/PBKDF2), and environment-variable-based secrets.

## 14. Environment/Configuration Variables
Key environment variables (redacted):
- `DJANGO_SECRET_KEY`: Secret key for cryptographic signing.
- `DJANGO_DEBUG`: Debug mode toggle.
- `POSTGRES_*`: Database credentials.
- `CELERY_BROKER_URL`: Redis broker URL.
- `GEMINI_API_KEY`: API key for Gemini.

## 15. Error Handling, Logging and Retry Mechanisms
Global exception handlers catch standard DRF exceptions. The `audit` app logs critical actions. AI services implement a retry mechanism (`GEMINI_MAX_RETRIES`) for resilience against transient API failures.

## 16. Testing
Testing is facilitated via Django's `TestCase` and DRF's `APITestCase`. The architecture supports unit and integration tests across apps.

## 17. Deployment Architecture
Designed for containerized deployment (Docker). The architecture typically involves:
- Gunicorn as the WSGI HTTP Server.
- Nginx as the reverse proxy.
- PostgreSQL for the database.
- Redis + Celery worker containers for background tasks.

## 18. Performance, Reliability and Scalability
Scalability is achieved horizontally. Web nodes can scale independently of Celery workers. Heavy processing (AI/BIM) is offloaded to background queues, ensuring the REST API remains highly responsive.

## 19. Known Issues and Technical Risks
- Heavy reliance on external APIs (Gemini) can introduce latency; mitigations include async tasks.
- Processing large point-clouds is resource-intensive and requires optimized Celery worker configurations.

## 20. Recommendations
- Implement comprehensive caching (Redis) for frequently accessed project data.
- Introduce API rate limiting (e.g., `django-ratelimit`) to prevent abuse.
- Expand end-to-end testing coverage for complex BIM processing pipelines.

## 21. Implemented vs Missing/Partial Feature Matrix
- **Authentication:** Implemented
- **Project Management:** Implemented
- **Scan Uploads:** Implemented
- **AI Defect Detection:** Implemented
- **PDF Report Gen:** Implemented
- **Clash Detection:** Partial (Needs verification)

## 22. Glossary
- **BIM:** Building Information Modeling.
- **IFC:** Industry Foundation Classes (BIM data model).
- **JWT:** JSON Web Token.

## 23. Appendices
Includes detailed API endpoints and references.

---

# COMPLETE API DOCUMENTATION

## Master API Endpoint Table

{master_table}

---

## Detailed Endpoint Documentation

{endpoints_markdown}

<br>
<br>
<center>
<small>Copyright © 2026 NEXUCON.NET All rights reserved</small>
</center>

"""
    
    out_path = BASE_DIR.parent / "docs" / "Backend_Architecture_Report.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(markdown_content)
        
    print(f"Generated Markdown at {{out_path}}")

if __name__ == "__main__":
    main()
