import logging

from optexity.exceptions import (
    AxtreeIndexActionFailedException,
    ElementNotFoundInAxtreeException,
)
from optexity.inference.cache.self_repair import cache_config, record_heal
from optexity.inference.core.interaction.handle_command import (
    command_based_action_with_retry,
)
from optexity.inference.core.interaction.utils import (
    LocatorExtraction,
    get_index_from_prompt,
    handle_download,
    update_screenshot_with_highlight,
)
from optexity.inference.infra.browser import Browser
from optexity.schema.actions.interaction_action import ClickElementAction
from optexity.schema.memory import Memory
from optexity.schema.task import Task

logger = logging.getLogger(__name__)


async def handle_click_element(
    click_element_action: ClickElementAction,
    task: Task,
    memory: Memory,
    browser: Browser,
    max_timeout_seconds_per_try: float,
    max_tries: int,
):

    if click_element_action.command and not click_element_action.skip_command:
        last_error = await command_based_action_with_retry(
            click_element_action,
            browser,
            memory,
            task,
            max_tries,
            max_timeout_seconds_per_try,
        )

        if last_error is None:
            return

    if not click_element_action.skip_prompt:
        logger.debug(
            f"Executing prompt-based action: {click_element_action.__class__.__name__}"
        )
        await click_element_index(click_element_action, browser, memory, task)


async def click_element_index(
    click_element_action: ClickElementAction,
    browser: Browser,
    memory: Memory,
    task: Task,
):

    try:
        index = await get_index_from_prompt(
            memory, click_element_action.prompt_instructions, browser, task
        )
        if index is None:
            return
        try:
            await update_screenshot_with_highlight(browser, memory, index)
        except Exception as e:
            logger.error(
                f"Error in updating screenshot with highlight in click_element_index: {e}"
            )

        async def _actual_click_element():
            print(
                f"Clicking element with index: {index} and button: {click_element_action.button}"
            )
            action_model = browser.backend_agent.ActionModel(
                **{"click": {"index": index, "button": click_element_action.button}}
            )
            results = await browser.backend_agent.multi_act([action_model])
            heal_info = await LocatorExtraction.log_interacted_locator(
                browser,
                index,
                f".click(button={click_element_action.button!r})",
                memory,
            )
            # Self-repairing cache: write the relearned locator+fingerprint back into the
            # node so the cache stops re-paying the LLM for this step. No-op unless enabled.
            if cache_config.heal_on_fallback:
                record_heal(click_element_action, heal_info, memory)
            if results and results[0].error:
                raise RuntimeError(
                    f"browseruse click failed at index {index}: {results[0].error}"
                )

        try:
            if click_element_action.expect_download:
                await handle_download(
                    _actual_click_element,
                    memory,
                    browser,
                    task,
                    click_element_action.download_filename,
                )
            else:
                await _actual_click_element()
        except Exception as e:
            raise AxtreeIndexActionFailedException(
                message=f"Failed to click element at axtree index {index}",
                index=index,
                original_error=e,
            )
    except (ElementNotFoundInAxtreeException, AxtreeIndexActionFailedException):
        raise
    except Exception as e:
        logger.error(f"Error in click_element_index: {e}")
        return
