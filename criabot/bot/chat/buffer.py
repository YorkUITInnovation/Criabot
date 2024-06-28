from pprint import pprint
from typing import List, Optional, Type

import tiktoken
from CriadexSDK.routers.agents.azure.chat import ChatMessage


def string_tokens(string: str, encoding_name: str = "cl100k_base") -> int:
    """
    Returns the number of tokens in a text string.

    Based on https://github.com/openai/openai-cookbook/blob/main/examples/How_to_count_tokens_with_tiktoken.ipynb
    "cl100k_base" works for models gpt-4, gpt-3.5-turbo, text-embedding-ada-002

    """

    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))

    return num_tokens


History: Type = List[ChatMessage]


class ChatBuffer:
    EXTRA_TOKEN_MARGIN: int = 5
    TOKEN_COUNT_META_NAME: str = "token_count"
    EPHEMERAL_META_NAME: str = "is_ephemeral"

    def __init__(
            self,
            max_tokens: int,
            history: List[ChatMessage]
    ):
        self._history: List[ChatMessage] = history
        self._max_tokens: int = max_tokens

    @classmethod
    def history_tokens(cls, history_with_metadata: List[ChatMessage]) -> int:
        """
        Count the number of tokens in a chat history

        :param history_with_metadata: The chat history assuming it has the token count metadata
        :return: The sum of all their values

        """

        return sum([m.metadata.get(ChatBuffer.TOKEN_COUNT_META_NAME, 0) for m in history_with_metadata])

    @classmethod
    def create_history_token_metadata(cls, history: History) -> None:
        """Calculate tokens in a message and add it to the metadata"""
        for message in history:
            if cls.TOKEN_COUNT_META_NAME not in message.metadata:
                cls.create_chat_token_metadata(message=message)

    @classmethod
    def create_chat_token_metadata(cls, message: ChatMessage) -> int:
        token_count: int = string_tokens(message.content)
        message.metadata[cls.TOKEN_COUNT_META_NAME] = token_count
        return token_count

    @classmethod
    def get_token_metadata(cls, message: ChatMessage) -> Optional[int]:
        """Retrieve the metadata for a token"""
        return message.metadata.get(cls.TOKEN_COUNT_META_NAME)

    @property
    def history(self) -> List[ChatMessage]:
        """Get a copy of the history"""
        return self._history

    def add_message(self, message: ChatMessage, update_buffer: bool = True) -> History:
        """Add a message to the history"""
        self._history.append(message)
        return self.buffer() if update_buffer else self._history

    @classmethod
    def get_system(cls, history: History) -> Optional[ChatMessage]:
        system_messages: List[ChatMessage] = [m for m in history if m.role == "system"]

        if len(system_messages) > 1:
            raise ValueError("There should only be one system message! Got: " + str(system_messages))

        return system_messages[0] if bool(system_messages) else None

    @classmethod
    def pop_system(cls, history: History) -> Optional[ChatMessage]:
        """In-place list mod to pop a system message from a list"""

        system_message: Optional[ChatMessage] = cls.get_system(history=history)
        history[:] = [m for m in history if m.role != "system"]
        return system_message

    def buffer(
            self,
            system_ephemeral: Optional[ChatMessage] = None,
            print_debug: bool = False
    ) -> List[ChatMessage]:
        """Update the buffer. Ephemerals are NOT included in history but are returned by the func."""

        # Shallow-copy history & pop the system message
        history: History = self._history.copy()
        system_message: Optional[ChatMessage] = self.pop_system(history=history)

        # Calculate the tokens for the whole history
        self.create_history_token_metadata(history=history)

        # Set tokens & set ephemeral
        if isinstance(system_ephemeral, ChatMessage):
            system_ephemeral.metadata[self.EPHEMERAL_META_NAME] = True
            self.create_chat_token_metadata(message=system_ephemeral)

        # Set tokens & set not ephemeral
        if isinstance(system_message, ChatMessage):
            system_message.metadata[self.EPHEMERAL_META_NAME] = False
            self.create_chat_token_metadata(message=system_message)

        # Calculate the available tokens
        available_tokens: int = max(
            0,
            self._max_tokens
            - (self.get_token_metadata(system_message) if system_message else 0)
            - (self.get_token_metadata(system_ephemeral) if system_ephemeral else 0)
            - self.EXTRA_TOKEN_MARGIN
        )

        if print_debug:
            print("Available Tokens Before Reserved:", self._max_tokens)
            print("Available Tokens After Reserved:", available_tokens)
            print("Tokens Reserved:", (self._max_tokens - available_tokens))

        message_count: int = len(history)
        while self.history_tokens(history[-message_count:]) > available_tokens and message_count > 1:
            message_count -= 1

        history: History = history[-message_count:]

        # Handle single-prompt length issue
        if len(history) == 1:
            self.buffer_message(
                message=history[0],
                max_tokens=available_tokens,
                print_debug=print_debug
            )

        # Re-add the system message
        if system_message is not None:
            history.insert(0, system_message)

        # Update stored history EXCLUDING the ephemeral
        self._history = history.copy()

        # The ephemeral is always the SECOND-LAST message (just before user prompt)
        if system_ephemeral is not None:
            history.insert(
                -1 if len(history) > 1 else 1,
                system_ephemeral
            )

        return history

    @classmethod
    def buffer_message(
            cls,
            message: ChatMessage,
            max_tokens: int,
            print_debug: bool = False
    ) -> ChatMessage:

        while cls.create_chat_token_metadata(message) > max_tokens:
            excess_tokens: int = abs(max_tokens - cls.get_token_metadata(message))

            # 1 token is approx. 4 chars, so we'll do some math to remove an approximate amount
            # We use a val of 3 to multiply because we don't want to accidentally overshoot
            remove_n_chars: int = excess_tokens * 3

            if print_debug:
                print(
                    "Prompt Characters:", len(message.content),
                    "| Prompt Tokens:", cls.get_token_metadata(message),
                    "| Remove N Characters:", remove_n_chars,
                    "| Exceeding N Tokens:", excess_tokens,
                    "| Current Prompt: ", f"\"{message.content}\""
                )

            message.content = message.content[:-remove_n_chars]

        # Update at the end
        message.metadata[cls.TOKEN_COUNT_META_NAME] = cls.get_token_metadata(message)
        return message


if __name__ == '__main__':
    chat_buffer: ChatBuffer = ChatBuffer(
        50,
        history=[
            ChatMessage(
                role="system",
                content="This has to be included no matter what lol"
            ),
            ChatMessage(
                role="user",
                content="What's the weather like in New York today?"
            ),
            ChatMessage(
                role="assistant",
                content="I'm sorry, I cannot provide real-time updates. Would you like me to bot_auth for the weather "
                        "forecast for New York?"
            ),
            ChatMessage(
                role="user",
                content="Yes, please do that"
            ),
            ChatMessage(
                role="assistant",
                content="No can do buckaroo"
            ),
            ChatMessage(
                role="user",
                content="How can I improve my French language skills quickly?"
            ),
            ChatMessage(
                role="assistant",
                content="To improve your French quickly, you could immerse yourself in the language, practice regularly "
                        "with native speakers, use language learning apps, and take formal classes. Consistent daily "
                        "practice and immersion are key to rapid progress."
            )
        ]
    )

    pprint(
        chat_buffer.buffer(
            print_debug=False,
            system_ephemeral=ChatMessage(
                content="Hey there this is a long message",
                role="system"
            )
        )
    )

    pprint(chat_buffer.history)

