import datetime
import re
import urllib.parse
import uuid
from typing import Dict

from CriadexSDK.ragflow_schemas import Asset, GroupSearchResponse

EXTRACTION_PATTER: re.Pattern = re.compile(r'!\[(.*?)\]\((.*?)\)')


def extract_markdown_image_ids(input_text: str) -> set[uuid.UUID]:
    """
    Extracts IDs from markdown image syntax ![Image <id>](id) in the given text.

    :param input_text: str, input text containing markdown images
    :return: list, extracted IDs

    >>> extract_markdown_image_ids('![image1](12345) Some text ![image2](67890)')
    set()

    >>> extract_markdown_image_ids('![image1](616b1f1a-12e7-4ff5-abe2-4920c0d416e0) Some text ![image2](67890) ![image3](616b1f1a-12e7-4ff5-abe2-4920c0d416e0)')
    {'616b1f1a-12e7-4ff5-abe2-4920c0d416e0'}

    """

    matches = EXTRACTION_PATTER.findall(input_text)
    asset_uuids: set[uuid.UUID] = set()

    for match in matches:
        try:
            asset_uuids.add(uuid.UUID(match[1]))
        except ValueError:
            pass

    return asset_uuids


def extract_used_assets(text: str, assets: list[Asset]) -> list[Asset]:
    """Extract assets that are used in the response."""

    used_asset_uuids: set[uuid.UUID] = extract_markdown_image_ids(text)
    yielded_asset_uuids: set[str] = set()

    for asset in assets:

        if asset.uuid in yielded_asset_uuids:
            continue

        if uuid.UUID(asset.uuid) in used_asset_uuids:
            yielded_asset_uuids.add(asset.uuid)
            yield asset


def strip_asset_data_from_group_responses(group_responses: Dict[str, GroupSearchResponse]) -> Dict[str, GroupSearchResponse]:
    """Remove assets from group responses."""

    for group_response in group_responses.values():
        asset_copies = []

        for asset in group_response.assets:
            asset_copy = asset.model_copy(deep=True)
            asset_copy.data = "<stripped>"
            asset_copies.append(asset_copy)

        group_response.assets = asset_copies

    return group_responses


"""
class Asset(BaseModel):
    id: int
    uuid: str
    document_id: int
    group_id: int
    mimetype: str
    data: str
    created: datetime
    description: str
"""


def embed_assets_in_message(message_text: str, assets: list[Asset]) -> str:
    """
    Embed assets in the message text.

    :param message_text: The message text
    :param assets: The assets to embed

    """

    for asset in assets:
        asset_uuid_hex = uuid.UUID(asset.uuid).hex
        escaped_data = urllib.parse.quote(asset.data)
        message_text = re.sub(
            rf'!\[.*?]\({asset_uuid_hex}\)',
            f'<img id="{asset.uuid}" class="reply-asset" style="width: 100%" src="data:image/png;base64,{escaped_data}" alt="{asset.description}" />',
            message_text
        )

    return message_text


if __name__ == '__main__':
    _uuid = '95ee8d49-9591-42bd-b847-bfef8fa05596'
    _uuid_hex = uuid.UUID(_uuid).hex
    test_data = 'iV\nBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAABlBMVEX///+/v7+jQ3Y5AAAADklEQVQI12P4AIX8EAgALgAD/aNpbtEAAAAASUVORK5CYII'
    _test_text = f"This is some sample text ![Image {_uuid_hex}]({_uuid_hex}) and some trailing text."

    _asset = Asset(
        id=1,
        uuid=_uuid,
        document_id=1,
        group_id=1,
        mimetype='image/png',
        data=test_data,
        created=datetime.datetime.now(),
        description='description'
    )

    print(embed_assets_in_message(_test_text, [_asset]))
