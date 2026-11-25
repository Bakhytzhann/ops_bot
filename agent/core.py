import asyncio
import json
import re
from time import perf_counter

from loguru import logger
from ollama import AsyncClient

from agent.memory import memory
from agent.prompts import MODEL_TOOL_NAMES, SYSTEM_PROMPT
from agent.tool_registry import registry
from agent.tools.system_tools import register_system_tools
from config.settings import settings


class FlightAgent:
    def __init__(self):
        self.client = AsyncClient(
            host=settings.ollama_host,
        )

        self.model = settings.ollama_model
        self.max_tool_rounds = 5
        self._inference_slots = asyncio.Semaphore(settings.ollama_max_parallel)

    async def warmup(self):
        async with self._inference_slots:
            await self.client.generate(
                model=self.model,
                prompt="",
                keep_alive=settings.ollama_keep_alive,
                options={"num_ctx": settings.ollama_num_ctx},
            )

    @staticmethod
    def clean_response(text: str) -> str:
        text = re.sub(
            r"<think>.*?</think>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        text = re.sub(
            r"<think>.*$",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        if re.search(r"</think>", text, re.IGNORECASE):
            text = re.split(
                r"</think>",
                text,
                flags=re.IGNORECASE,
            )[-1]

        return text.strip()

    async def chat(
        self,
        message: str,
        chat_id: int | None = None,
        user_id: int | None = None,
    ) -> str:

        use_memory = chat_id is not None and user_id is not None

        history = (
            await asyncio.to_thread(memory.get_history, chat_id, user_id)
            if use_memory
            else []
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            *history,
            {
                "role": "user",
                "content": message,
            },
        ]

        tools = registry.get_ollama_tools(names=MODEL_TOOL_NAMES)

        for round_index in range(self.max_tool_rounds + 1):
            queued_at = perf_counter()
            async with self._inference_slots:
                started_at = perf_counter()
                response = await self.client.chat(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    think=False,
                    keep_alive=settings.ollama_keep_alive,
                    options={
                        "temperature": settings.ollama_temperature,
                        "num_ctx": settings.ollama_num_ctx,
                        "num_predict": 1024,
                    },
                )
            logger.info(
                "Ollama round={} queue_s={:.2f} inference_s={:.2f} load_s={:.2f}",
                round_index,
                started_at - queued_at,
                perf_counter() - started_at,
                (response.load_duration or 0) / 1_000_000_000,
            )

            assistant_message = response.message
            tool_calls = assistant_message.tool_calls or []

            if not tool_calls:
                answer = self.clean_response(assistant_message.content or "")

                if not answer:
                    return "Модель не вернула ответ."

                if use_memory:
                    await asyncio.to_thread(
                        memory.save_history,
                        chat_id,
                        user_id,
                        [
                            *history,
                            {
                                "role": "user",
                                "content": message,
                            },
                            {
                                "role": "assistant",
                                "content": answer,
                            },
                        ],
                    )

                return answer

            messages.append(assistant_message.model_dump(exclude_none=True))

            if round_index >= self.max_tool_rounds:
                return "Достигнут лимит вызовов инструментов."

            for call in tool_calls:
                tool_name = call.function.name
                arguments = call.function.arguments

                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}

                if not isinstance(arguments, dict):
                    arguments = {}

                if tool_name in MODEL_TOOL_NAMES:
                    result = await registry.execute(name=tool_name, arguments=arguments)
                else:
                    result = {"ok": False, "error": "Use /menu for live operations"}

                messages.append(
                    {
                        "role": "tool",
                        "tool_name": tool_name,
                        "content": json.dumps(
                            result,
                            ensure_ascii=False,
                            default=str,
                        ),
                    }
                )

        return "Не удалось завершить запрос."


register_system_tools()

agent = FlightAgent()
