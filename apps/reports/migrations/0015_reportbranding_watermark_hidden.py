from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0014_alter_reportbranding_logo_position_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='reportbranding',
            name='watermark_hidden',
            field=models.BooleanField(
                default=False,
                help_text='When True no watermark is drawn at all — neither custom nor the default laboratory watermark'
            ),
        ),
    ]
