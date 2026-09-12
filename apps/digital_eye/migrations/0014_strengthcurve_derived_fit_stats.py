"""Backfill derived fit statistics (r2/SE/AIC) on existing StrengthCurves.

StrengthCurve.save() now derives these from each curve's real stored
calibration pairs (see apps.digital_eye.strength_curves.curve_fit_stats).
Curves saved before that change carry nulls; this migration recomputes them
from the same stored data — nothing is invented, curves without usable
pairs keep their nulls.
"""
from django.db import migrations


def backfill_curve_stats(apps, schema_editor):
    from apps.digital_eye.strength_curves import curve_fit_stats

    StrengthCurve = apps.get_model('digital_eye', 'StrengthCurve')
    for curve in StrengthCurve.objects.exclude(data_points=[]):
        r2, std_err, aic = curve_fit_stats(
            curve.curve_type, curve.formula_params or {},
            curve.data_points or [])
        if (curve.r2_score, curve.standard_error, curve.aic) != (r2, std_err, aic):
            curve.r2_score, curve.standard_error, curve.aic = r2, std_err, aic
            curve.save(update_fields=['r2_score', 'standard_error', 'aic'])


def unbackfill_curve_stats(apps, schema_editor):
    # The statistics are derived data — nothing to reverse.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('digital_eye', '0013_alter_rebartest_structural_element_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_curve_stats, unbackfill_curve_stats),
    ]
