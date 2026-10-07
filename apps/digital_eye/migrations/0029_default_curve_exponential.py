# Generated manually on 2026-10-06

from django.db import migrations


def apply_default_curve_exponential(apps, schema_editor):
    StrengthCurve = apps.get_model('digital_eye', 'StrengthCurve')
    default_curve = StrengthCurve.objects.filter(is_default=True).first()
    if default_curve:
        default_curve.name = 'Platform default (laboratory exponential)'
        default_curve.curve_type = 'exponential'
        default_curve.standard = 'BS 1881-203 / BS EN 12504-4 exponential calibration'
        default_curve.formula_params = {'a': 1.20, 'b': 0.00085, 'c': 0.0}
        default_curve.valid_range_min_ms = 2000.0
        default_curve.valid_range_max_ms = 5000.0
        default_curve.provenance = {
            'source': 'apps.reports.ndt_reports.ECS_EXP_A/ECS_EXP_B_MS/ECS_EXP_C',
            'reference': 'Documented laboratory exponential calibration f_cu = 1.20 * exp(0.00085 * V), valid 2.0-5.0 km/s',
            'note': 'Concrete acoustic velocity behavior is non-linear (exponential model default).',
        }
        default_curve.save()
    else:
        StrengthCurve.objects.create(
            name='Platform default (laboratory exponential)',
            curve_type='exponential',
            standard='BS 1881-203 / BS EN 12504-4 exponential calibration',
            velocity_unit='m/s',
            strength_unit='MPa',
            formula_params={'a': 1.20, 'b': 0.00085, 'c': 0.0},
            valid_range_min_ms=2000.0,
            valid_range_max_ms=5000.0,
            is_default=True,
            provenance={
                'source': 'apps.reports.ndt_reports.ECS_EXP_A/ECS_EXP_B_MS/ECS_EXP_C',
                'reference': 'Documented laboratory exponential calibration f_cu = 1.20 * exp(0.00085 * V), valid 2.0-5.0 km/s',
                'note': 'Concrete acoustic velocity behavior is non-linear (exponential model default).',
            },
        )


def revert_default_curve_exponential(apps, schema_editor):
    StrengthCurve = apps.get_model('digital_eye', 'StrengthCurve')
    default_curve = StrengthCurve.objects.filter(is_default=True).first()
    if default_curve:
        default_curve.name = 'Platform default (laboratory linear)'
        default_curve.curve_type = 'linear'
        default_curve.standard = 'Nexucon laboratory calibration (documented)'
        default_curve.formula_params = {'m': 0.008961, 'c': -7.97}
        default_curve.valid_range_min_ms = 2000.0
        default_curve.valid_range_max_ms = 5000.0
        default_curve.provenance = {
            'source': 'apps.reports.ndt_reports.ECS_SLOPE/ECS_INTERCEPT',
            'reference': 'Documented laboratory linear calibration f_cu = 8.961*V(km/s) - 7.97, valid 2.0-5.0 km/s',
        }
        default_curve.save()


class Migration(migrations.Migration):

    dependencies = [
        ('digital_eye', '0028_punditanalysiscomment'),
    ]

    operations = [
        migrations.RunPython(apply_default_curve_exponential, revert_default_curve_exponential),
    ]
