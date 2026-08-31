import os
import django
import sys
import uuid
from decimal import Decimal

# Setup Django
sys.path.append(r'C:\Users\USER\OneDrive\Desktop\coding\nexucon\Nexucon_backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from apps.projects.models import Project

def create_dummy_projects():
    # Dummy Project 1
    p1 = Project.objects.create(
        name="Skyline Horizon Towers",
        project_type="Commercial",
        description="A massive dual-tower commercial skyscraper featuring modern AI-managed climate control systems and an advanced observation deck.",
        status="ACTIVE",
        estimated_project_value=Decimal('150000000.00'),
        number_of_floors=55,
        developer_name="Apex Construction Group",
        developer_organization="Apex Corp",
        developer_email="contact@apexcorp.com",
        developer_phone="+1 555-0198",
        site_address="42 Horizon Boulevard",
        state="Lagos",
        lga="Eti-Osa",
        ward_area="Victoria Island",
    )
    
    # Dummy Project 2
    p2 = Project.objects.create(
        name="Lakeside Eco-Estate",
        project_type="Residential",
        description="A 200-unit residential eco-estate built entirely with sustainable materials and solar-powered smart home features.",
        status="APPROVED",
        estimated_project_value=Decimal('45000000.00'),
        number_of_floors=3,
        developer_name="GreenLeaf Developments",
        developer_organization="GreenLeaf Ltd",
        developer_email="info@greenleafeco.com",
        developer_phone="+1 555-0222",
        site_address="Plot 101, Lakeside Avenue",
        state="Lagos",
        lga="Ikeja",
        ward_area="Alabang",
    )
    
    print(f"Created project: {p1.name} (Status: {p1.status})")
    print(f"Created project: {p2.name} (Status: {p2.status})")

if __name__ == '__main__':
    create_dummy_projects()
