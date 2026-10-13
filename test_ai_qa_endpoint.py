#!/usr/bin/env python
"""
Nexucon QA Assistant - Chartered Structural Engineer Verification Runner.

Tests page-by-page NDT report auditing powered by Anthropic Claude or DeepSeek
against real project test data and statutory engineering standards.

Usage:
    python test_ai_qa_endpoint.py
    python test_ai_qa_endpoint.py --page 24 --model anthropic
    python test_ai_qa_endpoint.py --page 5 --model deepseek --query "Check 4-corner coordinates"
"""
import os
import sys
import argparse
import django

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from apps.projects.models import Project
from apps.reports.views import NDTReportPageQAView
from rest_framework.test import APIRequestFactory, force_authenticate
from django.contrib.auth import get_user_model

def run_test(page_number=24, model_provider="anthropic", query=None):
    User = get_user_model()
    user = User.objects.filter(is_superuser=True).first() or User.objects.first()
    if not user:
        print("[ERROR] No user found in database.")
        return

    # Find Ladol project or first project
    project = Project.objects.filter(name__icontains="Ladol").first() or Project.objects.first()
    if not project:
        print("[ERROR] No project found in database.")
        return

    print("=" * 75)
    print("NEXUCON QA ASSISTANT — CHARTERED STRUCTURAL ENGINEER AUDIT")
    print("=" * 75)
    print(f"Project:        {project.name} ({project.id})")
    print(f"Target Page:    Page {page_number}")
    print(f"Model Choice:   {model_provider.upper()}")
    print(f"User Query:     {query or 'Comprehensive automated engineering scan'}")
    print(f"Auditing User:  {user.email}")
    print("-" * 75)
    print("Extracting active PDF page buffer and calling AI service...")

    factory = APIRequestFactory()
    req = factory.post(
        f'/api/v1/reports/projects/{project.id}/ndt-report-qa/',
        {
            'page_number': page_number,
            'model_provider': model_provider,
            'user_query': query or 'Perform an automated engineering QA scan of this active page.',
        },
        format='json'
    )
    force_authenticate(req, user=user)

    view = NDTReportPageQAView.as_view()
    resp = view(req, project_id=str(project.id))

    if resp.status_code != 200:
        print(f"[FAIL] HTTP {resp.status_code}: {resp.data}")
        return

    data = resp.data
    print(f"\n[HTTP 200 SUCCESS]")
    print(f"Provider Used: {data.get('provider_used')} | Model: {data.get('model_used')}")
    print(f"Page Range:    Page {data.get('page_number')} of {data.get('total_pages')}")
    print("\n" + "=" * 75)
    print("AUDIT FINDINGS SUMMARY")
    print("=" * 75)

    print(f"\n[VERIFIED] ({len(data.get('verified', []))} items):")
    for v in data.get('verified', []):
        print(f"  [OK] {v}")

    discrepancies = data.get('discrepancies', [])
    print(f"\n[DISCREPANCY] ({len(discrepancies)} items):")
    if not discrepancies:
        print("  [OK] None detected (100% data consistency)")
    else:
        for d in discrepancies:
            print(f"  [X] Error:       {d.get('error')}")
            print(f"      Raw Value:   {d.get('raw_payload_value')}")
            print(f"      Replacement: {d.get('exact_replacement')}")

    compliance_flags = data.get('compliance_flags', [])
    print(f"\n[COMPLIANCE FLAG] ({len(compliance_flags)} items):")
    if not compliance_flags:
        print("  [OK] Full statutory compliance confirmed")
    else:
        for c in compliance_flags:
            print(f"  [!] {c}")

    print("\n" + "=" * 75)
    print("NARRATIVE FEEDBACK (Chartered Engineer Report):")
    print("=" * 75)
    print(data.get('narrative_feedback', ''))
    print("=" * 75)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Test Nexucon QA Assistant")
    parser.add_argument('--page', type=int, default=24, help="Page number to inspect")
    parser.add_argument('--model', type=str, default='anthropic', choices=['anthropic', 'deepseek', 'auto'], help="AI model choice")
    parser.add_argument('--query', type=str, default=None, help="Custom engineering question")
    args = parser.parse_args()

    run_test(page_number=args.page, model_provider=args.model, query=args.query)
