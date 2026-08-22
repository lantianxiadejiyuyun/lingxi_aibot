"""模型统一出口：导入所有模型以注册到 db.metadata。"""
from app.models.user import User
from app.models.event import Event
from app.models.task import Task
from app.models.note import Note
from app.models.conversation import Conversation, Message
from app.models.notification import Notification
from app.models.scheduled_job import ScheduledJob
from app.models.setting import Setting
from app.models.webpage import WebPage
from app.models.image import ImageAsset
from app.models.skill import Skill
from app.models.memory import Memory
from app.models.embedding import Embedding
from app.models.fitness import FitnessRecord
from app.models.travel import TripPlan
from app.models.expense import ExpenseRecord

__all__ = [
    "User", "Event", "Task", "Note", "Conversation", "Message",
    "Notification", "ScheduledJob", "Setting", "WebPage", "ImageAsset",
    "Skill", "Memory", "Embedding", "FitnessRecord", "TripPlan", "ExpenseRecord",
]
