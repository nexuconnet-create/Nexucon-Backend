from django.db import migrations

REPORT_TEMPLATES = [
    {
        'name': 'QA/QC Summary',
        'description': 'The full engineering drawing set: site health dashboard, annotated site map, defect action cards, progress validation, thermal analysis, BIM deviation, clash detection and prioritised recommendations.',
        'report_type': 'qaqc',
        'sort_order': 10,
    },
    {
        'name': 'Progress Report',
        'description': 'Construction progress validation: LiDAR point-cloud coverage against the approved BIM design, mapped area and progress score.',
        'report_type': 'progress',
        'sort_order': 20,
    },
    {
        'name': 'Deviation Analysis',
        'description': 'BIM comparison focused: mean and top deviation points between the as-built scan and the design model, with ISO 19650 tolerance assessment.',
        'report_type': 'deviation',
        'sort_order': 30,
    },
    {
        'name': 'Earthworks Volume',
        'description': 'Volumetric report from the scanned point cloud: covered area and volume metrics derived from BIM alignment.',
        'report_type': 'earthworks',
        'sort_order': 40,
    },
    {
        'name': 'Compliance Summary',
        'description': 'The verdict sheet: traffic-light site health check and overall recommended action, for regulatory sign-off.',
        'report_type': 'compliance',
        'sort_order': 50,
    },
]


def seed_report_templates(apps, schema_editor):
    ReportTemplate = apps.get_model('reports', 'ReportTemplate')
    for tpl in REPORT_TEMPLATES:
        ReportTemplate.objects.update_or_create(
            report_type=tpl['report_type'],
            defaults={**tpl, 'is_active': True},
        )


def unseed_report_templates(apps, schema_editor):
    ReportTemplate = apps.get_model('reports', 'ReportTemplate')
    ReportTemplate.objects.filter(
        report_type__in=[tpl['report_type'] for tpl in REPORT_TEMPLATES]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0006_qualityreport_report_type'),
    ]

    operations = [
        migrations.RunPython(seed_report_templates, unseed_report_templates),
    ]
