"""Normalize the site's JSON into plain Python records."""

from __future__ import annotations

import html
import json
import re
from typing import Any


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_bool_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        return int(value.strip().lower() in {"true", "1", "yes"})
    return int(bool(value))


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def plain_text(markup: str | None) -> str | None:
    if not markup:
        return None
    text = re.sub(r"<[^>]+>", " ", markup)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def parse_hadith(data: dict[str, Any]) -> dict[str, Any]:
    hadith_id = as_int(data.get("hadithId") or data.get("id"))
    if hadith_id is None:
        raise ValueError("hadith payload has no hadithId")
    text = as_text(data.get("text"))
    return {
        "hadith_id": hadith_id,
        "source_id": as_int(data.get("sourceId")),
        "author_title": as_text(data.get("authorTitle")),
        "book_title": as_text(data.get("bookTitle")),
        "book_title_short": as_text(data.get("bookTitleShort")),
        "vol": as_int(data.get("vol")),
        "page_num": as_int(data.get("pageNum")),
        "start_index_in_page": as_int(data.get("startIndexInPage")),
        "access_level": as_int(data.get("accessLevel")),
        "text": text,
        "text_plain": plain_text(text),
        "short_text": as_text(data.get("shortText")),
        "text_no_erab": as_text(data.get("textNoErab")),
        "qael_title_list": as_text(data.get("qaelTitleList")),
        "toc_text": None,
        "translate_count": as_int(data.get("translateCount")),
        "explanation_count": as_int(data.get("explanationCount")),
        "hadith_in_book_count": as_int(data.get("hadithInbookCount")),
        "hadith_related_count": as_int(data.get("hadithRelatedCount")),
        "hadith_same_toc_count": as_int(data.get("hadithSameTocCount")),
        "group_hadith_count": as_int(data.get("groupHadithCount")),
        "has_translate": as_bool_int(data.get("hasTranslate")),
        "has_explanation": as_bool_int(data.get("hasExplanation")),
        "has_similar": as_bool_int(data.get("hasSimilar")),
        "has_family": as_bool_int(data.get("hasFamily")),
        "noor_lib_link": as_text(data.get("noorLibLink")),
    }


def parse_fallback_hadith(data: dict[str, Any]) -> dict[str, Any]:
    """Map the legacy HadithById response into the main hadith columns."""
    hadith_id = as_int(data.get("hadithId") or data.get("id"))
    if hadith_id is None:
        raise ValueError("fallback hadith payload has no hadithId")
    text = as_text(data.get("text"))
    return {
        "hadith_id": hadith_id,
        "source_id": None,
        "author_title": None,
        "book_title": as_text(data.get("mainTitle")),
        "book_title_short": as_text(data.get("shortTitle")),
        "vol": as_int(data.get("vol")),
        "page_num": as_int(data.get("pageNum")),
        "start_index_in_page": None,
        "access_level": None,
        "text": text,
        "text_plain": plain_text(text),
        "short_text": as_text(data.get("shortText")),
        "text_no_erab": None,
        "qael_title_list": as_text(data.get("qael")),
        "toc_text": as_text(data.get("toc")),
        "translate_count": None,
        "explanation_count": None,
        "hadith_in_book_count": None,
        "hadith_related_count": None,
        "hadith_same_toc_count": None,
        "group_hadith_count": None,
        "has_translate": None,
        "has_explanation": None,
        "has_similar": None,
        "has_family": None,
        "noor_lib_link": None,
    }


def parse_group_members(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in data.get("groupTogetherList") or []:
        group_id = as_int(item.get("groupId"))
        member_id = as_int(item.get("hadithId"))
        if group_id is None or group_id <= 0 or member_id is None:
            # shortcut: groupId <= 0 means the API did not group this hadith,
            # so there is nothing reliable to link. Upgrade if the API changes.
            continue
        key = (group_id, member_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "group_id": group_id,
                "hadith_id": member_id,
                "source_id": as_int(item.get("sourceId")),
                "source_main_title": as_text(item.get("sourceMainTitle")),
                "source_short_title": as_text(item.get("sourceShortTitle")),
                "source_hadith_id": as_int(item.get("id")),
                "vol": as_int(item.get("vol")),
                "page_num": as_int(item.get("pageNum")),
                "century_id": as_int(item.get("centuryId")),
                "access_level": as_int(item.get("accessLevel")),
                "section": as_int(item.get("section")),
                "group_hadith_count": as_int(item.get("groupHadithCount")),
                "size_id": as_int(item.get("sizeId")),
                "size_title": as_text(item.get("sizeTitle")),
                "size_json": json_text(item.get("sizeJson")),
                "book_json": json_text(item.get("bookJson")),
                "sort_order": len(rows),
            }
        )
    return rows


def parse_qael(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in data.get("qaelList") or []:
        qael_id = as_int(item.get("id"))
        if qael_id is None:
            continue
        role_id = as_int(item.get("roleId") or item.get("qaelRoleId")) or 0
        key = (qael_id, role_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "qael_id": qael_id,
                "title": as_text(item.get("title")),
                "role_id": role_id,
                "sort_order": as_int(item.get("order")),
            }
        )
    return rows


def parse_tocs(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for depth, item in enumerate(data.get("tocInfo") or [], start=1):
        toc_id = as_int(item.get("id"))
        if toc_id is None or toc_id in seen:
            continue
        seen.add(toc_id)
        parent_id = as_int(item.get("parentId"))
        rows.append(
            {
                "id": toc_id,
                "source_id": as_int(item.get("sourceId")),
                "parent_id": parent_id if parent_id else None,
                "title": as_text(item.get("title")),
                "sample_title": as_text(item.get("sampleTitle")),
                "access_level": as_int(item.get("accessLevel")),
                "level_num": as_int(item.get("levelNum")),
                "hadith_count": as_int(item.get("hadithCount")),
                "child_count": as_int(item.get("childCount")),
                "is_toc": as_int(item.get("isToc")),
                "sort_order": as_int(item.get("order")),
                "depth": depth,
            }
        )
    return rows


def parse_translations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        text_id = as_int(item.get("id"))
        hadith_id = as_int(item.get("hadithId"))
        if text_id is None or hadith_id is None:
            continue
        text = as_text(item.get("text"))
        rows.append(
            {
                "id": text_id,
                "hadith_id": hadith_id,
                "source_id": as_int(item.get("sourceId")),
                "source_title": as_text(item.get("sourceMainTitle")),
                "source_short_title": as_text(item.get("sourceShortTitle")),
                "author_title": as_text(item.get("authorTitle")),
                "language_id": as_int(item.get("languageId")),
                "text": text,
                "text_plain": plain_text(text),
                "short_text": as_text(item.get("shortText")),
                "is_shorted": as_bool_int(item.get("isShorted")),
                "vol": as_int(item.get("vol")),
                "page_num": as_int(item.get("pageNum")),
                "comment_count": as_int(item.get("commentCount")),
                "insert_date": as_text(item.get("insertDate")),
                "noor_lib_link": as_text(item.get("noorLibLink")),
            }
        )
    return rows


def parse_explanations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    # Explanations use the same item shape as translations.
    return parse_translations(payload)


def parse_rejal(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {"sanads": [], "ravis": []}

    hint_by_ravi: dict[int, str] = {}
    sanads: list[dict[str, Any]] = []
    for sanad_index, sanad_wrapper in enumerate(data.get("sanadList") or []):
        segments: list[dict[str, Any]] = []
        for segment in sanad_wrapper.get("sanad") or []:
            ravis: list[dict[str, Any]] = []
            for ravi in segment.get("raviList") or []:
                ravi_id = as_int(ravi.get("raviId"))
                if ravi_id is None:
                    continue
                hint = as_text(ravi.get("hint"))
                if hint:
                    hint_by_ravi.setdefault(ravi_id, hint)
                ravis.append(
                    {
                        "ravi_id": ravi_id,
                        "ravi_index": len(ravis),
                        "hint": hint,
                    }
                )
            segments.append(
                {
                    "segment_order": as_int(segment.get("order")),
                    "segment_type": as_int(segment.get("type")),
                    "title": as_text(segment.get("title")),
                    "ravis": ravis,
                }
            )
        sanads.append({"sanad_index": sanad_index, "segments": segments})

    ravi_rows: dict[int, dict[str, Any]] = {}
    for item in data.get("raviList") or []:
        ravi_id = as_int(item.get("raviId"))
        if ravi_id is None:
            continue
        hint = hint_by_ravi.get(ravi_id)
        ravi_rows[ravi_id] = {
            "ravi_id": ravi_id,
            "name": hint.split(",", 1)[0].strip() if hint else None,
            "hint": hint,
            "info_json": json_text(item),
        }
    for ravi_id, hint in hint_by_ravi.items():
        ravi_rows.setdefault(
            ravi_id,
            {
                "ravi_id": ravi_id,
                "name": hint.split(",", 1)[0].strip() if hint else None,
                "hint": hint,
                "info_json": None,
            },
        )

    return {"sanads": sanads, "ravis": list(ravi_rows.values())}


def parse_sources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": as_int(item.get("id")),
            "main_title": as_text(item.get("mainTitle")),
            "title": as_text(item.get("title")),
            "group_title": as_text(item.get("groupTitle")),
            "order_num": as_int(item.get("orderNum")),
            "sample_word": as_text(item.get("sampleWord")),
            "author_title": as_text(item.get("authorTitle")),
            "hadith_count": as_int(item.get("hadithCount")),
        }
        for item in payload.get("data") or []
        if as_int(item.get("id")) is not None
    ]


def parse_formats(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": as_int(item.get("id")),
            "title": as_text(item.get("title")),
            "tag_name": as_text(item.get("tagName")),
            "access_level": as_int(item.get("accessLevel")),
            "order_num": as_int(item.get("order")),
        }
        for item in payload.get("data") or []
        if as_int(item.get("id")) is not None
    ]


def parse_languages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": as_int(item.get("id")),
            "title": as_text(item.get("title")),
            "native_name": as_text(item.get("nativeName")),
            "two_char": as_text(item.get("twoChar")),
            "is_active": as_bool_int(item.get("isActive")),
            "is_rtl": as_bool_int(item.get("isRtl")),
            "order_num": as_int(item.get("orderNum")),
            "is_for_data": as_bool_int(item.get("isForData")),
            "is_for_interface": as_bool_int(item.get("isForInterface")),
        }
        for item in payload.get("data") or []
        if as_int(item.get("id")) is not None
    ]


def parse_qael_roles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": as_int(item.get("id")), "title": as_text(item.get("title"))}
        for item in payload.get("data") or []
        if as_int(item.get("id")) is not None
    ]
