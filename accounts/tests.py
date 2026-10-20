from django.test import TestCase, override_settings
from django.urls import reverse


class SignupAvailabilityTests(TestCase):
    @override_settings(ALLOW_SIGNUPS=False)
    def test_signup_is_not_available_when_disabled(self):
        url = reverse("signup")

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"email": "new@example.com"}).status_code, 404)

    @override_settings(ALLOW_SIGNUPS=False)
    def test_signup_links_are_hidden_when_disabled(self):
        response = self.client.get(reverse("login"))

        self.assertNotContains(response, reverse("signup"))
