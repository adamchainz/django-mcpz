from __future__ import annotations

import datetime as dt
from http import HTTPStatus

from django.contrib.auth.models import User
from django.test import TestCase

from django_mcpz.oauth.models import RefreshToken
from tests.oauth.utils import RESOURCE, make_access_token, make_client


class AdminTests(TestCase):
    admin: User

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin", password="pw")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_client_list(self):
        make_client(name="Claude")

        response = self.client.get("/admin/django_mcpz_oauth/client/")

        self.assertContains(response, "Claude")

    def test_client_no_add(self):
        response = self.client.get("/admin/django_mcpz_oauth/client/add/")

        assert response.status_code == HTTPStatus.FORBIDDEN

    def test_token_list_and_revoke(self):
        token, _ = make_access_token(user=self.admin)

        response = self.client.get("/admin/django_mcpz_oauth/accesstoken/")
        assert response.status_code == HTTPStatus.OK

        response = self.client.post(
            "/admin/django_mcpz_oauth/accesstoken/",
            {"action": "revoke", "_selected_action": [token.pk]},
            follow=True,
        )

        assert response.status_code == HTTPStatus.OK
        token.refresh_from_db()
        assert not token.is_valid
        (message,) = list(response.context["messages"])
        assert str(message) == "Revoked 1 token(s)."

    def test_refresh_token_list_and_change(self):
        access, _ = make_access_token(user=self.admin)
        refresh, _ = RefreshToken.create(
            lifetime=dt.timedelta(days=1),
            client=access.client,
            user=self.admin,
            resource=RESOURCE,
            access_token=access,
        )

        response = self.client.get("/admin/django_mcpz_oauth/refreshtoken/")
        assert response.status_code == HTTPStatus.OK
        response = self.client.get(
            f"/admin/django_mcpz_oauth/refreshtoken/{refresh.pk}/change/"
        )

        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Used at")

    def test_revoked_filter(self):
        response = self.client.get(
            "/admin/django_mcpz_oauth/accesstoken/?revoked_at__isempty=1"
        )

        assert response.status_code == HTTPStatus.OK
