# models.py
from pydantic import BaseModel

class SendMessage(BaseModel):
    sender: int   # 0=client, 1=agent
    text: str

class EditMessage(BaseModel):
    text: str     # Новый текст сообщения

class CreatePaymentLink(BaseModel):
    actor_id: int | None = None  # tg/max user id спеца (может быть None, тогда fallback по токену)
    role: str               # "agent"
    amount: int             # RUB, целое число
    use_bonus: bool = False # применить баллы клиента автоматически до лимита
