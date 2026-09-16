from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0010_reportbranding'),
    ]

    operations = [
        migrations.AddField(
            model_name='reportbranding',
            name='cover_logo',
            field=models.FileField(blank=True, default='', help_text='Replaces the default Lagos State coat of arms on the report cover (drawn in the same top-left position)', upload_to='reports/branding/%Y/%m/'),
        ),
        migrations.AddField(
            model_name='reportbranding',
            name='cover_logo_hidden',
            field=models.BooleanField(default=False, help_text='When True the cover carries no logo at all — the default coat of arms is not drawn either'),
        ),
    ]
