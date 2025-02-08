import re
import uuid
from typing import Dict

from CriadexSDK.routers.content.search import Asset, GroupSearchResponse

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
