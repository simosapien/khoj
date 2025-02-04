import logging
import os
from threading import Thread
from typing import Dict

import openai
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random_exponential,
)

from khoj.processor.conversation.utils import (
    ThreadedGenerator,
    commit_conversation_trace,
)
from khoj.utils import state
from khoj.utils.helpers import in_debug_mode

logger = logging.getLogger(__name__)

openai_clients: Dict[str, openai.OpenAI] = {}


@retry(
    retry=(
        retry_if_exception_type(openai._exceptions.APITimeoutError)
        | retry_if_exception_type(openai._exceptions.APIError)
        | retry_if_exception_type(openai._exceptions.APIConnectionError)
        | retry_if_exception_type(openai._exceptions.RateLimitError)
        | retry_if_exception_type(openai._exceptions.APIStatusError)
    ),
    wait=wait_random_exponential(min=1, max=10),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(logger, logging.DEBUG),
    reraise=True,
)
def completion_with_backoff(
    messages, model, temperature=0, openai_api_key=None, api_base_url=None, model_kwargs=None, tracer: dict = {}
) -> str:
    client_key = f"{openai_api_key}--{api_base_url}"
    client: openai.OpenAI | None = openai_clients.get(client_key)
    if not client:
        client = openai.OpenAI(
            api_key=openai_api_key,
            base_url=api_base_url,
        )
        openai_clients[client_key] = client

    formatted_messages = [{"role": message.role, "content": message.content} for message in messages]
    stream = True

    # Update request parameters for compatability with o1 model series
    # Refer: https://platform.openai.com/docs/guides/reasoning/beta-limitations
    if model.startswith("o1"):
        stream = False
        temperature = 1
        model_kwargs.pop("stop", None)
        model_kwargs.pop("response_format", None)

    if os.getenv("KHOJ_LLM_SEED"):
        model_kwargs["seed"] = int(os.getenv("KHOJ_LLM_SEED"))

    chat = client.chat.completions.create(
        stream=stream,
        messages=formatted_messages,  # type: ignore
        model=model,  # type: ignore
        temperature=temperature,
        timeout=20,
        **(model_kwargs or dict()),
    )

    if not stream:
        return chat.choices[0].message.content

    aggregated_response = ""
    for chunk in chat:
        if len(chunk.choices) == 0:
            continue
        delta_chunk = chunk.choices[0].delta  # type: ignore
        if isinstance(delta_chunk, str):
            aggregated_response += delta_chunk
        elif delta_chunk.content:
            aggregated_response += delta_chunk.content

    # Save conversation trace
    tracer["chat_model"] = model
    tracer["temperature"] = temperature
    if in_debug_mode() or state.verbose > 1:
        commit_conversation_trace(messages, aggregated_response, tracer)

    return aggregated_response


@retry(
    retry=(
        retry_if_exception_type(openai._exceptions.APITimeoutError)
        | retry_if_exception_type(openai._exceptions.APIError)
        | retry_if_exception_type(openai._exceptions.APIConnectionError)
        | retry_if_exception_type(openai._exceptions.RateLimitError)
        | retry_if_exception_type(openai._exceptions.APIStatusError)
    ),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(logger, logging.DEBUG),
    reraise=True,
)
def chat_completion_with_backoff(
    messages,
    compiled_references,
    online_results,
    model_name,
    temperature,
    openai_api_key=None,
    api_base_url=None,
    completion_func=None,
    model_kwargs=None,
    tracer: dict = {},
):
    g = ThreadedGenerator(compiled_references, online_results, completion_func=completion_func)
    t = Thread(
        target=llm_thread,
        args=(g, messages, model_name, temperature, openai_api_key, api_base_url, model_kwargs, tracer),
    )
    t.start()
    return g


def llm_thread(
    g,
    messages,
    model_name,
    temperature,
    openai_api_key=None,
    api_base_url=None,
    model_kwargs=None,
    tracer: dict = {},
):
    try:
        client_key = f"{openai_api_key}--{api_base_url}"
        if client_key not in openai_clients:
            client = openai.OpenAI(
                api_key=openai_api_key,
                base_url=api_base_url,
            )
            openai_clients[client_key] = client
        else:
            client = openai_clients[client_key]

        formatted_messages = [{"role": message.role, "content": message.content} for message in messages]
        stream = True

        # Update request parameters for compatability with o1 model series
        # Refer: https://platform.openai.com/docs/guides/reasoning/beta-limitations
        if model_name.startswith("o1"):
            stream = False
            temperature = 1
            model_kwargs.pop("stop", None)
            model_kwargs.pop("response_format", None)

        if os.getenv("KHOJ_LLM_SEED"):
            model_kwargs["seed"] = int(os.getenv("KHOJ_LLM_SEED"))

        chat = client.chat.completions.create(
            stream=stream,
            messages=formatted_messages,
            model=model_name,  # type: ignore
            temperature=temperature,
            timeout=20,
            **(model_kwargs or dict()),
        )

        aggregated_response = ""
        if not stream:
            aggregated_response = chat.choices[0].message.content
            g.send(aggregated_response)
        else:
            for chunk in chat:
                if len(chunk.choices) == 0:
                    continue
                delta_chunk = chunk.choices[0].delta
                text_chunk = ""
                if isinstance(delta_chunk, str):
                    text_chunk = delta_chunk
                elif delta_chunk.content:
                    text_chunk = delta_chunk.content
                if text_chunk:
                    aggregated_response += text_chunk
                    g.send(text_chunk)

        # Save conversation trace
        tracer["chat_model"] = model_name
        tracer["temperature"] = temperature
        if in_debug_mode() or state.verbose > 1:
            commit_conversation_trace(messages, aggregated_response, tracer)
    except Exception as e:
        logger.error(f"Error in llm_thread: {e}", exc_info=True)
    finally:
        g.close()
