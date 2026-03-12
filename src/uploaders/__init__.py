# uploaders module - Upload handlers for various services
from .immich import ImmichUploader, get_immich_uploader
from .telegram import TelegramUploader, get_telegram_uploader

__all__ = [
    'ImmichUploader',
    'get_immich_uploader', 
    'TelegramUploader',
    'get_telegram_uploader',
]
