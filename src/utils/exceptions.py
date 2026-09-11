# src/utils/exceptions.py

class ResearchPaperExtractorException(Exception):
    """Base exception for the extraction system."""
    pass

class PDFProcessingException(ResearchPaperExtractorException):
    """Raised when PDF reading or parsing fails."""
    pass

class LLMProviderError(ResearchPaperExtractorException):
    """Raised when the LLM provider fails (Auth, Rate Limit, etc.)."""
    pass

class ExtractionValidationError(ResearchPaperExtractorException):
    """Raised when extracted data fails validation/schema checks."""
    pass

class ConfigurationError(ResearchPaperExtractorException):
    """Raised when config is missing or invalid."""
    pass