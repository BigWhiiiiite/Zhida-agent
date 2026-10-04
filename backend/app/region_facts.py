"""Read levels explicitly present in a confirmed region, without a geocoder.

The complete personal fact remains authoritative. A province-only ATS answer
must not erase a city/district learned earlier, and a city must never be
borrowed from current residence or inferred from a different province.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RegionParts:
    province: str = ""
    city: str = ""
    district: str = ""


def region_path(value: str) -> list[str]:
    """Only explicit levels, with municipality's duplicate city removed."""
    parts = region_parts(value)
    path = [item for item in (parts.province, parts.city, parts.district) if item]
    return list(dict.fromkeys(path))


def region_values_match(expected: str, actual: str) -> bool:
    # Separators are presentation only; no substring/leaf-only equivalence.
    clean = lambda value: re.sub(r"[\s/／,，、>＞·]+", "", value)
    left, right = clean(expected), clean(actual)
    if not left or not right:
        return False
    if left == right:
        return True
    a, b = region_path(expected), region_path(actual)
    if a and b:
        return a == b
    # A standalone administrative name may omit its suffix (北京 / 北京市).
    suffix = r"(?:特别行政区|壮族自治区|回族自治区|维吾尔自治区|自治区|省|市)$"
    return len(a) <= 1 and len(b) <= 1 and re.sub(suffix, "", left) == re.sub(suffix, "", right)


def region_parts(value: str) -> RegionParts:
    text = re.sub(r"[\s/／,，、]+", "", value)
    province = city = district = ""
    # Municipalities are explicitly named province-level cities, not a lookup
    # of an unprovided parent for an arbitrary city.
    for name in ("北京市", "上海市", "天津市", "重庆市"):
        if text.startswith(name):
            province = city = name
            text = text[len(name):]
            break
    if not province:
        match = re.match(r"^([\u4e00-\u9fff]{2,}?(?:特别行政区|壮族自治区|回族自治区|维吾尔自治区|自治区|省))", text)
        if match:
            province = match.group(1)
            text = text[len(province):]
    if not city:
        match = re.match(r"^([\u4e00-\u9fff]{2,}?(?:自治州|地区|盟|市))", text)
        if match:
            city = match.group(1)
            text = text[len(city):]
    if text and re.fullmatch(r"[\u4e00-\u9fff]{2,}?(?:自治县|自治旗|县|旗|区)", text):
        district = text
        text = ""
    # Extra prose, street addresses, or more than one path are ambiguous.
    return RegionParts() if text else RegionParts(province, city, district)


def question_region_level(question: str) -> str:
    text = question.casefold()
    hints = {
        "province": r"省份|省级|所在省|所属省|籍贯省|\bprovince\b|\bstate\b|[（(：:]\s*省\s*[)）]|^省$",
        "city": r"城市|地级市|所在市|所属市|籍贯市|\bcity\b|[（(：:]\s*市\s*[)）]|^市$",
        "district": r"区县|区/县|区级|所在区|所属区|籍贯区|\bdistrict\b|\bcounty\b|[（(：:]\s*区\s*[)）]|^区$",
    }
    found = [level for level, pattern in hints.items() if re.search(pattern, text)]
    return found[0] if len(found) == 1 else ""


def hometown_for_question(value: str, question: str) -> str:
    level = question_region_level(question)
    return getattr(region_parts(value), level) if level else value.strip()


def merge_confirmed_hometown(current: str, incoming: str, question: str) -> str:
    """Keep finer facts only when the confirmed coarser answer agrees.

    A changed province/city does not inherit old descendants. No new parent is
    generated for a standalone city/district. A full edit in the main profile
    remains available if the user deliberately wants to remove detail.
    """
    incoming = incoming.strip()
    old, new = region_parts(current), region_parts(incoming)
    level = question_region_level(question)
    if current and level:
        old_level = getattr(old, level)
        suffix = {"province": r"(?:特别行政区|壮族自治区|回族自治区|维吾尔自治区|自治区|省|市)$",
                  "city": r"(?:自治州|地区|盟|市)$", "district": r"(?:自治县|自治旗|县|旗|区)$"}[level]
        if old_level and re.sub(suffix, "", incoming) == re.sub(suffix, "", old_level):
            return current
    # Unqualified single-level choices (e.g. a provincial dropdown titled
    # 籍贯) may also express a compatible prefix, not a deletion of detail.
    old_path = [old.province, old.city, old.district]
    new_path = [new.province, new.city, new.district]
    if any(new_path) and any(old_path) and all(not item or item == old_path[i]
                                              for i, item in enumerate(new_path)):
        return current
    return incoming
