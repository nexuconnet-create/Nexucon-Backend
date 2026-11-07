from django.core.management.base import BaseCommand
from apps.government.models import District

ZONES_DATA = [
    {
        "name": "Ikeja North",
        "code": "IKJ-N",
        "state_region": "Lagos",
        "description": "Ikeja North zonal jurisdiction covering CBD, Government Secretariat, and Ikeja GRA.",
        "office_address": "Block 4, Secretariat Complex, Alausa, Ikeja, Lagos",
    },
    {
        "name": "Ikeja Central & Capital District",
        "code": "IKJ-C",
        "state_region": "Lagos",
        "description": "Metropolitan center covering Ikeja Central, Maryland, Anthony, and Allen Avenue.",
        "office_address": "12 Commercial Avenue, Ikeja, Lagos",
    },
    {
        "name": "Victoria Island & Ikoyi Maritime",
        "code": "VI-IKY",
        "state_region": "Lagos",
        "description": "High-density coastal commercial and diplomatic zone, Banana Island, and Waterfront.",
        "office_address": "Plot 8A, Bishop Aboyade Cole Street, Victoria Island, Lagos",
    },
    {
        "name": "Lekki Phase 1 & Coastal Corridor",
        "code": "LEK-CST",
        "state_region": "Lagos",
        "description": "Rapidly growing residential and mixed-use urban expansion corridor along Lekki-Epe expressway.",
        "office_address": "Admiralty Way, Lekki Phase 1, Lagos",
    },
    {
        "name": "Epe & Lekki Free Trade Zone",
        "code": "EPE-FTZ",
        "state_region": "Lagos",
        "description": "Special industrial, manufacturing, refinery, and deep sea port development jurisdiction.",
        "office_address": "Free Trade Zone Expressway, Ibeju-Lekki / Epe, Lagos",
    },
    {
        "name": "Eti-Osa Central District",
        "code": "ETI-OSA",
        "state_region": "Lagos",
        "description": "Covers Chevron, Ajah, Sangotedo, and central Eti-Osa suburban developments.",
        "office_address": "Km 22, Lekki-Epe Expressway, Ajah, Lagos",
    },
    {
        "name": "Surulere & Yaba Innovation Zone",
        "code": "SRL-YAB",
        "state_region": "Lagos",
        "description": "High-density residential, university campus, and technology innovation precinct.",
        "office_address": "Herbert Macaulay Way, Yaba, Lagos",
    },
    {
        "name": "Apapa Marine & Logistics Zone",
        "code": "APP-LOG",
        "state_region": "Lagos",
        "description": "Seaport terminals, freight transit, container terminals, and heavy industrial facilities.",
        "office_address": "Wharf Road, Apapa, Lagos",
    },
    {
        "name": "Alimosho & Agege West",
        "code": "ALM-AGG",
        "state_region": "Lagos",
        "description": "Extensive residential suburban sector and medium-scale commercial centers.",
        "office_address": "Lagos-Abeokuta Expressway, Iyana-Ipaja, Lagos",
    },
    {
        "name": "Ikorodu Regional District",
        "code": "IKD-REG",
        "state_region": "Lagos",
        "description": "Major northern residential hub, agrarian boundary, and light manufacturing cluster.",
        "office_address": "Ikorodu Zonal Office, Lagos Road, Ikorodu, Lagos",
    },
    {
        "name": "Badagry Heritage & Border Zone",
        "code": "BDG-BDR",
        "state_region": "Lagos",
        "description": "Western trade corridor, international transit boundary, and heritage tourism developments.",
        "office_address": "Marina Badagry Waterfront Road, Badagry, Lagos",
    },
    {
        "name": "Oshodi-Isolo Industrial Corridor",
        "code": "OSH-ISO",
        "state_region": "Lagos",
        "description": "Industrial manufacturing clusters, logistics hubs, and airport transit corridor.",
        "office_address": "Apapa-Oshodi Expressway, Isolo, Lagos",
    },
    {
        "name": "Abuja Central Business District",
        "code": "FCT-CBD",
        "state_region": "Federal Capital Territory",
        "description": "Federal government administrative center, ministries, and diplomatic zone.",
        "office_address": "Central Area, Cadastral Zone A00, Abuja, FCT",
    },
    {
        "name": "Port Harcourt Industrial Zone",
        "code": "PHC-IND",
        "state_region": "Rivers",
        "description": "Petrochemical hubs, energy infrastructure, and coastal Niger Delta operations.",
        "office_address": "Trans-Amadi Industrial Layout, Port Harcourt, Rivers State",
    },
]

class Command(BaseCommand):
    help = "Seed comprehensive official operational zones (Districts) into the database."

    def handle(self, *args, **options):
        created_count = 0
        updated_count = 0
        for item in ZONES_DATA:
            district, created = District.objects.get_or_create(
                code=item["code"],
                defaults={
                    "name": item["name"],
                    "state_region": item["state_region"],
                    "description": item["description"],
                    "office_address": item["office_address"],
                    "is_active": True,
                }
            )
            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"Created District: {district.name} ({district.code})"))
            else:
                updated = False
                if district.name != item["name"]:
                    district.name = item["name"]
                    updated = True
                if not district.is_active:
                    district.is_active = True
                    updated = True
                if updated:
                    district.save()
                    updated_count += 1
                    self.stdout.write(self.style.WARNING(f"Updated District: {district.name} ({district.code})"))
                else:
                    self.stdout.write(f"District already exists: {district.name} ({district.code})")

        self.stdout.write(self.style.SUCCESS(
            f"Successfully seeded operational zones: {created_count} created, {updated_count} updated, {len(ZONES_DATA)} total active."
        ))
