"""Exact option synonyms shared by planning, execution and readback.

These are not fuzzy matches. A generic postgraduate level, a degree title or
a minimum requirement is never promoted to a specific education level.
"""

OPTION_ALIASES = (
    {"男", "male", "man"}, {"女", "female", "woman"},
    {"中国", "中国大陆", "中华人民共和国", "china", "mainlandchina", "chn"},
    {"远程", "线上", "远程面试", "线上面试", "remote", "online"},
    {"英语", "英文", "english"}, {"普通话", "中文", "汉语", "mandarin", "chinese"},
    {"硕士", "硕士研究生"}, {"博士", "博士研究生"},
)
