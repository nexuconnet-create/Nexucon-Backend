"""
Fix the calibration factor 'b' in the project's exponential strength curve.

Background
----------
The fitted exponential model  f_cu = A × exp(B × V) + C  was originally
stored with B rounded to 0.000003 (3×10⁻⁶) instead of the true regression
value 2.71395×10⁻⁶.  That rounding produced negative R² and inflated
standard-error statistics when the report re-fits the curve against its
calibration pairs, because a straight-line fit at the wrong parameter value
performs worse than the flat mean of the response data.

This command:
  1. Finds every StrengthCurve whose 'b' parameter is the rounded value
     0.000003 (or anything close to it that is not 2.71395e-06).
  2. Corrects 'b' to 2.71395e-06, preserving 'a' and 'c' exactly.
  3. Re-runs the regression helper (if calibration pairs are present) to
     recompute r2_score and standard_error against the corrected parameter.
  4. Prints a before/after summary for audit.

Safe to re-run: if the value is already correct the record is untouched.
"""
import math
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)

# The rounded value that was incorrectly stored.
WRONG_B = 0.000003

# The true, full-precision regression coefficient.
CORRECT_B = 2.71395e-06

# Tolerance: treat any b within this distance of WRONG_B as needing a fix,
# and any b within this distance of CORRECT_B as already correct.
EPSILON = 1e-9


class Command(BaseCommand):
    help = (
        "Correct the 'b' calibration factor in exponential StrengthCurve "
        "records from the rounded 0.000003 to the true 2.71395e-06."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Show what would be changed without saving.',
        )
        parser.add_argument(
            '--curve-id',
            type=int,
            default=None,
            help='Only fix the curve with this primary key (optional).',
        )

    def handle(self, *args, **options):
        from apps.digital_eye.models import StrengthCurve
        from apps.digital_eye.strength_curves import curve_fit_stats

        dry_run = options['dry_run']
        curve_id = options.get('curve_id')

        qs = StrengthCurve.objects.filter(curve_type='exponential')
        if curve_id is not None:
            qs = qs.filter(pk=curve_id)

        fixed = 0
        skipped_already_correct = 0
        skipped_different = 0

        for curve in qs.iterator():
            params = curve.formula_params or {}
            b_raw = params.get('b')
            if b_raw is None:
                continue
            try:
                b_val = float(b_raw)
            except (TypeError, ValueError):
                continue

            # Already correct — nothing to do.
            if abs(b_val - CORRECT_B) < EPSILON:
                skipped_already_correct += 1
                continue

            # The wrong rounded value — fix it.
            if abs(b_val - WRONG_B) < EPSILON:
                a_val = params.get('a')
                c_val = params.get('c', 0.0)
                self.stdout.write(
                    f"Curve pk={curve.pk}  '{curve.name}'\n"
                    f"  project_id : {curve.project_id}\n"
                    f"  b  BEFORE  : {b_val!r}  ({b_val:.6g})\n"
                    f"  b  AFTER   : {CORRECT_B!r}  ({CORRECT_B:.6g})\n"
                    f"  a          : {a_val!r}\n"
                    f"  c          : {c_val!r}\n"
                )
                if not dry_run:
                    with transaction.atomic():
                        new_params = dict(params)
                        new_params['b'] = CORRECT_B
                        curve.formula_params = new_params

                        # Re-evaluate the corrected parameters against the
                        # stored calibration pairs so r2_score / standard_error
                        # reflect the true coefficient.
                        data_points = curve.data_points or []
                        if len(data_points) >= 2:
                            try:
                                r2, se, _aic = curve_fit_stats(
                                    curve.curve_type,
                                    new_params,
                                    data_points,
                                )
                                if r2 is not None and math.isfinite(r2):
                                    curve.r2_score = r2
                                if se is not None and math.isfinite(se):
                                    curve.standard_error = se
                                self.stdout.write(
                                    f"  r2_score updated: {curve.r2_score}\n"
                                    f"  standard_error updated: {curve.standard_error}\n"
                                )
                            except Exception as exc:
                                self.stderr.write(
                                    f"  WARNING: could not recompute stats: {exc}\n"
                                )

                        curve.save()
                    self.stdout.write(self.style.SUCCESS(
                        f"  → SAVED (pk={curve.pk})\n"))
                else:
                    self.stdout.write("  → DRY RUN — not saved.\n")
                fixed += 1
                continue

            # Some other b value — leave it alone.
            skipped_different += 1
            self.stdout.write(
                f"  SKIP pk={curve.pk}: b={b_val:.6g} "
                f"(neither wrong rounded value nor correct value)\n"
            )

        suffix = " [DRY RUN]" if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone{suffix}: {fixed} fixed, "
                f"{skipped_already_correct} already correct, "
                f"{skipped_different} left untouched (different b value)."
            )
        )
