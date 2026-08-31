import json
from channels.generic.websocket import AsyncWebsocketConsumer

class ProcessingStatusConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.session_id = self.scope['url_route']['kwargs']['session_id']
        self.group_name = f'processing_{self.session_id}'

        # Join room group
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )

        await self.accept()

        # Send an initial message
        await self.send(text_data=json.dumps({
            'status': 'connected',
            'message': 'Connected to processing status stream.'
        }))

    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )

    # Receive message from room group
    async def processing_status_update(self, event):
        status = event['status']
        message = event.get('message', '')
        
        # Send message to WebSocket
        await self.send(text_data=json.dumps({
            'status': status,
            'message': message
        }))
