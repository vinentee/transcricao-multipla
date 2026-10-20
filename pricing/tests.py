from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .models import PricingConfig


class SeedDevelopmentPricingTests(TestCase):
    @override_settings(DEBUG=False, DEV_BYPASS_PAYMENT=True)
    def test_seed_is_allowed_for_private_test_mode(self):
        call_command("seed_dev_pricing", stdout=StringIO())

        self.assertTrue(
            PricingConfig.objects.filter(notes__startswith="DESENVOLVIMENTO:", active=True).exists()
        )

    @override_settings(DEBUG=True, DEV_BYPASS_PAYMENT=False)
    def test_seed_is_rejected_without_test_payment_mode(self):
        with self.assertRaises(CommandError):
            call_command("seed_dev_pricing", stdout=StringIO())
