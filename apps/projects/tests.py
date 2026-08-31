from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth.models import User
from apps.projects.models import Project
from rest_framework_simplejwt.tokens import RefreshToken

class ProjectAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='projuser', password='testpass')
        refresh = RefreshToken.for_user(self.user)
        self.token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def test_create_and_list_project(self):
        # Create a project
        create_url = reverse('project-list')  # DRF router default name
        data = {'name': 'Demo Project', 'description': 'Test description'}
        response = self.client.post(create_url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        proj_id = response.data['id']
        # List projects
        response = self.client.get(create_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(any(p['id'] == proj_id for p in response.data))
