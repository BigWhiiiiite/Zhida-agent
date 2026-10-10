"""Subject and declaration boundaries shared by planning and execution.

Use the question's own wording, not nearby questions or a model's claimed
confidence. These policies are independent of the employer or UI library.
"""
from __future__ import annotations

import re


FAMILY_SUBJECT = re.compile(
    r"父亲|母亲|父母|配偶|子女|亲属|家属|监护人|"
    r"\b(?:father|mother|parent(?:s)?|spouse|children|guardian|next of kin)\b", re.I,
)
DECLARATION = re.compile(
    r"承诺|声明|真实可信|真实性|我保证|法律责任|协议|条款|隐私|"
    r"consent|privacy|terms|legal|declaration|attest|certify|acknowledge|undertaking", re.I,
)
FORMAL_ONLY = re.compile(
    r"正式(?:签订)?劳动合同|签订劳动合同并缴纳社保|正式工作经历|"
    r"实习(?:经历)?(?:请勿|不得|不应|不能|不要)填写|"
    r"(?:exclude|excluding|do not include)\s+internships?", re.I,
)


def own_question(field) -> str:
    return " ".join(filter(None, (
        field.question_text or field.label, field.group_label, field.name,
    )))


def family_subject(field) -> bool:
    question = own_question(field)
    if FAMILY_SUBJECT.search(question):
        return True
    # A short caption such as 学历 can belong to a family record. Do not
    # interpret the generic section title alone as a candidate education row.
    section = " ".join([field.section, *field.section_path])
    return bool(FAMILY_SUBJECT.search(section) and re.fullmatch(
        r"[\s*＊]*(?:姓名|学历|学位|单位|职务|电话|手机|邮箱)[\s:：]*", question))


def family_scope(field) -> str:
    if not family_subject(field):
        return ""
    text = own_question(field)
    if not FAMILY_SUBJECT.search(text):
        text = " ".join([field.section, *field.section_path])
    roles = [(role, pattern) for role, pattern in (
        ("father", r"父亲|\bfather\b"), ("mother", r"母亲|\bmother\b"),
        ("spouse", r"配偶|\bspouse\b"), ("child", r"子女|\bchildren\b"),
        ("guardian", r"监护人|\bguardian\b"),
    ) if re.search(pattern, text, re.I)]
    return "third_party:" + (roles[0][0] if len(roles) == 1 else "family")


def is_declaration(field) -> bool:
    # Banks also render attestations as comboboxes/text, not just checkboxes.
    return bool(DECLARATION.search(" ".join((
        own_question(field), field.option_label,
    ))) or field.name.casefold() in {"agreechk", "agreement", "consent"})


def formal_employment_only(field) -> bool:
    question = own_question(field)
    section = " ".join([field.section, *field.section_path])
    if FORMAL_ONLY.search(question) or FORMAL_ONLY.search(section):
        return True
    # A form-wide help paragraph cannot impose a neighbouring work section's
    # policy on personal, education or internship controls.
    in_work_section = bool(re.search(
        r"工作(?:经验|经历)|任职经历|employment history|work experience", section, re.I,
    )) and not re.search(r"实习|internship", section, re.I)
    return bool(in_work_section and FORMAL_ONLY.search(
        " ".join((field.help_text, field.context))))
