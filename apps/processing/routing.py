from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/processing/(?P<session_id>[\w-]+)/$', consumers.ProcessingStatusConsumer.as_asgi()),
]
