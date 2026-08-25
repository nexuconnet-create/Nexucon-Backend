from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class BaseEmailProvider(ABC):
    """
    Abstract interface for transactional email delivery providers.
    """
    @abstractmethod
    def send_email(
        self, 
        to_email: str, 
        subject: str, 
        html_content: str, 
        text_content: Optional[str] = None, 
        from_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Deliver an email message. Returns dict with:
        {'success': bool, 'id': str, 'error': str, 'raw_response': Any}
        """
        pass
