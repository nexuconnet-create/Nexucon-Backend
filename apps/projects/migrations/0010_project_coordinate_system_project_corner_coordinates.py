# Generated for coordinate system and four corner coordinates support

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0009_project_assigned_inspector_user'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='coordinate_system',
            field=models.CharField(blank=True, default='WGS84_DD', help_text='Coordinate format/system (e.g. WGS84_DD, UTM_31N_WGS84, etc.)', max_length=50),
        ),
        migrations.AddField(
            model_name='project',
            name='corner_coordinates',
            field=models.JSONField(blank=True, default=dict, help_text='Four-corner boundary coordinates (e.g. 4 corner points with lat/lng/easting/northing/dms)', null=True),
        ),
    ]
