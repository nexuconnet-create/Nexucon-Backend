from django.test import TestCase, override_settings
from unittest.mock import patch, MagicMock
from apps.common.ai_service import AIService

class AIServiceTests(TestCase):

    def test_fallback_mock_responses(self):
        """Verify that when no API keys are present, the service falls back to default mocks."""
        # 1. Defect detection fallback
        defects = AIService.detect_visual_defects("mock_url")
        self.assertGreater(len(defects), 0)

        # 2. Thermal anomaly fallback
        anomalies = AIService.detect_thermal_anomalies("mock_url")
        self.assertGreaterEqual(len(anomalies), 0)

        # 3. Recommendations fallback
        recs = AIService.generate_recommendations([{"type": "crack", "severity": "high"}], [], 0.01)
        self.assertTrue(any("structural engineer" in r.lower() for r in recs))

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="fake-gemini-key")
    @patch("google.generativeai.GenerativeModel")
    def test_gemini_visual_defects(self, mock_generative_model):
        """Test Gemini integration for visual defect detection."""
        mock_model_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '[{"type": "spalling", "severity": "medium", "description": "Concrete spalling on column", "location_x": 1.0, "location_y": 2.0, "location_z": 3.0}]'
        mock_model_instance.generate_content.return_value = mock_response
        mock_generative_model.return_value = mock_model_instance

        defects = AIService.detect_visual_defects("http://example.com/image.jpg")
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["type"], "spalling")
        self.assertEqual(defects[0]["severity"], "medium")

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="fake-openai-key")
    @patch("apps.common.ai_service.OpenAI")
    def test_openai_visual_defects(self, mock_openai_class):
        """Test OpenAI integration for visual defect detection."""
        mock_client = MagicMock()
        mock_chat = MagicMock()
        mock_completions = MagicMock()
        
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_message.content = '[{"type": "corrosion", "severity": "critical", "description": "Severe rebar corrosion", "location_x": 0.0, "location_y": 0.0, "location_z": 0.0}]'
        
        mock_choice.message = mock_message
        mock_response.choices = [mock_choice]
        mock_completions.create.return_value = mock_response
        mock_chat.completions = mock_completions
        mock_client.chat = mock_chat
        mock_openai_class.return_value = mock_client

        defects = AIService.detect_visual_defects("http://example.com/image.jpg")
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["type"], "corrosion")
        self.assertEqual(defects[0]["severity"], "critical")
